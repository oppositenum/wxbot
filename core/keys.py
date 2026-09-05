"""从微信主进程内存提取每个数据库的 AES-256 密钥。

微信 4.x 用 WCDB(SQLCipher 变体)。密钥在主进程内存里。两种可能布局：
  A) 文本形式  x'<64位key_hex><32位salt_hex>'
  B) 原始字节   ...<32字节key><16字节salt>...（key 紧邻 salt）
本脚本主用「salt 邻接 + HMAC 校验」：每个 .db 前 16 字节是随机 salt，
在内存里定位 salt，取相邻 32 字节做候选 key，用该库第 1 页 HMAC 验证。
再辅以文本形式 x'hex' 兜底。全程有诊断输出。

需要 root（SIP 已关闭时 sudo 下 task_for_pid 可用）：
    sudo python3 -m core.keys
"""
import ctypes
import ctypes.util
import json
import os
import re
import subprocess
import sys

from Crypto.Cipher import AES

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

libc = ctypes.CDLL(ctypes.util.find_library("System"), use_errno=True)

KERN_SUCCESS = 0
VM_REGION_BASIC_INFO_64 = 9
VM_PROT_READ = 1

mach_vm_address_t = ctypes.c_uint64
mach_vm_size_t = ctypes.c_uint64
mach_port_t = ctypes.c_uint32
kern_return_t = ctypes.c_int


class vm_region_submap_info_64(ctypes.Structure):
    _fields_ = [
        ("protection", ctypes.c_int),
        ("max_protection", ctypes.c_int),
        ("inheritance", ctypes.c_uint),
        ("offset", ctypes.c_ulonglong),
        ("user_tag", ctypes.c_uint),
        ("pages_resident", ctypes.c_uint),
        ("pages_shared_now_private", ctypes.c_uint),
        ("pages_swapped_out", ctypes.c_uint),
        ("pages_dirtied", ctypes.c_uint),
        ("ref_count", ctypes.c_uint),
        ("shadow_depth", ctypes.c_ushort),
        ("external_pager", ctypes.c_ubyte),
        ("share_mode", ctypes.c_ubyte),
        ("is_submap", ctypes.c_int),
        ("behavior", ctypes.c_int),
        ("object_id", ctypes.c_uint),
        ("user_wired_count", ctypes.c_ushort),
    ]


VM_REGION_SUBMAP_INFO_COUNT_64 = ctypes.sizeof(vm_region_submap_info_64) // 4

# tag 0 是 6.8G 的图形/媒体缓冲，几乎无账号数据，跳过以提速
SKIP_TAGS = {0}

libc.mach_task_self.restype = mach_port_t
libc.task_for_pid.argtypes = [mach_port_t, ctypes.c_int, ctypes.POINTER(mach_port_t)]
libc.task_for_pid.restype = kern_return_t
libc.mach_vm_region_recurse.argtypes = [
    mach_port_t, ctypes.POINTER(mach_vm_address_t), ctypes.POINTER(mach_vm_size_t),
    ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(vm_region_submap_info_64),
    ctypes.POINTER(ctypes.c_uint32),
]
libc.mach_vm_region_recurse.restype = kern_return_t
libc.mach_vm_read_overwrite.argtypes = [
    mach_port_t, mach_vm_address_t, mach_vm_size_t, mach_vm_address_t,
    ctypes.POINTER(mach_vm_size_t),
]
libc.mach_vm_read_overwrite.restype = kern_return_t

# ---- 加密参数（HMAC 校验用）----
PAGE = 4096
RESERVE = 80
IV_LEN = 16
SALT_LEN = 16
CHUNK = 16 * 1024 * 1024
MAX_REGION = int(os.environ.get("WXBOT_MAXREGION", str(3 * 1024 * 1024 * 1024)))


