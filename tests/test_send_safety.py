"""Offline control-logic evidence. No real WeChat identity/UI or model validation."""
import ast
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from core import account_session as sessions, sender, send_ledger, bot, schedule, docker_wx, memory, tools, llm


def forbidden(*a, **kw):
    raise AssertionError('real network/process/model forbidden')


class Adapter:
    available = True
    def __init__(self, token):
        self.proof = dict(token, chat='chat-A', trusted=True, session_identity='test-session')
        self.calls = 0
        self.opens = 0
        self.delivered = True
        self.open_ok = True
    def open(self, display, chat):
        self.opens += 1
        return self.open_ok
    def identity(self):
        return dict(self.proof)
    def send(self, kind, payload, jid):
        self.calls += 1
    def receipt(self, jid):
        return dict(self.proof, job_id=jid, message_id='test-receipt') if self.delivered else None


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='wx-send-test-')
        self.addCleanup(self.tmp.cleanup)
        self.account = 'account-A'
        for target, value in [('config.ACCOUNTS_DIR', self.tmp.name+'/accounts'),
                              ('config.WORK_DIR', self.tmp.name+'/work'),
                              ('config.wxid', lambda: self.account),
                              ('socket.socket.connect', forbidden), ('socket.create_connection', forbidden),
                              ('subprocess.run', forbidden), ('subprocess.Popen', forbidden),
                              ('core.llm._post', forbidden), ('core.llm.chat', forbidden),
                              ('core.llm.gen_image', forbidden), ('core.llm.load_cfg', lambda: {})]:
            p=patch(target,value);p.start();self.addCleanup(p.stop)
        sessions._current=None
        self.token=sessions.capture()
        self.adapter=Adapter(self.token)
        p=patch.object(sender,'_adapter',self.adapter);p.start();self.addCleanup(p.stop)
        bot._pending.clear();bot._pending_session=self.token
    def send(self, **kw):
        return sender.send_text('same name','test-body',chat_username='chat-A',job_id='job-1',session=self.token,**kw)
    def switch(self, account):
        self.account=account
        return sessions.observe()
    def pending(self):
        p={'session':self.token,'msgs':[{'local_id':1,'content':'hi','sender':'user'}],
           'ctx':[], 'rule':{'name':'r','action':{'type':'reply_ai','persona':'p'}}}
        bot._pending['chat-A']=p
        return p
    def task(self, once=True):
        return {'id':1,'target_username':'chat-A','target_display':'same name',
                'prompt':'test-body','enabled':True, 'once_at':'2020-01-01 06:00' if once else None,
                'cron':None if once else '* * * * *'}


