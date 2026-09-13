"""Account-bound Moments cache, drafts and automation settings.

The cache is a last-observed snapshot, not a complete history or deletion oracle.
Client operations and durable jobs live in moments_native and moments_jobs.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from core import account_session as sessions

CHINA = ZoneInfo('Asia/Shanghai')
_sync_lock = threading.Lock()
MAX_IMAGE_BYTES = 15 * 1024 * 1024
DEFAULTS = dict(revision=0, sync_enabled=False, sync_interval_minutes=10,
                auto_comment=False, auto_publish=False, chat_reflection=False, quiet_start='22:00', quiet_end='08:00',
                daily_comment_limit=0, daily_publish_limit=0, min_interval_minutes=0,
                publish_interval_minutes=120,
                friend_allowlist=[], publish_time='12:30', moods=['喜悦', '平静', '趣事'],
                publish_images=True, publish_web_opinions=True,
                comment_since=0, publish_since=0)


class Conflict(ValueError):
    pass


class Unavailable(RuntimeError):
    pass


def capabilities():
    from core.moments_native import Native
    ready, reason = Native.capability()
    return dict(read=True, drafts=True, send=ready, auto_comment=ready, auto_publish=ready, chat_reflection=ready,
                media_preview=False, image_publish=ready, reason=reason)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


@contextlib.contextmanager
def database():
    sessions.check()
    p = Path(config.account_dir()) / 'moments.sqlite3'
    c = sqlite3.connect(p, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS feed (
          id TEXT PRIMARY KEY, author TEXT NOT NULL, created INTEGER NOT NULL,
          payload TEXT NOT NULL, digest TEXT NOT NULL, observed INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS feed_order ON feed(created DESC, id DESC);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS drafts (
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, feed_id TEXT NOT NULL,
          payload TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL,
          created INTEGER NOT NULL, updated INTEGER NOT NULL, digest TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS assets (
          id TEXT PRIMARY KEY, mime TEXT NOT NULL, width INTEGER NOT NULL,
          height INTEGER NOT NULL, created INTEGER NOT NULL);
        ''')
        yield c
        sessions.check()
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()


def _get(c, key, default):
    row = c.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _set(c, key, value):
    c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, _json(value)))


def _text(node, path, limit=20000):
    return (node.findtext(path) or '')[:limit]


def _int(value, default=0):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _id(value):
    # SNS IDs exceed JS safe integers and may be signed in SQLite.
    n = int(value)
    if not -(1 << 63) <= n < (1 << 64) or n == 0:
        raise ValueError('invalid_sns_id')
    return str(n % (1 << 64))


def _comment_id(item, prefix=''):
    for name in [prefix + 'comment_64id', prefix + 'comment_id']:
        value = _text(item, name, 100)
        if value and value != '0':
            return value
    return ''


def parse_feed(tid, author, xml):
    if not isinstance(xml, str) or len(xml) > 2_000_000 or '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():
        raise ValueError('invalid_sns_xml')
    root = ET.fromstring(xml)
    t = root.find('TimelineObject')
    if t is None:
        raise ValueError('missing_timeline')
    fid = _id(tid)
    if _id(_text(t, 'id')) != fid or not author or _text(t, 'username') != author:
        raise ValueError('feed_identity_mismatch')
    created = _int(_text(t, 'createTime'))
    if not 0 < created < 32503680000:
        raise ValueError('invalid_feed_time')
    media = []
    for m in t.findall('./ContentObject/mediaList/media')[:100]:
        size = m.find('size')
        media.append(dict(id=_text(m, 'id', 100), type=_text(m, 'type', 20),
                          width=_int(size.get('width')) if size is not None else 0,
                          height=_int(size.get('height')) if size is not None else 0,
                          duration=_int(_text(m, 'videoDuration')), preview_available=False))
    comments, likes = [], []
    for name, output in [('comment_user_list', comments), ('like_user_list', likes)]:
        for item in root.findall('.//' + name + '/user_comment')[:2000]:
            if _int(_text(item, 'b_deleted')):
                continue
            output.append(dict(id=_comment_id(item),
                               author=_text(item, 'username', 200), name=_text(item, 'nickname', 200),
                               text=_text(item, 'content'), created=_int(_text(item, 'create_time')),
                               reply_to=_text(item, 'ref_username', 200),
                               reply_id=_comment_id(item, 'ref_')))
    return dict(id=fid, author=author, name=_text(root, 'LocalExtraInfo/nickname', 200) or author,
                created=created, text=_text(t, 'contentDesc'), media=media,
                content_type=_text(t, 'ContentObject/type', 20),
                title=_text(t, 'ContentObject/title', 1000), comments=comments, likes=likes)


@contextlib.contextmanager
def _decrypted_timeline():
    """Decrypt sns.db into a temporary snapshot and yield a read-only connection.

    No GUI navigation, model/network calls or key capture. Account root is pinned.
    """
    with sessions.bind() as token:
        base = config.db_storage_dir()
        if not base or config._strip_folder_suffix(Path(base).parent.name) != token['account']:
            raise Unavailable('当前账号的数据目录无法确认')
        src = Path(base) / 'sns/sns.db'
        if not src.is_file():
            raise Unavailable('尚未发现朋友圈缓存，请先在微信中打开朋友圈')
        from core.decrypt import decrypt_db
        with open(config.keys_json()) as f:
            key = json.load(f).get('sns/sns.db')
        if not key:
            raise Unavailable('当前账号缺少朋友圈读取密钥，请在已有密钥管理中处理')
        with tempfile.TemporaryDirectory(prefix='wx-moments-') as d:
            p = str(Path(d) / 'sns.db')
            decrypt_db(str(src), key, p)
            with contextlib.closing(sqlite3.connect('file:' + p + '?mode=ro', uri=True)) as c:
                if c.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise Unavailable('微信正在同步，缓存快照不完整，请稍后重试')
                yield c


def sync():
    """Commit the timeline snapshot only after complete validation."""
    if not _sync_lock.acquire(blocking=False):
        raise Conflict('朋友圈正在同步，请稍后刷新')
    try:
        with _decrypted_timeline() as c:
            rows = c.execute('SELECT tid,user_name,content FROM SnsTimeLine').fetchall()
        return ingest(rows)
    finally:
        _sync_lock.release()


def raw_content(feed_id):
    """Just-in-time raw timeline XML for one feed (image url/key live only here).

    Decrypts on demand and returns the single matching row's XML; the url/key it
    contains are used transiently for media download and never persisted.
    """
    fid = _id(feed_id)
    with _decrypted_timeline() as c:
        for tid, _user, content in c.execute('SELECT tid,user_name,content FROM SnsTimeLine'):
            try:
                if _id(tid) == fid:
                    return content
            except (ValueError, TypeError):
                continue
    return None


def ingest(rows):
    now = int(time.time())
    items, invalid = [], 0
    for row in rows:
        try:
            items.append(parse_feed(*row))
        except (ValueError, TypeError, ET.ParseError):
            invalid += 1
    if rows and not items:
        raise Unavailable('朋友圈格式无法解析，已保留上次数据')
    added = changed = 0
    with database() as c:
        for item in items:
            payload = _json(item)
            digest = hashlib.sha256(payload.encode()).hexdigest()
            old = c.execute('SELECT digest FROM feed WHERE id=?', (item['id'],)).fetchone()
            added += int(old is None)
            changed += int(old is not None and old[0] != digest)
            c.execute('INSERT OR REPLACE INTO feed VALUES (?,?,?,?,?,?)',
                      (item['id'], item['author'], item['created'], payload, digest, now))
        result = dict(at=now, source_count=len(rows), parsed=len(items), invalid=invalid,
                      added=added, changed=changed)
        _set(c, 'sync', result)
        _set(c, 'sync_error', None)
        _set(c, 'last_sync_attempt', now)
    return result


def settings():
    with database() as c:
        return dict(DEFAULTS, **_get(c, 'settings', {}))


def save_settings(patch, revision):
    if not isinstance(patch, dict) or set(patch) - (set(DEFAULTS) - {'revision','comment_since','publish_since'}):
        raise ValueError('未知设置字段')
    with database() as c:
        c.execute('BEGIN IMMEDIATE')
        value = dict(DEFAULTS, **_get(c, 'settings', {}))
        if type(revision) is not int or revision != value['revision']:
            raise Conflict('设置已更新，请重新加载后保存')
        previous = dict(value)
        value.update(patch)
        for key in ['sync_enabled', 'auto_comment', 'auto_publish', 'chat_reflection', 'publish_images', 'publish_web_opinions']:
            if type(value[key]) is not bool:
                raise ValueError('开关必须是布尔值')
        for key, lo, hi in [('sync_interval_minutes', 5, 1440), ('daily_comment_limit', 0, 1000),
                            ('daily_publish_limit', 0, 1000), ('min_interval_minutes', 0, 10080),
                            ('publish_interval_minutes', 30, 1440)]:
            if type(value[key]) is not int or not lo <= value[key] <= hi:
                raise ValueError('频率或数量超出允许范围')
        for key in ['quiet_start', 'quiet_end', 'publish_time']:
            if not isinstance(value[key], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value[key]):
                raise ValueError('免打扰时间应为 HH:MM')
        if value['quiet_start'] == value['quiet_end']:
            raise ValueError('免打扰开始和结束时间不能相同')
        # Only reject when the user actively moves the publish time INTO the quiet
        # window. Sliding the quiet window over an unchanged publish_time must not
        # fail the whole save (that made 免打扰 edits appear to "reset" — the form
        # posts every field at once, so one rejected field discards them all). The
        # scheduler already skips auto-publish during quiet hours, so a publish_time
        # that later falls inside quiet stays consistent at runtime.
        if value['auto_publish'] and value['publish_time'] != previous['publish_time']:
            a,b,t=value['quiet_start'],value['quiet_end'],value['publish_time']
            if (a<=t<b if a<b else t>=a or t<b):
                raise Conflict('发布时间处于免打扰时段，请调整发布时间后保存')
        friends = value['friend_allowlist']
        if not isinstance(friends, list) or len(friends) > 200 or any(not isinstance(x, str) for x in friends):
            raise ValueError('好友范围无效')
        if friends:
            from core import contacts
            known = {x['username'] for x in contacts.list_contacts()}
            if any(x not in known or x.endswith('@chatroom') for x in friends):
                raise ValueError('好友不属于当前账号')
        value['friend_allowlist'] = list(dict.fromkeys(friends))
        if (value['auto_comment'] or value['auto_publish'] or value['chat_reflection']) and not capabilities()['send']:
            raise Unavailable(capabilities()['reason'])
        if not isinstance(value['moods'], list) or not value['moods'] or any(x not in ['喜悦','愤怒','轻微烦躁','平静','趣事','随想'] for x in value['moods']):
            raise ValueError('请选择有效的情绪与话题')
        value['moods'] = list(dict.fromkeys(value['moods']))
        for flag, watermark in [('auto_comment','comment_since'),('auto_publish','publish_since')]:
            if value[flag] and not previous[flag]:value[watermark] = time.time()
        value['revision'] += 1
        _set(c, 'settings', value)
    return value


def quiet_now(value, now=None):
    moment = datetime.fromtimestamp(time.time() if now is None else now, CHINA)
    clock = moment.strftime('%H:%M')
    start, end = value['quiet_start'], value['quiet_end']
    return start <= clock < end if start < end else clock >= start or clock < end


def catalog(limit=30, offset=0, author='', search=''):
    limit, offset = int(limit), int(offset)
    if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
        raise ValueError('分页范围无效')
    if len(author) > 200 or len(search) > 200:
        raise ValueError('查询过长')
    where, args = [], []
    if author:
        where.append('author=?'); args.append(author)
    if search:
        # Literal substring, not a SQL LIKE pattern.
        where.append('instr(payload,?)>0'); args.append(search)
    clause = ' WHERE ' + ' AND '.join(where) if where else ''
    with database() as c:
        total = c.execute('SELECT count(*) FROM feed' + clause, args).fetchone()[0]
        rows = c.execute('SELECT payload,observed,digest FROM feed' + clause +
                         ' ORDER BY created DESC,id DESC LIMIT ? OFFSET ?', args + [limit, offset]).fetchall()
        authors = [dict(author=r[0], count=r[1]) for r in c.execute('SELECT author,count(*) FROM feed GROUP BY author ORDER BY count(*) DESC')]
        last = _get(c, 'sync', None)
    return dict(items=[dict(json.loads(r[0]), observed=r[1], digest=r[2]) for r in rows],
                total=total, offset=offset, limit=limit, authors=authors, last_sync=last)


def detail(fid):
    with database() as c:
        row = c.execute('SELECT payload,digest,observed FROM feed WHERE id=?', (_id(fid),)).fetchone()
    if row is None:
        raise ValueError('动态不在当前账号缓存中')
    return dict(json.loads(row[0]), digest=row[1], observed=row[2])


def _draft(row):
    out = dict(row)
    out.update(json.loads(out.pop('payload')))
    return out


def drafts():
    with database() as c:
        return [_draft(r) for r in c.execute('SELECT * FROM drafts ORDER BY updated DESC LIMIT 200')]


def save_draft(body):
    kind = body.get('kind')
    text = body.get('text', '')
    assets = body.get('assets', [])
    if kind not in ('comment', 'publish') or not isinstance(text, str) or len(text) > (2000 if kind == 'comment' else 10000):
        raise ValueError('草稿类型或文字长度无效')
    if not isinstance(assets, list) or len(assets) > 9 or any(not isinstance(x, str) for x in assets):
        raise ValueError('最多选择九张图片')
    if not text.strip() and not assets:
        raise ValueError('请填写文字或选择图片')
    if kind == 'comment' and (assets or not text.strip()):
        raise ValueError('朋友圈评论只接受文字')
    fid, snapshot = '', None
    reply_id = body.get('reply_id', '')
    if not isinstance(reply_id, str):
        raise ValueError('回复评论 ID 无效')
    if kind == 'comment':
        item = detail(body.get('feed_id'))
        fid = item['id']
        if body.get('feed_digest') != item['digest']:
            raise Conflict('动态或评论已更新，请重新打开后核对')
        if reply_id and not any(x['id'] == reply_id for x in item['comments']):
            raise Conflict('要回复的评论已不在缓存中，请重新核对')
        snapshot = dict(author=item['author'], name=item['name'], text=item['text'],
                        created=item['created'], digest=item['digest'])
    elif reply_id:
        raise ValueError('发布草稿不能指定回复评论')
    payload = dict(text=text.strip(), assets=list(dict.fromkeys(assets)), reply_id=reply_id, snapshot=snapshot)
    digest = hashlib.sha256(_json([kind, fid, payload]).encode()).hexdigest()
    jid = body.get('id') or uuid.uuid4().hex
    if not isinstance(jid, str) or not re.fullmatch('[a-f0-9]{32}', jid):
        raise ValueError('草稿 ID 无效')
    now = int(time.time())
    with database() as c:
        c.execute('BEGIN IMMEDIATE')
        for aid in assets:
            if not c.execute('SELECT 1 FROM assets WHERE id=?', (aid,)).fetchone():
                raise ValueError('图片不属于当前账号')
        old = c.execute('SELECT * FROM drafts WHERE id=?', (jid,)).fetchone()
        if old:
            if old['status'] != 'draft' or body.get('revision') != old['revision']:
                raise Conflict('草稿已变化，请刷新后核对')
            if old['kind'] != kind or old['feed_id'] != fid:
                raise Conflict('不能将已有草稿改为其他目标')
        elif body.get('revision', 0) != 0:
            raise Conflict('草稿不存在')
        revision = old['revision'] + 1 if old else 1
        c.execute('INSERT OR REPLACE INTO drafts VALUES (?,?,?,?,?,?,?,?,?)',
                  (jid, kind, fid, _json(payload), 'draft', revision, old['created'] if old else now, now, digest))
        result = _draft(c.execute('SELECT * FROM drafts WHERE id=?', (jid,)).fetchone())
    return result


def discard_draft(jid, revision):
    with database() as c:
        cur = c.execute("UPDATE drafts SET status='discarded',revision=revision+1,updated=? WHERE id=? AND revision=? AND status='draft'",
                        (int(time.time()), jid, revision))
        if cur.rowcount != 1:
            raise Conflict('草稿已变化，请重新加载')
    return dict(id=jid, status='discarded')


def upload(stream):
    """Decode/re-encode image to remove metadata and reject filename/path payloads."""
    from PIL import Image, ImageOps
    raw = stream.read(MAX_IMAGE_BYTES + 1)
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError('图片不能超过 15 MB')
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.format not in ('JPEG', 'PNG', 'WEBP') or source.width * source.height > 25_000_000:
                raise ValueError('只支持不超过 2500 万像素的 JPG、PNG、WebP 图片')
            source.load()
            im = ImageOps.exif_transpose(source).convert('RGB')
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError('无法读取图片') from exc
    buf = io.BytesIO(); im.save(buf, format='JPEG', quality=92)
    raw = buf.getvalue(); aid = hashlib.sha256(raw).hexdigest()
    directory = Path(config.account_dir()) / 'moments_assets'
    directory.mkdir(exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.upload-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(raw)
        with database() as c:
            sessions.check()
            os.replace(temp, directory / (aid + '.jpg'))
            c.execute('INSERT OR IGNORE INTO assets VALUES (?,?,?,?,?)', (aid, 'image/jpeg', im.width, im.height, int(time.time())))
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return dict(id=aid, width=im.width, height=im.height, mime='image/jpeg')


def asset(aid):
    if not re.fullmatch('[a-f0-9]{64}', aid):
        raise ValueError('图片 ID 无效')
    with database() as c:
        if not c.execute('SELECT 1 FROM assets WHERE id=?', (aid,)).fetchone():
            raise ValueError('图片不属于当前账号')
    return Path(config.account_dir()) / 'moments_assets' / (aid + '.jpg')


def periodic_once(now=None):
    """Optional local cache sync only; never dispatches or calls a model."""
    now = time.time() if now is None else now
    with sessions.bind():
        value = settings()
        if not value['sync_enabled']:
            return 'disabled'
        with database() as c:
            attempt = _get(c, 'last_sync_attempt', 0)
            if now - attempt < value['sync_interval_minutes'] * 60:
                return 'waiting'
            _set(c, 'last_sync_attempt', now)
        try:
            sync()
        except Exception:
            with database() as c:
                _set(c, 'sync_error', dict(at=int(now), message='自动同步未完成，保留上次数据；可手动重试'))
            return 'failed'
        with database() as c:
            _set(c, 'sync_error', None)
        return 'synced'


def status():
    with database() as c:
        return dict(last_sync=_get(c, 'sync', None), sync_error=_get(c, 'sync_error', None),
                    count=c.execute('SELECT count(*) FROM feed').fetchone()[0],
                    automation_running=bool(_worker and _worker.is_alive()),
                    last_worker_tick=_get(c,'last_worker_tick',None),worker_error=_get(c,'worker_error',None))


_worker = None
_worker_lock = threading.Lock()


def start_loop():
    global _worker
    with _worker_lock:
        if _worker and _worker.is_alive():
            return
        def loop():
            while True:
                try:
                    from core import moments_jobs
                    moments_jobs.tick()
                except Exception:
                    try:
                        with database() as c:
                            _set(c,'worker_error','自动任务检查未完成，请查看执行记录或后台日志')
                    except Exception:pass  # no usable account: leave its data untouched
                from core.moments_jobs import wake
                wake.wait(15); wake.clear()
        _worker = threading.Thread(target=loop, name='moments-automation', daemon=True)
        _worker.start()
