"""Existing Linux X11 client controls. No hook, injection or model calls.

Commands travel on stdin. Clipboard contents and screenshots are never logged.
UI_LOCK is owned by caller; a human VNC session is outside that Python lock.
"""
import base64
import hashlib
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path
from PIL import Image
import config
from core import docker_wx

HELPER = r'''
import sys,json,os,glob,subprocess,time,hashlib,base64,io
from PIL import Image
q=json.load(sys.stdin)
E=dict(os.environ,DISPLAY=':0')
def run(*args):
 r=subprocess.run(list(args),env=E,capture_output=True,timeout=5)
 if r.returncode:raise RuntimeError('ui_command_failed')
 return r.stdout
def key(*keys):run('xdotool','key','--clearmodifiers',*keys)
def click(x,y,b=1):run('xdotool','mousemove',str(int(x)),str(int(y)),'click',str(b))
def clipget():return run('xclip','-selection','clipboard','-o','-target','UTF8_STRING').decode()
def clipset(v):
 p=subprocess.Popen(['xclip','-selection','clipboard'],env=E,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 p.communicate(v.encode(),timeout=3)
def state():
 pids=run('pgrep','-x','wechat').decode().split()
 if len(pids)!=1:raise RuntimeError('client_count_not_one')
 pid=pids[0];roots={}
 for fd in glob.glob('/proc/'+pid+'/fd/*'):
  try:p=os.readlink(fd)
  except OSError:continue
  # Only open, live canonical account DBs. Exclude deleted, backup and all_users.
  if '/db_storage/' not in p:continue
  root,rel=p.split('/db_storage/',1)
  if os.path.dirname(root)!='/root/xwechat_files' or root.endswith('/all_users'):continue
  if os.path.exists(p):roots.setdefault(root,set()).add(rel)
 candidates=[root for root,rels in roots.items() if {'contact/contact.db','session/session.db','message/message_0.db'}<=rels]
 if len(candidates)!=1:raise RuntimeError('active_account_ambiguous')
 root=candidates[0]
 ids=run('xdotool','search','--onlyvisible','--pid',pid).decode().split()
 mains=[]
 for wid in ids:
  title=run('xdotool','getwindowname',wid).decode().strip()
  if title!='微信':continue
  geom=dict(l.split('=',1) for l in run('xdotool','getwindowgeometry','--shell',wid).decode().splitlines())
  if int(geom['WIDTH'])>=800 and int(geom['HEIGHT'])>=600:mains.append((wid,geom))
 if len(mains)!=1:raise RuntimeError('main_window_ambiguous')
 wid,geom=mains[0]
 stamp=[]
 for name in ['login_config','login_configv2']:
  p=root+'/config/'+name
  if os.path.exists(p):
   s=os.stat(p);stamp.append([name,s.st_ino,s.st_size,s.st_mtime_ns])
 signature=hashlib.sha256(json.dumps([pid,open('/proc/'+pid+'/stat').read().split()[21],root,stamp],sort_keys=True).encode()).hexdigest()
 return dict(account_folder=os.path.basename(root),signature=signature,window=wid,geometry={k:int(geom[k]) for k in ['X','Y','WIDTH','HEIGHT']})
def shot():
 # Capture existing framebuffer without focusing any window.
 data=run('scrot','-o','/tmp/wxbot-native-frame.png')
 with open('/tmp/wxbot-native-frame.png','rb') as f:data=f.read()
 os.unlink('/tmp/wxbot-native-frame.png')
 return data
def header(s):
 g=s['geometry'];im=Image.open(io.BytesIO(shot())).crop((g['X']+285,g['Y']+25,g['X']+g['WIDTH']-40,g['Y']+75)).convert('RGB')
 return hashlib.sha256(im.tobytes()).hexdigest()
a=q['action']
if a=='state':out=state()
elif a=='shot':out={'png':base64.b64encode(shot()).decode()}
elif a=='clipboard':out={'value':clipget()}
elif a=='restore':clipset(q['value']);out={'ok':True}
elif a=='click':click(q['x'],q['y'],q.get('button',1));time.sleep(.25);out={'ok':True}
elif a=='key':key(*q['keys']);time.sleep(.2);out={'ok':True}
elif a=='scroll':
 click(q['x'],q['y'],4 if q.get('up') else 5);time.sleep(.15);out={'ok':True}
elif a=='fingerprint':
 s=state();out=dict(s,header=header(s),active=run('xdotool','getactivewindow').decode().strip()==s['window'])
elif a=='copy_field':
 old=clipget();clipset(q['sentinel']);click(q['x'],q['y'],3);time.sleep(.25);out={'ok':True,'original':old}
elif a=='read_input':
 s=state();g=s['geometry'];old=clipget();sentinel=q['sentinel'];clipset(sentinel)
 click(g['X']+g['WIDTH']//2,g['Y']+g['HEIGHT']-70);key('ctrl+a');key('ctrl+c');time.sleep(.15)
 v=clipget();key('Right');clipset(old);out={'text':'' if v==sentinel else v}
elif a=='stage':
 s=state();g=s['geometry']
 if s['signature']!=q['signature'] or header(s)!=q['header']:raise RuntimeError('target_changed')
 old=clipget();sentinel=q['sentinel'];clipset(sentinel)
 click(g['X']+g['WIDTH']//2,g['Y']+g['HEIGHT']-70);key('ctrl+a');key('ctrl+c');time.sleep(.15)
 if clipget()!=sentinel:clipset(old);raise RuntimeError('existing_draft')
 clipset(q['text']);key('ctrl+v');time.sleep(.2);key('ctrl+a');key('ctrl+c');time.sleep(.15)
 exact=clipget()==q['text'];key('Right');clipset(old)
 if not exact:raise RuntimeError('input_verification_failed')
 out={'ok':True}
elif a=='discard_stage':
 s=state()
 if s['signature']!=q['signature'] or header(s)!=q['header']:out={'cleared':False}
 else:
  g=s['geometry'];old=clipget();clipset(q['sentinel'])
  click(g['X']+g['WIDTH']//2,g['Y']+g['HEIGHT']-70);key('ctrl+a');key('ctrl+c');time.sleep(.15)
  same=clipget()==q['text']
  if same:key('BackSpace')
  else:key('Right')
  clipset(old);out={'cleared':same}
elif a=='send_once':
 s=state()
 if s['signature']!=q['signature'] or header(s)!=q['header']:raise RuntimeError('target_changed_before_click')
 if run('xdotool','getactivewindow').decode().strip()!=s['window']:raise RuntimeError('focus_changed')
 g=s['geometry'];click(g['X']+g['WIDTH']-65,g['Y']+g['HEIGHT']-25)
 out={'initiated':True}
else:raise RuntimeError('unknown_native_action')
print(json.dumps(out))
'''


