"""Offline legacy integration checks: synthetic accounts and no real sending."""
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import unittest
from unittest.mock import patch

from test_send_safety import Isolated, forbidden
from core import sender, send_ledger, sendq, docker_wx, schedule, bot
from core.legacy_sender import LegacySearchAdapter


class Legacy(Isolated):
    def setUp(self):
        super().setUp()
        p = patch.object(sender, '_adapter', LegacySearchAdapter())
        p.start(); self.addCleanup(p.stop)
        p = patch.object(docker_wx, '_exec', return_value=SimpleNamespace(returncode=0, stdout='OK\n'))
        self.execute = p.start(); self.addCleanup(p.stop)

    def contacts(self, rows):
        path = Path(self.tmp.name) / 'contacts.sqlite3'
        with sqlite3.connect(path) as con:
            con.execute('CREATE TABLE contact (username TEXT, alias TEXT, remark TEXT, nick_name TEXT)')
            con.executemany('INSERT INTO contact VALUES (?,?,?,?)', rows)
        def connect(_):
            con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
            con.row_factory = sqlite3.Row
            return con
        return patch('core.db.connect', side_effect=connect)

    def test_legacy_script_opens_search_hit_without_clicking_media_area(self):
        src = Path('docker/wx_send_legacy.py').read_text(encoding='utf-8')
        open_fn = src[src.index('def open_chat'):src.index('def focus_input')]
        send_fn = src[src.index('def send_text'):src.index('def send_image')]
        self.assertIn('click_search_hit', open_fn)
        self.assertIn('header_matches', src)
        self.assertIn('searched_by_wechat_id', open_fn)
        self.assertIn('ERR:search-unfocused', src)
        self.assertIn('ERR:chat-not-opened', src)
        self.assertIn('ERR:wrong-chat', src)
        self.assertIn('windowfocus', src)
        self.assertIn('搜索聊天记录', src)
        self.assertNotIn('search_still_open', src)
        self.assertNotIn('click_search(px, py)\n    return search_box_holds', src)
        self.assertNotIn('key("Down")', open_fn)
        self.assertIn('key("Return")', open_fn)
        self.assertIn('key("Escape")', open_fn)
        self.assertNotIn('px + w // 2', open_fn)
        self.assertNotIn('py + h // 2', open_fn)
        self.assertIn('clear_input()', send_fn)
        self.assertNotIn('key("Escape")', send_fn)
        id_open = open_fn[open_fn.index('if searched_by_wechat_id'):open_fn.index('# 群名/备注')]
        self.assertIn('click_search_hit', id_open)
        self.assertIn('clicks=2', id_open)
        self.assertIn('ERR:chat-not-opened', id_open)
        self.assertNotIn('ERR:wrong-chat', id_open)
        self.assertNotIn('key("Return")', id_open)
        self.assertIn('--repeat', src)

    def test_unfocused_search_is_not_sent_to_the_visible_chat(self):
        self.execute.return_value = SimpleNamespace(returncode=3, stdout='ERR:search-unfocused\n')
        r = self.send()
        self.assertEqual(r['status'], 'not_sent')
        self.assertEqual(r['reason'], 'legacy_search_unfocused')
        self.send(); self.assertEqual(self.execute.call_count, 1)

    def test_unfocused_window_is_not_sent(self):
        self.execute.return_value = SimpleNamespace(returncode=3, stdout='ERR:no-focus\n')
        r = self.send()
        self.assertEqual(r['status'], 'not_sent')
        self.assertEqual(r['reason'], 'legacy_no_focus')

    def test_search_hit_without_opening_chat_is_not_sent_to_the_visible_chat(self):
        with self.contacts([('chat-A', 'unique_a', 'same name', 'A')]):
            self.execute.return_value = SimpleNamespace(returncode=3, stdout='ERR:chat-not-opened\n')
            r = self.send()
        self.assertEqual(r['status'], 'not_sent')
        self.assertEqual(r['reason'], 'legacy_chat_not_opened')
        self.send(); self.assertEqual(self.execute.call_count, 1)

    def test_header_ocr_typo_still_matches_expected_chat(self):
        src = Path('docker/wx_send_legacy.py').read_text(encoding='utf-8')
        start = src.index('def header_matches')
        end = src.index('\ndef open_chat')
        ns = {'__name__': 'header'}
        exec(compile(src[start:end], 'header.py', 'exec'), ns)
        self.assertTrue(ns['header_matches']('老浪one', '老婆one'))
        self.assertTrue(ns['header_matches']('老婆two', '老婆two'))
        self.assertFalse(ns['header_matches']('猫来了小队', '老婆two'))
        self.assertTrue(ns['searched_by_wechat_id']('suwanwan2005'))
        self.assertTrue(ns['searched_by_wechat_id']('unique_a'))
        self.assertFalse(ns['searched_by_wechat_id']('二奶'))
        self.assertFalse(ns['searched_by_wechat_id']('same name'))
        self.assertFalse(ns['searched_by_wechat_id']('group@chatroom'))

    def test_wrong_header_is_not_sent(self):
        self.execute.return_value = SimpleNamespace(returncode=3, stdout='ERR:wrong-chat\n')
        r = self.send()
        self.assertEqual(r['status'], 'not_sent')
        self.assertEqual(r['reason'], 'legacy_wrong_chat')
        self.send(); self.assertEqual(self.execute.call_count, 1)

    def test_same_remark_contacts_use_their_distinct_wechat_ids(self):
        with self.contacts([('chat-A', 'unique_a', 'same name', 'A'),
                            ('chat-B', 'unique_b', 'same name', 'B')]):
            self.assertEqual(self.send()['status'], 'submitted')
            r = sender.send_text('same name', 'body', chat_username='chat-B')
            self.assertEqual(r['status'], 'submitted')
        self.assertEqual([call.args[3] for call in self.execute.call_args_list],
                         ['unique_a', 'unique_b'])

    def test_missing_alias_uses_original_display_name(self):
        with self.contacts([('chat-A', '', 'same name', 'A')]):
            self.send()
        self.assertEqual(self.execute.call_args.args[3], 'same name')

    def test_image_also_searches_by_wechat_id(self):
        image = Path(self.tmp.name) / 'image.png'; image.write_bytes(b'synthetic')
        with self.contacts([('chat-A', 'unique_a', 'same name', 'A')]), patch.object(
                docker_wx, 'LOCAL', True):
            r = sender.send_image('same name', str(image), chat_username='chat-A')
        self.assertEqual(r['status'], 'submitted')
        self.assertEqual(self.execute.call_args.args[2:4], ('image', 'unique_a'))

    def test_original_transport_once_without_native_identity_or_receipt(self):
        with patch('core.native_sender.NativeContactAdapter.open', forbidden), patch.object(
                sender._adapter, 'receipt', forbidden):
            r = self.send()
            again = self.send()
        self.assertEqual(r['status'], 'submitted')
        self.assertTrue(r['ok']); self.assertFalse(r['verified'])
        self.assertFalse(r['retryable']); self.assertEqual(again, r)
        self.execute.assert_called_once_with('python3', '/usr/local/bin/wx_send_legacy.py',
                                            'text', 'same name', 'test-body', 'same name', timeout=40)

    def test_timeout_is_never_retried(self):
        self.execute.side_effect = TimeoutError()
        self.assertEqual(self.send()['status'], 'uncertain')
        self.send(); sender.retry('job-1', self.token); sender.reconcile('job-1', self.token)
        self.assertEqual(self.execute.call_count, 1)

    def test_missing_window_is_not_sent_and_not_replayed(self):
        self.execute.return_value = SimpleNamespace(returncode=2, stdout='ERR:no-window\n')
        self.assertEqual(self.send()['status'], 'not_sent')
        self.send(); self.assertEqual(self.execute.call_count, 1)

    def test_group_text_uses_original_display_name(self):
        self.assertIsNone(sender.preflight('group@chatroom'))
        r = sender.send_text('group name', 'body', chat_username='group@chatroom')
        self.assertEqual(r['status'], 'submitted')
        self.assertEqual(self.execute.call_args.args[3:5], ('group name', 'body'))

    def test_image_copy_before_single_send(self):
        image = Path(self.tmp.name) / 'image.png'; image.write_bytes(b'synthetic')
        with patch.object(docker_wx, 'LOCAL', False), patch.object(docker_wx, '_docker',
                return_value=SimpleNamespace(returncode=0)) as cp:
            r = sender.send_image('name', str(image), chat_username='chat-A')
        self.assertEqual(r['status'], 'submitted'); cp.assert_called_once()
        self.assertEqual(self.execute.call_args.args[2:4], ('image', 'name'))

    def test_failed_copy_never_sends(self):
        image = Path(self.tmp.name) / 'image.png'; image.write_bytes(b'synthetic')
        with patch.object(docker_wx, 'LOCAL', False), patch.object(docker_wx, '_docker',
                return_value=SimpleNamespace(returncode=1)):
            r = sender.send_image('name', str(image), chat_username='chat-A')
        self.assertEqual(r['status'], 'not_sent'); self.execute.assert_not_called()

    def test_account_change_does_not_send(self):
        self.switch('account-B')
        self.assertEqual(self.send()['status'], 'stale')
        self.execute.assert_not_called()

    def test_different_body_cannot_reuse_submitted_job(self):
        self.send()
        r = sender.send_text('same name', 'different', chat_username='chat-A',
                             job_id='job-1', session=self.token)
        self.assertEqual(r['reason'], 'job_identity_conflict')
        self.execute.assert_called_once()

    def test_submitted_reply_clears_pending_batch(self):
        pending = self.pending()
        with patch.object(bot, 'do_action', return_value={
                'status': 'submitted', 'reason': 'legacy_ui_submitted'}), patch.object(bot, '_maybe_learn'):
            bot.process_pending('chat-A', pending, {}, lambda *a: None)
        self.assertNotIn('chat-A', bot._pending)

    def test_success_is_terminal_in_web_queue_without_delivery_claim(self):
        self.send(); status = sendq.status('job-1')
        self.assertEqual(status['status'], 'done')
        self.assertEqual(status['send_status'], 'submitted')
        self.assertFalse(status['verified'])

    def test_at_is_not_silently_changed_into_plain_text(self):
        r = sender.send_at('group', 'group@chatroom', 'member', 'name', 'body')
        self.assertEqual(r['reason'], 'legacy_payload_not_supported')
        self.execute.assert_not_called()

    def test_schedule_submission_is_terminal_without_replay(self):
        with patch.object(schedule, '_record_fire') as record:
            r = schedule.fire(self.task(), occurrence='once:test', session=self.token)
            again = schedule.fire(self.task(), occurrence='once:test', session=self.token)
        self.assertEqual(r['status'], 'submitted'); self.assertEqual(again['status'], 'submitted')
        self.execute.assert_called_once(); record.assert_called_once()


if __name__ == '__main__':
    unittest.main()
