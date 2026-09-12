import unittest
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo
from test_send_safety import Isolated
from core import bot

class Group(Isolated):
    def test_opt_in_wait_cooldown_and_next_inbound(self):
        now=datetime(2026,9,12,14,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        rows=[dict(local_id=1,content='今天真开心',is_self=False,create_time=now-301)]
        rules={'proactive':{'enabled':True,'group_enabled':True},'rules':[{'action':{'type':'reply_ai'}}]}
        state={}
        with patch('core.bot.llm.available',return_value=True), patch('core.bot.sender.preflight',return_value=None), patch('core.bot.conversation_state.ticket',return_value={'ok':True}), patch('core.bot.conversation_state.allowed',return_value=True), patch('core.bot.personalization.resolve_persona',return_value={'persona':{}}), patch('core.bot.personalization.role_context',return_value=''), patch('core.bot._passive_turns',return_value=[]), patch('core.bot.llm.chat',return_value='很开心呢'), patch('core.bot.send_name_for',return_value='群'), patch('core.bot.sender.send_text',return_value={'status':'submitted'}) as send:
            self.assertFalse(bot._maybe_group_nudge('g@chatroom',rows,{'proactive':{'group_enabled':True}},state,lambda x:None,now))
            self.assertFalse(bot._maybe_group_nudge('g@chatroom',[dict(rows[0],create_time=now-100)],rules,state,lambda x:None,now))
            self.assertTrue(bot._maybe_group_nudge('g@chatroom',rows,rules,state,lambda x:None,now))
            self.assertFalse(bot._maybe_group_nudge('g@chatroom',rows,rules,state,lambda x:None,now+2000))
            self.assertFalse(bot._maybe_group_nudge('g@chatroom',[dict(rows[0],local_id=2)],rules,state,lambda x:None,now+600))
            self.assertEqual(send.call_count,1)

    def test_beijing_quiet_cross_midnight_and_same_day(self):
        ts=lambda h:datetime(2026,9,12,h,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        for hour,expected in [(22,True),(7,True),(8,False),(21,False)]:
            self.assertEqual(bot._quiet_now(ts(hour),(22,8)),expected)
        self.assertTrue(bot._quiet_now(ts(14),(12,16)))
        self.assertFalse(bot._quiet_now(ts(8),(12,16)))
        self.assertFalse(bot._quiet_now(ts(14),(0,0)))

    def test_batch_deadline_not_reset_by_later_input(self):
        rule={'action':{'type':'reply_ai'}}
        p=bot.enqueue_pending('chat-A',{'local_id':1},[],rule,100)
        bot.enqueue_pending('chat-A',{'local_id':2},[],rule,104)
        self.assertEqual(p['first_seen'],100)
        self.assertEqual(bot._settle_for([{'type':1}]),5)
