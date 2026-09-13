"""Offline job/policy regressions: never invoke the real client or model."""
import json
import threading
import time
import unittest
from datetime import datetime
from unittest.mock import patch
import test_moments as fixtures
from test_moments import FID, xml
from core import moments as m, moments_jobs as j, account_session as sessions
from core.ui_lock import UILock

class Jobs(unittest.TestCase):
    setUp=fixtures.Moments.setUp
    seed=fixtures.Moments.seed

    def ready(self):
        p=patch('core.moments.capabilities',return_value=dict(send=True));p.start();self.addCleanup(p.stop)

    def test_submission_idempotent_and_cancelled_draft_is_not_editable(self):
        self.ready();d=m.save_draft(dict(kind='publish',text='一个小小的想法'))
        first=j.enqueue_draft(d['id'],1);second=j.enqueue_draft(d['id'],1)
        self.assertEqual(first['id'],second['id']);self.assertEqual(len(j.listing()),1)
        self.assertEqual(m.drafts()[0]['status'],'queued')
        with self.assertRaises(m.Conflict):m.save_draft(dict(kind='publish',text='变化',id=d['id'],revision=1))
        j.cancel(first['id']);self.assertEqual(m.drafts()[0]['status'],'cancelled')
        self.assertEqual(j.enqueue_draft(d['id'],1)['state'],'cancelled')

    def test_restart_cancels_waiting_and_never_replays_initiated(self):
        with j.db() as c:
            a=j.insert(c,'a','publish','manual',{},time.time())
            b=j.insert(c,'b','publish','manual',{},time.time(),state='initiated')
        sessions._current=None;sessions.observe();j.recover()
        states={x['id']:x['state'] for x in j.listing()}
        self.assertEqual(states[a['id']],'cancelled');self.assertEqual(states[b['id']],'uncertain')
        j.recover();self.assertEqual(len(j.listing()),2)

    def test_retry_requeues_only_never_initiated_jobs(self):
        with j.db() as c:
            failed=j.insert(c,'f','comment','manual',dict(text='x',attempts=6,retry_at=time.time()+99),time.time(),state='failed')
            initiated=j.insert(c,'i','publish','manual',{},time.time(),state='initiated')
            uncertain=j.insert(c,'u','publish','manual',{},time.time(),state='uncertain')
        got=j.retry(failed['id']);self.assertEqual(got['state'],'queued')
        again={x['id']:x for x in j.listing()}
        self.assertEqual(again[failed['id']]['state'],'queued')
        # attempts / retry_at cleared so the job runs promptly with a fresh budget.
        self.assertNotIn('retry_at',again[failed['id']]['payload'])
        self.assertNotIn('attempts',again[failed['id']]['payload'])
        # A human retry is forced: it bypasses the automatic-only staleness/quiet/
        # limit guards so a long-expired auto job still runs instead of re-skipping.
        job=again[failed['id']];self.assertTrue(job['payload']['forced'])
        job['created']=time.time()-999999
        self.assertEqual(j.policy(job,m.settings(),time.time()),'')
        # An initiated (possibly-live) or uncertain send is never replayable.
        with self.assertRaises(m.Conflict):j.retry(initiated['id'])
        with self.assertRaises(m.Conflict):j.retry(uncertain['id'])
        with self.assertRaises(ValueError):j.retry('nope')

    def test_forced_retry_tolerates_thread_growth_but_needs_target(self):
        item=dict(id='f',digest='new',comments=[dict(id='227',text='原文')])
        # Non-forced: any digest change still blocks.
        self.assertFalse(j._target_intact(dict(reply_id='227',snapshot=dict(digest='old')),item))
        # Forced reply whose target comment survives the growth: allowed.
        self.assertTrue(j._target_intact(dict(forced=True,reply_id='227'),item))
        # Forced reply whose target comment was deleted: still blocked.
        self.assertFalse(j._target_intact(dict(forced=True,reply_id='999'),item))
        # Forced top-level comment on the post: allowed despite added comments.
        self.assertTrue(j._target_intact(dict(forced=True,reply_id=''),item))

    def test_retry_reopens_cancelled_draft(self):
        self.ready();d=m.save_draft(dict(kind='publish',text='想法'))
        task=j.enqueue_draft(d['id'],1);j.cancel(task['id'])
        self.assertEqual(m.drafts()[0]['status'],'cancelled')
        j.retry(task['id'])
        self.assertEqual(m.drafts()[0]['status'],'queued')

    def test_interval_publish_paces_by_gap_and_skips_quiet(self):
        self.ready();now=datetime(2026,9,12,12,30,tzinfo=m.CHINA).timestamp()
        with patch('time.time',return_value=now-60):
            m.save_settings(dict(auto_publish=True,publish_interval_minutes=120),0)
        # Enabling publishes on the next tick; the in-flight guard blocks a duplicate.
        j.schedule(now);j.schedule(now+1);self.assertEqual(len(j.listing()),1)
        task=j.listing()[0];self.assertEqual((task['origin'],task['kind']),('automatic','publish'))
        j.finish(task['id'],'confirmed','ok')
        # Within the interval (default 2h + up to 1h jitter): no new post yet.
        j.schedule(now+600);self.assertEqual(len(j.listing()),1)
        # Past the maximum gap (3h): the next post is scheduled.
        j.schedule(now+3*3600+1);self.assertEqual(len(j.listing()),2)
        # Quiet hours suppress scheduling entirely (returns before the interval check).
        night=datetime(2026,9,13,23,0,tzinfo=m.CHINA).timestamp()
        j.schedule(night);self.assertEqual(len(j.listing()),2)

    def test_activation_watermark_does_not_reply_to_old_posts(self):
        self.ready();self.seed()
        now=1789207400
        with patch('time.time',return_value=now):m.save_settings(dict(auto_comment=True),0)
        with patch('core.contacts.list_contacts',return_value=[dict(username='friend-A')]):
            j.schedule(now);self.assertEqual(j.listing(),[])
            m.ingest([(-1,'friend-A',xml().replace('1789207200',str(now+1)))])
            j.schedule(now+2);j.schedule(now+3)
        self.assertEqual(len(j.listing()),1)

    def _seed_comment_job(self, state, extra=None):
        """Ingest a friend post, schedule its comment job, then drive it to `state`."""
        self.ready();self.seed()
        now=1789207400
        with patch('time.time',return_value=now):m.save_settings(dict(auto_comment=True,min_interval_minutes=0),0)
        with patch('core.contacts.list_contacts',return_value=[dict(username='friend-A')]):
            m.ingest([(-1,'friend-A',xml().replace('1789207200',str(now+1)))])
            with patch('time.time',return_value=now+2):j.schedule(now+2)
        jid=j.listing()[0]['id']
        with patch('time.time',return_value=now+2):j.finish(jid,state,'transient')
        if extra:
            with j.db() as c:
                p=json.loads(c.execute('SELECT payload FROM moments_jobs WHERE id=?',(jid,)).fetchone()[0])
                p.update(extra);c.execute('UPDATE moments_jobs SET payload=? WHERE id=?',(m._json(p),jid))
        return now,jid

    def test_transient_comment_failure_retries_only_after_cooldown(self):
        # Seed stale generated text + an old digest snapshot to prove they're dropped.
        now,jid=self._seed_comment_job('failed',dict(text='旧文案',snapshot=dict(digest='old')))
        with patch('core.contacts.list_contacts',return_value=[dict(username='friend-A')]):
            # Before the cooldown elapses the buried comment is left alone.
            with patch('time.time',return_value=now+3):j.schedule(now+3)
            self.assertEqual(j.listing()[0]['state'],'failed')
            # After the cooldown the same row is reset to queued for another attempt.
            later=now+2+j.AUTO_RETRY_COOLDOWN+1
            with patch('time.time',return_value=later):j.schedule(later)
        row=j.listing()[0]
        self.assertEqual(row['state'],'queued')
        self.assertEqual(row['payload']['auto_attempts'],2)
        # Stale text/snapshot are cleared so process_one regenerates against the live thread.
        self.assertNotIn('text',row['payload'])
        self.assertNotIn('snapshot',row['payload'])

    def test_decided_skip_comment_is_never_auto_retried(self):
        now,jid=self._seed_comment_job('skipped',dict(decided_skip=True))
        with patch('core.contacts.list_contacts',return_value=[dict(username='friend-A')]):
            later=now+2+j.AUTO_RETRY_COOLDOWN+10
            with patch('time.time',return_value=later):j.schedule(later)
        self.assertEqual(j.listing()[0]['state'],'skipped')

    def test_comment_retries_are_capped(self):
        now,jid=self._seed_comment_job('failed',dict(auto_attempts=j.MAX_AUTO_ATTEMPTS))
        with patch('core.contacts.list_contacts',return_value=[dict(username='friend-A')]):
            later=now+2+j.AUTO_RETRY_COOLDOWN+10
            with patch('time.time',return_value=later):j.schedule(later)
        self.assertEqual(j.listing()[0]['state'],'failed')

    def test_policy_rechecks_switch_revision_quiet_and_counts_uncertainty(self):
        now=datetime(2026,9,12,12,30,tzinfo=m.CHINA).timestamp()
        value=dict(m.DEFAULTS,auto_publish=True,revision=2,daily_publish_limit=1)
        job=dict(kind='publish',origin='automatic',created=now,payload=dict(settings_revision=2))
        self.assertEqual(j.policy(job,value,now),'')
        self.assertTrue(j.policy(job,dict(value,auto_publish=False),now))
        self.assertTrue(j.policy(job,dict(value,revision=3),now))
        self.assertTrue(j.policy(job,value,now+40000))
        with j.db() as c:
            r=j.insert(c,'uncertain','publish','automatic',{},now,state='uncertain')
            c.execute('UPDATE moments_jobs SET initiated=? WHERE id=?',(now-500,r['id']))
        self.assertIn('上限',j.policy(job,value,now))
        self.assertEqual(j.policy(dict(job,origin='manual'),value,now),'')

    def test_receipt_requires_new_self_comment_and_exact_reference(self):
        item=self.seed();since=1789207300
        job=dict(kind='comment',payload=dict(feed_id=FID,reply_id='99999999999999999',text='谢谢'))
        base=dict(id='77',author=self.account,text='谢谢',created=since,reply_id='wrong',reply_to='friend-B')
        with patch('core.moments.detail',return_value=dict(item,comments=[base])):
            self.assertEqual(j.receipt(job,set(),since),'')
        with patch('core.moments.detail',return_value=dict(item,comments=[dict(base,reply_id='99999999999999999')])):
            self.assertEqual(j.receipt(job,set(),since),'77');self.assertEqual(j.receipt(job,{'77'},since),'')
        with patch('core.moments.detail',return_value=dict(item,comments=[dict(base,author='someone',reply_id='99999999999999999')])):
            self.assertEqual(j.receipt(job,set(),since),'')

    def test_failure_before_submit_and_after_submit_are_distinct(self):
        self.ready()
        from core import docker_wx
        class Fake:
            fail=False
            def prepare_publish(self,*a):
                if self.fail:raise m.Unavailable('prepare failed')
            def cleanup(self):pass
            def submit(self):raise m.Unavailable('click result unknown')
        with patch.object(docker_wx,'UI_LOCK',threading.RLock()),patch.object(docker_wx,'priority_pending',return_value=False),patch('core.moments_native.Native',Fake):
            for fail in [True,False]:
                Fake.fail=fail;d=m.save_draft(dict(kind='publish',text='两类异常要分清'))
                task=j.enqueue_draft(d['id'],1);j.process_one()
                found=next(x for x in j.listing() if x['id']==task['id'])
                self.assertEqual(found['state'],'failed' if fail else 'uncertain')
            j.process_one();self.assertEqual(len(j.listing()),2)

    def test_model_decision_invalid_json_is_not_sent(self):
        from core import moments_ai
        item=self.seed()
        with patch('core.contacts.list_contacts',return_value=[]),patch('core.llm.load_cfg',return_value={}),patch('core.llm.chat',return_value='bad json'):
            with self.assertRaises(m.Unavailable):moments_ai.generate(dict(feed_id=FID,feed_digest=item['digest']),decide=True)

    def test_images_and_new_post_receipt_require_expected_count(self):
        from PIL import Image
        import io
        self.ready();buf=io.BytesIO();Image.new('RGB',(20,30),'red').save(buf,format='PNG');buf.seek(0)
        asset=m.upload(buf)['id'];d=m.save_draft(dict(kind='publish',text='配图分享',assets=[asset]))
        task=j.enqueue_draft(d['id'],1)
        self.assertEqual(task['payload']['assets'],[asset])
        post=dict(id='123',author=self.account,created=100,text='配图分享',media=[])
        with patch('core.moments.catalog',return_value=dict(items=[post])):
            self.assertEqual(j.receipt(task,set(),100),'')
        with patch('core.moments.catalog',return_value=dict(items=[dict(post,media=[{}])])):
            self.assertEqual(j.receipt(task,set(),100),'123')

    def test_new_draft_does_not_bypass_recent_content_dedup(self):
        self.ready();d=m.save_draft(dict(kind='publish',text='避免重复发送'))
        task=j.enqueue_draft(d['id'],1);j.finish(task['id'],'uncertain','unknown')
        other=m.save_draft(dict(kind='publish',text='避免重复发送'))
        with self.assertRaises(m.Conflict):j.enqueue_draft(other['id'],1)

    def test_publish_time_cannot_fall_in_quiet_period(self):
        self.ready()
        with self.assertRaises(m.Conflict):m.save_settings(dict(auto_publish=True,publish_time='23:00'),0)
        self.assertFalse(m.settings()['auto_publish'])

    def test_moving_quiet_over_unchanged_publish_time_is_allowed(self):
        # Regression: the daily publish_time (default 12:30) stays put while the user
        # shifts 免打扰 to a daytime window that covers it. This must save, not bounce —
        # otherwise the whole settings patch is rejected and every field "resets".
        self.ready()
        s=m.save_settings(dict(auto_publish=True),0)
        s=m.save_settings(dict(quiet_start='09:00',quiet_end='18:00'),s['revision'])
        self.assertEqual((s['quiet_start'],s['quiet_end'],s['publish_time']),('09:00','18:00','12:30'))

    def test_button_template_rejects_disabled_or_wrong_button(self):
        from PIL import Image
        from pathlib import Path
        from core.moments_native import Native,NativeError
        for kind,label in [('publish','发表'),('comment','发送')]:
            im=Image.open(Path(__file__).resolve().parents[1]/'core/moments_templates'/f'{kind}.png').convert('RGB')
            screen=Image.new('RGB',(200,80),'white');screen.paste(im,(20,20))
            n=Native();n.shot=lambda box:screen.crop(box)
            hit=n.green_button(label,(0,0,200,80));self.assertGreater(hit['x'],20)
            with self.assertRaises(NativeError):n.green_button('发送' if label=='发表' else '发表',(0,0,200,80))
            disabled=screen.convert('L').convert('RGB');n.shot=lambda box:disabled.crop(box)
            with self.assertRaises(NativeError):n.green_button(label,(0,0,200,80))

    def test_ui_lock_reentrant_and_excludes_other_instances(self):
        path=self.temp.name+'/ui.lock';a=UILock(path);b=UILock(path)
        self.assertTrue(a.acquire(False));self.assertTrue(a.acquire(False))
        self.assertFalse(b.acquire(False));a.release();self.assertFalse(b.acquire(False))
        a.release();self.assertTrue(b.acquire(False));b.release()

