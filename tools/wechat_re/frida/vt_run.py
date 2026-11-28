"""vt_run.py hooks.txt 秒数:挂 hook_vt.js,到时打印汇总"""
import json, sys, time, os
import frida
hooks = open(sys.argv[1]).read().split(); secs = float(sys.argv[2])
dev = frida.get_local_device(); pid = min(p.pid for p in dev.enumerate_processes() if p.name == "wechat")
s = dev.attach(pid); src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook_vt.js")).read()
sc = s.create_script("const CFG = " + json.dumps({"hooks": hooks}) + ";\n" + src)
sc.on("message", lambda m, d: print("MSG", m, flush=True)); sc.load()
open("/tmp/wre/vt_ready", "w").write("1")
time.sleep(secs)
for k, v in sorted(sc.exports_sync.take().items(), key=lambda kv: -kv[1]): print(v, k)
sc.unload(); s.detach()