class Coordinator(Isolated):
    def test_wrong_current_chat_zero_actions(self):
        self.adapter.proof['chat']='chat-B'
        self.assertEqual(self.send()['reason'],'cannot_confirm_target')
        self.assertEqual(self.adapter.calls,0)
    def test_same_display_name_is_not_identity(self):
        self.adapter.proof={'display_name':'same name','trusted':True}
        self.assertEqual(self.send()['status'],'not_sent')
        self.assertEqual(self.adapter.calls,0)
    def test_default_adapter_blocks_without_navigation(self):
        with patch.object(sender,'_adapter',sender.UnavailableIdentityAdapter()),patch.object(docker_wx,'open_chat',forbidden):
            self.assertEqual(self.send()['reason'],'cannot_confirm_target')
    def test_identity_checked_twice(self):
        with patch.object(self.adapter,'identity',side_effect=[dict(self.adapter.proof),{}]) as identity:
            self.assertEqual(self.send()['reason'],'target_changed')
            self.assertEqual(identity.call_count,2)
        self.assertEqual(self.adapter.calls,0)
    def test_delayed_receipt_only_one_send_and_read_only_reconcile(self):
        self.adapter.delivered=False
        self.assertEqual(self.send()['status'],'uncertain')
        self.assertEqual(self.send()['status'],'uncertain')
        self.adapter.delivered=True
        self.assertEqual(sender.reconcile('job-1',self.token)['status'],'confirmed')
        self.assertEqual(self.adapter.calls,1)
        self.assertEqual(self.adapter.opens,1)
    def test_send_exception_is_uncertain(self):
        def action(*a):
            self.adapter.calls+=1
            raise TimeoutError()
        with patch.object(self.adapter,'send',action):
            self.assertEqual(self.send()['status'],'uncertain')
            self.send()
        self.assertEqual(self.adapter.calls,1)
    def test_post_send_disk_failure_restart_no_replay(self):
        original=send_ledger.Ledger.update
        def fail_end(ledger,jid,status,reason,*a):
            if status in ('confirmed','uncertain'): raise OSError('simulated fsync failure')
            return original(ledger,jid,status,reason,*a)
        with patch.object(send_ledger.Ledger,'update',fail_end):
            self.assertEqual(self.send()['status'],'uncertain')
        self.assertEqual(send_ledger.Ledger().get('job-1')['status'],'initiated')
        sessions._current=None
        self.assertEqual(self.send()['status'],'uncertain')
        self.assertEqual(self.adapter.calls,1)
    def test_intent_commit_failure_never_sends(self):
        original=send_ledger.Ledger.update
        def fail_intent(ledger,jid,status,reason,*a):
            if status=='initiated': raise OSError()
            return original(ledger,jid,status,reason,*a)
        with patch.object(send_ledger.Ledger,'update',fail_intent):
            self.assertEqual(self.send()['status'],'not_sent')
        self.assertEqual(self.adapter.calls,0)
    def test_pre_send_failure_retry_is_delayed_and_bounded(self):
        self.adapter.open_ok=False
        with patch('core.sender.time.time',return_value=100):
            first=self.send();self.send()
        self.assertTrue(first['retryable']);self.assertEqual(self.adapter.opens,1)
        for tick in (131,162,193):
            with patch('core.sender.time.time',return_value=tick): final=self.send()
        self.assertFalse(final['retryable'])
        self.assertEqual(self.adapter.opens,3);self.assertEqual(self.adapter.calls,0)
    def test_successful_presend_retry_only_one_send(self):
        self.adapter.open_ok=False
        with patch('time.time',return_value=100): self.send()
        self.adapter.open_ok=True
        with patch('time.time',return_value=131): self.assertEqual(self.send()['status'],'confirmed')
        self.assertEqual(self.adapter.calls,1)
    def test_account_switch_and_aba(self):
        self.switch('account-B');self.switch('account-A')
        self.assertEqual(self.send()['status'],'stale')
        self.assertEqual(self.adapter.calls,0)
    def test_change_during_send_remains_uncertain(self):
        def action(*a): self.adapter.calls+=1;self.switch('account-B')
        with patch.object(self.adapter,'send',action):
            self.assertEqual(self.send()['reason'],'account_changed_after_initiation')
        self.assertEqual(self.adapter.calls,1)
    def test_ui_switch_cannot_interleave(self):
        inside=threading.Event();attempt=threading.Event();switched=threading.Event()
        original=self.adapter.identity
        def identity():
            inside.set()
            self.assertTrue(attempt.wait(2))
            self.assertFalse(switched.is_set())
            return original()
        def nav():
            inside.wait(2);attempt.set()
            with docker_wx.UI_LOCK: switched.set()
        t=threading.Thread(target=nav);t.start()
        try:
            with patch.object(self.adapter,'identity',identity):self.send()
        finally:t.join(2)
        self.assertTrue(switched.is_set());self.assertEqual(self.adapter.calls,1)
    def test_concurrent_same_job_one_action(self):
        results=[]
        threads=[threading.Thread(target=lambda:results.append(self.send())) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(3)
        self.assertEqual(len(results),2);self.assertEqual(self.adapter.calls,1)


class Callers(Isolated):
    def test_ai_failure_retains_pending_and_no_busy_loop(self):
        p=self.pending()
        with patch.object(bot.distill,'load_persona',return_value={'name':'P'}),patch.object(bot,'_ai_reply',side_effect=RuntimeError()) as ai:
            bot.process_pending('chat-A',p,{},lambda *a:None)
            bot.process_pending('chat-A',p,{},lambda *a:None)
        self.assertEqual(ai.call_count,1);self.assertIn('chat-A',bot._pending)
        self.assertEqual(p['send_status'],'not_sent');self.assertEqual(self.adapter.calls,0)
    def test_send_failure_retains_pending(self):
        p=self.pending()
        with patch.object(bot,'do_action',return_value={'status':'not_sent','reason':'cannot_confirm_target'}):
            bot.process_pending('chat-A',p,{},lambda *a:None)
        self.assertIn('chat-A',bot._pending)
        stored=json.loads(Path(bot.pending_file()).read_text())
        self.assertEqual(stored['chat-A']['send_status'],'not_sent')
    def test_model_switch_no_send_no_new_account_memory(self):
        p=self.pending()
        def model(*a,**kw):self.switch('account-B');return 'old reply'
        with patch.object(bot.distill,'load_persona',return_value={'name':'P'}),patch.object(bot,'_ai_reply',model),patch.object(bot,'send_name_for',return_value='same name'),patch.object(bot,'_maybe_learn') as learn:
            bot.process_pending('chat-A',p,{},lambda *a:None)
        self.assertEqual(self.adapter.calls,0);learn.assert_not_called()
        self.assertFalse(Path(config.ACCOUNTS_DIR,'account-B').exists())
    def test_missing_new_account_pending_clears_old_memory(self):
        self.pending();bot.save_pending();self.switch('account-B');bot.load_pending()
        self.assertEqual(bot._pending,{})
        self.assertTrue(Path(config.ACCOUNTS_DIR,'account-A','bot_pending.json').exists())
    def test_restart_pending_is_held(self):
        self.pending();bot.save_pending();sessions._current=None;bot.load_pending()
        self.assertEqual(bot._pending['chat-A']['send_status'],'stale')
    def test_stale_learning_write_rejected(self):
        with sessions.bind(self.token):
            self.switch('account-B');self.switch('account-A')
            with self.assertRaises(sessions.StaleAccount):
                memory.save_profile({'wxid':'friend','facts':[]})
        self.assertFalse(Path(config.ACCOUNTS_DIR,'account-B').exists())
    def test_once_uncertain_retained_and_not_repeated(self):
        self.adapter.delivered=False
        tasks=[self.task()]
        with patch.object(schedule,'load_tasks',return_value=tasks),patch.object(schedule,'save_tasks') as save:
            schedule.tick();schedule.tick()
        self.assertEqual(self.adapter.calls,1);save.assert_not_called()
        self.assertEqual(len(tasks),1)
    def test_once_confirmed_record_failure_no_repeat(self):
        with patch.object(schedule,'_record_fire',side_effect=OSError()):
            first=schedule.fire(self.task(),occurrence='once:test',session=self.token)
            second=schedule.fire(self.task(),occurrence='once:test',session=self.token)
        self.assertEqual(first['status'],'confirmed');self.assertEqual(second['status'],'confirmed')
        self.assertEqual(self.adapter.calls,1)
    def test_cron_occurrence_deduplicated_and_next_minute_distinct(self):
        with patch.object(schedule,'_record_fire'):
            for occurrence in ('cron:1','cron:1','cron:2'):
                schedule.fire(self.task(False),occurrence=occurrence,session=self.token)
        self.assertEqual(self.adapter.calls,2)
    def test_schedule_model_switch_no_send(self):
        def compute(t):self.switch('account-B');return 'old reply'
        with patch.object(schedule,'_compute_text',compute):
            r=schedule.fire(self.task(),occurrence='once:test',session=self.token)
        self.assertEqual(r['status'],'stale');self.assertEqual(self.adapter.calls,0)
    def test_raw_sending_entrypoints_fail_closed(self):
        for fn,args in [(docker_wx.paste_text,('x',)),(docker_wx.paste_at,('x','y')),
                        (docker_wx.paste_image_open,('/no/file',)),(docker_wx.send_text,('x','y')),
                        (docker_wx.send_image,('x','/no/file'))]:
            self.assertEqual(fn(*args)['status'],'not_sent')
        self.assertEqual(self.adapter.calls,0)
    def test_all_bot_sends_use_coordinator(self):
        for f in ('core/bot.py','core/schedule.py','core/sendq.py','core/tools.py','server.py'):
            tree=ast.parse(Path(f).read_text())
            raw=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)
                 and isinstance(n.func.value,ast.Name) and n.func.value.id=='docker_wx'
                 and n.func.attr in ('send_text','send_image','paste_text','paste_at','paste_image_open')]
            self.assertEqual(raw,[],f)

    def test_pending_uncertain_survives_restart(self):
        p=self.pending();p['send_status']='uncertain';bot.save_pending()
        sessions._current=None;bot.load_pending()
        held=bot._pending['chat-A']
        bot.process_pending('chat-A',held,{},lambda *a:None)
        self.assertEqual(held['send_status'],'uncertain')
        self.assertEqual(self.adapter.calls,0)
    def test_schedule_json_failure_does_not_repeat_execution(self):
        with patch.object(schedule,'load_tasks',return_value=[self.task()]),patch.object(schedule,'_record_fire'),patch.object(schedule,'save_tasks',side_effect=OSError()):
            for _ in range(2):
                with self.assertRaises(OSError):schedule.tick()
        self.assertEqual(self.adapter.calls,1)
    def test_pending_bounded_retry_does_not_regenerate(self):
        p=self.pending();self.adapter.open_ok=False
        with patch('core.reply_inbox.refresh'), patch('core.conversation_state.latest', return_value=[]), patch.object(bot.distill,'load_persona',return_value={'name':'P'}),patch.object(bot,'_ai_reply',return_value='test-body') as ai,patch.object(bot,'send_name_for',return_value='same name'):
            with patch('time.time',return_value=100):bot.process_pending('chat-A',p,{},lambda *a:None)
            self.adapter.open_ok=True
            with patch('time.time',return_value=131):bot.process_pending('chat-A',p,{},lambda *a:None)
        self.assertEqual(ai.call_count,1);self.assertEqual(self.adapter.calls,1)
        self.assertNotIn('chat-A',bot._pending)
    def test_concurrent_schedule_execution_one_send(self):
        entered=threading.Event();release=threading.Event();results=[]
        def compute(t):entered.set();release.wait(2);return 'test-body'
        with patch.object(schedule,'_compute_text',compute),patch.object(schedule,'_record_fire'):
            thread=threading.Thread(target=lambda:results.append(schedule.fire(self.task(),occurrence='once:1',session=self.token)))
            thread.start();self.assertTrue(entered.wait(2))
            second=schedule.fire(self.task(),occurrence='once:1',session=self.token)
            release.set();thread.join(3)
        self.assertEqual(second['status'],'uncertain');self.assertEqual(self.adapter.calls,1)
    def test_queue_binds_before_account_switch_and_persists(self):
        import queue
        from core import sendq
        q=queue.Queue()
        with patch.object(sendq,'_q',q),patch.object(sendq,'_ensure_worker',lambda:None),patch.object(sendq,'_jobs',{}):
            jid,_=sendq.enqueue('text','same name','chat-A','test-body')
            row=send_ledger.Ledger().get(jid)
            self.assertEqual(row['account'],'account-A')
            item=q.get_nowait();self.switch('account-B')
            with patch.object(q,'get',side_effect=[item,StopIteration]),patch.object(q,'task_done'):
                with self.assertRaises(StopIteration):sendq._run()
            self.assertEqual(sendq.status(jid)['send_status'],'stale')
        self.assertEqual(self.adapter.calls,0)
    def test_tool_image_uses_coordinator_and_preserves_uncertain(self):
        self.adapter.delivered=False
        from core import read_access
        access=read_access.Access('account-A',os.path.realpath(config.account_dir()),'chat-A',frozenset({'chat-A'}),(('chat-A','name'),))
        with patch.object(llm,'gen_image',return_value=b'offline fake image'),patch('tempfile.tempdir',self.tmp.name):
            reply=tools.draw_image('test prompt',{'chat':'chat-A','read_access':access})
        self.assertIn('待核对',reply);self.assertEqual(self.adapter.calls,1)
    def test_tool_model_aba_cannot_send(self):
        from core import read_access
        access=read_access.Access('account-A',os.path.realpath(config.account_dir()),'chat-A',frozenset({'chat-A'}),(('chat-A','name'),))
        def generate(*a,**kw):self.switch('account-B');self.switch('account-A');return b'offline image'
        with patch.object(llm,'gen_image',generate):
            with self.assertRaises(sessions.StaleAccount):tools.draw_image('test',{'chat':'chat-A','read_access':access})
        self.assertEqual(self.adapter.calls,0)
    def test_learning_model_switch_cannot_write_profile(self):
        def generate(*a,**kw):
            self.switch('account-B')
            return '{"facts":{"friend":[{"text":"test fact","type":"preference"}]}}'
        msgs=[{'sender':'friend','sender_name':'friend','content':'offline test line'} for _ in range(3)]
        with patch.object(llm,'available',return_value=True),patch.object(llm,'chat',generate):
            with self.assertRaises(sessions.StaleAccount):memory.extract_from_messages(msgs)
        self.assertFalse(Path(config.ACCOUNTS_DIR,'account-B').exists())
    def test_standalone_cli_send_helpers_are_blocked(self):
        import runpy
        module=runpy.run_path('docker/wx_send.py',run_name='offline_import')
        for name,args in [('send_text',('name','text')),('send_image',('name','path')),
                          ('paste_text',('text',)),('paste_image',('path',)),('paste_at',('name','text'))]:
            with self.assertRaisesRegex(RuntimeError,'cannot_confirm_target'):module[name](*args)

    def test_model_cannot_repeat_uncertain_image_tool_in_same_round(self):
        from core import read_access
        self.adapter.delivered=False
        access=read_access.Access('account-A',os.path.realpath(config.account_dir()),'chat-A',frozenset({'chat-A'}),(('chat-A','name'),))
        with send_ledger.operation('tool-round'),patch.object(llm,'gen_image',return_value=b'offline image') as generate,patch('tempfile.tempdir',self.tmp.name):
            for _ in range(2):tools.draw_image('test prompt',{'chat':'chat-A','read_access':access})
        self.assertEqual(generate.call_count,1)
        self.assertEqual(self.adapter.calls,1)

    def test_push_webhook_timeout_is_durable_and_not_reposted(self):
        with patch('urllib.request.urlopen',side_effect=TimeoutError()) as post:
            for _ in range(2):
                r=sender.send_webhook('https://offline.invalid/hook',{'body':'offline'},job_id='hook-1',session=self.token)
        self.assertEqual(post.call_count,1);self.assertEqual(r['status'],'uncertain')
        self.assertEqual(self.adapter.calls,0)

    def test_stale_push_is_recorded_without_killing_worker_on_switch(self):
        import queue
        payload={'chat':'source-chat','local_id':3}
        item=('offline',payload,[{'type':'wechat','to':'same name','username':'chat-A'}],lambda *a:None,self.token)
        q=queue.Queue()
        self.switch('account-B')
        with patch.object(bot,'_push_q',q),patch.object(q,'get',side_effect=[item,StopIteration]),patch.object(q,'task_done') as done:
            with self.assertRaises(StopIteration):bot._push_worker()
        jid=send_ledger.stable_id('account-A','push','source-chat',3,0)
        self.assertEqual(send_ledger.result(jid)['status'],'stale')
        done.assert_called_once();self.assertEqual(self.adapter.calls,0)


if __name__=='__main__':unittest.main(verbosity=2)