class Likes(unittest.TestCase):
    """自动点赞:范围内全部点赞(不接大模型),与自动评论独立开关。"""
    setUp=fixtures.Moments.setUp
    seed=fixtures.Moments.seed

    def ready(self):
        p=patch('core.moments.capabilities',return_value=dict(send=True));p.start();self.addCleanup(p.stop)

    def _enable(self, now, **extra):
        with patch('time.time',return_value=now-1):m.save_settings(dict(auto_like=True,**extra),0)

    def test_like_scan_enqueues_for_unliked_friend_post(self):
        self.ready();now=1789207400;self._enable(now)
        with patch('core.contacts.list_contacts',return_value=[dict(username='friend-A')]):
            m.ingest([(-1,'friend-A',xml().replace('1789207200',str(now+1)))])
            j.schedule(now+2)
        rows=j.listing();self.assertEqual(len(rows),1)
        job=rows[0]
        self.assertEqual((job['kind'],job['origin'],job['dedup']),('like','automatic','like:'+FID))
        self.assertEqual(job['payload']['feed_id'],FID)
        # 再扫一次不会重复入队(dedup + 单飞闸)。
        j.schedule(now+3);self.assertEqual(len(j.listing()),1)

    def test_like_scan_skips_when_already_liked(self):
        self.ready();now=1789207400;self._enable(now)
        already=xml().replace('1789207200',str(now+1)).replace('friend-C',self.account)
        with patch('core.contacts.list_contacts',return_value=[dict(username='friend-A')]):
            m.ingest([(-1,'friend-A',already)])
            j.schedule(now+2)
        self.assertEqual(j.listing(),[])

    def test_like_scan_never_likes_own_post(self):
        self.ready();now=1789207400;self._enable(now)
        own=xml().replace('1789207200',str(now+1)).replace('friend-A',self.account)
        with patch('core.contacts.list_contacts',return_value=[dict(username=self.account)]):
            m.ingest([(-1,self.account,own)])
            j.schedule(now+2)
        self.assertEqual(j.listing(),[])

    def test_auto_comment_off_still_likes(self):
        # 独立开关:关掉自动评论,自动点赞仍应入队(验证 schedule 条件块改造)。
        self.ready();now=1789207400
        with patch('time.time',return_value=now-1):m.save_settings(dict(auto_like=True,auto_comment=False),0)
        with patch('core.contacts.list_contacts',return_value=[dict(username='friend-A')]):
            m.ingest([(-1,'friend-A',xml().replace('1789207200',str(now+1)))])
            j.schedule(now+2)
        rows=j.listing();self.assertEqual(len(rows),1);self.assertEqual(rows[0]['kind'],'like')

    def test_like_scan_respects_activation_watermark(self):
        # 刚开点赞不补赞历史旧动态(created<=like_since 的不点)。
        self.ready();now=1789207400;self._enable(now)  # like_since=now-1
        with patch('core.contacts.list_contacts',return_value=[dict(username='friend-A')]):
            m.ingest([(-1,'friend-A',xml())])  # 原始 createTime 1789207200 < 水位
            j.schedule(now+2)
        self.assertEqual(j.listing(),[])

    def test_like_policy_gates_flag_and_daily_limit(self):
        now=datetime(2026,9,12,12,30,tzinfo=m.CHINA).timestamp()
        value=dict(m.DEFAULTS,auto_like=True,revision=2,daily_like_limit=1)
        job=dict(kind='like',origin='automatic',created=now,payload=dict(settings_revision=2))
        self.assertEqual(j.policy(job,value,now),'')
        self.assertEqual(j.policy(job,dict(value,auto_like=False),now),'自动开关已关闭')
        with j.db() as c:
            r=j.insert(c,'likecap','like','automatic',{},now,state='initiated')
            c.execute('UPDATE moments_jobs SET initiated=? WHERE id=?',(now-100,r['id']))
        self.assertIn('上限',j.policy(job,value,now))

    def test_like_receipt_confirms_when_self_newly_appears(self):
        item=self.seed();since=1789207300
        job=dict(kind='like',payload=dict(feed_id=FID))
        with patch('core.moments.detail',return_value=dict(item,likes=[dict(author='friend-C')])):
            self.assertEqual(j.receipt(job,{'friend-C'},since),'')
        with patch('core.moments.detail',return_value=dict(item,likes=[dict(author='friend-C'),dict(author=self.account)])):
            self.assertEqual(j.receipt(job,{'friend-C'},since),FID)
        # 若点赞前自己已在列表里(before 含自己),不算这次新增。
        with patch('core.moments.detail',return_value=dict(item,likes=[dict(author=self.account)])):
            self.assertEqual(j.receipt(job,{self.account},since),'')

    def test_save_settings_validates_like_and_sets_watermark(self):
        self.ready()
        with patch('time.time',return_value=1000.0):s=m.save_settings(dict(auto_like=True,daily_like_limit=5),0)
        self.assertTrue(s['auto_like']);self.assertEqual(s['daily_like_limit'],5);self.assertEqual(s['like_since'],1000.0)
        with self.assertRaises(ValueError):m.save_settings(dict(daily_like_limit=99999),s['revision'])
        with self.assertRaises(ValueError):m.save_settings(dict(like_since=5),s['revision'])  # 水位不可由前端直接设置

