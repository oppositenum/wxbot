"""界面手动操作小工具(逆向抓包用)。ui.py open 名字 | shot 路径 | click x y [3] | paste 文字 | key k.. | geom"""
import sys, time, subprocess
sys.path.insert(0, "/usr/local/bin")
import wx_send_legacy as L
cmd = sys.argv[1]; a = sys.argv[2:]
wid = L.win_id()
if cmd == "open":
    print(L.open_chat(wid, a[0], a[1] if len(a) > 1 else ""))
elif cmd == "geom":
    print(L.win_geom(wid))
elif cmd == "shot":
    subprocess.run(["scrot", "-o", a[0]])
elif cmd == "click":
    subprocess.run(["xdotool", "mousemove", a[0], a[1], "click", a[2] if len(a) > 2 else "1"]); time.sleep(0.4)
elif cmd == "paste":
    L.set_clip_text(a[0]); time.sleep(0.1); L.key("ctrl+v"); time.sleep(0.3)
elif cmd == "type":
    subprocess.run(["xdotool", "type", "--delay", "80", a[0]]); time.sleep(0.4)
elif cmd == "key":
    for k in a: L.key(k); time.sleep(0.25)
if cmd == "restore":
    print(L.restore_main(wid))
