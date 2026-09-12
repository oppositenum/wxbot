"""Opt-in and lifecycle regressions: temporary accounts, no network or real UI."""
import copy
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from test_contact_personalization import Isolated, msg, forbidden
from core import personalization as p, conversation_state as cs, sender, bot, send_ledger, account_session as sessions

REAL_PREFLIGHT = sender.preflight


class Defaults(Isolated):
    def test_unconfigured_features_have_no_side_effect_or_hidden_gate(self):
        data = p.get('friend-A')
        for field in ('personalization_enabled', 'auto_update', 'conversation_control_enabled'):
            self.assertIs(data[field], False)
        self.assertEqual(p.learn_live('friend-A', [msg()]), 0)
        self.assertFalse(cs.observe('friend-A', [msg(text='先这样，拜拜')])['paused'])
        with patch.object(cs, 'latest', side_effect=forbidden):
            gate = cs.ticket('friend-A')
            self.assertTrue(cs.allowed(gate))
        self.assertEqual(p.preferences_context('friend-A'), '')
        self.assertFalse(Path(p._path()).exists())
        self.assertEqual(self.role()['persona_id'], 'P')

    def test_persona_only_save_does_not_enable_other_features(self):
        self.change('friend-A', persona_id='Q')
        self.assertEqual(self.role()['persona_id'], 'Q')
        self.assertFalse(p.get('friend-A')['auto_update'])
        self.assertFalse(p.get('friend-A')['personalization_enabled'])
        self.assertFalse(p.get('friend-A')['conversation_control_enabled'])
        self.assertEqual(self.role('friend-B')['persona_id'], 'P')

    def test_explicit_switches_independent_and_account_scoped(self):
        self.change('friend-A', auto_update=True)
        self.assertEqual(p.learn_live('friend-A', [msg()]), 1)
        self.assertEqual(p.preferences_context('friend-A'), '')
        self.change('friend-A', personalization_enabled=True)
        self.assertIn('简短', p.preferences_context('friend-A'))
        self.assertNotIn('简短', p.preferences_context('friend-A', '请详细回答'))
        self.assertFalse(p.get('friend-B')['personalization_enabled'])
        self.account='account-B'; sessions.observe()
        self.assertFalse(p.get('friend-A')['personalization_enabled'])

    def legacy(self, source):
        data=dict(p._default(), auto_update=True, personalization_enabled=True)
        data['preferences']['length']=dict(value='简短', source='user_explicit', evidence_ids=['1'], scope='chat:friend-A')
        with p._db(True) as con:
            p._put(con, 'friend-A', data, source)

    def test_old_automatic_defaults_are_not_mistaken_for_manual_configuration(self):
        self.legacy('user_explicit')
        before=Path(p._path()).read_bytes()
        self.assertFalse(p.get('friend-A')['auto_update'])
        self.assertEqual(p.preferences_context('friend-A'), '')
        self.assertIn('length', p.get('friend-A')['preferences'])
        self.assertEqual(before, Path(p._path()).read_bytes())

    def test_old_manual_switches_and_role_are_preserved(self):
        self.legacy('admin_manual')
        self.assertTrue(p.get('friend-A')['auto_update'])
        self.assertIn('简短', p.preferences_context('friend-A'))
        self.change('friend-A', persona_id='Q')
        self.assertTrue(p.get('friend-A')['personalization_enabled'])

    def test_closure_enabled_only_for_selected_contact_and_setting_change_invalidates_ticket(self):
        off=cs.ticket('friend-A', [])
        self.change('friend-A', conversation_control_enabled=True)
        self.assertFalse(cs.allowed(off, refresh=False))
        cs.observe('friend-A', [msg(text='先这样，拜拜')])
        self.assertIsNone(cs.ticket('friend-A', []))
        cs.observe('friend-B', [msg(chat='friend-B', sender='friend-B', text='拜拜')])
        self.assertFalse(cs.get('friend-B')['paused'])
        self.change('friend-A', conversation_control_enabled=False)
        with patch.object(cs, 'latest', side_effect=forbidden):
            self.assertTrue(cs.allowed(cs.ticket('friend-A')))
        self.assertTrue(cs.get('friend-A')['paused'])  # Disable does not erase evidence.

    def test_enabled_closure_and_longterm_consent_persist_across_restart(self):
        self.change('friend-A', conversation_control_enabled=True)
        cs.observe('friend-A', [msg(text='以后不要主动给我发消息')])
        sessions._current=None; sessions.observe()
        cs.observe('friend-A', [msg(2, text='我有个问题')])
        self.assertIsNone(cs.ticket('friend-A', []))
        self.assertTrue(cs.get('friend-A')['no_proactive'])


