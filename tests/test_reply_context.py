"""Attribution and request-body checks with synthetic dialogue and blocked I/O."""
import copy
import unittest
from unittest.mock import patch

from test_send_safety import Isolated, forbidden
from core import bot, reply_context as context, llm, agent

real_chat = llm.chat


def msg(local_id, text, own=False, sender=None, **extra):
    return dict(local_id=local_id, server_id=1000+local_id, type=1,
                sender=sender or ('account-A' if own else 'chat-A'), is_self=own,
                content=text, create_time=100+local_id, **extra)


def build(history, batch, group=False):
    return context.build(history, batch, account='account-A', is_group=group,
                         render=lambda m:m['content'], name=lambda s:s,
                         timestamp=lambda m:str(m['create_time']),
                         scheduled=lambda m:m['content'].startswith('【定时提醒】'))


class Builder(unittest.TestCase):
    def test_both_sides_have_real_roles_and_batch_is_not_duplicated(self):
        old = msg(1, '我方旧回复', True); new = msg(2, '对方新问题')
        history, current, batch = build([old, new, new], [new, new])
        self.assertEqual([m['role'] for m in history], ['user', 'assistant'])
        self.assertIn('不是对方发言', history[0]['content'])
        self.assertNotIn('我方旧回复', current)
        self.assertEqual(current.count('对方新问题'), 1)
        self.assertEqual(len(batch), 1)

    def test_answered_question_stays_in_history_not_latest_request(self):
        rows = [msg(1, '以前的问题'), msg(2, '已经给出的答案', True), msg(3, '新的问题')]
        history, current, _ = build(rows, [rows[-1]])
        self.assertEqual([m['role'] for m in history], ['user', 'assistant'])
        self.assertNotIn('以前的问题', current); self.assertNotIn('已经给出的答案', current)

    def test_equal_text_from_different_sides_is_not_deduplicated(self):
        rows = [msg(1, '一样的句子', True), msg(2, '一样的句子')]
        history, current, _ = build(rows, [rows[-1]])
        self.assertEqual(history[-1]['role'], 'assistant')
        self.assertIn('一样的句子', history[-1]['content']); self.assertIn('一样的句子', current)

    def test_native_sender_identity_overrides_missing_self_flag(self):
        own = msg(1, '本账号说的话', sender='account-A')
        new = msg(2, '新的问题')
        history, current, batch = build([own, new], [own, new])
        self.assertEqual(history[-1]['role'], 'assistant')
        self.assertEqual(batch, [new]); self.assertNotIn('本账号说的话', current)

    def test_quote_keeps_original_author_and_current_speaker_separate(self):
        quoted = msg(2, '这句话是什么意思', refer={
            'type':'1', 'chatusr':'account-A', 'content':'你之前说的句子'})
        _, current, _ = build([], [quoted])
        self.assertIn('引用本账号的旧消息', current)
        self.assertIn('对方「chat-A」本次说：这句话是什么意思', current)

    def test_group_members_are_labeled_and_only_account_is_assistant(self):
        history, current, _ = build([msg(1,'甲说的',sender='member-A'),
                                    msg(2,'本账号说的',True)], [msg(3,'乙说的',sender='member-B')], True)
        self.assertEqual([m['role'] for m in history], ['user','assistant'])
        self.assertIn('群成员「member-A」',history[0]['content'])
        self.assertIn('群成员「member-B」',current)

    def test_entire_batch_survives_history_window_and_id_namespaces(self):
        batch=[msg(i,'新消息'+str(i)) for i in range(20,45)]
        history, current, _ = build([msg(1,'旧回复',True)]+batch,batch)
        self.assertEqual(len(history),2)
        for i in range(20,45):self.assertEqual(current.count('新消息'+str(i)),1)
        old=msg(1,'服务器编号碰巧等于另条本地编号',True);old['server_id']=20
        self.assertEqual(len(build([old]+batch,batch)[0]),2)

    def test_scheduled_self_message_is_excluded_but_users_reference_kept(self):
        history, current, _ = build([msg(1,'【定时提醒】合成提醒',True)],
                                    [msg(2,'【定时提醒】是什么意思')])
        self.assertEqual(history,[]);self.assertIn('【定时提醒】是什么意思',current)


