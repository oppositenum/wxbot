import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo
from test_send_safety import Isolated
from core import bot

class Group(Isolated):
    def test_nudge_requires_explicit_private_share(self):
        now=datetime(2026,9,12,14,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        rows=[dict(local_id=1,content='hi',is_self=True,create_time=now-3*3600),
              dict(local_id=2,content='hi',is_self=False,create_time=now-4*3600)]
        with patch('core.bot.llm.available',return_value=True), patch('core.bot.sender.preflight',return_value=None):
            self.assertFalse(bot._maybe_nudge('friend-A',rows,{'proactive':{'enabled':True}},{},lambda x:None,now))
            self.assertFalse(bot._maybe_nudge('friend-A',rows,{'proactive':{'enabled':True,'private_share_enabled':False}},{},lambda x:None,now))

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

    def test_settle_uses_wall_clock_not_stale_cycle_start(self):
        src = Path(__file__).resolve().parents[1].joinpath('core/bot.py').read_text()
        settle = src[src.index('# 去抖：对方停顿够久'):src.index('process_futures.append')]
        self.assertIn('time.time() - p.get("first_seen"', settle)
        self.assertNotIn('now - p.get("first_seen"', settle)

    def test_group_auto_reply_off_by_default(self):
        from core import decrypt, messages, conversation_state, personalization
        room='g@chatroom'
        store={'msgs':[dict(local_id=1,type=1,is_self=False,sender='wxid_other',
                            content='hi',create_time=0,at_me=True,quote_me=False)]}
        queued=[]
        patches=[
            patch.object(decrypt,'run',lambda force=False:None),
            patch.object(messages,'get_messages',lambda chat,limit=40:list(store['msgs']) if chat==room else []),
            patch.object(conversation_state,'observe',lambda *a,**k:None),
            patch.object(personalization,'learn_live',lambda *a,**k:None),
            patch.object(bot,'_proactive_worker',lambda *a,**k:None),
            patch.object(bot,'enqueue_pending',lambda chat,m,msgs,rule,now:queued.append(chat) or {'msgs':[m]}),
        ]
        for p in patches:p.start();self.addCleanup(p.stop)
        rules={'include_self':False,'watch':[room],'poll_interval':5,
               'rules':[{'name':'r','match':{'type':'auto'},'action':{'type':'reply_ai'}}]}
        state=bot.load_state();bot.load_pending();bot._pending.clear()
        bot.run_once(rules,state,log=lambda *_:None)
        store['msgs'].append(dict(store['msgs'][0],local_id=2,content='@me',create_time=1))
        bot.run_once(rules,state,log=lambda *_:None)
        self.assertEqual(queued,[])
        rules['group_auto_reply']=True
        bot.run_once(rules,state,log=lambda *_:None)
        store['msgs'].append(dict(store['msgs'][0],local_id=3,content='@me again',create_time=2))
        bot.run_once(rules,state,log=lambda *_:None)
        self.assertEqual(queued,[room])