class Chats(unittest.TestCase):
    """点赞后主动私聊:like 确认→入队 chat;情绪感知发送;深夜无视免打扰。"""
    setUp=fixtures.Moments.setUp
    seed=fixtures.Moments.seed

    def ready(self):
        p=patch('core.moments.capabilities',return_value=dict(send=True));p.start();self.addCleanup(p.stop)

    def _detail(self):
        return dict(id=FID,author='friend-A',name='朋友甲',text='今天好累好难过',created=1789207200,likes=[],comments=[])

    def test_enqueue_chat_after_like_inserts_when_enabled(self):
        self.ready()
        with patch('time.time',return_value=1000.0):m.save_settings(dict(auto_like=True,auto_chat_after_like=True),0)
        with patch('core.moments.detail',return_value=self._detail()):
            j._enqueue_chat_after_like(FID)
        rows=j.listing();self.assertEqual(len(rows),1)
        job=rows[0]
        self.assertEqual((job['kind'],job['origin'],job['dedup']),('chat','automatic','chat:'+FID))
        self.assertEqual(job['payload']['author'],'friend-A')
        self.assertEqual(job['payload']['text'],'今天好累好难过')
        # 同一条动态不重复入队(dedup)。
        with patch('core.moments.detail',return_value=self._detail()):j._enqueue_chat_after_like(FID)
        self.assertEqual(len(j.listing()),1)

    def test_enqueue_chat_after_like_noop_when_disabled(self):
        self.ready()  # auto_chat_after_like 默认 False
        with patch('core.moments.detail',return_value=self._detail()):j._enqueue_chat_after_like(FID)
        self.assertEqual(j.listing(),[])

    def test_enqueue_chat_after_like_skips_own_post(self):
        self.ready()
        with patch('time.time',return_value=1000.0):m.save_settings(dict(auto_like=True,auto_chat_after_like=True),0)
        own=dict(self._detail(),author=self.account)
        with patch('core.moments.detail',return_value=own):j._enqueue_chat_after_like(FID)
        self.assertEqual(j.listing(),[])

    def test_chat_policy_gates_flag_and_daily_limit(self):
        now=datetime(2026,9,12,12,30,tzinfo=m.CHINA).timestamp()  # 白天,非免打扰
        value=dict(m.DEFAULTS,auto_chat_after_like=True,revision=2,daily_chat_limit=1)
        job=dict(kind='chat',origin='automatic',created=now,payload=dict(settings_revision=2))
        self.assertEqual(j.policy(job,value,now),'')
        self.assertEqual(j.policy(job,dict(value,auto_chat_after_like=False),now),'自动开关已关闭')
        with j.db() as c:
            r=j.insert(c,'chatcap','chat','automatic',{},now,state='initiated')
            c.execute('UPDATE moments_jobs SET initiated=? WHERE id=?',(now-100,r['id']))
        self.assertIn('上限',j.policy(job,value,now))

    def test_chat_policy_late_night_override(self):
        night=datetime(2026,9,12,2,0,tzinfo=m.CHINA).timestamp()  # 02:00 落在默认 22:00-08:00 免打扰
        base=dict(m.DEFAULTS,auto_chat_after_like=True,auto_like=True,auto_comment=True,revision=2)
        chat=dict(kind='chat',origin='automatic',created=night,payload=dict(settings_revision=2))
        like=dict(kind='like',origin='automatic',created=night,payload=dict(settings_revision=2))
        comment=dict(kind='comment',origin='automatic',created=night,payload=dict(settings_revision=2))
        # override 开:chat/like 夜间放行,评论仍受免打扰。
        on=dict(base,late_night_override=True)
        self.assertEqual(j.policy(chat,on,night),'')
        self.assertEqual(j.policy(like,on,night),'')
        self.assertIn('免打扰',j.policy(comment,on,night))
        # override 关:chat 夜间也被免打扰拦。
        off=dict(base,late_night_override=False)
        self.assertIn('免打扰',j.policy(chat,off,night))

    def test_save_settings_validates_chat_and_sets_watermark(self):
        self.ready()
        with patch('time.time',return_value=2000.0):
            s=m.save_settings(dict(auto_chat_after_like=True,daily_chat_limit=5,late_night_override=False),0)
        self.assertTrue(s['auto_chat_after_like']);self.assertEqual(s['daily_chat_limit'],5)
        self.assertFalse(s['late_night_override']);self.assertEqual(s['chat_since'],2000.0)
        with self.assertRaises(ValueError):m.save_settings(dict(daily_chat_limit=99999),s['revision'])
        with self.assertRaises(ValueError):m.save_settings(dict(chat_since=5),s['revision'])  # 水位不可由前端直接设置

if __name__=='__main__':unittest.main()