class NativeUI:
    def __init__(self):
        self.binary = os.path.join(config.WORK_DIR, 'native-adapter', 'recognize-text')

    @property
    def available(self):
        return os.path.isfile(self.binary) and os.access(self.binary, os.X_OK)

    def call(self, action, **args):
        command=['python3','-c',HELPER]
        if not docker_wx.LOCAL:command=['docker','exec','-i',docker_wx.CONTAINER]+command
        r=subprocess.run(command,input=json.dumps(dict(action=action,**args)),capture_output=True,text=True,timeout=12)
        if r.returncode:
            # Neither stderr nor command arguments may enter logs.
            reason=next((s for s in ['existing_draft','target_changed','focus_changed','active_account_ambiguous','client_count_not_one','input_verification_failed'] if s in r.stderr),'native_ui_failed')
            raise RuntimeError(reason)
        return json.loads(r.stdout)

    def ocr(self, box):
        raw=base64.b64decode(self.call('shot')['png']);im=Image.open(io.BytesIO(raw)).crop(box)
        with tempfile.TemporaryDirectory(prefix='wx-native-ocr-') as td:
            path=Path(td)/'crop.png';im.save(path)
            r=subprocess.run([self.binary,str(path)],capture_output=True,text=True,timeout=12)
            if r.returncode:raise RuntimeError('local_ocr_failed')
            rows=json.loads(r.stdout)
        w,h=im.size
        for row in rows:
            x,y,rw,rh=row['box'];row['point']=(box[0]+(x+rw/2)*w,box[1]+(1-y-rh/2)*h)
            row['right_point']=(box[0]+(x+rw*.9)*w,box[1]+(1-y-rh/2)*h)
        return rows
