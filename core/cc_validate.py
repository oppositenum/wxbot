"""验证 lldb 抓到的候选 key(/tmp/cc_keys.txt)，匹配到各数据库，写 keys.json。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from core import keys as K

CAND_FILE = "/tmp/cc_keys.txt"


def main():
    if not os.path.exists(CAND_FILE):
        print("没有候选文件", CAND_FILE)
        sys.exit(1)
    cands = []
    for line in open(CAND_FILE):
        line = line.strip()
        if len(line) == 64:
            try:
                cands.append(bytes.fromhex(line))
            except ValueError:
                pass
    print(f"候选 key {len(cands)} 个")

    db_storage = config.db_storage_dir()
    meta = K.load_db_meta(db_storage)   # rel -> {salt,page1,path}
    result = {}
    for rel, m in meta.items():
        for cand in cands:
            if K.precheck_key(cand, m["page1"]):
                result[rel] = cand.hex()
                break

    config.ensure_dirs()
    with open(config.keys_json(), "w") as f:
        json.dump(result, f, indent=2)
    su = os.environ.get("SUDO_USER")
    if su and su != "root":
        import pwd
        pw = pwd.getpwnam(su)
        try:
            os.chown(config.keys_json(), pw.pw_uid, pw.pw_gid)
        except OSError:
            pass

    print(f"匹配到 {len(result)}/{len(meta)} 个库密钥，写入 {config.keys_json()}")
    core = config.CORE_DBS
    missing = [k for k, v in core.items() if v not in result]
    for k, v in core.items():
        print(f"  {k:12} {'✓' if v in result else '✗ 缺'}")
    if missing:
        print("核心库仍缺：", missing, "—— 请在微信里多滑动几个会话/联系人后重跑抓取。")
        sys.exit(2)
    print("✅ 核心库密钥齐全。")


if __name__ == "__main__":
    main()