class GenerationAndQueue(Isolated):
    def setUp(self):
        super().setUp()
        mp=patch.object(sender, 'preflight', REAL_PREFLIGHT); mp.start(); self.addCleanup(mp.stop)
        mp=patch.object(sender, '_adapter', sender.UnavailableIdentityAdapter()); mp.start(); self.addCleanup(mp.stop)
        bot.load_pending()

    def test_greet_unavailable_does_not_read_media_or_call_model(self):
        with patch.object(bot.messages, 'get_messages', side_effect=forbidden), patch.object(cs, 'ticket', side_effect=forbidden):
            result=bot.greet('friend-A')
        self.assertEqual(result['status'], 'not_sent')
        self.assertEqual(result['reason'], 'cannot_confirm_target')

    def test_pat_and_nudge_unavailable_do_not_generate(self):
        self.assertEqual(bot._reply_pat('friend-A', msg(), self.rules, lambda *a: None)['status'], 'not_sent')
        self.assertFalse(bot._maybe_nudge('friend-A', [], self.rules, {}, lambda *a: None))
        self.assertEqual(bot._last_pat, {})

    def test_blocked_batch_not_regenerated_and_new_inbound_is_separate(self):
        row=bot.enqueue_pending('friend-A', msg(), [msg()], self.rules['rules'][0], 1)
        for _ in range(3):bot.process_pending('friend-A', row, self.rules, lambda *a: None)
        self.assertEqual(row['send_status'], 'not_sent')
        old_id=row['job_id']
        new=bot.enqueue_pending('friend-A', msg(2), [msg(),msg(2)], self.rules['rules'][0], 2)
        self.assertEqual([m['local_id'] for m in new['msgs']], [2])
        self.assertNotIn('job_id', new)
        with send_ledger.Ledger().connect() as con:
            saved=con.execute('SELECT * FROM held_replies WHERE id=?', (old_id,)).fetchone()
            self.assertEqual(json.loads(saved['data'])['send_status'], 'not_sent')
        bot.save_pending();sessions._current=None;sessions.observe();bot.load_pending()
        self.assertEqual(bot._pending['friend-A']['send_status'], 'stale')
        self.assertEqual(len(bot._pending['friend-A']['msgs']), 1)

    def test_uncertain_and_old_epoch_preserved_not_replayed(self):
        old=dict(msgs=[msg()],ctx=[msg()],rule=self.rules['rules'][0],session=self.token,
                 job_id='uncertain-old',send_status='uncertain',reason='receipt_not_available')
        bot._pending['friend-A']=old
        bot.process_pending('friend-A', old, self.rules, lambda *a: None)
        new=bot.enqueue_pending('friend-A', msg(2), [msg(2)], self.rules['rules'][0], 2)
        self.assertEqual(len(new['msgs']), 1)
        with send_ledger.Ledger().connect() as con:
            saved=con.execute('SELECT data FROM held_replies WHERE id=?', ('uncertain-old',)).fetchone()
            self.assertEqual(json.loads(saved['data'])['send_status'], 'uncertain')

    def test_failed_archive_keeps_original_queue(self):
        old=dict(msgs=[msg()],session=self.token,send_status='failed')
        bot._pending['friend-A']=old
        with patch.object(send_ledger.Ledger,'hold_reply',side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):bot.enqueue_pending('friend-A', msg(2), [], self.rules['rules'][0], 2)
        self.assertIs(bot._pending['friend-A'],old)

    def test_schedule_preflight_no_paid_generation_no_task_mutation(self):
        from core import schedule
        task=dict(id='task', created=1, target_username='friend-A', prompt='synthetic', use_llm=True)
        before=copy.deepcopy(task)
        with patch.object(schedule,'_compute_text',side_effect=forbidden):
            first=schedule.fire(task, 'same-due-time')
            second=schedule.fire(task, 'same-due-time')
        self.assertEqual(first['status'],'not_sent')
        self.assertEqual(first['job_id'],second['job_id'])
        self.assertEqual(task,before)

    def test_draw_image_unavailable_never_generates(self):
        from core import tools
        with patch('core.read_access.valid',return_value=True):
            result=tools.draw_image('synthetic',dict(chat='friend-A'))
        self.assertIn('未生成、未发送',result)


class Lifecycle(Isolated):
    def setUp(self):
        super().setUp()
        import server
        self.server=server
        self.client=server.app.test_client()
        self.saved=dict(server._bot)
        self.addCleanup(lambda: server._bot.update(self.saved))
        server._bot.update(running=False,thread=None)

    def test_start_stop_start_uses_single_worker(self):
        class Worker:
            created=0
            def __init__(self, **kw):Worker.created+=1;self.alive=False
            def start(self):self.alive=True
            def is_alive(self):return self.alive
        with patch.object(self.server.threading,'Thread',Worker):
            for _ in range(4):
                self.client.post('/api/bot/start')
                self.client.post('/api/bot/stop')
            self.client.post('/api/bot/start')
        self.assertEqual(Worker.created,1)
        self.assertTrue(self.server._bot['running'])

    def test_old_harvest_endpoint_never_operates_ui_or_changes_bot_state(self):
        for running in (True,False):
            self.server._bot['running']=running
            with patch('core.harvest.harvest',side_effect=forbidden), patch.object(self.server.threading,'Thread',side_effect=forbidden):
                response=self.client.post('/api/harvest',json={'name':'synthetic'})
            self.assertEqual(response.status_code,200)
            self.assertFalse(response.json['ui_navigation'])
            self.assertEqual(self.server._bot['running'],running)

    def test_capture_does_not_toggle_running_or_restart_worker(self):
        self.server._bot['running']=True
        def work(**kw):
            self.assertTrue(self.server._bot['running'])
            raise OSError('synthetic')
        with patch('core.imgdec.img_key',return_value=''), patch('core.docker_wx.capture_img_key',side_effect=work),patch.object(self.server.threading,'Thread',side_effect=forbidden):
            response=self.client.post('/api/keys/capture_img',json={})
        self.assertEqual(response.status_code,200)
        self.assertTrue(self.server._bot['running'])


if __name__=='__main__':unittest.main()