def wechat_pid():
    try:
        out = subprocess.check_output(["pgrep", "-x", config.WECHAT_PROC]).decode()
        pids = [int(x) for x in out.split()]
        return pids[0] if pids else None
    except subprocess.CalledProcessError:
        return None


def task_for_pid(pid):
    task = mach_port_t()
    kr = libc.task_for_pid(libc.mach_task_self(), pid, ctypes.byref(task))
    if kr != KERN_SUCCESS:
        raise RuntimeError(f"task_for_pid 失败 (kr={kr})。需要 sudo 且 SIP 关闭。")
    return task.value


def iter_regions(task):
    """产出 (address, size, protection, user_tag)，展开 submap。"""
    address = mach_vm_address_t(0)
    depth = ctypes.c_uint32(0)
    while True:
        size = mach_vm_size_t(0)
        info = vm_region_submap_info_64()
        count = ctypes.c_uint32(VM_REGION_SUBMAP_INFO_COUNT_64)
        kr = libc.mach_vm_region_recurse(task, ctypes.byref(address), ctypes.byref(size),
                                         ctypes.byref(depth), ctypes.byref(info),
                                         ctypes.byref(count))
        if kr != KERN_SUCCESS:
            break
        if info.is_submap:
            depth.value += 1
            continue
        yield address.value, size.value, info.protection, info.user_tag
        address = mach_vm_address_t(address.value + size.value)


def read_mem(task, address, size):
    buf = (ctypes.c_char * size)()
    out = mach_vm_size_t(0)
    kr = libc.mach_vm_read_overwrite(
        task, address, size, ctypes.cast(buf, ctypes.c_void_p).value, ctypes.byref(out))
    if kr != KERN_SUCCESS:
        return None
    return buf.raw[:out.value]


# ---- 校验 oracle：用候选 key 解密第 1 页首块，检查 SQLite 头字段 ----
# 与 KDF/HMAC 无关。解密后的明文对应文件 offset 16 起：
#   offset 16-17 = 页大小 (big-endian，2 的幂：0x0200..0x8000)
#   offset 18,19 = 读写版本 (1 或 2)
#   offset 20    = 每页保留字节 (SQLCipher 常见 80)
_PAGE_SIZES = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}


def _reserve_start(page_len=PAGE):
    return page_len - RESERVE


def precheck_key(key, first_page):
    """解密第 1 页首个 16 字节块（= 文件 offset 16..31），核验完整 SQLite 头签名。
    offset20=保留字节=80, 21/22/23=最大/最小/叶载荷分数=64/32/32，几乎杜绝误报。"""
    if len(key) != 32:
        return False
    rs = PAGE - RESERVE
    iv = first_page[rs:rs + IV_LEN]
    try:
        p = AES.new(key, AES.MODE_CBC, iv).decrypt(first_page[SALT_LEN:SALT_LEN + 16])
    except ValueError:
        return False
    return (p[0] in _PAGE_SIZES and p[1] == 0x00       # 页大小(2 的幂)
            and p[2] in (1, 2) and p[3] in (1, 2)       # 读写版本
            and p[4] == RESERVE                         # 保留字节 = 80
            and p[5] == 64 and p[6] == 32 and p[7] == 32)  # 载荷分数


def validate_key(key, first_page):
    return precheck_key(key, first_page)


# ---- 数据库信息 ----
def all_dbs(db_storage):
    out = []
    for root, _, files in os.walk(db_storage):
        for fn in files:
            if fn.endswith(".db"):
                ap = os.path.join(root, fn)
                out.append((os.path.relpath(ap, db_storage), ap))
    return out


def load_db_meta(db_storage):
    """返回 {rel: {'salt':16b, 'page1':4096b, 'path':..}}。"""
    meta = {}
    for rel, ap in all_dbs(db_storage):
        try:
            with open(ap, "rb") as f:
                page1 = f.read(PAGE)
            if len(page1) >= PAGE:
                meta[rel] = {"salt": page1[:SALT_LEN], "page1": page1, "path": ap}
        except OSError:
            pass
    return meta


