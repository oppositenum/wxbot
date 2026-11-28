"""数据库密钥:验证、合并、按需用 AES hook 抓取(微信 4.1.13+ 内存扫描抓不到密钥)。

微信 4.1.13 起派生后的库密钥不常驻内存(WCDB 闲置会关连接并清零),docker/linux_keys.py 的内存扫描一个都找不到。
但 SQLCipher 每读一页都会重新设一次 AES 密钥:hook OpenSSL 的 aes_v8_set_encrypt_key,收集 256 位密钥,
逐个拿各库第 1 页验证即可。期间要让微信读一下目标库——capture() 的 trigger 回调负责(比如往文件传输助手直发
一条文字/视频/语音)。细节见 docs/wechat-direct-send-re.md 3.15。

keys.json 只合并验证通过的密钥,绝不删除已有的(以前刷新失败会把整份 keys.json 覆盖掉)。
"""
import json
import os
import shutil
import threading
import time

import config

PAGE = 4096
# 模块内偏移(文件偏移 + 0x10000):OpenSSL ARMv8 aes_v8_set_encrypt_key,紧跟在 rcon 表后
SET_ENC_KEY = {"3539690ff31b8b85af3bb84b8f1087edda9ce324db9fdab10f40d7622fba42d9": 0x8d4da00}
EXTRA_DBS = ("sns/sns.db",)     # 朋友圈
# 图片 .dat 用软件 AES(OpenSSL aes_core 的查表实现)解密,不走上面的硬件路径。
# 4.1.13 有两套:set_decrypt_key 0x8cb8584(图片用这个)/ 0x84b8334,set_encrypt_key 0x8cb7fa4。
# 定位:二进制里搜 Te0/Td0 表常量(c66363a5…/51f4a750…)→ xref → 开头 cmp w1,#0x80 的就是 set_encrypt_key。
IMG_KEY_FNS = {"3539690ff31b8b85af3bb84b8f1087edda9ce324db9fdab10f40d7622fba42d9": [0x8cb8584, 0x84b8334, 0x8cb7fa4]}

IMG_JS = """
const base = Process.findModuleByName('wechat').base;
const seen = new Set(); const keys = [];
for (const off of %s) {
  Interceptor.attach(base.add(off), { onEnter(a) {
    if (a[1].toInt32() !== 128) return;
    const h = Array.from(new Uint8Array(a[0].readByteArray(16))).map(b => ('0'+b.toString(16)).slice(-2)).join('');
    if (!seen.has(h)) { seen.add(h); keys.push(h); }
  }});
}
rpc.exports = { take() { return keys.splice(0); }, count() { return seen.size; } };
"""

JS = """
const base = Process.findModuleByName('wechat').base;
const seen = new Set(); const keys = [];
Interceptor.attach(base.add(%d), { onEnter(a) {
  if (a[1].toInt32() !== 256) return;
  const h = Array.from(new Uint8Array(a[0].readByteArray(32))).map(b => ('0'+b.toString(16)).slice(-2)).join('');
  if (!seen.has(h)) { seen.add(h); keys.push(h); }
}});
rpc.exports = { take() { return keys.splice(0); }, count() { return seen.size; } };
"""


def page_ok(key_hex, page1):
    """密钥能不能解开这个库的第 1 页(页大小 0x1000、保留区 64/32/32)。"""
    from core import decrypt
    try:
        p = decrypt._decrypt_page(bytes.fromhex(key_hex), page1, 1)
    except Exception:
        return False
    return p[16:18] == b"\x10\x00" and p[21:24] == bytes([64, 32, 32])


def wanted_rels():
    return list(dict.fromkeys(list(config.CORE_DBS.values()) + list(EXTRA_DBS)))


def _page1(db_storage, rel):
    try:
        with open(os.path.join(db_storage, rel), "rb") as f:
            return f.read(PAGE)
    except OSError:
        return b""


def missing(keys, db_storage, rels=None):
    """这些库里(存在于磁盘上的)哪些没有能用的密钥。"""
    out = []
    for rel in rels or wanted_rels():
        p1 = _page1(db_storage, rel)
        if len(p1) < PAGE:
            continue
        k = keys.get(rel)
        if not (isinstance(k, str) and len(k) == 64 and page_ok(k, p1)):
            out.append(rel)
    return out


