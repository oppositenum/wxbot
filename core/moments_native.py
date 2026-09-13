"""Linux WeChat Moments adapter: local OCR, exact clipboard text, live identity.

No network vision, hooks, database writes or unchecked chat-sender fallback.
Callers must own docker_wx.UI_LOCK for the entire prepare/submit/cleanup cycle.
"""
import csv
import difflib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

from PIL import Image, ImageOps, ImageChops, ImageStat
import config
from core import account_session as sessions, moments as m


def norm(text):
    return re.sub(r'[^\w\u4e00-\u9fff]', '', text).lower()


class NativeError(m.Unavailable):
    pass


class Native:
    def __init__(self):
        self.env = dict(os.environ, DISPLAY=os.environ.get('DISPLAY', ':0'))
        self.proof = None
        self.window = None
        self.staged = None
        self.submit_point = None
        self.editor = None
        self.opened = False
        self.submitted = False

    @staticmethod
    def capability():
        tools = ['xdotool', 'wmctrl', 'scrot', 'xclip', 'tesseract', 'xprop', 'xwininfo']
        if os.environ.get('WXBOT_LOCAL') != '1' or any(not shutil.which(x) for x in tools):
            return False, '朋友圈操作需要同容器 Linux 微信和本地文字识别组件'
        return True, ''

    def run(self, *args, timeout=6):
        r = subprocess.run(list(args), env=self.env, capture_output=True, timeout=timeout)
        if r.returncode:
            raise NativeError('微信界面命令未完成')
        return r.stdout

    def windows(self):
        out = []
        r = self.run('wmctrl', '-lp').decode()
        for line in r.splitlines():
            parts = line.split(None, 4)
            if len(parts) == 5:
                out.append(dict(id=str(int(parts[0], 16)), pid=int(parts[2]), title=parts[4]))
        return out

    def state(self):
        pids = self.run('pgrep', '-x', 'wechat').decode().split()
        if len(pids) != 1:
            raise NativeError('无法唯一确认运行中的微信进程')
        pid = pids[0]
        roots = {}
        for fd in Path('/proc', pid, 'fd').iterdir():
            try:
                path = os.readlink(fd)
            except OSError:
                continue
            if '/db_storage/' not in path or path.endswith(' (deleted)'):
                continue
            root, rel = path.split('/db_storage/', 1)
            roots.setdefault(os.path.realpath(root), set()).add(rel)
        candidates = [r for r, rels in roots.items() if {'contact/contact.db', 'session/session.db', 'message/message_0.db'} <= rels]
        if len(candidates) != 1:
            raise NativeError('当前登录账号不能唯一确认')
        root = candidates[0]
        token = sessions.check()
        if config._strip_folder_suffix(Path(root).name) != token['account']:
            raise NativeError('界面登录账号与任务账号不一致')
        stamp = [pid, Path('/proc', pid, 'stat').read_text().split()[21], root]
        for rel in ['contact/contact.db', 'session/session.db', 'message/message_0.db']:
            stat = Path(root, 'db_storage', rel).stat(); stamp.append([rel,stat.st_ino])
        return dict(pid=int(pid), signature=hashlib.sha256(json.dumps(stamp).encode()).hexdigest(), session=token)

    def check(self, foreground=False):
        state = self.state()
        if self.proof and state != self.proof:
            raise NativeError('操作期间微信登录状态变化，已停止')
        if foreground:
            wid = self.run('xdotool', 'getactivewindow').decode().strip()
            pid = int(self.run('xdotool', 'getwindowpid', wid))
            if pid != state['pid']:
                raise NativeError('微信窗口焦点已变化，已停止')
        return state

    def geom(self, wid):
        values = dict(x.split('=', 1) for x in self.run('xdotool', 'getwindowgeometry', '--shell', str(wid)).decode().splitlines())
        return tuple(int(values[x]) for x in ('X', 'Y', 'WIDTH', 'HEIGHT'))

    def shot(self, box=None):
        with tempfile.TemporaryDirectory(prefix='moments-ui-') as d:
            p = Path(d) / 'screen.png'
            self.run('scrot', str(p))
            im = Image.open(p).convert('RGB')
            return im.crop(box) if box else im.copy()

    def ocr(self, box, psm=11, white=False):
        im = self.shot(box)
        if white:
            im = im.convert("L").point(lambda v: 0 if v > 235 else 255)
        with tempfile.TemporaryDirectory(prefix='moments-ocr-') as d:
            p = Path(d) / 'crop.png'; im.resize((im.width*3, im.height*3)).save(p)
            raw = self.run('tesseract', str(p), 'stdout', '-l', 'chi_sim', '--psm', str(psm), 'tsv', timeout=10).decode()
        grouped = {}
        for r in csv.DictReader(io.StringIO(raw), delimiter='\t'):
            if not r.get('text', '').strip() or float(r['conf']) < 10:
                continue
            key = (r['block_num'], r['par_num'], r['line_num'])
            g = grouped.setdefault(key, dict(text='', x=100000, y=100000, r=0, b=0))
            x, y, w, h = (int(r[z])/3 for z in ('left', 'top', 'width', 'height'))
            g.update(text=g['text']+r['text'], x=min(g['x'], x), y=min(g['y'], y), r=max(g['r'], x+w), b=max(g['b'], y+h))
        return [dict(text=g['text'], x=box[0]+(g['x']+g['r'])/2, y=box[1]+(g['y']+g['b'])/2,
                     left=box[0]+g['x'], top=box[1]+g['y'], bottom=box[1]+g['b']) for g in grouped.values()]

    def key(self, *keys):
        self.check(True); self.run('xdotool', 'key', '--clearmodifiers', *keys); time.sleep(.15)

    def click(self, x, y, button=1):
        self.check(True)
        self.run('xdotool', 'mousemove', str(int(x)), str(int(y)), 'click', str(button)); time.sleep(.25)

    def label(self, text, box, exact=True):
        rows = self.ocr(box)
        if not any(norm(r['text'])==norm(text) for r in rows):
            rows += self.ocr(box,psm=6)
        if text in ('发送', '发表'):
            rows += self.ocr(box, white=True)
        hits = [r for r in rows if norm(r['text']) == norm(text) or (not exact and norm(text) in norm(r['text']))]
        hits = [r for i,r in enumerate(hits) if not any(abs(r['x']-q['x'])<8 and abs(r['y']-q['y'])<8 for q in hits[:i])]
        if len(hits) != 1:
            if text in ('发送','发表'):
                return self.green_button(text,box)
            raise NativeError('界面操作入口无法唯一识别：'+text)
        return hits[0]

    def green_button(self, text, box):
        # Visual templates captured from the supported Linux client, containing
        # only the two button labels. Match shape + glyphs, not position alone.
        im=self.shot(box); pixels=im.load(); points=set()
        for y in range(im.height):
            for x in range(im.width):
                r,g,b=pixels[x,y]
                if r<40 and 150<g<240 and 50<b<180:points.add((x,y))
        template=Image.open(Path(__file__).parent/'moments_templates'/('comment.png' if text=='发送' else 'publish.png')).convert('RGB')
        hits=[]
        while points:
            start=points.pop(); stack=[start]; group=[start]
            while stack:
                x,y=stack.pop()
                for q in ((x-1,y),(x+1,y),(x,y-1),(x,y+1)):
                    if q in points:points.remove(q);stack.append(q);group.append(q)
            if len(group)<600:continue
            l=min(q[0] for q in group);r=max(q[0] for q in group)+1
            t=min(q[1] for q in group);b=max(q[1] for q in group)+1
            if abs((r-l)-template.width)>3 or abs((b-t)-template.height)>3:continue
            crop=im.crop((l,t,r,b))
            a={(ix,iy) for iy in range(3,crop.height-3) for ix in range(3,crop.width-3) if min(crop.getpixel((ix,iy)))>200}
            z={(ix,iy) for iy in range(3,template.height-3) for ix in range(3,template.width-3) if min(template.getpixel((ix,iy)))>200}
            best=1.0
            for dx in range(-2,3):
                for dy in range(-1,2):
                    shifted={(ix+dx,iy+dy) for ix,iy in a}
                    if shifted|z:best=min(best,len(shifted^z)/len(shifted|z))
            if best<.4:hits.append(dict(x=box[0]+(l+r)/2,y=box[1]+(t+b)/2))
        if len(hits)!=1:raise NativeError('提交按钮外观不匹配，未发送')
        return hits[0]

    def clip(self):
        r = subprocess.run(['xclip', '-selection', 'clipboard', '-o', '-target', 'UTF8_STRING'], env=self.env, capture_output=True, timeout=3)
        return r.stdout.decode(errors='replace') if r.returncode == 0 else ''

    def put_clip(self, text):
        proc = subprocess.Popen(['xclip', '-selection', 'clipboard'], env=self.env, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.communicate(text.encode(), timeout=3)

    # The comment context menu is a fixed vertical stack in this order.
    MENU=['复制','搜一搜','回复','删除']

    def _menu_open(self, x, y):
        """Right-click a comment and OCR the popped-up context menu region."""
        self.click(x, y, 3)
        screen = self.shot()
        box = (max(0,x-10), max(0,y-10), min(screen.width,x+190), min(screen.height,y+190))
        return self.ocr(box)

    def _menu_point(self, allrows, want):
        """Locate a menu item's click point among the fixed 复制/搜一搜/回复/删除 stack.

        OCR fuses the copy glyph into its label ("全复制"/"作复制") and, near the
        screen bottom, bleed-through comment text garbles whichever item overlaps
        it — so we match each label by substring on the short menu rows, then, if
        the wanted item itself is unreadable, infer its position from the evenly
        spaced neighbours (the order and row spacing are fixed).
        """
        idx={l:i for i,l in enumerate(self.MENU)}
        found={}
        for r in sorted(allrows,key=lambda r:r['y']):
            nr=norm(r['text'])
            if len(nr)>5:continue  # menu labels are short; skip bleed-through comment text
            for l in self.MENU:
                if l in nr and l not in found:found[l]=r
        if not found:raise NativeError('复制菜单项无法唯一识别')
        wi=idx[want]
        if want in found:return dict(x=found[want]['x'],y=found[want]['y'])
        anchors=sorted(((idx[l],r) for l,r in found.items()),key=lambda a:a[0])
        if len(anchors)>=2:
            two=sorted(anchors,key=lambda a:abs(a[0]-wi))[:2]
            (i0,r0),(i1,r1)=sorted(two,key=lambda a:a[0])
            spacing=(r1['y']-r0['y'])/(i1-i0)
        else:
            spacing=30.0
        if not 18<=spacing<=60:raise NativeError('复制菜单项无法唯一识别')
        base=min(anchors,key=lambda a:abs(a[0]-wi))
        return dict(x=base[1]['x'],y=base[1]['y']+(wi-base[0])*spacing)

    def copy_at(self, x, y):
        original = self.clip(); sentinel = 'wx-moments-'+os.urandom(8).hex()
        try:
            self.put_clip(sentinel)
            spot=self._menu_point(self._menu_open(x, y),'复制')
            self.click(spot['x'], spot['y'])
            # WeChat writes the clipboard asynchronously after the click; poll
            # briefly so we read the copied text rather than the stale sentinel.
            result = self.clip()
            for _ in range(10):
                if result != sentinel:break
                time.sleep(.1); result = self.clip()
            return '' if result == sentinel else result
        except NativeError:
            self.key('Escape'); return ''
        finally:
            self.put_clip(original)

    def reply_at(self, x, y):
        """Open the reply editor for the comment at (x,y) via its 回复 menu item.

        A left-click on the comment is ambiguous — a short body puts the row centre
        on the author's name, which opens their profile card instead of the editor.
        The 回复 context-menu entry targets the exact comment unambiguously.
        """
        spot=self._menu_point(self._menu_open(x, y),'回复')
        self.click(spot['x'], spot['y'])

    def open(self):
        self.proof = self.state()
        windows = self.windows()
        mains = [w for w in windows if w['pid']==self.proof['pid'] and w['title']=='微信']
        if len(mains)!=1:
            raise NativeError('微信主窗口无法唯一确认')
        matches = [w for w in windows if w['pid']==self.proof['pid'] and w['title']=='朋友圈']
        visible=matches and 'IsViewable' in self.run('xwininfo','-id',matches[0]['id']).decode()
        if not visible:
            self.opened=True
            main=mains[0]['id'];self.run('xdotool','windowactivate','--sync',main)
            x,y,w,h=self.geom(main)
            # Supported Linux WeChat 4.1 sidebar: fourth icon. This only opens UI.
            self.click(x+32,y+250)
            matches=[w for w in self.windows() if w['pid']==self.proof['pid'] and w['title']=='朋友圈']
        if len(matches)!=1:
            raise NativeError('朋友圈窗口未打开')
        self.window=matches[0]['id']
        for candidate in self.windows():
            if candidate['pid']!=self.proof['pid'] or candidate['id']==self.window:continue
            prop=self.run('xprop','-id',candidate['id'],'WM_TRANSIENT_FOR','_NET_WM_STATE').decode()
            if hex(int(self.window)) in prop and '_NET_WM_STATE_MODAL' in prop:
                self.opened=False
                raise NativeError('微信朋友圈有待处理对话框，请先在微信中完成或取消')
        self.run('xdotool','windowactivate','--sync',self.window)
        x,y,w,h=self.geom(self.window)
        if w<500 or h<450:
            raise NativeError('朋友圈窗口尺寸不受支持')
        rows=self.ocr((x,y,x+w,y+h))
        if any(norm(r['text'])=='取消' for r in rows) and any('提醒谁看' in norm(r['text']) or '这一刻的想法' in norm(r['text']) for r in rows):
            self.opened=False
            raise NativeError('微信中已有朋友圈编辑草稿，请先完成或取消，系统不会覆盖')
        return x,y,w,h

    def refresh(self):
        x,y,w,h=self.open()
        self.click(x+126,y+22)  # refresh icon in this supported client
        time.sleep(.8)
        self.check(True)

    def _avatars(self):
        x,y,w,h=self.geom(self.window)
        im=self.shot((x,y,x+w,y+h))
        bands=[];start=None
        # Feed avatars occupy a fixed left column. Ignore dark cover rows.
        for iy in range(65,h-30):
            row=[im.getpixel((ix,iy)) for ix in range(25,64)]
            colorful=sum(max(p)-min(p)>25 and min(p)<220 for p in row)>5
            if colorful and start is None:start=iy
            if not colorful and start is not None:
                if 22<=iy-start<=55:bands.append(start)
                start=None
        return [y+b for b in bands]

    def find_post(self, item):
        if not item['text'].strip():
            raise NativeError('该动态没有可复制的正文，暂不能可靠定位')
        with m.database() as c:
            duplicates=[json.loads(r[0]) for r in c.execute('SELECT payload FROM feed')]
        # We require an exact, unique body in the account cache. Same text in any
        # other cached post is ambiguous even when display names differ.
        if sum(p['text']==item['text'] for p in duplicates)!=1:
            raise NativeError('存在相同正文的动态，无法唯一确认目标')
        x,y,w,h=self.open()
        self.run('xdotool','mousemove',str(x+w-30),str(y+h-65),'click','--repeat','20','--delay','15','4')
        time.sleep(.3)
        deadline=time.monotonic()+25
        for step in range(16):
            if time.monotonic()>deadline:raise NativeError('朋友圈定位超时，本次未发送')
            copied_attempts=0
            for top in self._avatars():
                from core import docker_wx
                if time.monotonic()>deadline or docker_wx.priority_pending():
                    raise NativeError('聊天优先或朋友圈定位超时，本次未发送')
                # Copying every avatar is expensive and visibly floods the clipboard.
                # OCR narrows the candidates first; copy only a few ambiguous rows.
                if copied_attempts >= 4:
                    break
                copied_attempts += 1
                copied=self.copy_at(x+96,top+38)
                if copied.strip()==item['text'].strip():
                    # The body is already globally unique in cache (line 323) and was
                    # copied verbatim here, so identity is established. The author label
                    # is a secondary sanity check against a stale-cache mix-up — but OCR
                    # of a tiny name strip is lossy (drops chars, chokes on emoji), so
                    # match it fuzzily and, when OCR reads nothing at all, defer to the
                    # unique-body proof rather than hard-failing a correct target.
                    authors=self.ocr((x+75,top-15,x+w-40,top+13),psm=6)
                    names={norm(item['name'])}
                    from core import contacts
                    for c in contacts.list_contacts():
                        if c['username']==item['author']:
                            names.update(norm(c.get(k) or '') for k in ['name','nick_name','remark'])
                    names.discard('')
                    read=[norm(r['text']) for r in authors];read=[t for t in read if t]
                    def _name_ok(t):
                        return any(t==nm or (min(len(t),len(nm))>=2 and (t in nm or nm in t))
                                   or difflib.SequenceMatcher(None,t,nm).ratio()>=0.6 for nm in names)
                    if read and not any(_name_ok(t) for t in read):
                        raise NativeError('动态作者名称未能核对，未发送')
                    self.check(True)
                    return dict(x=x,y=y,w=w,h=h,top=top,body=(x+96,top+38))
            if step<15:
                from core import docker_wx
                if docker_wx.priority_pending():
                    raise NativeError('聊天发送优先，本次朋友圈定位已暂停')
                self.run('xdotool','mousemove',str(x+w-30),str(y+h-65),'click','5');time.sleep(.2)
        raise NativeError('没有在当前可见朋友圈中找到目标，未发送')

    def _scroll_to_comment(self, pos, target):
        """Scroll the target comment into view and return its OCR hit row.

        Comments always render below their post avatar, and find_post leaves our
        avatar visible, so a target comment is only ever at or below the fold —
        we only scroll down. Each notch we re-anchor our post's avatar top
        geometrically (no clipboard copies), keep the region fenced between our
        avatar and the next post's avatar so a body that is unique only within
        this post stays unambiguous, and OCR that region. OCR is lossy (it drops
        or garbles characters), so a row is matched by fuzzy similarity, not exact
        substring — this only LOCATES a candidate to right-click; copy_at then
        re-verifies the exact text before anything is sent, so a mis-locate fails
        cleanly. Raises NativeError (original wording) if nothing resolves in budget.
        """
        x,y,w,h,top=(pos[k] for k in ('x','y','w','h','top'))
        nt=norm(target['text'])
        def _score(r):
            nr=norm(r['text'])
            return difflib.SequenceMatcher(None,nt,nr).ratio() if nr else 0.0
        NOTCH_MAX=200  # one scroll notch is shorter than a post that has comments
        own_top=top    # trusted: find_post already copy_at-verified this avatar
        deadline=time.monotonic()+30
        last_sig=None;stall=0
        for _ in range(24):
            from core import docker_wx
            if time.monotonic()>deadline or docker_wx.priority_pending():
                raise NativeError('聊天优先或朋友圈定位超时，本次未发送')
            avatars=sorted(self._avatars())
            if own_top is not None:
                cand=[t for t in avatars if own_top-NOTCH_MAX<=t<=own_top]
                own_top=max(cand) if cand else None  # None => our avatar left the top (TAIL)
            if own_top is not None:
                region_top=own_top+60
                below=[t for t in avatars if t>own_top+40]
            else:
                region_top=y+56  # previous post is already gone above the fold
                below=list(avatars)  # smallest avatar is the next post
            bottom=min(below) if below else y+h-25
            # When the post header itself sits near the fold its comments are still
            # entirely below the viewport, so the fenced region is empty (top>=bottom):
            # skip OCR and scroll rather than crop an inverted box.
            if bottom>region_top+10:
                rows=self.ocr((x+78,region_top,x+w-20,bottom))
                best=max(rows,key=_score,default=None)
                # 0.8 clears real OCR noise (dropped/garbled glyphs score ~0.87-0.93)
                # while a merely similar neighbouring comment stays below it; copy_at
                # is the exact gate, so keep scrolling rather than grabbing a weak row.
                if best is not None and _score(best)>=0.8:return best
            if own_top is None and below and min(below)<=y+120:break  # next post reached the top
            sig=tuple(round(t) for t in avatars)
            stall=stall+1 if sig==last_sig else 0;last_sig=sig
            if stall>=2:break  # feed bottom, nothing new revealed
            self.run('xdotool','mousemove',str(x+w-30),str(y+h-65),'click','5');time.sleep(.2)
        raise NativeError('目标评论未完整显示，未发送')

    def input_text(self, point, text):
        self.click(*point)
        old=self.clip();sentinel='wx-empty-'+os.urandom(8).hex()
        try:
            self.put_clip(sentinel);self.key('ctrl+a');self.key('ctrl+c')
            if self.clip() not in ('',sentinel):
                raise NativeError('微信输入框已有内容，未覆盖')
            self.put_clip(text);self.key('ctrl+v');self.staged=(point,text);self.key('ctrl+a');self.key('ctrl+c')
            if self.clip()!=text:
                raise NativeError('输入内容回读不一致，未发送')
            self.key('Right')
            self.staged=(point,text)
        finally:
            self.put_clip(old)

    def prepare_comment(self, item, reply_id, text):
        pos=self.find_post(item);x,y,w,h,top=(pos[k] for k in ('x','y','w','h','top'))
        if reply_id:
            matches=[c for c in item['comments'] if c['id']==reply_id]
            if len(matches)!=1:raise NativeError('目标评论不唯一')
            target=matches[0]
            if sum(c['text']==target['text'] for c in item['comments'])!=1 or not norm(target['text']):
                raise NativeError('评论内容重复或为空，无法核对回复对象')
            # Later comments can sit below the fold; scroll the target into view
            # (fenced within this post) before verifying and clicking it.
            hit=self._scroll_to_comment(pos,target)
            copied=self.copy_at(hit['x'],hit['y'])
            # copy_at yields one whole comment. A top-level comment copies as the
            # body alone (or 昵称+正文); a reply copies as "昵称 回复 某人：正文",
            # so also accept when both the author name and the exact body appear.
            nc=norm(copied);nt=norm(target['text']);nn=norm(target['name'])
            if nc not in {nt,norm(target['name']+target['text'])} and not (nt and nt in nc and nn and nn in nc):
                raise NativeError('目标评论正文核对失败')
            self.reply_at(hit['x'],hit['y'])
        else:
            rows=self.ocr((x+75,top+42,x+w-15,min(y+h,top+260)))
            times=[r for r in rows if any(t in r['text'] for t in ['分钟前','小时前','昨天','天前','刚刚','月','年'])]
            if not times:raise NativeError('动态操作栏未识别')
            self.click(x+w-40,times[0]['y'])
            hit=self.label('评论',(x+w-143,times[0]['y']-18,x+w-65,times[0]['y']+19),exact=False)
            self.click(hit['x'],hit['y'])
        time.sleep(.2)
        self.editor=self.window
        # The editor has a unique green outline even when its send button is disabled.
        im=self.shot((x,y,x+w,y+h)); bands=[]
        for iy in range(60,h-15):
            count=sum(1 for ix in range(80,w-20) if (lambda p:p[1]>120 and p[1]>p[0]*1.15 and p[1]>p[2]*1.1)(im.getpixel((ix,iy))))
            if count>w*.6:bands.append(iy)
        if not bands or max(bands)-min(bands)<55:raise NativeError('评论编辑框未识别')
        self.input_text((x+115,y+min(bands)+22),text)
        send=self.label('发送',(x+w-120,y+max(bands)-55,x+w-20,y+max(bands)))
        self.submit_point=(send['x'],send['y'])
        self.anchor=self.shot((x,y,x+w,y+60)).tobytes()

    def prepare_publish(self, text, paths):
        x,y,w,h=self.open()
        self.click(x+78,y+22,3)
        title='选照片或视频' if paths else '发表文字'
        hit=self.label(title,(x+87,y+30,x+194,y+94))
        self.click(hit['x'],hit['y'])
        if paths:
            # GTK file chooser; exact file paths are account-owned draft assets.
            self.key('ctrl+l');old=self.clip()
            try:
                filenames=' '.join('"'+str(p)+'"' for p in paths)
                self.put_clip(filenames);self.key('ctrl+a');self.key('ctrl+v');self.key('ctrl+a');self.key('ctrl+c')
                if self.clip()!=filenames:raise NativeError('图片路径回读失败，未发布')
                self.key('Return')
            finally:self.put_clip(old)
            time.sleep(.5)
        windows=[v for v in self.windows() if v['pid']==self.proof['pid'] and v['title'] in ('发表朋友圈','发表文字','朋友圈')]
        wid=self.run('xdotool','getactivewindow').decode().strip()
        self.editor=wid
        ex,ey,ew,eh=self.geom(wid)
        # This WeChat build renders the composer inside the Moments window.
        if len(paths)>=3:
            self.run('xdotool','mousemove',str(ex+ew//2),str(ey+eh-180),'click','--repeat','12','--delay','20','5');time.sleep(.2)
        self.label('公开',(ex,ey,ex+ew,ey+eh),exact=False)
        if len(paths)>=3:
            self.run('xdotool','mousemove',str(ex+ew//2),str(ey+eh-180),'click','--repeat','20','--delay','20','4');time.sleep(.2)
        self.label('取消',(ex,ey,ex+ew,ey+eh))
        placeholder=self.label('这一刻的想法',(ex,ey,ex+ew,ey+eh),exact=False)
        if paths:
            # The supported composer uses 100px square thumbnails, three columns.
            left=ex+(ew-378)//2+34;top=round(placeholder['y'])+103
            for index,path in enumerate(paths):
                tx=left+(index%3)*105;ty=top+(index//3)*105
                actual=self.shot((tx+5,ty+5,tx+95,ty+50))
                expected=ImageOps.fit(Image.open(path).convert('RGB'),(100,100)).crop((5,5,95,50))
                error=sum(ImageStat.Stat(ImageChops.difference(actual,expected)).mean)/(3*255)
                if error>.10:raise NativeError('图片预览与待发图片不匹配，未发布')
        self.input_text((placeholder['x'],placeholder['y']),text)
        hit=self.label('发表',(ex,ey,ex+ew,ey+eh),exact=True)
        self.submit_point=(hit['x'],hit['y'])
        self.anchor=self.shot((ex,ey,ex+ew,ey+40)).tobytes()

    def submit(self):
        self.check(True)
        wid=self.run('xdotool','getactivewindow').decode().strip()
        if not self.staged or wid!=self.editor:
            raise NativeError('提交前编辑窗口已变化')
        point,text=self.staged;old=self.clip()
        try:
            self.click(*point);self.key('ctrl+a');self.key('ctrl+c')
            if self.clip()!=text:raise NativeError('提交前文案已变化')
            self.key('Right');self.staged=None;self.submitted=True;self.click(*self.submit_point)
        finally:self.put_clip(old)

    def cleanup(self):
        if self.submitted:return  # do not touch an editor/feed after an ambiguous click
        try:
            self.check()
            if self.staged:
                old=self.clip();point,text=self.staged
                self.click(*point);self.key('ctrl+a');self.key('ctrl+c')
                if self.clip()==text:self.key('BackSpace')
                else:self.key('Right')
                self.put_clip(old)
            if self.editor==self.window and self.editor:
                x,y,w,h=self.geom(self.editor)
                try:
                    hit=self.label('取消',(x,y,x+w,y+h));self.click(hit['x'],hit['y'])
                    active=self.run('xdotool','getactivewindow').decode().strip()
                    if active!=self.window:
                        props=self.run('xprop','-id',active,'WM_TRANSIENT_FOR').decode()
                        if hex(int(self.window)) in props:
                            ax,ay,aw,ah=self.geom(active)
                            discard=self.label('不保留',(ax,ay,ax+aw,ay+ah))
                            self.click(discard['x'],discard['y'])
                except NativeError:pass
            if self.editor and self.editor!=self.window:
                # Window manager close is safe only for our existing editor.
                if any(w['id']==self.editor for w in self.windows()):
                    self.run('wmctrl','-ic',hex(int(self.editor)))
            if self.opened and self.window and any(w['id']==self.window for w in self.windows()):
                self.run('wmctrl','-ic',hex(int(self.window)))
        except Exception:
            pass
