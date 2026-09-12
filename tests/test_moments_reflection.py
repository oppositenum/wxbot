"""Isolated chat-to-Moments path; no real chat, model or UI sends."""
import json
import threading
import unittest
from unittest.mock import patch
import test_moments as fixtures
from core import moments as m, moments_jobs as jobs, moments_reflection as reflection

class Reflection(unittest.TestCase):
    setUp=fixtures.Moments.setUp

    def setup_reflection(self):
        reflection._pending.clear();self.addCleanup(reflection._pending.clear)
        for name,value in [('core.moments.capabilities',dict(send=True)),('core.contacts.list_contacts',[dict(username='friend',name='朋友甲')]),('core.personalization.resolve_persona',dict(error='',persona=dict(persona='温和自然'))),('core.llm.load_cfg',{})]:
            p=patch(name,return_value=value);p.start();self.addCleanup(p.stop)
        p=patch('core.moments.quiet_now',return_value=False);p.start();self.addCleanup(p.stop)
        m.save_settings(dict(chat_reflection=True),0)

    def capture(self):
        return reflection.capture('friend',[dict(local_id=1,content='努力了很久终于完成目标了',sender='friend')],[], '为你的坚持感到高兴。')

    def test_disabled_no_capture_or_model(self):
        with patch('core.llm.chat') as model:
            self.assertIsNone(self.capture());model.assert_not_called()
        self.assertEqual(jobs.listing(),[])

    def test_private_context_not_in_ledger_and_event_dedup(self):
        self.setup_reflection();jid=self.capture();self.assertTrue(jid)
        self.assertIsNone(self.capture());self.assertEqual(len(jobs.listing()),1)
        self.assertNotIn('努力',json.dumps(jobs.listing(),ensure_ascii=False))
        self.assertNotIn('朋友甲',json.dumps(jobs.listing(),ensure_ascii=False))
        self.assertIn(jid,reflection._pending)
        jobs.cancel(jid);self.assertNotIn(jid,reflection._pending)

    def test_independent_switch_and_shared_limits(self):
        self.setup_reflection();jid=self.capture();task=jobs.listing()[0];v=m.settings()
        self.assertFalse(v['auto_publish']);self.assertEqual(jobs.policy(task,v,task['created']),'')
        self.assertTrue(jobs.policy(task,dict(v,chat_reflection=False),task['created']))
        with patch('core.moments.quiet_now',return_value=True):self.assertIsNone(self.capture())
        reflection.forget(jid)

    def test_groups_and_non_allowlisted_contacts_do_not_trigger(self):
        self.setup_reflection()
        self.assertIsNone(reflection.capture('room@chatroom',[dict(local_id=1,content='hello')],[],'reply'))
        m.save_settings(dict(friend_allowlist=['friend']),1)
        self.assertIsNone(reflection.capture('other',[dict(local_id=1,content='hello')],[],'reply'))

    def answer(self,text,**extra):
        return json.dumps(dict(action='publish',text=text,emotion='开心',confidence=.95,privacy_safe=True,**extra),ensure_ascii=False)

    def test_generate_can_skip_and_discards_private_memory(self):
        self.setup_reflection();jid=self.capture()
        with patch('core.llm.chat',return_value='{"action":"skip"}'):
            self.assertTrue(reflection.generate(jid)['skip'])
        self.assertNotIn(jid,reflection._pending)

    def test_rejects_names_quotes_and_malformed_model_output(self):
        self.setup_reflection();jid=self.capture();data=reflection._pending[jid]
        for text in ['朋友甲总是这样让我感觉开心极了。','努力了很久终于完成目标了，这感觉真好。','很想把号码123456789分享给大家。']:
            reflection._pending[jid]=data
            with patch('core.llm.chat',return_value=self.answer(text)):
                self.assertTrue(reflection.generate(jid)['skip'])
        reflection._pending[jid]=data
        with patch('core.llm.chat',return_value='not json'):
            with self.assertRaises(m.Unavailable):reflection.generate(jid)

    def test_generated_reflection_reaches_existing_receipt_worker(self):
        self.setup_reflection();jid=self.capture()
        text='原来认真积攒的小小勇气，也能让平凡的一天亮起来。'
        from core import docker_wx
        calls=[]
        class Native:
            def prepare_publish(self,body,paths):calls.append(body)
            def submit(self):calls.append('submit')
            def cleanup(self):pass
        with patch('core.llm.chat',side_effect=[self.answer(text),'{"safe":true}']),patch.object(docker_wx,'priority_pending',return_value=False),patch.object(docker_wx,'UI_LOCK',threading.RLock()),patch('core.moments_native.Native',Native),patch('core.moments.sync'),patch('core.moments_jobs.receipt',return_value='receipt-id'),patch('core.moments_jobs.time.sleep'):
            jobs.process_one()
        task=jobs.listing()[0];self.assertEqual(task['state'],'confirmed');self.assertEqual(task['origin'],'reflection')
        self.assertEqual(calls,[text,'submit']);self.assertEqual(task['receipt'],'receipt-id')
        self.assertNotIn(jid,reflection._pending)

    def test_turning_off_before_processing_never_calls_model(self):
        self.setup_reflection();jid=self.capture();m.save_settings(dict(chat_reflection=False),1)
        with patch('core.llm.chat') as model:jobs.process_one();model.assert_not_called()
        self.assertEqual(jobs.listing()[0]['state'],'skipped');self.assertNotIn(jid,reflection._pending)

    def test_chat_hook_only_after_confirmed_ai_reply(self):
        self.setup_reflection()
        from core import bot
        rule=dict(name='test',action=dict(type='reply_ai'))
        msg=dict(local_id=1,content='今天特别开心',sender='friend')
        with patch('core.bot._ai_reply',return_value='替你开心'),patch('core.bot.send_name_for',return_value='friend'),patch('core.sender.preflight',return_value=None),patch('core.personalization.resolve_persona',return_value=dict(persona=dict(name='角色',persona='自然'))),patch('core.moments_reflection.capture') as capture:
            for status in ['submitted','uncertain','failed']:
                with patch('core.sender.send_text',return_value=dict(status=status)):
                    bot.do_action(rule,msg,'friend',{},lambda *a:None)
            capture.assert_not_called()
            with patch('core.sender.send_text',return_value=dict(status='confirmed')):
                bot.do_action(rule,msg,'friend',{},lambda *a:None)
            capture.assert_called_once_with('friend',[msg],[],'替你开心')

    def test_public_review_can_veto_generated_expression(self):
        self.setup_reflection();jid=self.capture()
        with patch('core.llm.chat',side_effect=[self.answer('原来认真积攒的小小勇气，也能让平凡的一天亮起来。'),'{"safe":false}']):
            self.assertTrue(reflection.generate(jid)['skip'])

    def test_switch_off_during_generation_blocks_submission(self):
        self.setup_reflection();self.capture()
        from core import docker_wx
        calls=[]
        class Native:
            def prepare_publish(self,*args):calls.append('prepared')
            def submit(self):calls.append('submitted')
            def cleanup(self):calls.append('cleanup')
        responses=iter([self.answer('原来认真积攒的小小勇气，也能让平凡的一天亮起来。'),'{"safe":true}'])
        def model(*args,**kwargs):
            result=next(responses)
            if result=='{"safe":true}':m.save_settings(dict(chat_reflection=False),1)
            return result
        with patch('core.llm.chat',side_effect=model),patch.object(docker_wx,'priority_pending',return_value=False),patch.object(docker_wx,'UI_LOCK',threading.RLock()),patch('core.moments_native.Native',Native):
            jobs.process_one()
        self.assertNotIn('submitted',calls);self.assertEqual(jobs.listing()[0]['state'],'skipped')

    def test_expired_memory_removed(self):
        self.setup_reflection();jid=self.capture();reflection._pending[jid]['created']-=1801
        reflection.prune();self.assertNotIn(jid,reflection._pending)
        self.assertTrue(reflection.generate(jid)['skip'])

if __name__=='__main__':unittest.main()
