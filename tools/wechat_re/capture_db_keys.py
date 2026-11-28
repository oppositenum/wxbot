"""命令行抓数据库密钥(容器内跑):hook AES 设置密钥,补齐验证不过的库,写回本账号 keys.json(先备份)。不打印密钥。
逻辑在 core/db_keys.py(后台「刷新密钥」也用它)。

用法: PYTHONPATH=/app python3 tools/wechat_re/capture_db_keys.py [秒数=60] [库 rel ...] [--trigger]
      --trigger:自动往文件传输助手直发文字/视频/语音,让微信读库;不加时请自己在微信里点开会话、收发消息。
"""
import os
import sys

sys.path.insert(0, "/app")
import config  # noqa: E402
from core import db_keys  # noqa: E402


def main():
    args = [a for a in sys.argv[1:] if a != "--trigger"]
    secs = int(args[0]) if args and args[0].isdigit() else 60
    rels = [a for a in args if not a.isdigit()]
    st = config.db_storage_dir()
    want = rels or db_keys.missing(db_keys.load(), st)
    if not want:
        print("所有库的密钥都能用,不用抓")
        return
    print("要补的库:", want)
    found = db_keys.capture(want, st, secs=secs,
                            trigger=(lambda: db_keys.trigger_reads()) if "--trigger" in sys.argv else None)
    if found:
        db_keys.merge(found)
        print("已写入", config.keys_json())
    left = [r for r in want if r not in found]
    if left:
        print("没抓到:", left, "(让微信读一下这些库再跑,比如点开有视频的会话)")
        sys.exit(2)


if __name__ == "__main__":
    main()
