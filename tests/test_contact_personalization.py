"""Synthetic, isolated integration tests. Never contact models, WeChat or production DBs."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from core import personalization as p, account_session as sessions, bot, memory, media_read, messages
from core.voice_text import from_packed


def forbidden(*a, **kw):
    raise AssertionError('network / UI / real model calls forbidden')


def msg(i=1, text='以后短一点', **extra):
    return dict(dict(local_id=i, server_id=10000+i, type=1, chat='friend-A', sender='friend-A', is_self=False,
                     create_time=int(time.time()) + i, content=text), **extra)


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='wx-contact-test-')
        self.addCleanup(self.tmp.cleanup)
        self.account = 'account-A'
        self.roles = {'P': {'name': '角色 P', 'persona': 'ROLE_P', 'samples': []},
                      'Q': {'name': '角色 Q', 'persona': 'ROLE_Q', 'samples': []}}
        self.rules = {'watch': ['friend-A'], 'rules': [{'name': 'old', 'match': {'type': 'auto'}, 'action': {'type': 'reply_ai', 'persona': 'P'}}]}
        for target, value in [('config.ACCOUNTS_DIR', self.tmp.name+'/accounts'), ('config.WORK_DIR', self.tmp.name+'/work'),
                              ('config.wxid', lambda: self.account), ('core.distill.load_persona', lambda slug: copy.deepcopy(self.roles.get(slug))),
                              ('core.bot.load_rules', lambda: copy.deepcopy(self.rules)), ('core.llm.load_cfg', lambda: {}),
                              ('socket.socket.connect', forbidden), ('socket.create_connection', forbidden),
                              ('subprocess.run', forbidden), ('subprocess.Popen', forbidden), ('core.llm._post', forbidden),
                              ('core.llm.chat', forbidden), ('core.llm.gen_image', forbidden), ('core.llm.transcribe', forbidden),
                              ('core.sender.preflight', lambda *a, **kw: None), ('core.sender.send_text', forbidden), ('core.sender.send_image', forbidden), ('core.sender.send_at', forbidden)]:
            mp = patch(target, value); mp.start(); self.addCleanup(mp.stop)
        sessions._current = None
        self.token = sessions.capture()
        bot._media_desc.clear()
        bot._last_pat.clear()
        bot._pending.clear()

    def change(self, who, **patch_):
        return p.update(who, patch_, p.get(who)['revision'])

    def role(self, who='friend-A'):
        return p.resolve_persona(who, self.rules)

    def history(self, n=65):
        root=Path(config.decrypted_dir());root.mkdir(parents=True, exist_ok=True)
        con=sqlite3.connect(root/'message.db')
        con.execute('CREATE TABLE Name2Id(user_name TEXT)')
        con.executemany('INSERT INTO Name2Id VALUES(?)', [('friend-A',), ('account-A',), ('someone-else',)])
        table=messages.msg_table('friend-A')
        con.execute(f'CREATE TABLE {table}(local_id INTEGER PRIMARY KEY,server_id INTEGER,local_type INTEGER,real_sender_id INTEGER,create_time INTEGER,message_content TEXT,source TEXT,packed_info_data BLOB)')
        con.executemany(f'INSERT INTO {table} VALUES(?,?,?,?,?,?,?,?)',
                        [(i,10000+i,1,1,int(time.time())+i,'以后短一点','',None) for i in range(1,n+1)])
        con.commit();con.close()
        return root/'message.db'


class Personas(Isolated):
    def test_dynamic_global_and_explicit_and_clear(self):
        self.assertEqual(self.role()['source'], 'legacy_global')
        p.set_global('P', 0, self.rules)
        self.assertEqual(self.role()['source'], 'global')
        self.change('friend-A', persona_id='Q')
        self.assertEqual(self.role()['persona_id'], 'Q')
        self.assertEqual(self.role('friend-B')['persona_id'], 'P')
        p.set_global('Q', 1, self.rules)
        self.assertEqual(self.role('friend-B')['persona_id'], 'Q')
        p.set_global('P', 2, self.rules)
        self.assertEqual(self.role()['persona_id'], 'Q')
        self.change('friend-A', persona_id=None)
        self.assertEqual(self.role()['persona_id'], 'P')

    def test_same_name_renaming_and_two_accounts(self):
        self.change('friend-A', persona_id='Q')
        self.assertEqual(self.role('friend-B')['persona_id'], 'P')
        self.account = 'account-B';sessions.observe()
        self.assertEqual(self.role()['persona_id'], 'P')
        self.change('friend-A', persona_id='P')
        self.account = 'account-A';sessions.observe()
        self.assertEqual(self.role()['persona_id'], 'Q')

    def test_deleted_corrupt_and_no_global_fallback(self):
        self.change('friend-A', persona_id='Q')
        del self.roles['Q']
        r=self.role();self.assertEqual(r['source'], 'fallback');self.assertIn('不存在', r['error'])
        self.roles['Q']={'persona':42}
        self.assertEqual(self.role()['persona'], p.DEFAULT_PERSONA)
        self.rules={'rules':[]}
        self.assertEqual(self.role('friend-B')['source'], 'builtin')

    def test_broken_config_safe(self):
        self.change('friend-A', persona_id='Q')
        con=sqlite3.connect(p._path());con.execute("UPDATE settings SET data='[]'");con.commit();con.close()
        self.assertEqual(self.role()['source'], 'fallback')
        self.assertEqual(p.preferences_context('friend-A'), '')
        # Explicit owner repair retains revision check.
        self.change('friend-A', persona_id=None)
        self.assertEqual(self.role()['source'], 'legacy_global')

    def test_corrupt_sqlite_does_not_crash_reply_resolution(self):
        Path(p._path()).write_bytes(b'not a sqlite database')
        self.assertEqual(self.role()['source'], 'fallback')

    def test_unique_legacy_migration_preserves_effect_and_no_real_auto_write(self):
        before=self.role()['persona'];self.assertFalse(Path(p._path()).exists())
        p.set_global('P', 0, self.rules)
        self.assertEqual(self.role()['persona'], before)
        self.rules['rules'][0]['action']['persona']='Q'
        self.assertEqual(self.role()['persona_id'], 'P')  # no hidden rule override

    def test_conflicting_rules_preserve_exact_trigger_and_refuse_migration(self):
        other={'name':'second', 'action':{'type':'reply_ai','persona':'Q'},'match':{'type':'keyword','value':'x'}}
        self.rules['rules'].append(other)
        inv=p.legacy_inventory(self.rules)
        self.assertTrue(inv['conflict']);self.assertEqual([e['index'] for e in inv['entries']], [0,1])
        self.assertEqual(p.resolve_persona('friend-A',self.rules,other)['persona_id'],'Q')
        self.assertEqual(self.role()['source'], 'legacy_rule_conflict')
        with self.assertRaises(p.Conflict):p.set_global('P',0,self.rules)
        self.change('friend-A', persona_id='P')
        self.assertEqual(p.resolve_persona('friend-A',self.rules,other)['source'],'contact')
        self.assertEqual(p.resolve_persona('friend-A',self.rules,other)['persona_id'],'P')

    def test_group_conflicting_legacy_rules_do_not_switch_by_speaker(self):
        other={'name':'second','action':{'type':'reply_ai','persona':'Q'}}
        self.rules['rules'].append(other)
        a=p.resolve_persona('room@chatroom',self.rules,self.rules['rules'][0])
        b=p.resolve_persona('room@chatroom',self.rules,other)
        self.assertEqual(a['persona_id'],'P');self.assertEqual(b['persona_id'],'Q')
        self.assertEqual(a['source'],'legacy_rule_conflict');self.assertTrue(a['legacy']['conflict'])

    def test_first_migration_must_preserve_unique_old_role(self):
        with self.assertRaises(p.Conflict):p.set_global('Q',0,self.rules)
        self.assertFalse(Path(p._path()).exists())

    def test_group_role_never_follows_speaker_or_private_preferences(self):
        self.change('friend-A',persona_id='Q',preferences={'tone':{'value':'直接'}})
        self.assertEqual(self.role('room@chatroom')['persona_id'],'P')
        self.assertEqual(p.preferences_context('room@chatroom'), '')
        self.change('room@chatroom',persona_id='Q')
        self.assertEqual(self.role('room@chatroom')['persona_id'],'Q')
        with self.assertRaises(ValueError):self.change('room@chatroom',preferences={'tone':{'value':'温和'}})

    def test_stale_task_cannot_save_or_resume_on_aba(self):
        with sessions.bind(self.token):
            self.account='account-B';sessions.observe()
            self.account='account-A';sessions.observe()
            with self.assertRaises(sessions.StaleAccount):p.update('friend-A',{'persona_id':'Q'},0)
        self.assertEqual(p.get('friend-A')['revision'],0)


class Preferences(Isolated):
    def test_locked_manual_wins(self):
        self.change('friend-A',auto_update=True,personalization_enabled=True)
        self.change('friend-A',preferences={'length':{'value':'详细','locked':True}})
        self.assertEqual(p.learn_live('friend-A',[msg()]),0)
        pref=p.get('friend-A')['preferences']['length']
        self.assertEqual((pref['source'],pref['value']),('admin_manual','详细'))

    def test_new_explicit_replaces_old_inferred_and_records_evidence(self):
        self.change('friend-A',auto_update=True,personalization_enabled=True)
        with p._db(True) as con:
            d=p._get(con,'friend-A');d['preferences']['length']=dict(value='详细',source='inferred',confidence=.4,updated=1,scope='chat:friend-A',evidence_ids=['old1','old2','old3'])
            p._put(con,'friend-A',d,'inferred')
        self.assertEqual(p.learn_live('friend-A',[msg()]),1)
        pref=p.get('friend-A')['preferences']['length']
        self.assertEqual(pref['value'],'简短');self.assertEqual(pref['source'],'user_explicit');self.assertEqual(pref['evidence_ids'],['10001'])
        self.assertEqual(len(p.audit('friend-A')),3)
        self.assertEqual(p.learn_live('friend-A',[msg()]),0)

    def test_temporary_negated_quoted_emotion_and_short_questions_not_permanent(self):
        for text in ['这次简短说','今天回复短一点','我今天没心情聊天，先休息了。','嗯','为什么','他说以后短一点','以后不要回复短一点','以后短一点？','以后如果我很忙就短一点','“以后短一点”']:
            self.assertEqual(p.extract('friend-A',msg(text=text)),{},text)
        self.assertEqual(p.extract('friend-A',msg()),{'length':'简短'})
        self.assertEqual(p.extract('friend-A',msg(text='以后请叫我小云。')),{'address':'小云'})

    def test_system_own_schedule_fiction_quote_wrong_scope_filtered(self):
        for extra in [{'is_self':True},{'sender':'other'},{'chat':'other'},{'type':10000},{'type':49},
                      {'origin':'scheduled'},{'system':True},{'scheduled':True},{'fictional':True},{'refer':{'x':1}}]:
            self.assertEqual(p.extract('friend-A',msg(**extra)),{},str(extra))
        self.assertEqual(p.extract('room@chatroom',msg(chat='room@chatroom')), {})

    def test_older_explicit_does_not_replace_newer(self):
        self.change('friend-A',auto_update=True,personalization_enabled=True)
        p.learn_live('friend-A',[msg(2,'以后回复详细一点')])
        p.learn_live('friend-A',[msg(1)])
        self.assertEqual(p.get('friend-A')['preferences']['length']['value'],'详细')

    def test_stop_update_disable_adaptation_delete(self):
        self.change('friend-A',auto_update=True,personalization_enabled=True)
        self.change('friend-A',auto_update=False)
        self.assertEqual(p.learn_live('friend-A',[msg()]),0)
        self.change('friend-A',auto_update=True)
        self.assertEqual(p.learn_live('friend-A',[msg()]),1)
        self.assertIn('简短',p.preferences_context('friend-A'))
        self.change('friend-A',personalization_enabled=False)
        self.assertEqual(p.preferences_context('friend-A'),'')
        self.change('friend-A',preferences={'length':None})
        self.assertFalse(p.get('friend-A')['preferences'])
        # Already-consumed evidence cannot resurrect an administrator's deletion.
        p.learn_live('friend-A',[msg()]);self.assertFalse(p.get('friend-A')['preferences'])

    def test_current_detail_request_and_bounded_preferences(self):
        self.change('friend-A',auto_update=True,personalization_enabled=True)
        self.change('friend-A',preferences={'length':{'value':'简短'},'tone':{'value':'温和'},'emoji':{'value':'少量'},
                                            'language':{'value':'中文'},'followup':{'value':'少追问'},'advice':{'value':'先听对方说'}})
        context=p.preferences_context('friend-A','请详细解释步骤')
        self.assertNotIn('回复长度',context)
        self.assertLessEqual(context.count('：'),4)
        self.assertIn('当前明确要求优先',context)

    def test_revision_conflict_and_concurrent_manual_update(self):
        results=[]
        def edit(v):
            try:p.update('friend-A',{'preferences':{'tone':{'value':v,'locked':True}}},0);results.append('ok')
            except p.Conflict:results.append('conflict')
        ts=[threading.Thread(target=edit,args=(v,)) for v in ['直接','温和']]
        for t in ts:t.start()
        for t in ts:t.join()
        self.assertCountEqual(results,['ok','conflict']);self.assertEqual(p.get('friend-A')['revision'],1)

    def test_role_switch_and_late_fact_write_do_not_drop_preferences_or_facts(self):
        profile=memory.load_profile('friend-A');profile['facts']=[{'text':'真实事实','scope':'chat:friend-A','status':'active'}]
        memory.save_profile(profile)
        stale=memory.load_profile('friend-A')
        self.change('friend-A',persona_id='Q',preferences={'tone':{'value':'直接','locked':True}})
        memory.save_profile(stale)
        self.assertEqual(memory.load_profile('friend-A')['facts'][0]['text'],'真实事实')
        self.assertEqual(p.get('friend-A')['preferences']['tone']['value'],'直接')


class Jobs(Isolated):
    def test_preview_no_write_and_zero_model_calls(self):
        self.history()
        r=p.preview_history('friend-A',65)
        self.assertEqual((r['lo'],r['hi'],r['total'],r['estimated_calls']),(0,65,65,0))
        self.assertFalse(Path(p._path()).exists())
        with self.assertRaises(ValueError):p.preview_history('friend-A',1001)

    def test_progress_restart_no_repeated_completed_range(self):
        self.change('friend-A',auto_update=True,personalization_enabled=True)
        self.history();r=p.preview_history('friend-A',65);jid=p.create_job('friend-A',r['lo'],r['hi'],r['total'])
        calls=[];original=p.history_rows
        def read(*a,**kw):calls.append(a[1]);return original(*a,**kw)
        with patch.object(p,'history_rows',read):
            self.assertEqual(p.step_job(jid)['cursor'],50)
            sessions._current=None;sessions.observe()  # process restart epoch
            self.assertEqual(p.step_job(jid)['cursor'],65)
            self.assertEqual(p.step_job(jid)['status'],'done')
        self.assertEqual(calls,[0,50])
        self.assertEqual(p.preview_history('friend-A',100)['total'],0)
        self.assertEqual(p.jobs()[0]['scanned'],65)

    def test_failed_chunk_rolls_back_preferences_seen_and_cursor_then_recovers(self):
        self.change('friend-A',auto_update=True,personalization_enabled=True)
        self.history();jid=p.create_job('friend-A',0,65,65)
        original=p._ingest
        def broken(*args):original(*args);raise OSError('synthetic commit interruption')
        with patch.object(p,'_ingest',broken):
            with self.assertRaises(OSError):p.step_job(jid)
        self.assertEqual(p.get('friend-A')['revision'],1)
        job=p.jobs()[0];self.assertEqual((job['cursor'],job['scanned'],job['status'],job['attempts']),(0,0,'failed',1))
        p.step_job(jid);self.assertEqual(p.jobs()[0]['cursor'],50)
        self.assertEqual(p.get('friend-A')['preferences']['length']['evidence_ids'],['10050'])

    def test_account_switch_during_scan_rolls_back(self):
        self.change('friend-A',auto_update=True,personalization_enabled=True)
        self.history();jid=p.create_job('friend-A',0,65,65);read=p.history_rows
        def changed(*a,**kw):
            rows=read(*a,**kw);self.account='account-B';sessions.observe();return rows
        with patch.object(p,'history_rows',changed):
            with self.assertRaises(sessions.StaleAccount):p.step_job(jid)
        self.assertEqual(p.jobs(),[])  # New account has no previous task.
        self.account='account-A';sessions.observe()
        self.assertEqual(p.jobs()[0]['cursor'],0)
        self.assertEqual(p.get('friend-A')['revision'],1)

    def test_live_failed_update_keeps_independent_recoverable_intent(self):
        self.change('friend-A',auto_update=True,personalization_enabled=True)
        self.history(2)
        with patch.object(p,'_ingest',side_effect=OSError('synthetic write failure')):
            with self.assertRaises(OSError):p.learn_live('friend-A',[msg(1),msg(2)])
        job=p.jobs()[0]
        self.assertEqual((job['kind'],job['status'],job['cursor']),('live','failed',0))
        self.assertEqual(p.get('friend-A')['revision'],1)
        sessions._current=None;sessions.observe()
        self.assertEqual(p.step_job(job['id'])['status'],'done')
        self.assertEqual(p.get('friend-A')['preferences']['length']['value'],'简短')
        # Live consumption must not skip older records in an explicit historical build.
        self.assertEqual(p.preview_history('friend-A',2)['lo'],0)

    def test_disabled_update_pauses_and_unavailable_range_not_done(self):
        path=self.history();jid=p.create_job('friend-A',0,65,65)
        self.change('friend-A',auto_update=False)
        with self.assertRaises(p.Conflict):p.step_job(jid)
        self.assertEqual(p.jobs()[0]['cursor'],0)
        self.change('friend-A',auto_update=True)
        con=sqlite3.connect(path);con.execute('DELETE FROM '+messages.msg_table('friend-A'));con.commit();con.close()
        with self.assertRaises(p.Conflict):p.step_job(jid)
        self.assertEqual(p.jobs()[0]['status'],'failed')

    def test_real_reader_filters_sender_type_and_does_not_read_media(self):
        path=self.history(4);con=sqlite3.connect(path);table=messages.msg_table('friend-A')
        con.execute(f'UPDATE {table} SET real_sender_id=2 WHERE local_id=2')
        con.execute(f'UPDATE {table} SET local_type=10000 WHERE local_id=3')
        con.execute(f'UPDATE {table} SET real_sender_id=3 WHERE local_id=4')
        con.commit();con.close()
        r=p.preview_history('friend-A',100)
        self.assertEqual([x['message_id'] for x in r['proposals']],['10001'])


class Entries(Isolated):
    def test_reply_greet_pat_nudge_share_contact_role(self):
        self.rules['proactive'] = {'enabled': True, 'private_share_enabled': True}
        self.change('friend-A',personalization_enabled=True)
        self.change('friend-A',persona_id='Q',preferences={'tone':{'value':'温和'}})
        systems=[];sends=[]
        def chat(system,*a,**kw):systems.append(system);return 'synthetic reply'
        def send(*a,**kw):sends.append(kw['chat_username']);return {'ok':True,'status':'confirmed'}
        context=[msg(1,'你好',create_time=time.time()-100000),msg(2,'你好',is_self=True,sender='account-A',create_time=time.time()-99000)]
        with patch('core.llm.chat',chat), patch('core.llm.available',return_value=True), patch('core.sender.send_text',send), \
                patch('core.bot.send_name_for',return_value='same display'),patch('core.bot._sender_name',return_value='对方'), \
                patch('core.memory.select_memories',return_value=[]),patch('core.agent.agent_config',return_value={'enabled':False}), \
                patch('core.messages.get_messages',return_value=context),patch('core.conversation_state.latest',return_value=context),patch('core.schedule.is_scheduled_msg',return_value=False), \
                patch('core.send_ledger.result',return_value=None),patch('core.bot._quiet_now',return_value=False):
            bot.do_action(self.rules['rules'][0],msg(), 'friend-A',{},lambda *a:None,context,self.rules)
            bot.greet('friend-A')
            bot._reply_pat('friend-A',msg(),self.rules,lambda *a:None)
            self.assertTrue(bot._maybe_nudge('friend-A',context,self.rules,{},lambda *a:None))
        self.assertEqual(len(systems),4);self.assertEqual(sends,['friend-A']*4)
        for system in systems:
            self.assertIn('ROLE_Q',system);self.assertNotIn('ROLE_P',system);self.assertIn('温和',system)
            self.assertLess(system.index('【通用要求】'),system.index('【本轮机器人角色】'))
            self.assertLess(system.index('ROLE_Q'),system.index('【当前相关交流偏好】'))

    def test_group_reply_uses_group_memory_scope_and_no_private_preferences(self):
        self.change('friend-A',preferences={'dislikes':{'value':'PRIVATE_STYLE'}})
        captured=[]
        with patch('core.llm.chat',side_effect=lambda system,*a:captured.append(system) or 'mock'), \
                patch('core.bot._sender_name',return_value='群成员'),patch('core.memory.select_memories',return_value=[]) as select, \
                patch('core.agent.agent_config',return_value={'enabled':False}):
            bot._ai_reply(self.roles['P'],'room@chatroom',msg(),[],self.rules)
        self.assertEqual(select.call_args.kwargs['scope'],'group:room@chatroom')
        self.assertNotIn('PRIVATE_STYLE',captured[0])

    def test_reply_current_request_order_and_old_role_not_control(self):
        self.change('friend-A',preferences={'length':{'value':'简短'}})
        captured=[]
        with patch('core.llm.chat',side_effect=lambda s,m:captured.append((s,m)) or 'mock'), \
                patch('core.bot._sender_name',return_value='对方'),patch('core.memory.select_memories',return_value=[]), \
                patch('core.agent.agent_config',return_value={'enabled':False}),patch('core.schedule.is_scheduled_msg',return_value=False):
            bot._ai_reply(self.roles['Q'],'friend-A',msg(text='请详细说明每一步'),
                          [msg(1,'旧角色指令 ROLE_P',is_self=True,sender='account-A')],self.rules)
        system,user=captured[0]
        self.assertNotIn('ROLE_P',system);self.assertIn('旧机器人回复只供理解',system)
        self.assertNotIn('回复长度：简短',system);self.assertNotIn('回 1~2 句',user[0]['content'])
        self.assertIn('请详细说明每一步',user[0]['content'])


class Voice(Isolated):
    @staticmethod
    def packed(text,status=2):
        raw=text.encode();sub=b'\x08'+bytes([status])+b'\x12'+bytes([len(raw)])+raw
        return b'\x2a'+bytes([len(sub)])+sub

    def test_native_voice_success_without_stt_or_audio_getter(self):
        transcript=from_packed(self.packed('合成测试语音'))
        self.assertEqual(transcript,'合成测试语音')
        m=msg(type=34,voice_transcript=transcript,voice_transcript_source='wechat_packed_v1')
        with patch('core.media.get_msg_voice',side_effect=forbidden):
            r=media_read.read('friend-A',m,{'enable_stt':False})
        self.assertEqual(r.status,'success');self.assertIn(transcript,r.context())
        self.assertIn(transcript,bot._passive_content('friend-A',m))

    def test_native_voice_enters_final_reply_context_without_audio_request(self):
        m=msg(type=34,content='[语音]',voice_transcript='请详细解释合成测试',voice_transcript_source='wechat_packed_v1')
        captured=[]
        with patch('core.llm.chat',side_effect=lambda system,history:captured.append(history) or 'mock'), \
                patch('core.media.get_msg_voice',side_effect=forbidden),patch('core.bot._sender_name',return_value='对方'), \
                patch('core.memory.select_memories',return_value=[]),patch('core.agent.agent_config',return_value={'enabled':False}):
            self.assertEqual(bot._ai_reply(self.roles['P'],'friend-A',m,[],self.rules),'mock')
        self.assertIn('请详细解释合成测试',captured[0][0]['content'])
        self.assertNotIn('语音未读取',captured[0][0]['content'])

    def test_unknown_partial_invalid_native_voice_not_claimed_read(self):
        for raw in [None,b'garbage',self.packed('合成',1),self.packed(''),self.packed('合成')+b'\x80']:self.assertEqual(from_packed(raw),'')
        self.assertEqual(media_read.read('friend-A',msg(type=34,voice_transcript='untrusted'),{}) .status,'disabled')

    def test_database_message_reader_reuses_native_transcript(self):
        path=self.history(1);con=sqlite3.connect(path);con.execute('UPDATE '+messages.msg_table('friend-A')+' SET local_type=34,message_content=?,packed_info_data=?',('<msg><voicemsg voicelength="1000"/></msg>',self.packed('合成语音')));con.commit();con.close()
        with patch('core.messages._contact_names',return_value={}),patch('core.messages._resolve_revokes'):
            rows=messages.get_messages('friend-A',1)
        self.assertEqual(rows[0]['voice_transcript'],'合成语音')
        self.assertEqual(media_read.read('friend-A',rows[0],{}).text,'合成语音')


class API(Isolated):
    def setUp(self):
        super().setUp()
        import server
        self.client=server.app.test_client()
        for target,value in [('core.contacts.list_contacts',lambda:[{'username':'friend-A','name':'同名'},{'username':'friend-B','name':'同名'}]),
                             ('core.contacts.list_groups',lambda:[{'username':'room@chatroom','name':'测试群'}]),
                             ('core.distill.list_personas',lambda:[{'slug':k,'name':v['name']} for k,v in self.roles.items()])]:
            m=patch(target,value);m.start();self.addCleanup(m.stop)
        m=patch.dict(os.environ,{'WXBOT_ADMIN_READ_TOKEN':'synthetic-token'});m.start();self.addCleanup(m.stop)
        self.headers={'X-Wxbot-Admin-Token':'synthetic-token'}

    def post(self,path,**body):
        return self.client.post('/api/personalization'+path,json=dict(session=self.token,**body),headers=self.headers)

    def test_page_load_auth_and_no_model_no_history_no_storage_creation(self):
        with patch.object(p,'history_rows',side_effect=forbidden):
            self.assertEqual(self.client.get('/personalization').status_code,200)
            self.assertEqual(self.client.get('/api/personalization').status_code,403)
            r=self.client.get('/api/personalization',headers=self.headers)
            self.assertEqual(r.status_code,200);self.assertEqual(len(r.json['contacts']),3)
            self.assertEqual(self.client.get('/api/personalization/contact?contact=friend-A',headers=self.headers).status_code,200)
        self.assertFalse(Path(p._path()).exists())

    def test_account_token_and_id_authorization_mutation(self):
        self.assertEqual(self.post('/contact',contact='unknown',patch={},revision=0).status_code,400)
        self.assertEqual(self.post('/contact',contact='friend-A',patch={'persona_id':'Q'},revision=0).status_code,200)
        self.assertEqual(self.post('/contact',contact='friend-A',patch={},revision=0).status_code,409)
        self.account='account-B';sessions.observe()
        self.assertEqual(self.post('/contact',contact='friend-A',patch={},revision=0).status_code,409)
        self.assertEqual(p.get('friend-A')['revision'],0)

    def test_preview_redacted_zero_calls_and_global_migration_gate(self):
        self.change('friend-A',preferences={'address':{'value':'私密称呼'}})
        r=self.post('/preview',contact='friend-A',query='称呼')
        self.assertEqual(r.status_code,200);self.assertEqual(r.json['model_calls'],0)
        self.assertNotIn('私密称呼',r.text);self.assertNotIn('ROLE_P',r.text)
        self.assertEqual(self.post('/global',persona_id='P',revision=0).status_code,400)
        self.assertEqual(self.post('/global',persona_id='P',revision=0,accept_migration=True).status_code,200)
        self.assertEqual(self.client.post('/api/bot/persona',json={'slug':'Q'}).status_code,409)
        self.assertEqual(self.role()['persona_id'],'P')

    def test_batch_preview_limits_and_no_jobs_just_from_read(self):
        self.history(2)
        r=self.post('/history/preview',contacts=['friend-A'],limit=2)
        self.assertEqual(r.status_code,200);self.assertEqual(r.json['model_calls'],0)
        self.assertFalse(Path(p._path()).exists())
        self.change('friend-A',auto_update=True,personalization_enabled=True)
        self.assertEqual(self.post('/history/preview',contacts=['friend-A']*11,limit=2).status_code,400)
        self.assertEqual(self.post('/history/preview',contacts=['room@chatroom'],limit=2).status_code,400)
        jid=self.post('/history/create',**r.json['previews'][0]).json['id']
        self.assertEqual(self.post('/history/step',id=jid).json['job']['status'],'done')


if __name__=='__main__':
    unittest.main()