class Integration(Isolated):
    def setUp(self):
        super().setUp()
        for target,value in [('core.bot._sender_name',lambda s:s or 'unknown'),
                             ('core.bot.send_name_for',lambda s:s),
                             ('core.personalization.preferences_context',lambda *a:''),
                             ('core.memory.select_memories',lambda *a,**k:[]),
                             ('core.bot._diag',lambda *a,**k:None),
                             ('core.schedule.is_scheduled_msg',lambda *a:False)]:
            p=patch(target,value);p.start();self.addCleanup(p.stop)
        self.rows=[msg(1,'早先的问题'),msg(2,'本账号已给出的答案',True),msg(3,'本次的新问题')]

    def test_plain_reply_sends_distinct_dialogue_turns(self):
        with patch.object(agent,'agent_config',return_value={'enabled':False}), patch.object(llm,'chat',return_value='response') as chat:
            bot._ai_reply({'persona':'test'},'chat-A',self.rows[-1],self.rows,batch_msgs=[self.rows[-1]])
        system,turns=chat.call_args.args
        self.assertEqual([m['role'] for m in turns],['user','assistant','user'])
        self.assertIn('本账号已给出的答案',turns[1]['content'])
        self.assertNotIn('早先的问题',turns[-1]['content'])
        self.assertEqual(str(turns).count('本次的新问题'),1)
        self.assertIn('不要重复自己的上一段回复',system)

    def test_tool_reply_keeps_same_roles_through_agent(self):
        with patch.object(agent,'agent_config',return_value={'enabled':True,'tools':[]}), \
             patch('core.read_access.issue',return_value=object()), patch('core.tools.specs_for',return_value=[]), \
             patch.object(llm,'chat_tools',return_value='response') as chat:
            bot._ai_reply({'persona':'test'},'chat-A',self.rows[-1],self.rows,batch_msgs=[self.rows[-1]])
        turns=chat.call_args.args[1]
        self.assertEqual([m['role'] for m in turns],['user','assistant','user'])
        self.assertNotIn('本账号已给出的答案',turns[-1]['content'])

    def test_all_self_batch_does_not_call_reply_model(self):
        with patch.object(llm,'chat',forbidden),patch.object(agent,'run',forbidden):
            self.assertEqual(bot._ai_reply({'persona':'test'},'chat-A',self.rows[1],self.rows,
                                          batch_msgs=[self.rows[1]]),'')

    def test_greeting_and_nudge_history_share_attribution(self):
        turns=bot._passive_turns('chat-A',self.rows,200)
        self.assertEqual([m['role'] for m in turns],['user','assistant','user'])

    def test_both_provider_bodies_preserve_roles_in_plain_and_tool_modes(self):
        turns=[{'role':'user','content':'synthetic question'},
               {'role':'assistant','content':'synthetic prior answer'},
               {'role':'user','content':'synthetic new question'}]
        for provider in ['claude','gpt']:
            cfg={'provider':provider,provider:{'api_key':'TEST_ONLY','base_url':'https://example.invalid','model':'test-model'}}
            response=({'content':[{'type':'text','text':'result'}]} if provider=='claude'
                      else {'choices':[{'message':{'content':'result'}}]})
            for tool_mode in [False,True]:
                with self.subTest(provider=provider,tools=tool_mode),patch.object(llm,'_post',return_value=response) as post:
                    if tool_mode:llm.chat_tools('test',turns,[],forbidden,cfg=cfg)
                    else:real_chat('test',turns,cfg=cfg)
                    body=post.call_args.args[2]
                    actual=[m for m in body['messages'] if m['role']!='system']
                    self.assertEqual(actual,turns)


if __name__=='__main__':unittest.main()
