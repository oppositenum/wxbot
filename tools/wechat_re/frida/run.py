"""在容器里附加微信并常驻运行一个 Frida 脚本,消息逐行写到 jsonl。

准备(容器里没有 pip/frida,手动装一次;容器重建后要重装):
  # 宿主机下载与容器架构匹配的 wheel(aarch64, abi3),版本和下面 agent 无关,用当前 frida 即可
  pip download frida --only-binary=:all: --platform manylinux2014_aarch64 --python-version 3.12 -d /tmp/fw
  docker cp /tmp/fw/frida-*.whl wxbot-ubuntu-manual:/tmp/
  docker exec wxbot-ubuntu-manual sh -c 'cd /tmp && python3 -m zipfile -e frida-*.whl /tmp/fr'
  # 以后用 PYTHONPATH=/tmp/fr python3 ...;wechat 用户即可附加,不需要 root

用法(容器内):
  PYTHONPATH=/tmp/fr python3 run.py hook_sendmessage.js --cfg '{"send":["0x4baee74"],"size":1120,"log":"/tmp/send.log"}' --out /tmp/send.jsonl
  然后在界面里发一条消息,看 /tmp/send.jsonl。Ctrl+C 结束(会自动卸载 hook)。
"""
import argparse
import json
import sys
import threading

import frida


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("script")
    ap.add_argument("--cfg", default="{}", help="注入为 const CFG = <json>")
    ap.add_argument("--out", default="/tmp/frida_run.jsonl")
    a = ap.parse_args()
    dev = frida.get_local_device()
    pids = [p.pid for p in dev.enumerate_processes() if p.name == "wechat"]
    if not pids:
        sys.exit("微信没有在运行")
    session = dev.attach(min(pids))
    src = "const CFG = " + a.cfg + ";\n" + open(a.script, encoding="utf-8").read()
    script = session.create_script(src)

    def on(msg, _data):
        payload = msg.get("payload") if msg.get("type") == "send" else msg
        with open(a.out, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        print(json.dumps(payload, ensure_ascii=False)[:300], flush=True)

    script.on("message", on)
    script.load()
    print("已附加 pid", min(pids), "输出", a.out, flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        session.detach()


if __name__ == "__main__":
    main()
