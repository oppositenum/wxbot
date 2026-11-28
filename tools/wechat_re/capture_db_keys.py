"""微信 4.1.13 抓数据库密钥(容器内跑):hook OpenSSL aes_v8_set_encrypt_key,收集微信读库时用到的
AES-256 密钥,逐个拿各库第 1 页验证,把验证通过的写进本账号 keys.json(先备份)。不打印密钥。

4.1.13 起派生后的库密钥不常驻内存(WCDB 闲置会关连接并清掉),docker/linux_keys.py 的内存扫描抓不到;
但 SQLCipher 每读一页都会重新设一次 AES 密钥,所以只要微信读了这个库,就能截到。

用法: PYTHONPATH=/app python3 tools/wechat_re/capture_db_keys.py [秒数=60] [库 rel ...]
      默认只补验证不过的库;期间在微信里点开有视频/图片的会话能加快触发。
"""
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/app")
import config  # noqa: E402
from core import decrypt, direct_send  # noqa: E402

# 模块内偏移(文件偏移 + 0x10000):OpenSSL ARMv8 aes_v8_set_encrypt_key,紧跟在 rcon 表后
SET_ENC_KEY = {"3539690ff31b8b85af3bb84b8f1087edda9ce324db9fdab10f40d7622fba42d9": 0x8d4da00}

JS = """
const base = Process.findModuleByName('wechat').base;
const seen = new Set(); const keys = [];
Interceptor.attach(base.add(%d), { onEnter(a) {
  if (a[1].toInt32() !== 256) return;
  const k = a[0].readByteArray(32); const h = Array.from(new Uint8Array(k)).map(b => ('0'+b.toString(16)).slice(-2)).join('');
  if (!seen.has(h)) { seen.add(h); keys.push(h); }
}});
rpc.exports = { take() { const out = keys.splice(0); return out; }, count() { return seen.size; } };
"""


def page_ok(key_hex, page1):
    p = decrypt._decrypt_page(bytes.fromhex(key_hex), page1, 1)
    return p[16:18] == b"\x10\x00" and p[21:24] == bytes([64, 32, 32])


def main():
    secs = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    st = config.db_storage_dir()
    kpath = config.keys_json()
    keys = json.load(open(kpath))
    want = sys.argv[2:] or [rel for rel in keys if not rel.startswith("_") and os.path.exists(os.path.join(st, rel))
                            and not page_ok(keys[rel], open(os.path.join(st, rel), "rb").read(4096))]
    if not want:
        print("所有库的密钥都能用,不用抓")
        return
    print("要补的库:", want)
    pages = {rel: open(os.path.join(st, rel), "rb").read(4096) for rel in want}
    sha, _ = direct_send.binary_version()
    off = SET_ENC_KEY.get(sha)
    if off is None:
        print("这个微信版本没有 aes_v8_set_encrypt_key 偏移")
        sys.exit(1)
    frida = direct_send._import_frida()
    dev, pid = direct_send._wechat_pid(frida)
    sess = dev.attach(pid)
    sc = sess.create_script(JS % off)
    sc.load()
    found = {}
    end = time.time() + secs
    try:
        while time.time() < end and len(found) < len(want):
            for k in sc.exports_sync.take():
                for rel in want:
                    if rel not in found and page_ok(k, pages[rel]):
                        found[rel] = k
                        print("✓ 抓到", rel, flush=True)
            time.sleep(0.5)
        print("候选密钥共", sc.exports_sync.count(), "个")
    finally:
        sc.unload()
        sess.detach()
    if found:
        bak = kpath + time.strftime(".bak-%Y%m%d-%H%M%S")
        shutil.copy2(kpath, bak)
        keys = json.load(open(kpath))
        keys.update(found)
        tmp = kpath + ".tmp"
        with open(tmp, "w") as f:
            json.dump(keys, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, kpath)
        print("已写入", kpath, "备份", bak)
    missing = [r for r in want if r not in found]
    if missing:
        print("没抓到:", missing, "(让微信读一下这些库再跑,比如点开有视频的会话)")
        sys.exit(2)


if __name__ == "__main__":
    main()
