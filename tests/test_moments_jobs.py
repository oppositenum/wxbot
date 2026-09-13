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
        # An initiated (possibly-live) or uncertain send is never replayable.
        with self.assertRaises(m.Conflict):j.retry(initiated['id'])
        with self.assertRaises(m.Conflict):j.retry(uncertain['id'])
        with self.assertRaises(ValueError):j.retry('nope')

    def test_retry_reopens_cancelled_draft(self):
        self.ready();d=m.save_draft(dict(kind='publish',text='想法'))
        task=j.enqueue_draft(d['id'],1);j.cancel(task['id'])
        self.assertEqual(m.drafts()[0]['status'],'cancelled')
        j.retry(task['id'])
        self.assertEqual(m.drafts()[0]['status'],'queued')

    def test_daily_schedule_china_no_catchup_and_once(self):
        self.ready();now=datetime(2026,9,12,12,30,tzinfo=m.CHINA).timestamp()
        with patch('time.time',return_value=now-60):m.save_settings(dict(auto_publish=True),0)
        j.schedule(now);j.schedule(now+1);self.assertEqual(len(j.listing()),1)
        task=j.listing()[0];self.assertEqual(task['origin'],'automatic')
        j.finish(task['id'],'confirmed','ok');j.schedule(now+10);self.assertEqual(len(j.listing()),1)
        j.schedule(now+86400+1801);self.assertEqual(len(j.listing()),1)

    def test_activation_watermark_does_not_reply_to_old_posts(self):
        self.ready();self.seed()
        now=1789207400
        with patch('time.time',return_value=now):m.save_settings(dict(auto_comment=True),0)
        with patch('core.contacts.list_contacts',return_value=[dict(username='friend-A')]):
            j.schedule(now);self.assertEqual(j.listing(),[])
            m.ingest([(-1,'friend-A',xml().replace('1789207200',str(now+1)))])
            j.schedule(now+2);j.schedule(now+3)
        self.assertEqual(len(j.listing()),1)

    def test_policy_rechecks_switch_revision_quiet_and_counts_uncertainty(self):
        now=datetime(2026,9,12,12,30,tzinfo=m.CHINA).timestamp()
        value=dict(m.DEFAULTS,auto_publish=True,revision=2)
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

if __name__=='__main__':unittest.main()