def load(path=None):
    try:
        with open(path or config.keys_json(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def merge(found, path=None, backup=True):
    """把验证过的密钥并进 keys.json(保留其它字段,如 _img_key)。返回写入的条数。"""
    if not found:
        return 0
    path = path or config.keys_json()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if backup and os.path.exists(path):
        shutil.copy2(path, path + time.strftime(".bak-%Y%m%d-%H%M%S"))
    d = load(path)
    d.update(found)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, path)
    return len(found)


def validated(candidates, db_storage, rels=None):
    """{rel: key} 里能解开对应库的那些。"""
    out = {}
    for rel, k in (candidates or {}).items():
        if rel.startswith("_") or not isinstance(k, str) or len(k) != 64:
            continue
        if rels and rel not in rels:
            continue
        p1 = _page1(db_storage, rel)
        if len(p1) == PAGE and page_ok(k, p1):
            out[rel] = k
    return out


def hook_supported():
    from core import direct_send
    sha, _ = direct_send.binary_version()
    return sha in SET_ENC_KEY


def capture(rels, db_storage, secs=60, trigger=None, log=print):
    """hook AES 设置密钥,收集 secs 秒(全抓到就提前结束)。trigger() 在 hook 挂上后在后台线程里跑一次。
    返回 {rel: key}(只含验证通过的)。"""
    from core import direct_send
    sha, _ = direct_send.binary_version()
    off = SET_ENC_KEY.get(sha or "")
    if off is None:
        raise RuntimeError("这个微信版本没有 aes_v8_set_encrypt_key 偏移,不能 hook 抓密钥")
    frida = direct_send._import_frida()
    if frida is None:
        raise RuntimeError("容器里没有 frida")
    dev, pid = direct_send._wechat_pid(frida)
    if pid is None:
        raise RuntimeError("微信没有在运行")
    pages = {rel: _page1(db_storage, rel) for rel in rels}
    pages = {rel: p for rel, p in pages.items() if len(p) == PAGE}
    sess = dev.attach(pid)
    sc = sess.create_script(JS % off)
    sc.load()
    found = {}
    if trigger:
        threading.Thread(target=trigger, name="dbkey-trigger", daemon=True).start()
    end = time.time() + secs
    try:
        while time.time() < end and len(found) < len(pages):
            for k in sc.exports_sync.take():
                for rel, p1 in pages.items():
                    if rel not in found and page_ok(k, p1):
                        found[rel] = k
                        log(f"✓ 抓到 {rel}")
            time.sleep(0.5)
        log(f"候选密钥共 {sc.exports_sync.count()} 个")
    finally:
        try:
            sc.unload()
        finally:
            sess.detach()
    return found


def img_hook_supported():
    from core import direct_send
    sha, _ = direct_send.binary_version()
    return sha in IMG_KEY_FNS


def _img_samples(limit=30):
    """本账号最近的图片 .dat(验证候选图片密钥用)。"""
    import glob
    st = config.db_storage_dir()
    if not st:
        return []
    base = os.path.dirname(st)
    paths = [p for p in glob.glob(base + "/**/*.dat", recursive=True) if "/Img/" in p or "/Bubble/" in p]
    paths.sort(key=lambda p: os.path.getmtime(p))
    out = []
    for p in paths[-limit:]:
        try:
            with open(p, "rb") as f:
                out.append(f.read())
        except OSError:
            pass
    return out


def img_key_ok(key, samples):
    from core import imgdec
    for d in samples:
        out = imgdec.decrypt_dat(d, key)
        if out and (out[:3] == b"\xff\xd8\xff" or out[:4] in (b"\x89PNG", b"GIF8")):
            return True
    return False


def img_key_text(key):
    """保存格式:32 位 hex(和以前 gdb 抓的一致,imgdec._key_bytes 认得)。"""
    return key.hex()


def capture_img_key(secs=40, trigger=None, log=print):
    """hook 软件 AES 的设置密钥(128 位),期间 trigger() 让微信显示几张没缓存的图片,
    用本地 .dat 验证。返回保存格式的密钥文本,没抓到返回 None。不会让微信崩溃(不用 gdb)。"""
    from core import direct_send
    sha, _ = direct_send.binary_version()
    fns = IMG_KEY_FNS.get(sha or "")
    if not fns:
        raise RuntimeError("这个微信版本没有软件 AES 偏移")
    samples = _img_samples()
    if not samples:
        raise RuntimeError("本地还没有图片 .dat(先收或发一张图片)")
    frida = direct_send._import_frida()
    if frida is None:
        raise RuntimeError("容器里没有 frida")
    dev, pid = direct_send._wechat_pid(frida)
    if pid is None:
        raise RuntimeError("微信没有在运行")
    sess = dev.attach(pid)
    sc = sess.create_script(IMG_JS % json.dumps(fns))
    sc.load()
    if trigger:
        threading.Thread(target=trigger, name="imgkey-trigger", daemon=True).start()
    end = time.time() + secs
    try:
        while time.time() < end:
            for k in sc.exports_sync.take():
                kb = bytes.fromhex(k)
                if img_key_ok(kb, samples):
                    log("✓ 抓到图片密钥(已用本地图片验证)")
                    return img_key_text(kb)
            time.sleep(0.5)
        log(f"候选 128 位密钥 {sc.exports_sync.count()} 个,都解不开本地图片")
        return None
    finally:
        try:
            sc.unload()
        finally:
            sess.detach()


def trigger_reads(log=print, need=None):
    """让微信读写各个库:往自己的文件传输助手直发一条文字、一条 2 秒语音;只有缺 message_resource.db 时
    才再发一段 1 秒视频(可删除)。视频发送失败会让微信之后的图片/文件上传全部失败(要重启微信),所以尽量少发、发了要查。"""
    import subprocess
    from core import direct_send
    tmp = "/tmp/wxbot_keytrigger"
    os.makedirs(tmp, exist_ok=True)
    mp4, wav = os.path.join(tmp, "init.mp4"), os.path.join(tmp, "init.wav")
    ff = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error"]
    subprocess.run(ff + ["-f", "lavfi", "-i", "sine=frequency=440:duration=2", wav], timeout=60)
    time.sleep(1)
    steps = [("文字", lambda: direct_send.send("filehelper", "[wxbot] 初始化读取密钥,可删除", "文件传输助手",
                                              source="keys"))]
    if direct_send.voice_supported():
        steps.append(("语音", lambda: direct_send.send_voice_msg("filehelper", wav, "文件传输助手", source="keys")))
    for name, fn in steps:
        try:
            fn()
            log(f"已往文件传输助手发{name}(触发微信读库)")
        except Exception as e:  # noqa: BLE001 直发不可用时只是少触发几个库
            log(f"发{name}失败:{e}")
        time.sleep(2)
    if need is not None and config.CORE_DBS["msgres"] not in need:
        return
    subprocess.run(ff + ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=15", "-f", "lavfi",
                         "-i", "sine=frequency=440:duration=1.2", "-t", "1.2", "-pix_fmt", "yuv420p",
                         "-c:a", "aac", "-shortest", mp4], timeout=60)
    try:
        t0 = time.time()
        direct_send.send_video("filehelper", mp4, "文件传输助手", source="keys")
        log("已往文件传输助手发视频(触发微信读 message_resource.db)")
        if direct_send.wait_upload("filehelper", 43, t0, timeout=30) == direct_send.UPLOAD_FAILED:
            log("⚠ 测试视频在微信里上传失败:之后的图片/视频/文件都会发不出去,请重启微信后再用")
    except Exception as e:  # noqa: BLE001
        log(f"发视频失败:{e}")


def refresh(db_storage, linux_keys_out=None, secs=60, trigger=True, log=print):
    """完整刷新:内存扫描结果(若有)→ 验证合并 → 还缺的用 AES hook 抓 → 验证合并。
    返回 (核心库是否齐全, 还缺的库列表)。"""
    if linux_keys_out:
        got = validated(load(linux_keys_out), db_storage)
        if got:
            log(f"内存扫描找到 {len(got)} 个能用的密钥")
            merge(got)
    keys = load()
    need = missing(keys, db_storage)
    if need and hook_supported():
        log(f"还缺 {len(need)} 个库的密钥,改用 AES hook 抓:{', '.join(need)}")
        got = capture(need, db_storage, secs=secs, trigger=(lambda: trigger_reads(log, need)) if trigger else None,
                      log=log)
        merge(got)
        need = missing(load(), db_storage)
    elif need:
        log("这个微信版本不支持 hook 抓密钥,缺的库只能等内存扫描")
    core_ok = not any(rel in need for rel in (config.CORE_DBS["message"], config.CORE_DBS["contact"],
                                               config.CORE_DBS["session"]))
    return core_ok, need
