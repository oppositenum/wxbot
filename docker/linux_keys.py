"""Linux 版微信数据库密钥提取（容器内运行，root）。
读 /proc/<pid>/mem 扫描：
  1) 文本形式 x'<64/96 hex>'
  2) 原始 32 字节 key —— 用第 1 页 SQLite 头做校验 oracle
匹配到各库 salt，输出 keys.json。
用法： python3 linux_keys.py /root/xwechat_files/<wxid>/db_storage [out.json]
"""
import os, re, sys, json, struct
from Crypto.Cipher import AES

PAGE, RESERVE, IV, SALT = 4096, 80, 16, 16
_PS = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}


def wechat_pid():
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")[0].decode()
        except OSError:
            continue
        if (cmd == "wechat" or cmd.endswith("/opt/wechat/wechat") or cmd == "/opt/wechat/wechat"
                or cmd.endswith("/usr/bin/wechat") or cmd == "/usr/bin/wechat"):
            return int(pid)
    return None


def precheck(key, page1):
    if len(key) != 32:
        return False
    rs = PAGE - RESERVE
    iv = page1[rs:rs + IV]
    try:
        p = AES.new(key, AES.MODE_CBC, iv).decrypt(page1[SALT:SALT + 16])
    except ValueError:
        return False
    return (p[0] in _PS and p[1] == 0 and p[2] in (1, 2) and p[3] in (1, 2)
            and p[4] == RESERVE and p[5] == 64 and p[6] == 32 and p[7] == 32)


def load_dbs(db_storage):
    out = {}
    for root, _, files in os.walk(db_storage):
        for fn in files:
            if fn.endswith(".db"):
                ap = os.path.join(root, fn)
                try:
                    with open(ap, "rb") as f:
                        p1 = f.read(PAGE)
                    if len(p1) >= PAGE:
                        out[os.path.relpath(ap, db_storage)] = p1
                except OSError:
                    pass
    return out


def iter_regions(pid):
    for line in open(f"/proc/{pid}/maps"):
        parts = line.split()
        if len(parts) < 2:
            continue
        addrs, perms = parts[0], parts[1]
        if "r" not in perms:
            continue
        a, b = addrs.split("-")
        start, end = int(a, 16), int(b, 16)
        # 跳过巨大的文件映射(可选)
        yield start, end


TEXT = re.compile(rb"x'([0-9a-fA-F]{64,96})'")


IMG_KEY_RE = re.compile(rb"(?<![A-Za-z0-9])[A-Za-z0-9]{16}(?![A-Za-z0-9])")
# 强特征(≥3字节)避免误报；微信图片多为 JPEG
IMG_MAGICS = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"RIFF")


def find_image_key(pid, account_dir):
    """扫内存找 16 字节图片密钥：用它 AES-128-ECB 解 .dat 首块应得图片头。"""
    sample = None
    attach = os.path.join(account_dir, "msg", "attach")
    for root, _, files in os.walk(attach):
        for fn in files:
            if fn.endswith(".dat") and not fn.endswith("_t.dat"):
                sample = os.path.join(root, fn)
                break
        if sample:
            break
    if not sample:
        return None
    with open(sample, "rb") as f:
        head = f.read(15 + 16)
    if head[:6] != b"\x07\x08V2\x08\x07" or len(head) < 31:
        return None
    block = head[15:31]
    hex_re = re.compile(rb"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")
    seen = set()

    def test(k):                      # k: 16 bytes → 是否解出图片头
        try:
            return AES.new(k, AES.MODE_ECB).decrypt(block).startswith(IMG_MAGICS)
        except ValueError:
            return False

    mem = open(f"/proc/{pid}/mem", "rb", 0)
    for start, end in iter_regions(pid):
        size = end - start
        if size <= 0 or size > 2 * 1024 ** 3:
            continue
        try:
            mem.seek(start)
            data = mem.read(size)
        except (OSError, ValueError, OverflowError):
            continue
        # (a) 16 字节 ASCII 字母数字形式
        for m in IMG_KEY_RE.finditer(data):
            k = m.group(0)
            if k not in seen:
                seen.add(k)
                if test(k):
                    return k.decode()
        # (b) 32 位 hex 文本 → 16 字节 key
        for m in hex_re.finditer(data):
            try:
                k = bytes.fromhex(m.group(0).decode())
            except ValueError:
                continue
            if k not in seen:
                seen.add(k)
                if test(k):
                    return "hex:" + k.hex()
    return None


def main():
    db_storage = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else "keys.json"
    pid = wechat_pid()
    if not pid:
        print("wechat 进程未找到"); sys.exit(1)
    print(f"wechat pid={pid}")
    dbs = load_dbs(db_storage)
    salt_index = {p1[:SALT]: rel for rel, p1 in dbs.items()}
    salts = list(salt_index.keys())
    print(f"目标 {len(dbs)} 个库")

    keys = {}
    text_hits = 0
    total = 0
    mem = open(f"/proc/{pid}/mem", "rb", 0)
    for start, end in iter_regions(pid):
        size = end - start
        if size <= 0 or size > 2 * 1024 ** 3:
            continue
        try:
            mem.seek(start)
            data = mem.read(size)
        except (OSError, ValueError, OverflowError):
            continue
        total += len(data)
        # 文本形式
        for m in TEXT.finditer(data):
            text_hits += 1
            hx = m.group(1).decode()
            cand = bytes.fromhex(hx[:64])
            for rel, p1 in dbs.items():
                if rel not in keys and precheck(cand, p1):
                    keys[rel] = cand.hex(); print("  ✓(text)", rel, cand.hex()[:16])
        # salt 邻接原始 key
        for salt in salts:
            rel = salt_index[salt]
            if rel in keys:
                continue
            idx = data.find(salt)
            while idx >= 0:
                for off in (idx - 32, idx + SALT):
                    if 0 <= off and off + 32 <= len(data):
                        c = data[off:off + 32]
                        if precheck(c, dbs[rel]):
                            keys[rel] = c.hex(); print("  ✓(salt)", rel, c.hex()[:16]); break
                idx = data.find(salt, idx + 1)
        if len(keys) >= len(dbs):
            break
    print(f"扫描 {total/1e6:.0f}MB, text_hits={text_hits}, 命中 {len(keys)}/{len(dbs)}")
    # 图片密钥（16 字节字母数字，解密 .dat 的 AES 段首块得到图片头）
    try:
        ik = find_image_key(pid, os.path.dirname(db_storage))
        if ik:
            keys["_img_key"] = ik
            print("  ✓ 图片密钥:", ik)
    except Exception as e:  # noqa: BLE001
        print("  图片密钥提取失败:", e)
    json.dump(keys, open(out_path, "w"), indent=2)
    core = ["contact/contact.db", "message/message_0.db", "session/session.db",
            "head_image/head_image.db"]
    for c in core:
        print(f"  {c}: {'✓' if c in keys else '✗'}")


if __name__ == "__main__":
    main()
