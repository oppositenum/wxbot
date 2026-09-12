"""Only temporary databases, synthetic messages and model doubles. No paid requests."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_contact_personalization import Isolated, msg, forbidden
from core import profile_drafts as d, conversation_state as cs, personalization as p, account_session as sessions, bot, llm, sender, send_ledger, messages


class DraftBase(Isolated):
    def setUp(self):
        self._real_chat=llm.chat
        super().setUp()
        self.cfg={'provider':'gpt','gpt':{'base_url':'https://synthetic.invalid/v1','api_key':'synthetic-test-only','model':'configured-name'}}
        mp=patch('core.llm.load_cfg',return_value=self.cfg);mp.start();self.addCleanup(mp.stop)
        self.calls=[]
        self.start=1700000000;self.end=self.start+4*86400

    def sample_rows(self):
        return [dict(local_id=i+1,message_id='message-'+str(i),text='以后回复短一点' if i%3==0 else '合成日常表达，问一个具体问题',
                     time=self.start+i*3600,snippet=i//3,source='text',sender='contact',scope='chat:friend-A',account=self.account,truncated=False) for i in range(9)]

    def preview(self,**kw):
        with patch.object(d,'sample',return_value=self.sample_rows()):
            return d.preview('friend-A',self.start,self.end,**kw)

    def model(self,category='expression',dimension='message_shape',value='分条短消息',mutate=None):
        def respond(system,history,cfg):
            self.calls.append((system,history,cfg));req=json.loads(history[0]['content']);rows=req['messages']
            item=dict(category=category,dimension=dimension,value=value,evidence_ids=[m['id'] for m in rows],rationale='多段合成表达的可见特征',conflict_ids=[],confidence='medium',sufficient=True,scope=req['scope'])
            if mutate:mutate(item,req)
            return json.dumps({'items':[item]},ensure_ascii=False)
        return patch('core.llm.chat',side_effect=respond)

    def analyzed(self,**kw):
        preview=self.preview();jid=preview['job']['id']
        with self.model(**kw):d.run_batch(jid,0,True)
        return d.detail(jid)


class Analysis(DraftBase):
    def test_preview_no_call_and_draft_does_not_affect_reply(self):
        before=p.preferences_context('friend-A')
        state=self.analyzed()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(p.preferences_context('friend-A'),before)
        self.assertEqual(p.get('friend-A')['revision'],0)
        self.assertEqual(state['items'][0]['status'],'pending')
        request=json.loads(self.calls[0][1][0]['content'])
        self.assertNotIn('account',request['messages'][0]);self.assertNotIn('friend-A',self.calls[0][1][0]['content'])
        self.assertIn('不可信分析数据',self.calls[0][0]);self.assertTrue(self.calls[0][2]['single_attempt'])

    def test_expression_accepted_is_observation_not_preference(self):
        state=self.analyzed();item=state['items'][0]
        result=d.review(item['id'],'accept',0)
        self.assertEqual(result['bucket'],'observations')
        self.assertIn('message_shape',p.get('friend-A')['observations'])
        self.assertEqual(p.get('friend-A')['preferences'],{})
        self.assertEqual(p.preferences_context('friend-A'),'')

    def test_accepted_reception_and_current_detailed_request(self):
        state=self.analyzed(category='reception',dimension='length',value='简短')
        d.review(state['items'][0]['id'],'accept',0)
        self.assertEqual(p.preferences_context('friend-A'),'')  # Reviewed does not implicitly enable adaptation.
        self.change('friend-A',personalization_enabled=True)
        self.assertIn('简短',p.preferences_context('friend-A'))
        self.assertNotIn('简短',p.preferences_context('friend-A','请详细逐步解释'))
        self.assertEqual(p.get('friend-A')['preferences']['length']['source'],'user_explicit')

    def test_review_preserves_manual_source_and_records_manual_edits(self):
        p.update('friend-A',{'preferences':{'length':{'value':'简短'}}},0)
        state=self.analyzed(category='reception',dimension='length',value='简短')
        d.review(state['items'][0]['id'],'accept',1)
        self.assertEqual(p.get('friend-A')['preferences']['length']['source'],'admin_manual')
        state=self.analyzed(category='reception',dimension='length',value='简短')
        d.review(state['items'][0]['id'],'accept',2,value='适中')
        self.assertEqual(p.get('friend-A')['preferences']['length']['source'],'admin_manual')

    def test_repeated_feedback_remains_inference_and_cannot_replace_manual(self):
        rows=[dict(r,text='你的回答太长了') for r in self.sample_rows()]
        with patch.object(self,'sample_rows',return_value=rows):
            state=self.analyzed(category='reception',dimension='length',value='简短')
        p.update('friend-A',{'preferences':{'length':{'value':'详细'}}},0)
        with self.assertRaises(p.Conflict):d.review(state['items'][0]['id'],'accept',1)
        p.update('friend-A',{'preferences':{'length':None}},1)
        d.review(state['items'][0]['id'],'accept',2)
        self.assertEqual(p.get('friend-A')['preferences']['length']['source'],'inferred')
        self.change('friend-A',personalization_enabled=True)
        self.assertIn('弱参考',p.preferences_context('friend-A'))

    def test_rejected_explicit_reassessment_and_new_user_requirement(self):
        state=self.analyzed(category='reception',dimension='length',value='简短');iid=state['items'][0]['id']
        d.review(iid,'reject',0)
        self.assertEqual(self.analyzed(category='reception',dimension='length',value='简短')['items'],[])
        calls=len(self.calls)
        self.assertEqual(d.review(iid,'reevaluate',0)['model_calls'],0)
        self.assertEqual(len(self.calls),calls)
        self.assertEqual(p.get('friend-A')['preferences'],{})
        d.review(iid,'reject',0)
        self.change('friend-A',auto_update=True)
        p.learn_live('friend-A',[msg(90,'以后回复短一点')])
        self.assertEqual(p.get('friend-A')['preferences']['length']['source'],'user_explicit')

    def test_actual_http_cap_equals_saved_reservation_for_both_adapters(self):
        for provider in ('gpt','claude'):
            self.cfg.update(provider=provider)
            self.cfg[provider]=dict(base_url='https://synthetic.invalid',api_key='synthetic',model='unchanged-model')
            state=self.preview();jid=state['job']['id'];requests=[]
            def transport(url,headers,body,proxy,**kwargs):
                requests.append((body,kwargs))
                return {'content':[{'type':'text','text':'{"items":[]}'}]} if provider=='claude' else {'choices':[{'message':{'content':'{"items":[]}'}}]}
            with patch.object(llm,'chat',self._real_chat),patch.object(llm,'_post',side_effect=transport):
                d.run_batch(jid,0,True)
            final=d.detail(jid)
            self.assertEqual(len(requests),1)
            self.assertEqual(requests[0][0]['max_tokens'],state['job']['output_token_limit'])
            self.assertEqual(requests[0][0]['model'],'unchanged-model')
            self.assertEqual(requests[0][1]['retries'],0)
            self.assertEqual(final['job']['tokens_reserved'],final['batches'][0]['input_estimate']+requests[0][0]['max_tokens'])

    def test_strategy_never_becomes_determined_preference(self):
        state=self.analyzed(category='strategy',dimension='structure',value='先给结论，按需展开')
        d.review(state['items'][0]['id'],'accept',0)
        self.assertEqual(p.get('friend-A')['preferences'],{})
        self.change('friend-A',personalization_enabled=True)
        self.assertIn('不是已确定偏好',p.preferences_context('friend-A'))
        self.assertEqual(p.preferences_context('friend-A','请详细解释'),'')

    def test_reception_from_short_messages_not_enough(self):
        with patch.object(self,'sample_rows',return_value=[dict(r,text='嗯') for r in self.sample_rows()]):
            state=self.analyzed(category='reception',dimension='length',value='简短')
        item=state['items'][0];self.assertFalse(item['sufficient'])
        with self.assertRaises(p.Conflict):d.review(item['id'],'accept',0)

    def test_missing_evidence_discarded(self):
        state=self.analyzed(mutate=lambda i,r:i.update(evidence_ids=[]))
        self.assertEqual(state['items'],[])

    def test_foreign_nonexistent_and_wrong_scope_evidence_rejected(self):
        for mutate in [lambda i,r:i.update(evidence_ids=['does-not-exist']),lambda i,r:i.update(scope='chat:other')]:
            preview=self.preview();jid=preview['job']['id']
            with self.model(mutate=mutate):
                with self.assertRaises(ValueError):d.run_batch(jid,0,True)
            self.assertEqual(d.detail(jid)['items'],[])
            self.assertEqual(d.detail(jid)['batches'][0]['status'],'failed')
        rows=self.sample_rows()
        for m in rows:m['id']=m['message_id']
        item=dict(category='expression',dimension='register',value='口语较多',evidence_ids=[rows[0]['id']],conflict_ids=[],rationale='合成',confidence='low',sufficient=True,scope=d._evidence_scope('friend-A'))
        for key,value in [('account','account-B'),('scope','group:x'),('sender','other')]:
            bad=[dict(rows[0],**{key:value})]+rows[1:]
            with self.assertRaises(ValueError):d.validate([item],bad,'friend-A')

    def test_cross_account_or_date_sample_rejected_before_model(self):
        for key,value in [('account','other-account'),('scope','chat:other'),('time',self.end+1)]:
            rows=[dict(r,**{key:value}) for r in self.sample_rows()]
            with patch.object(d,'sample',return_value=rows):
                with self.assertRaises(ValueError):d.preview('friend-A',self.start,self.end)
        self.assertEqual(d.list_jobs('friend-A'),[])

    def test_temporary_feedback_not_persistent_reception(self):
        rows=[dict(r,text='今天这次你解释太长了') for r in self.sample_rows()]
        with patch.object(self,'sample_rows',return_value=rows):
            state=self.analyzed(category='reception',dimension='length',value='简短')
        self.assertFalse(state['items'][0]['sufficient'])

    def test_repeated_feedback_across_snippets_can_form_draft(self):
        rows=[dict(r,text='你回复太长了') for r in self.sample_rows()]
        with patch.object(self,'sample_rows',return_value=rows):
            state=self.analyzed(category='reception',dimension='length',value='简短')
        self.assertTrue(state['items'][0]['sufficient'])
        self.assertEqual(p.get('friend-A')['preferences'],{})

    def test_counter_evidence_must_also_belong_to_allowed_batch(self):
        preview=self.preview()
        with self.model(mutate=lambda i,r:i.update(conflict_ids=['other-user-id'])):
            with self.assertRaises(ValueError):d.run_batch(preview['job']['id'],0,True)

    def test_banned_labels_and_unbounded_text_rejected(self):
        for mutate in [lambda i,r:i.update(value='情感依赖'),lambda i,r:i.update(rationale='这个人容易被说服'),lambda i,r:i.update(confidence=.98)]:
            preview=self.preview()
            with self.model(mutate=mutate):
                with self.assertRaises(ValueError):d.run_batch(preview['job']['id'],0,True)

    def test_insufficient_and_empty_samples_allow_no_suggestion(self):
        with patch.object(d,'sample',return_value=[]):preview=d.preview('friend-A',self.start,self.end)
        self.assertEqual(preview['job']['expected_calls'],0);self.assertEqual(preview['job']['status'],'insufficient')
        with self.assertRaises(ValueError):d.run_batch(preview['job']['id'],0,True)
        one=self.sample_rows()[:2]
        with patch.object(d,'sample',return_value=one):preview=d.preview('friend-A',self.start,self.end)
        with self.model():d.run_batch(preview['job']['id'],0,True)
        self.assertFalse(d.detail(preview['job']['id'])['items'][0]['sufficient'])

    def test_manual_lock_and_concurrent_edit_protected(self):
        state=self.analyzed(category='reception',dimension='length',value='简短');iid=state['items'][0]['id']
        self.change('friend-A',preferences={'length':{'value':'详细','locked':True}})
        with self.assertRaises(p.Conflict):d.review(iid,'accept',0)
        with self.assertRaises(p.Conflict):d.review(iid,'accept',1)
        self.assertEqual(p.get('friend-A')['preferences']['length']['value'],'详细')

    def test_locked_length_also_blocks_strategy(self):
        self.change('friend-A',preferences={'length':{'value':'详细','locked':True}})
        state=self.analyzed(category='strategy',dimension='structure',value='先给结论，按需展开')
        item=state['items'][0];self.assertTrue(item['locked_conflict'])
        with self.assertRaises(p.Conflict):d.review(item['id'],'accept',1)

    def test_review_modify_audit_undo_then_reapply_forbidden(self):
        state=self.analyzed(category='reception',dimension='length',value='简短');iid=state['items'][0]['id']
        result=d.review(iid,'accept',0,value='适中')
        self.assertEqual(p.get('friend-A')['preferences']['length']['value'],'适中')
        with self.assertRaises(p.Conflict):d.review(iid,'accept',1)
        undone=d.undo(result['application_id'],1)
        self.assertEqual(undone['revision'],2);self.assertEqual(p.get('friend-A')['preferences'],{})
        self.assertEqual([a['source'] for a in p.audit('friend-A')],['analysis_undo','analysis_review'])

    def test_undo_cannot_overwrite_later_manual_edit(self):
        state=self.analyzed();result=d.review(state['items'][0]['id'],'accept',0)
        self.change('friend-A',persona_id='Q')
        with self.assertRaises(p.Conflict):d.undo(result['application_id'],2)
        self.assertEqual(p.get('friend-A')['persona_id'],'Q')

    def test_rejected_suggestion_suppressed_in_future_jobs(self):
        state=self.analyzed();d.review(state['items'][0]['id'],'reject',0)
        newer=self.analyzed();self.assertEqual(newer['items'],[])
        self.assertEqual(p.get('friend-A')['revision'],0)

    def test_budget_reservation_failures_and_explicit_retry(self):
        preview=self.preview(max_calls=2);jid=preview['job']['id']
        with self.assertRaises(ValueError):d.run_batch(jid,0)
        self.assertEqual(d.detail(jid)['job']['calls_reserved'],0)
        with patch('core.llm.chat',side_effect=TimeoutError) as model:
            with self.assertRaises(ValueError):d.run_batch(jid,0,True)
            self.assertEqual(model.call_count,1)
        with self.assertRaises(p.Conflict):d.run_batch(jid,0,True)
        with self.model():d.run_batch(jid,0,True,True)
        state=d.detail(jid);self.assertEqual(state['job']['calls_reserved'],2)
        self.assertEqual(len(state['batches'][0]['attempt_history']),2)
        with patch('core.llm.chat',side_effect=forbidden):self.assertEqual(d.run_batch(jid,0,True)['model_calls'],0)

    def test_hard_budget_exhaustion_no_model_call(self):
        preview=self.preview(max_calls=1);jid=preview['job']['id']
        with patch('core.llm.chat',side_effect=TimeoutError):
            with self.assertRaises(ValueError):d.run_batch(jid,0,True)
        with self.assertRaises(p.Conflict):d.run_batch(jid,0,True,True)
        with self.assertRaises(ValueError):self.preview(token_budget=2000)

    def test_restart_inflight_reservation_requires_explicit_unknown_ack(self):
        preview=self.preview();jid=preview['job']['id']
        with patch('core.llm.chat',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):d.run_batch(jid,0,True)
        self.assertEqual(d.detail(jid)['batches'][0]['status'],'running')
        with self.assertRaises(p.Conflict):d.run_batch(jid,0,True,True)
        sessions._current=None;sessions.observe()
        with self.assertRaises(p.Conflict):d.run_batch(jid,0,True)
        with self.model():d.run_batch(jid,0,True,True)
        self.assertEqual(d.detail(jid)['job']['calls_reserved'],2)

    def test_model_account_switch_never_writes_result_to_new_account(self):
        preview=self.preview();jid=preview['job']['id']
        def switch(*a,**kw):self.account='account-B';sessions.observe();return '{"items":[]}'
        with patch('core.llm.chat',side_effect=switch):
            with self.assertRaises(sessions.StaleAccount):d.run_batch(jid,0,True)
        self.assertEqual(d.list_jobs('friend-A'),[])
        self.account='account-A';sessions.observe()
        self.assertEqual(d.detail(jid)['batches'][0]['status'],'running')
        self.assertEqual(d.detail(jid)['items'],[])

    def test_single_attempt_chat_adapter_has_no_hidden_http_retry(self):
        # Execute the real adapter with a synthetic HTTP function; no network.
        real_chat=self._real_chat
        calls=[]
        def post(url,headers,body,proxy,**kwargs):
            calls.append((body,kwargs));return {'choices':[{'message':{'content':'{"items":[]}'}}]}
        with patch('core.llm._post',side_effect=post):
            self.assertEqual(real_chat('synthetic',[{'role':'user','content':'synthetic'}],dict(self.cfg,single_attempt=True)),'{"items":[]}')
        self.assertEqual(len(calls),1);self.assertEqual(calls[0][1],{'retries':0})
        self.assertEqual(calls[0][0]['model'],'configured-name')

    def test_model_route_change_requires_new_preview(self):
        state=self.preview();self.cfg['gpt']['model']='another-model'
        with self.assertRaises(p.Conflict):d.run_batch(state['job']['id'],0,True)
        self.assertEqual(d.detail(state['job']['id'])['job']['calls_reserved'],0)

    def test_concurrent_batches_preserve_total_reserved_budget(self):
        rows=[]
        for i in range(30):rows.append(dict(self.sample_rows()[i%9],local_id=i,message_id='mid-'+str(i),snippet=i//3))
        with patch.object(d,'sample',return_value=rows):state=d.preview('friend-A',self.start,self.end,max_calls=3,token_budget=180000)
        jid=state['job']['id'];barrier=threading.Barrier(2);errors=[]
        def model(*a,**kw):barrier.wait(3);return '{"items":[]}'
        def execute(i):
            try:d.run_batch(jid,i,True)
            except Exception as e:errors.append(e)
        with patch('core.llm.chat',side_effect=model) as mock:
            threads=[threading.Thread(target=execute,args=(i,)) for i in (0,1)]
            for thread in threads:thread.start()
            for thread in threads:thread.join(5)
            self.assertEqual(mock.call_count,2)
        self.assertEqual(errors,[])
        final=d.detail(jid);self.assertEqual(final['job']['calls_reserved'],2)
        self.assertEqual([b['status'] for b in final['batches']],['done','done'])


class Sampling(DraftBase):
    def test_date_stratified_text_and_voice_exclude_other_sender_self_system(self):
        path=self.history(1);con=sqlite3.connect(path);table=messages.msg_table('friend-A');con.execute('DELETE FROM '+table)
        raw='合成语音转写'.encode();sub=b'\x08\x02\x12'+bytes([len(raw)])+raw;packed=b'\x2a'+bytes([len(sub)])+sub
        rows=[]
        for i in range(4):
            base=self.start+i*86400+100
            rows.extend([(i*10+1,i*10+1,1,1,base,'日常表达','',None),(i*10+2,i*10+2,34,1,base+1,'<voice/>','',packed),
                         (i*10+3,i*10+3,1,2,base+2,'机器人主动跟进','',None),(i*10+4,i*10+4,10000,1,base+3,'系统通知','',None),
                         (i*10+5,i*10+5,1,3,base+4,'其他联系人','',None)])
        con.executemany(f'INSERT INTO {table} VALUES(?,?,?,?,?,?,?,?)',rows);con.commit();con.close()
        sample=d.sample('friend-A',self.start,self.end,4)
        self.assertEqual(len(sample),8);self.assertEqual(len({r['snippet'] for r in sample}),4)
        self.assertEqual(sum(r['source']=='wechat_transcript' for r in sample),4)
        self.assertFalse(any('机器人' in r['text'] or '其他联系人' in r['text'] for r in sample))
        with self.assertRaises(ValueError):d.sample('room@chatroom',self.start,self.end)


class Closure(Isolated):
    def setUp(self):
        super().setUp()
        self.change('friend-A',conversation_control_enabled=True)

    def test_exact_endings_persist_and_do_not_touch_profile(self):
        state=cs.observe('friend-A',[msg(text='先这样，拜拜')])
        self.assertTrue(state['paused']);self.assertFalse(state['no_proactive'])
        self.assertIsNone(cs.ticket('friend-A',[msg(text='先这样，拜拜')]))
        sessions._current=None;sessions.observe()
        self.assertTrue(cs.get('friend-A')['paused']);self.assertEqual(p.get('friend-A')['revision'],1)

    def test_quotes_negations_hypotheses_topic_not_closure(self):
        for text in ['他说拜拜','如果我说拜拜','不是说不想聊了','不要说拜拜','“先这样，拜拜”','这个话题先不聊了']:
            self.assertNotIn(cs.classify(text),['closed','no_proactive'],text)
        self.assertEqual(cs.classify('先换个话题'),'topic_only')

    def test_new_real_emoji_voice_text_restore_session_not_longterm(self):
        cs.observe('friend-A',[msg(text='不想聊了')])
        for i,typ in enumerate([47,34,3,1],2):
            state=cs.observe('friend-A',[msg(i,type=typ,text='[合成消息]')]);self.assertFalse(state['paused'])
        state=cs.observe('friend-A',[msg(9,text='以后不要主动给我发消息')]);self.assertTrue(state['no_proactive'])
        state=cs.observe('friend-A',[msg(10,text='请回答一个问题')]);self.assertTrue(state['no_proactive'])
        state=cs.observe('friend-A',[msg(11,text='以后可以主动联系我')]);self.assertFalse(state['no_proactive'])

    def test_delayed_native_transcript_closes_existing_voice(self):
        cs.observe('friend-A',[msg(type=34,text='[语音]')])
        state=cs.observe('friend-A',[msg(type=34,voice_transcript='先这样，拜拜',voice_transcript_source='wechat_packed_v1')])
        self.assertTrue(state['paused'])
        self.assertEqual(state['revision'],2)

    def test_own_system_other_and_account_scope(self):
        cs.observe('friend-A',[msg(text='不想聊了')])
        for extra in [{'is_self':True},{'type':10000},{'sender':'friend-B'},{'scheduled':True}]:
            cs.observe('friend-A',[msg(2,text='你好',**extra)])
            self.assertTrue(cs.get('friend-A')['paused'])
        self.assertFalse(cs.get('friend-B')['paused'])
        self.account='account-B';sessions.observe();self.assertFalse(cs.get('friend-A')['paused'])

    def nudge(self,callback=None):
        now=time.time();rows=[msg(1,text='你好',create_time=now-100000),msg(2,text='已有正常回复',sender='account-A',is_self=True,create_time=now-99000)]
        def generated(*a,**kw):
            if callback:callback()
            return '合成主动回复'
        with patch('core.llm.chat',side_effect=generated) as model,patch('core.llm.available',return_value=True), \
                patch('core.bot._quiet_now',return_value=False),patch('core.bot._sender_name',return_value='对方'), \
                patch('core.bot.send_name_for',return_value='同名'),patch('core.schedule.is_scheduled_msg',return_value=False), \
                patch('core.conversation_state.latest',side_effect=lambda *a:rows),patch('core.sender.send_text',return_value={'status':'confirmed'}) as send:
            result=bot._maybe_nudge('friend-A',rows,self.rules,{},lambda *a:None,now)
            return result,model.call_count,send.call_count

    def test_closed_nudge_does_not_generate(self):
        cs.observe('friend-A',[msg(3,text='先这样，拜拜')])
        self.assertEqual(self.nudge(),(False,0,0))

    def test_model_inflight_end_or_new_inbound_stops_send(self):
        for text in ['先这样，拜拜','你好，又来问一个问题']:
            with p._db(True) as con:con.execute('DELETE FROM conversation_state')
            self.assertEqual(self.nudge(lambda:cs.observe('friend-A',[msg(3,text=text)])),(False,1,0))

    def test_regular_reply_allowed_during_longterm_no_proactive(self):
        cs.observe('friend-A',[msg(text='以后不要主动给我发消息')])
        with patch('core.bot._ai_reply',return_value='合成普通回复'),patch('core.bot.send_name_for',return_value='对方'), \
                patch('core.sender.send_text',return_value={'status':'confirmed'}) as send:
            r=bot.do_action(self.rules['rules'][0],msg(2,text='问题'), 'friend-A',{},lambda *a:None,rules=self.rules)
        self.assertEqual(r['status'],'confirmed');self.assertEqual(send.call_count,1)
        self.assertNotIn('proactive_ticket',send.call_args.kwargs)

    def test_schedule_not_connected_to_conversation_closure(self):
        from core import schedule
        cs.observe('friend-A',[msg(text='以后不要主动给我发消息')])
        with patch('core.conversation_state.allowed',side_effect=forbidden),patch('core.conversation_state.ticket',side_effect=forbidden):
            self.assertEqual(schedule._compute_text({'prompt':'独立提醒','use_llm':False}),'独立提醒')
        self.assertNotIn('conversation_state',Path('core/schedule.py').read_text())


class Coordinator(Isolated):
    def setUp(self):
        super().setUp()
        from test_send_safety import Adapter
        self.adapter=Adapter(self.token);self.adapter.proof['chat']='friend-A'
        mp=patch('core.sender._adapter',self.adapter);mp.start();self.addCleanup(mp.stop)
        self.change('friend-A',conversation_control_enabled=True)
        self.gate=cs.ticket('friend-A',[msg()])

    def send(self):
        return sender._send('name','friend-A','text','synthetic',job_id='proactive-1',proactive_ticket=self.gate)

    def test_final_coordinator_check_catches_change_during_navigation(self):
        def opened(*a):cs.observe('friend-A',[msg(2,text='不想聊了')]);return True
        self.adapter.open=opened
        with patch('core.conversation_state.latest',return_value=[]):result=self.send()
        self.assertEqual(self.adapter.calls,0);self.assertEqual(result['reason'],'proactive_context_changed')

    def test_durable_retry_retains_original_consent_ticket(self):
        self.adapter.open_ok=False
        with patch('core.conversation_state.latest',return_value=[]):result=self.send()
        self.assertEqual(result['reason'],'open_failed');self.assertEqual(self.adapter.calls,0)
        cs.observe('friend-A',[msg(2,text='以后不要主动给我发消息')]);self.adapter.open_ok=True
        ledger=send_ledger.Ledger();ledger.update('proactive-1','not_sent','open_failed',0)
        with patch('core.conversation_state.latest',return_value=[]):result=sender.retry('proactive-1')
        self.assertEqual(self.adapter.calls,0);self.assertEqual(result['reason'],'proactive_context_changed')

    def test_final_read_fails_closed_not_paid_or_sent(self):
        with patch('core.conversation_state.latest',side_effect=OSError):result=self.send()
        self.assertEqual(self.adapter.calls,0);self.assertEqual(result['status'],'not_sent')


class API(DraftBase):
    def setUp(self):
        super().setUp();import server
        self.client=server.app.test_client();self.headers={'X-Wxbot-Admin-Token':'synthetic'}
        for target,value in [('core.contacts.list_contacts',lambda:[{'username':'friend-A','name':'合成对象'}]),('core.contacts.list_groups',lambda:[]),('core.distill.list_personas',lambda:[])]:
            mp=patch(target,value);mp.start();self.addCleanup(mp.stop)
        mp=patch.dict(os.environ,{'WXBOT_ADMIN_READ_TOKEN':'synthetic'});mp.start();self.addCleanup(mp.stop)

    def post(self,path,**body):
        return self.client.post('/api/personalization/analysis/'+path,json=dict(session=sessions.capture(),**body),headers=self.headers)

    def test_loading_page_and_management_reads_no_scan_or_model(self):
        with patch.object(d,'sample',side_effect=forbidden),patch('core.llm.chat',side_effect=forbidden):
            r=self.client.get('/personalization');self.assertEqual(r.status_code,200);r.close()
            self.assertEqual(self.client.get('/api/personalization',headers=self.headers).status_code,200)
            self.assertEqual(self.client.get('/api/personalization/contact?contact=friend-A',headers=self.headers).status_code,200)
        self.assertFalse(Path(p._path()).exists())

    def test_unauthorized_data_or_model_request_rejected(self):
        self.assertEqual(self.client.get('/api/personalization/analysis/detail?id=x').status_code,403)
        self.assertEqual(self.client.post('/api/personalization/analysis/run',json={'id':'x'}).status_code,403)

    def test_explicit_preview_run_review_undo_api(self):
        with patch.object(d,'sample',return_value=self.sample_rows()):
            response=self.post('preview',contact='friend-A',start=self.start,end=self.end)
        self.assertEqual(response.status_code,200);jid=response.json['job']['id']
        self.assertEqual(self.post('run',id=jid,ordinal=0).status_code,400)
        with self.model():self.assertEqual(self.post('run',id=jid,ordinal=0,confirm_model_call=True).status_code,200)
        item=d.detail(jid)['items'][0]
        accepted=self.post('review',item_id=item['id'],action='accept',revision=0)
        self.assertEqual(accepted.status_code,200)
        self.assertEqual(self.post('undo',application_id=accepted.json['application_id'],revision=1).status_code,200)


if __name__=='__main__':unittest.main()
