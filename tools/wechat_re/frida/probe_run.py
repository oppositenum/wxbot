"""跑 probe_msg.js 一次:probe_run.py '<cfg json>' 会话 local_id create_time"""
import json, sys, time
import frida
cfg, chat, lid, ct = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
dev = frida.get_local_device(); pid = min(p.pid for p in dev.enumerate_processes() if p.name == "wechat")
s = dev.attach(pid); sc = s.create_script("const CFG = " + cfg + ";\n" + open(sys.argv[0].replace("probe_run.py", "probe_msg.js")).read())
sc.on("message", lambda m, d: print("MSG", m, flush=True)); sc.load()
print(sc.exports_sync.run(chat, lid, ct))
for _ in range(50):
    r = sc.exports_sync.poll()
    if r.get("step") == 9: break
    time.sleep(0.1)
print(json.dumps(r, ensure_ascii=False))