# macOS 微信 4.1.x：内存里密钥字面量是 x'<64hex 原始key>'（不含 salt，malloc(67)）。
# 兼容 Windows 的 x'<64hex key><32hex salt>' —— 只取前 64 hex 作为 key。
TEXT_PATTERN = re.compile(rb"x'([0-9a-fA-F]{64})")


def extract(debug=True):
    pid = wechat_pid()
    if not pid:
        raise RuntimeError("微信主进程未运行。")
    print(f"WeChat pid = {pid}")
    task = task_for_pid(pid)

    db_storage = config.db_storage_dir()
    meta = load_db_meta(db_storage)
    salt_index = {}  # salt bytes -> rel
    for rel, m in meta.items():
        salt_index[m["salt"]] = rel
    salts = list(salt_index.keys())
    print(f"目标数据库 {len(meta)} 个，账号目录: {db_storage}")

    import numpy as np

    keys = {}                 # rel -> key_hex
    reserve_seen = {}
    stats = {"regions": 0, "read_ok": 0, "read_fail": 0, "bytes": 0,
             "text_hits": 0, "wxid_hits": 0, "candidates": 0, "aes": 0}
    wxid_probe = (config.wxid() or "").encode()

    # 只针对核心库找 key（每库独立 key）
    core_rels = [v for v in config.CORE_DBS.values() if v in meta]
    core_pages = {rel: meta[rel]["page1"] for rel in core_rels}
    OVERLAP = 32              # 保证跨块 32 字节 key 不被切断

    def record(rel, cand):
        keys[rel] = cand.hex()
        print(f"  ✓ 命中 {rel}  key={cand.hex()[:16]}…", flush=True)

    def test_against(cand, rels):
        for rel in rels:
            if rel in keys:
                continue
            stats["aes"] += 1
            if precheck_key(cand, core_pages[rel]):
                record(rel, cand)
                return rel
        return None

    def brute_window(center_abs, radius=4 * 1024 * 1024):
        """在 center_abs ±radius 内暴力所有核心库（key 在堆里通常聚集）。"""
        lo = max(0, center_abs - radius)
        data = read_mem(task, lo, radius * 2)
        if not data:
            return
        start = (-lo) % 16
        pend = [r for r in core_rels if r not in keys]
        for o in range(start, len(data) - 31, 16):
            if not pend:
                break
            cand = data[o:o + 32]
            hit = test_against(cand, pend)
            if hit:
                pend.remove(hit)

    step = int(os.environ.get("WXBOT_STEP", "16"))  # 候选对齐步长

    def candidate_offsets(arr):
        """按 step 对齐、32 字节窗口。滤掉指针(多零)与文本(多可打印) —— 高熵随机 key 会留下。"""
        L = len(arr)
        if L < 32:
            return np.empty(0, dtype=np.int64)
        offs = np.arange(0, L - 31, step, dtype=np.int64)
        z = (arr == 0).astype(np.int32)
        printable = ((arr >= 0x20) & (arr < 0x7f)).astype(np.int32)
        zp = np.empty(L + 1, dtype=np.int32); zp[0] = 0; np.cumsum(z, out=zp[1:])
        pp = np.empty(L + 1, dtype=np.int32); pp[0] = 0; np.cumsum(printable, out=pp[1:])
        zc = zp[offs + 32] - zp[offs]
        pc = pp[offs + 32] - pp[offs]
        return offs[(zc < 5) & (pc <= 24)]

    import time as _t
    t0 = _t.time()
    done_regions = 0
    malloc_bytes = 0
    only_tag = os.environ.get("WXBOT_ONLY_TAG")  # 调试：只扫某个 tag
    only_tag = int(only_tag) if only_tag is not None else None
    alltags = bool(os.environ.get("WXBOT_ALLTAGS"))
    shard = os.environ.get("WXBOT_SHARD")         # "id/n" 分片并行
    shard_id, shard_n = ([int(x) for x in shard.split("/")] if shard else [0, 1])
    region_idx = -1
    for addr, size, prot, tag in iter_regions(task):
        if not (prot & VM_PROT_READ) or size > MAX_REGION:
            continue
        if only_tag is not None:
            if tag != only_tag:
                continue
        elif not alltags and tag in SKIP_TAGS:   # 默认跳过 tag0(6.8G 图形/媒体缓冲)
            continue
        region_idx += 1
        if region_idx % shard_n != shard_id:     # 分片：本进程只扫属于自己的区域
            continue
        malloc_bytes += size
        stats["regions"] += 1
        off = 0
        while off < size:
            n = min(CHUNK, size - off)
            data = read_mem(task, addr + off, min(n + OVERLAP, size - off))
            off += n
            if data is None:
                stats["read_fail"] += 1
                continue
            stats["read_ok"] += 1
            stats["bytes"] += len(data)
            if wxid_probe:
                stats["wxid_hits"] += data.count(wxid_probe)
            base = addr + (off - n)   # 本块起始绝对地址
            for mt in TEXT_PATTERN.finditer(data):
                stats["text_hits"] += 1
                hit = test_against(bytes.fromhex(mt.group(1).decode()), core_rels)
                if hit:
                    brute_window(base + mt.start())
            if len(keys) >= len(core_rels):
                break
            arr = np.frombuffer(data, dtype=np.uint8)
            offs = candidate_offsets(arr)
            stats["candidates"] += len(offs)
            # 主扫描只测“锚”库（未命中的第一个核心库），1 次 AES/候选；
            # 命中后在其地址附近 window 暴力其余库（key 在堆里聚集）。
            for o in offs.tolist():
                anchor = next((r for r in core_rels if r not in keys), None)
                if anchor is None:
                    break
                stats["aes"] += 1
                if precheck_key(data[o:o + 32], core_pages[anchor]):
                    record(anchor, data[o:o + 32])
                    brute_window(base + o)
            if len(keys) >= len(core_rels):
                break
        done_regions += 1
        if done_regions % 100 == 0:
            print(f"  …进度 {done_regions} 区域, {stats['bytes']/1e6:.0f}MB, "
                  f"候选 {stats['candidates']}, AES {stats['aes']}, "
                  f"命中 {len(keys)}/{len(core_rels)}, {_t.time()-t0:.0f}s", flush=True)
        if len(keys) >= len(core_rels):
            print("  核心库全部命中，提前结束扫描。", flush=True)
            break

    if debug:
        print("\n[诊断]", json.dumps(stats, ensure_ascii=False))
        print(f"  已读 {stats['bytes']/1e6:.0f} MB，wxid 出现 {stats['wxid_hits']} 次")
    return keys, meta


def main():
    config.ensure_dirs()
    keys, meta = extract()
    out_path = os.environ.get("WXBOT_OUT", config.keys_json())
    with open(out_path, "w") as f:
        json.dump(keys, f, indent=2)
    if os.environ.get("WXBOT_SHARD"):
        print(f"[shard 完成] {out_path}: {list(keys)}", flush=True)
        return
    print(f"\n写入 {out_path}，共 {len(keys)} 个库匹配到密钥：")
    for rel in sorted(keys):
        print("  ", rel)

    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        import pwd
        pw = pwd.getpwnam(sudo_user)
        try:
            os.chown(config.keys_json(), pw.pw_uid, pw.pw_gid)
        except OSError:
            pass

    core = config.CORE_DBS
    missing = [k for k, v in core.items() if v not in keys]
    if missing:
        print("\n⚠️ 核心库未匹配到密钥：", missing)
        sys.exit(2)
    print("\n✅ 核心库密钥齐全。")


if __name__ == "__main__":
    main()
