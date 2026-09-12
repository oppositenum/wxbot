"""Account-scoped, durable local-media readiness. No UI or model calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

import config
from core import account_session as sessions, imgdec, media

_thread = None
_start_lock = threading.Lock()
LABELS = {'ready':'本地媒体已就绪', 'preview':'目前只有图片预览', 'poster':'目前只有视频封面',
          'pending':'等待微信接收媒体文件', 'missing_key':'图片暂不可解密',
          'failed':'当前媒体暂不可读取', 'decoder_unavailable':'当前语音暂不可播放'}


@contextlib.contextmanager
def database():
    token = sessions.check()
    root = Path(config.account_dir())
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'media-state.sqlite3'
    con = sqlite3.connect(path, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        with con:
            con.execute('CREATE TABLE IF NOT EXISTS media (chat TEXT,id INTEGER,type INTEGER,'
                        'status TEXT,reason TEXT,attempts INTEGER,next_check REAL,updated REAL,'
                        'width INTEGER,height INTEGER,PRIMARY KEY(chat,id))')
            yield con
            sessions.check(token)
    finally:
        con.close()
    path.chmod(0o600)


@sessions.task
def observe(chat, rows, reset=False):
    if not isinstance(chat,str) or not chat or len(chat)>256:
        raise ValueError('invalid_chat')
    with database() as con:
        for m in rows:
            if m.get('type') not in (3,34,43) or not isinstance(m.get('local_id'),int):
                continue
            con.execute('INSERT OR IGNORE INTO media VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (chat,m['local_id'],m['type'],'pending','awaiting_local_file',0,0,0,0,0))
            if reset:
                con.execute("UPDATE media SET attempts=0,next_check=0 WHERE chat=? AND id=? AND status<>'ready'",
                            (chat,m['local_id']))
        con.execute('DELETE FROM media WHERE rowid NOT IN (SELECT rowid FROM media ORDER BY rowid DESC LIMIT 500)')
        result = {r['id']: dict(r) for r in con.execute('SELECT * FROM media WHERE chat=?',(chat,))}
    return result


def inspect(row):
    typ = row['type']; chat = row['chat']; lid = row['id']
    if typ == 3:
        data, reason = imgdec.get_msg_image(chat,lid)
        if not data:
            return dict(status='missing_key' if str(reason).startswith('no-img-key') else
                        'failed' if str(reason).startswith('decode') else 'pending', reason='image_unavailable')
        from PIL import Image
        try:
            with Image.open(io.BytesIO(data)) as im:
                im.load(); w,h=im.size
        except Exception:
            return dict(status='failed',reason='image_decode_failed')
        # Resolution alone cannot prove an original. Small images are labelled
        # preview conservatively; larger readable files are simply "ready".
        return dict(status='preview' if max(w,h)<=512 else 'ready',reason='local_image',width=w,height=h)
    if typ == 43:
        base = media._video_base(chat,lid)
        if base and media.video_paths(base):
            return dict(status='ready',reason='local_video')
        data,_ = media.get_msg_video_thumb(chat,lid)
        return dict(status='poster' if data else 'pending',reason='local_video_poster' if data else 'video_not_downloaded')
    rowdata = media._voice_row(chat,lid)
    if not rowdata or not rowdata[1]:
        return dict(status='pending',reason='voice_not_received')
    return dict(status='ready' if media._silk_available() else 'decoder_unavailable',reason='local_voice')


@sessions.task
def tick(limit=4):
    now=time.time()
    with database() as con:
        rows=[dict(r) for r in con.execute("SELECT * FROM media WHERE status<>'ready' AND attempts<90 "
                'AND next_check<=? ORDER BY next_check,rowid LIMIT ?', (now,limit))]
    keys = (['media'] if any(r['type']==34 for r in rows) else []) + (['msgres'] if any(r['type']==43 for r in rows) else [])
    if keys:
        from core import decrypt
        try:decrypt.run(force=False,only=keys)
        except sessions.StaleAccount:raise
        except Exception:pass
    for row in rows:
        try:
            result=inspect(row)
        except sessions.StaleAccount:
            raise
        except Exception:
            result=dict(status='failed',reason='local_read_failed')
        with database() as con:
            con.execute('UPDATE media SET status=?,reason=?,attempts=attempts+1,next_check=?,updated=?,width=?,height=? '
                        'WHERE chat=? AND id=?', (result['status'],result['reason'],now+10,now,
                        result.get('width',0),result.get('height',0),row['chat'],row['id']))
    return len(rows)


def hook_status():
    captured=imgdec._capture_dir()
    if not captured:
        return dict(state='waiting_for_wechat',label='等待微信登录')
    try:
        data=json.loads((Path(captured).parent/'status.json').read_text())
    except (OSError,ValueError):
        return dict(state='unavailable',label='媒体捕获尚未连接')
    live=time.time()-float(data.get('updated_at',0))<10
    ready=live and data.get('state')=='ready'
    return dict(state='ready' if ready else 'unavailable',label='媒体自动接收中' if ready else '媒体捕获暂未连接',
                captures=int(data.get('captures',0)),updated_at=data.get('updated_at'),ui_navigation=False)


def start():
    global _thread
    with _start_lock:
        if _thread and _thread.is_alive():return
        _thread=threading.Thread(target=_loop,name='local-media',daemon=True)
        _thread.start()


def _loop():
    while True:
        try:
            with sessions.bind():tick()
        except Exception:
            pass
        time.sleep(1)
