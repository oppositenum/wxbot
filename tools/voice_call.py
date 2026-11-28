"""AI 语音通话命令行(实验)。在微信容器里以 wechat 用户运行:

    cd /app && python3 tools/voice_call.py --selftest [--purpose 提醒吃药]   # 不打电话,测延迟
    cd /app && python3 tools/voice_call.py --dial wxid_xxx [--purpose 提醒吃药] # 拨号(对方要在可拨打名单里)

平时用控制台「语音通话」页或定时任务,不需要这个脚本。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
from core import voice_call  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dial", default="")
    ap.add_argument("--purpose", default="")
    a = ap.parse_args()
    if a.selftest:
        out = os.path.join(config.WORK_DIR, "voice_calls", "selftest-" + time.strftime("%Y%m%d-%H%M%S"))
        return voice_call.selftest_turns(out, purpose=a.purpose)
    if a.dial:
        done = []
        cid = voice_call.place_call(a.dial, purpose=a.purpose, source="cli", on_done=done.append)
        print("call", cid, flush=True)
        while not done:
            time.sleep(1)
        print("result", done[0].get("result"), done[0].get("error"), flush=True)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
