"""Durable account-scoped Moments jobs. An initiated send is NEVER replayed."""
import contextlib
from datetime import datetime
import json
import threading
import time
import uuid

from core import account_session as sessions, moments as m

wake = threading.Event()
_tick_lock = threading.Lock()

# A comment whose job only failed transiently (throttle, quiet hours, expiry, a model
# format hiccup or a UI miss) must not be buried forever by its dedup key. Re-enqueue it
# on a cooldown, capped, unless the model deliberately decided it wasn't worth replying.
AUTO_RETRY_COOLDOWN = 1800  # seconds between automatic retries of the same comment
MAX_AUTO_ATTEMPTS = 5       # give up after this many automatic attempts


@contextlib.contextmanager
def db():
    with m.database() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS moments_jobs (
          id TEXT PRIMARY KEY, dedup TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
          origin TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL,
          session TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
          initiated REAL NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
          receipt TEXT NOT NULL DEFAULT '');''')
        yield c


def row(r):
    d=dict(r);d['payload']=json.loads(d['payload']);d.pop('session',None);return d


def listing():
    with db() as c:
        return [row(r) for r in c.execute('SELECT * FROM moments_jobs ORDER BY created DESC LIMIT 100')]


def insert(c, dedup, kind, origin, payload, now, state='queued', message='等待处理'):
    jid=uuid.uuid4().hex
    c.execute('INSERT OR IGNORE INTO moments_jobs(id,dedup,kind,origin,state,payload,session,created,updated,message) VALUES (?,?,?,?,?,?,?,?,?,?)',
              (jid,dedup,kind,origin,state,m._json(payload),m._json(sessions.check()),now,now,message))
    return row(c.execute('SELECT * FROM moments_jobs WHERE dedup=?',(dedup,)).fetchone())


def enqueue_draft(jid, revision):
    if not m.capabilities()['send']:raise m.Unavailable(m.capabilities()['reason'])
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        d=c.execute('SELECT * FROM drafts WHERE id=?',(jid,)).fetchone()
        if not d:raise ValueError('草稿不存在')
        existing=c.execute('SELECT * FROM moments_jobs WHERE dedup=?',('draft:'+jid,)).fetchone()
        if existing:return row(existing)  # identical request returns the same job, never sends twice
        if d['revision']!=revision or d['status']!='draft':raise m.Conflict('草稿已变化')
        d=m._draft(d)
        if d['kind']=='comment' and m.detail(d['feed_id'])['digest']!=d['snapshot']['digest']:
            raise m.Conflict('动态已更新，请重新核对后保存新草稿')
        payload=dict(draft_id=jid,text=d['text'],assets=d['assets'],feed_id=d['feed_id'],reply_id=d['reply_id'],snapshot=d['snapshot'])
        for recent in c.execute("SELECT kind,payload FROM moments_jobs WHERE created>? AND state IN ('queued','preparing','initiated','confirmed','uncertain')",(time.time()-600,)):
            q=json.loads(recent['payload'])
            if recent['kind']==d['kind'] and all(q.get(k)==payload.get(k) for k in ['text','assets','feed_id','reply_id']):
                raise m.Conflict('相同内容已有近期任务，请先查看执行记录，避免重复发送')
        result=insert(c,'draft:'+jid,d['kind'],'manual',payload,time.time())
        c.execute("UPDATE drafts SET status='queued',updated=? WHERE id=?",(int(time.time()),jid))
    wake.set();return result


def finish(jid, state, message, receipt=''):
    from core.moments_reflection import forget
    forget(jid)
    with db() as c:
        c.execute('UPDATE moments_jobs SET state=?,message=?,receipt=?,updated=? WHERE id=?',
                  (state,message,receipt,time.time(),jid))
        r=c.execute('SELECT payload FROM moments_jobs WHERE id=?',(jid,)).fetchone()
        if r:
            did=json.loads(r[0]).get('draft_id')
            if did:c.execute('UPDATE drafts SET status=?,updated=? WHERE id=?',(state,int(time.time()),did))


def cancel(jid):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT * FROM moments_jobs WHERE id=?',(jid,)).fetchone()
        if not r or r['state']!='queued':raise m.Conflict('仅等待中的任务可以取消')
        c.execute("UPDATE moments_jobs SET state='cancelled',message='已取消',updated=? WHERE id=?",(time.time(),jid))
        did=json.loads(r['payload']).get('draft_id')
        if did:c.execute("UPDATE drafts SET status='cancelled' WHERE id=?",(did,))
    from core.moments_reflection import forget
    forget(jid)
    return dict(id=jid,state='cancelled')


# Only jobs that provably never reached the submit stage may be re-queued; an
# initiated/uncertain/confirmed job could already be live on WeChat and must
# never be replayed (see module docstring).
RETRYABLE = ('failed', 'cancelled', 'skipped')


def retry(jid):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT * FROM moments_jobs WHERE id=?',(jid,)).fetchone()
        if not r:raise ValueError('任务不存在')
        if r['state'] not in RETRYABLE:raise m.Conflict('仅失败、已取消或已跳过的任务可以重试')
        p=json.loads(r['payload']);p.pop('retry_at',None);p.pop('attempts',None)
        # A human clicking 重试 is an explicit send-now, like a manual draft submit,
        # so it bypasses the automatic-only policy guards (staleness window, quiet
        # hours, daily limits) — see policy(). The initiated-replay ban still holds.
        p['forced']=True
        # Re-pin to the current account so process_one (which only runs jobs for
        # the active session) will actually pick this up now.
        c.execute("UPDATE moments_jobs SET state='queued',message='已重新排队，等待处理',payload=?,session=?,updated=? WHERE id=?",
                  (m._json(p),m._json(sessions.check()),time.time(),jid))
        did=p.get('draft_id')
        if did:c.execute("UPDATE drafts SET status='queued',updated=? WHERE id=?",(int(time.time()),did))
    wake.set()
    return dict(id=jid,state='queued')


def recover():
    """Account switches or server restart invalidate in-flight work, not receipts."""
    token=m._json(sessions.check())
    with db() as c:
        stale=c.execute("SELECT id,state FROM moments_jobs WHERE session<>? AND state IN ('queued','preparing','initiated')",(token,)).fetchall()
    for r in stale:
        finish(r['id'],'uncertain' if r['state']=='initiated' else 'cancelled',
               '服务重启或账号变化；可能已经提交，请核对微信，系统不会自动重发' if r['state']=='initiated' else '服务重启或账号变化，旧任务已取消')


def refresh():
    from core import docker_wx
    from core.moments_native import Native
    if docker_wx.priority_pending() or not docker_wx.UI_LOCK.acquire(blocking=False):
        raise m.Conflict('聊天正在操作微信，稍后再刷新朋友圈')
    n=Native()
    try:n.refresh()
    finally:n.cleanup();docker_wx.UI_LOCK.release()
    return m.sync()


def _target_intact(p, item):
    """Whether a forced (human-retried) comment job may proceed despite a feed
    digest change. Unrelated thread growth (new comments, incl. our own just-sent
    replies) shifts the digest but must not block an explicit retry. We only need
    the thing we are about to reply to still be there: the target comment for a
    reply (matched by id — a deletion is a real reason to re-check; text identity
    is re-verified downstream by prepare_comment/copy_at), or the post itself for
    a top-level comment. Automatic (non-forced) jobs keep the strict guard.
    """
    if not p.get('forced'):
        return False
    rid=p.get('reply_id')
    if not rid:
        return True  # commenting on the post; digest drift is just added comments
    return sum(1 for c in item['comments'] if c['id']==rid)==1


def policy(job, value, now):
    if job['origin']=='manual' or job['payload'].get('forced'):return ''
    kind=job['kind'];p=job['payload']
    flag=('chat_reflection' if job['origin']=='reflection' else
          {'comment':'auto_comment','like':'auto_like','chat':'auto_chat_after_like'}.get(kind,'auto_publish'))
    if not value[flag]:return '自动开关已关闭'
    if value['revision']!=p['settings_revision']:return '设置已变化，旧任务已取消'
    # 深夜无视免打扰:开了 late_night_override,点赞与主动私聊在免打扰时段照常;评论/发布仍受限。
    if m.quiet_now(value,now) and not (value['late_night_override'] and kind in ('like','chat')):
        return '当前为免打扰时段'
    if now-job['created']>1800:return '已错过本次执行窗口，不补发过期内容'
    start=datetime.fromtimestamp(now,m.CHINA).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    with db() as c:
        count=c.execute("SELECT count(*) FROM moments_jobs WHERE kind=? AND initiated>=? AND state IN ('initiated','confirmed','uncertain')",(kind,start)).fetchone()[0]
        last=c.execute('SELECT MAX(initiated) FROM moments_jobs').fetchone()[0] or 0
    limit=value[{'comment':'daily_comment_limit','like':'daily_like_limit','chat':'daily_chat_limit'}.get(kind,'daily_publish_limit')]
    if limit>0 and count>=limit:return '今日数量已达上限'
    if value['min_interval_minutes']>0 and now-last<value['min_interval_minutes']*60:return '距离上次互动过近'
    return ''


def schedule(now):
    value=m.settings();token=sessions.check()
    # 深夜无视免打扰:开了 late_night_override 时,免打扰时段仍放行点赞(→触发主动私聊),
    # 但发布/评论扫描仍按免打扰跳过(下方各加 not quiet 判断)。未开则夜间整体不动。
    quiet=m.quiet_now(value,now)
    if quiet and not value['late_night_override']:return
    with db() as c:
        if c.execute("SELECT 1 FROM moments_jobs WHERE state IN ('queued','preparing','initiated')").fetchone():return
        if value['auto_publish'] and not quiet:
            # Publish on a rolling interval (default every 2–3h) rather than a fixed
            # daily time; quiet hours are already excluded above. The gap is the
            # configured interval plus up to an hour of jitter derived from the last
            # attempt, so the cadence feels natural instead of clockwork. Pacing keys
            # off the last automatic publish attempt (created), so a failed send still
            # waits a full interval before retrying instead of hammering every tick.
            last=c.execute("SELECT MAX(created) FROM moments_jobs WHERE kind='publish' AND origin='automatic'").fetchone()[0] or 0
            gap=value['publish_interval_minutes']*60+int(last)%3600
            if now-last>=gap:
                insert(c,'interval:'+str(int(last)),'publish','automatic',
                       dict(settings_revision=value['revision'],moods=value['moods'],assets=[]),now)
        # 自动评论与自动点赞是两个独立开关,分别扫描;每 tick 只入队一条(单飞闸在上)。
        known=None
        def known_contacts():
            nonlocal known
            if known is None:
                from core import contacts
                known={v['username'] for v in contacts.list_contacts() if not v['username'].endswith('@chatroom')}
            return known
        if value['auto_comment'] and not quiet:
            for r in c.execute('SELECT payload FROM feed ORDER BY created DESC LIMIT 100').fetchall():
                item=json.loads(r[0]);own=item['author']==token['account']
                targets=[(q['id'],q['author'],q['created']) for q in item['comments'] if q['id'] and q['author']!=token['account']] if own else [('',item['author'],item['created'])]
                for reply,who,created in targets:
                    if not value['comment_since']<created<=now or now-created>86400:continue
                    if value['friend_allowlist'] and who not in value['friend_allowlist']:continue
                    if who not in known_contacts():continue
                    # Already answered by the account (including manual WeChat activity).
                    if any(q['author']==token['account'] and (not reply or q['reply_id']==reply) for q in item['comments']):continue
                    key='event:'+item['id']+':'+reply
                    prior=c.execute('SELECT id,state,payload,updated FROM moments_jobs WHERE dedup=?',(key,)).fetchone()
                    if prior is None:
                        insert(c,key,'comment','automatic',dict(feed_id=item['id'],reply_id=reply,settings_revision=value['revision'],assets=[]),now)
                        return
                    pp=json.loads(prior['payload'])
                    # In-flight, already sent, confirmed, or a deliberate "not worth replying"
                    # decision: leave it alone (never re-reply; line-203 also guards real sends).
                    if prior['state'] not in RETRYABLE or pp.get('decided_skip'):continue
                    attempts=pp.get('auto_attempts',1)
                    if attempts>=MAX_AUTO_ATTEMPTS or now-prior['updated']<AUTO_RETRY_COOLDOWN:continue
                    # Transient failure/throttle/expiry: reset the row to queued for another try.
                    # Drop the stale generated text + feed-digest snapshot so process_one
                    # regenerates against the CURRENT thread; otherwise a since-grown digest
                    # trips the '动态已变化' guard on every retry until attempts run out.
                    pp.update(auto_attempts=attempts+1,settings_revision=value['revision'])
                    for k in ('retry_at','text','snapshot','image_prompt','emotion'):pp.pop(k,None)
                    # Re-pin to the current (live) session so recover() doesn't cancel the
                    # requeued job — schedule() runs under sessions.bind(), so this is the
                    # active account/generation.
                    c.execute("UPDATE moments_jobs SET state='queued',origin='automatic',created=?,updated=?,initiated=0,session=?,payload=?,message='等待重试' WHERE id=?",
                              (now,now,m._json(sessions.check()),m._json(pp),prior['id']))
                    return
        if value['auto_like']:
            # 范围内全部点赞:对好友(白名单)新动态本身点赞,不接大模型判断。只点别人的,不点自己的。
            for r in c.execute('SELECT payload FROM feed ORDER BY created DESC LIMIT 100').fetchall():
                item=json.loads(r[0])
                if item['author']==token['account']:continue  # 只给别人的动态点赞
                who=item['author'];created=item['created']
                if not value['like_since']<created<=now or now-created>86400:continue
                if value['friend_allowlist'] and who not in value['friend_allowlist']:continue
                if who not in known_contacts():continue
                # 本账号(含手动操作)已点过赞:不重复点(微信里再点一次=取消赞)。
                if any(q['author']==token['account'] for q in item['likes']):continue
                key='like:'+item['id']
                prior=c.execute('SELECT id,state,payload,updated FROM moments_jobs WHERE dedup=?',(key,)).fetchone()
                if prior is None:
                    insert(c,key,'like','automatic',dict(feed_id=item['id'],settings_revision=value['revision'],assets=[]),now)
                    return
                pp=json.loads(prior['payload'])
                if prior['state'] not in RETRYABLE:continue
                attempts=pp.get('auto_attempts',1)
                if attempts>=MAX_AUTO_ATTEMPTS or now-prior['updated']<AUTO_RETRY_COOLDOWN:continue
                pp.update(auto_attempts=attempts+1,settings_revision=value['revision']);pp.pop('retry_at',None)
                c.execute("UPDATE moments_jobs SET state='queued',origin='automatic',created=?,updated=?,initiated=0,session=?,payload=?,message='等待重试' WHERE id=?",
                          (now,now,m._json(sessions.check()),m._json(pp),prior['id']))
                return


def receipt(job, before, since):
    token=sessions.check();p=job['payload']
    if job['kind']=='publish':
        matches=[i for i in m.catalog(100,author=token['account'])['items'] if i['id'] not in before and i['created']>=since-5 and i['text']==p['text'] and len(i['media'])==len(p.get('assets',[]))]
        return matches[0]['id'] if len(matches)==1 else ''
    if job['kind']=='like':
        # 回执极可靠:本账号在 before 之后新出现在点赞列表里即确认。
        item=m.detail(p['feed_id'])
        return p['feed_id'] if any(q['author']==token['account'] for q in item['likes']) and token['account'] not in before else ''
    item=m.detail(p['feed_id'])
    matches=[q for q in item['comments'] if q['id'] not in before and q['id'] and q['author']==token['account'] and q['text']==p['text'] and q['created']>=since-5 and (q['reply_id']==p.get('reply_id','') or (not p.get('reply_id') and not q['reply_to']))]
    return matches[0]['id'] if len(matches)==1 else ''


def make_publish_image(prompt):
    """Text-to-image for an auto post; returns an asset id or None on any failure.

    Never raises: a picture is a nice-to-have, so image trouble degrades the post
    to text-only rather than losing it. Uses the same gen_image path as chat.
    """
    import io
    from core import llm
    value=m.settings()
    if not value.get('publish_images') or not m.capabilities().get('image_publish'):
        return None
    if not isinstance(prompt,str) or not prompt.strip():
        return None
    try:
        data=llm.gen_image(prompt.strip(),cfg=llm.load_cfg())
        if not data:return None
        return m.upload(io.BytesIO(data))['id']
    except Exception:
        return None


# Chat traffic always preempts Moments. An automatic job that keeps losing the
# WeChat window would otherwise re-locate (and visibly re-copy) every tick forever,
# so each yield is counted and spaced out, and we give up after MAX_CHAT_YIELD.
# 聊天忙时朋友圈拿不到窗口,等太久目标动态早被刷走再定位也是白费——快速放弃(3×90s≈4.5分钟),
# 且这是尽力而为的后台自动互动,放弃按"已跳过"而非"失败"处理,避免 UI 里刷屏红色失败。
MAX_CHAT_YIELD = 3
YIELD_BACKOFF = 90


def yield_to_chat(job, message):
    p=job['payload'];p['attempts']=p.get('attempts',0)+1
    with db() as c:
        if p['attempts']>=MAX_CHAT_YIELD:
            c.execute("UPDATE moments_jobs SET state='skipped',updated=?,payload=?,message=? WHERE id=?",
                      (time.time(),m._json(p),'聊天一直在用微信,这条自动互动已跳过(目标可能已刷新)',job['id']))
        else:
            p['retry_at']=time.time()+YIELD_BACKOFF
            c.execute("UPDATE moments_jobs SET state='queued',updated=?,payload=?,message=? WHERE id=?",
                      (time.time(),m._json(p),message,job['id']))


def _enqueue_chat_after_like(feed_id):
    """点赞确认后:若开了「点赞后主动私聊」,入队一条 chat 任务(dedup=chat:<feed_id>,一动态一私聊)。

    尽力而为——任何异常都不影响"点赞已确认"这个既成结果,只是这次不追私聊。
    """
    try:
        value=m.settings()
        if not value['auto_chat_after_like']:return
        item=m.detail(feed_id)
        if item['author']==sessions.check()['account']:return  # 不给自己私聊
        with db() as c:
            insert(c,'chat:'+feed_id,'chat','automatic',
                   dict(feed_id=feed_id,author=item['author'],name=item.get('name',''),
                        text=item.get('text',''),created=item.get('created',0),
                        settings_revision=value['revision'],assets=[]),time.time())
        wake.set()
    except Exception:
        pass


def _process_chat(job):
    """点赞后主动私聊:在 UI_LOCK 之外走 sender.send_text(它自带 SEND_LOCK),避免死锁。

    情绪感知文案由 moments_ai.generate_outreach 生成(伤感→关心可 2-3 条、趣事→共鸣、
    广告/无实质→跳过);回执用 send_text 返回状态,不走朋友圈 feed diff。
    """
    from core import sender, send_ledger, conversation_state, moments_ai, contacts
    p=job['payload'];chat=p['author'];account=sessions.check()['account']
    disp=next((c['name'] for c in contacts.list_contacts() if c['username']==chat),'') or p.get('name') or chat
    if sender.preflight(chat):
        finish(job['id'],'skipped','对方会话暂不可用,未主动私聊');return
    gate=conversation_state.ticket(chat,[])
    if gate is None or not conversation_state.allowed(gate):
        finish(job['id'],'skipped','对方已暂停主动打扰,未私聊');return
    try:
        out=moments_ai.generate_outreach(chat,p.get('text',''),p.get('name',''))
    except (m.Conflict,m.Unavailable) as exc:
        finish(job['id'],'skipped',str(exc));return
    if out.get('skip'):
        finish(job['id'],'skipped',out.get('reason','这条动态不值得主动打扰'));return
    parts=out['messages'][:3]
    now=time.time()
    with db() as c:
        c.execute("UPDATE moments_jobs SET state='initiated',initiated=?,updated=?,message='正在主动私聊' WHERE id=?",(now,now,job['id']))
    sent=0;last=None
    for idx,part in enumerate(parts,1):
        if not conversation_state.allowed(gate):break
        last=sender.send_text(disp,part,chat_username=chat,
                              job_id=send_ledger.stable_id(account,'moment_outreach',chat,p['feed_id'],idx),
                              proactive_ticket=gate)
        if (last or {}).get('status') not in ('confirmed','submitted'):break
        sent+=1
    if sent==len(parts):
        finish(job['id'],'confirmed','已主动私聊 %d 条'%sent,p['feed_id'])
    elif sent==0 and last is None:
        finish(job['id'],'skipped','对方已暂停主动打扰,未私聊')
    else:
        finish(job['id'],'uncertain','主动私聊仅送达 %d/%d 条,不自动重发'%(sent,len(parts)))


def process_one():
    from core import docker_wx, moments_ai
    from core.moments_native import Native
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute("SELECT * FROM moments_jobs WHERE state='queued' ORDER BY CASE origin WHEN 'manual' THEN 0 ELSE 1 END,created LIMIT 1").fetchone()
        if not r:return
        if json.loads(r['session'])!=sessions.check():return
        job=row(r)
        if job['payload'].get('retry_at',0)>time.time():return  # backing off after a chat-priority yield
        c.execute("UPDATE moments_jobs SET state='preparing',updated=?,message='正在准备' WHERE id=?",(time.time(),job['id']))
    initiated=False;n=None
    try:
        why=policy(job,m.settings(),time.time())
        if why:finish(job['id'],'skipped',why);return
        p=job['payload']
        # 主动私聊完全走 sender.send_text(自带 SEND_LOCK),绝不进下面的朋友圈原生 UI_LOCK 块,
        # 否则 UI_LOCK==SEND_LOCK 会死锁。回执用 send_text 状态,不走 feed diff。
        if job['kind']=='chat':
            _process_chat(job);return
        if job['origin']!='manual' and job['kind']!='like' and not p.get('text'):
            if moments_ai._generating.locked():
                with db() as c:c.execute("UPDATE moments_jobs SET state='queued',message='等待当前 AI 文案生成完成' WHERE id=?",(job['id'],))
                return
            if job['origin']=='reflection':
                from core import moments_reflection
                result=moments_reflection.generate(job['id'])
                if result.get('skip'):finish(job['id'],'skipped',result['reason']);return
                p.update(text=result['text'],emotion=result['emotion'],image_prompt=result.get('image_prompt',''))
            elif job['kind']=='publish':
                result=moments_ai.generate_post(p['moods'])
                p.update(text=result['text'],image_prompt=result.get('image_prompt',''))
            else:
                item=m.detail(p['feed_id'])
                result=moments_ai.generate(dict(feed_id=item['id'],feed_digest=item['digest'],reply_id=p['reply_id']),decide=True)
                if result.get('skip'):
                    p['decided_skip']=True  # a real decision not to reply — don't auto-retry this comment
                    with db() as c:c.execute('UPDATE moments_jobs SET payload=? WHERE id=?',(m._json(p),job['id']))
                    finish(job['id'],'skipped',result['reason']);return
                p.update(text=result['text'],reply_id=result['reply_id'],snapshot=dict(digest=result['feed_digest']))
            with db() as c:c.execute('UPDATE moments_jobs SET payload=? WHERE id=?',(m._json(p),job['id']))
        if job['kind']=='publish' and job['origin']!='manual' and p.get('image_prompt') and not p.get('assets'):
            aid=make_publish_image(p['image_prompt'])
            if aid:
                p['assets']=[aid]
                with db() as c:c.execute('UPDATE moments_jobs SET payload=? WHERE id=?',(m._json(p),job['id']))
        if docker_wx.priority_pending() or not docker_wx.UI_LOCK.acquire(timeout=2):
            yield_to_chat(job,'等待聊天操作完成，稍后重试')
            return
        try:
            n=Native()
            if job['kind']=='comment':
                item=m.detail(p['feed_id'])
                if item['digest']!=p['snapshot']['digest'] and not _target_intact(p,item):
                    raise m.Conflict('动态已变化，未发送；请重新核对')
                before={q['id'] for q in item['comments']};n.prepare_comment(item,p['reply_id'],p['text'])
            elif job['kind']=='like':
                item=m.detail(p['feed_id'])
                before={q['author'] for q in item['likes']};n.prepare_like(item)
            else:
                before={i['id'] for i in m.catalog(100,author=sessions.check()['account'])['items']}
                n.prepare_publish(p['text'],[m.asset(a) for a in p.get('assets',[])])
            why=policy(job,m.settings(),time.time())
            if why:finish(job['id'],'skipped',why);return
            sessions.check();since=time.time()
            with db() as c:
                c.execute("UPDATE moments_jobs SET state='initiated',initiated=?,updated=?,message='已进入提交阶段，正在核对微信记录' WHERE id=?",(since,since,job['id']))
            initiated=True;n.submit();time.sleep(1)
        finally:
            if n:n.cleanup()
            docker_wx.UI_LOCK.release()
        for attempt in range(12):
            try:
                m.sync();found=receipt(job,before,since)
                if found:
                    finish(job['id'],'confirmed','已在微信记录中确认发送',found)
                    if job['kind']=='like':_enqueue_chat_after_like(p['feed_id'])
                    return
            except (m.Conflict,m.Unavailable):pass
            time.sleep(1)
        finish(job['id'],'uncertain','已操作提交，但未查到唯一回执；请查看微信，系统不会自动重发')
    except sessions.StaleAccount:
        # Old account ledger stays pinned; next visit recovers it as uncertain.
        return
    except Exception as exc:
        raw=str(exc)
        # Chat traffic has priority over Moments. Keep the automatic job queued
        # so a temporary navigation timeout does not permanently lose a reply.
        if not initiated and ('聊天优先' in raw or '朋友圈定位超时' in raw):
            yield_to_chat(job,'聊天操作占用微信，稍后重试')
            return
        message=raw if isinstance(exc,(m.Conflict,m.Unavailable,ValueError)) else '客户端或模型操作异常'
        if initiated:
            finish(job['id'],'uncertain',message+'；可能已发送，不会自动重发')
        else:
            # 尽力而为的自动互动定位失败(找不到目标/评论未完整/编辑框未识别等)按"已跳过"降噪;
            # 手动操作是用户明确发起的,仍按"失败"如实呈现,便于用户知情重试。
            soft = job['origin'] != 'manual'
            finish(job['id'],'skipped' if soft else 'failed',message+'；未进入提交阶段')


def tick():
    if not _tick_lock.acquire(blocking=False):return
    try:
        with sessions.bind():
            with db() as c:
                m._set(c,'last_worker_tick',time.time());m._set(c,'worker_error',None)
            from core.moments_reflection import prune
            prune();recover();process_one()
            value=m.settings();now=time.time()
            if value['sync_enabled'] or value['auto_comment'] or value['auto_publish'] or value['auto_like']:
                with db() as c:
                    last=m._get(c,'native_refresh_attempt',0)
                    due=now-last>=value['sync_interval_minutes']*60
                    if due:m._set(c,'native_refresh_attempt',now)
                if due:
                    try:
                        refresh()
                        with db() as c:m._set(c,'sync_error',None)
                    except Exception:
                        with db() as c:m._set(c,'sync_error',dict(at=int(now),message='朋友圈刷新未完成，保留原缓存；稍后按间隔重试'))
            schedule(now);process_one()
    finally:_tick_lock.release()
