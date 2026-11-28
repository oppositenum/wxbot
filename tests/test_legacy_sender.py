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
        send_fn = src[src.index('def send_text'):src.index('def just_open')]
        self.assertIn('activate_chat_window', send_fn)
        img_fn = src[src.index('def send_image'):src.index('def just_open')]
        self.assertIn('activate_chat_window', img_fn)
        self.assertIn('click_search_hit', open_fn)
        self.assertIn('header_matches', src)
        self.assertIn('searched_by_wechat_id', open_fn)
        self.assertIn('focus_search_box', src)
        self.assertIn('ERR:chat-not-opened', src)
        self.assertIn('ERR:wrong-chat', src)
        self.assertIn('windowfocus', src)
        self.assertIn('搜索聊天记录', src)
        self.assertIn('focus_search_box', open_fn)
        self.assertIn('keydown", "ctrl"', src)
        self.assertIn('keep_window_size', src)
        self.assertNotIn('SEARCH_SENTINEL', src)
        self.assertIn('set_clip_text(name)', open_fn)
        self.assertIn('UTF8_STRING', src[src.index('def set_clip_text'):src.index('def set_clip_image')])
        self.assertIn('key("ctrl+a")', open_fn)
        self.assertIn('key("ctrl+v")', open_fn)
        self.assertNotIn('xdotool", "type"', open_fn)
        self.assertIn('search_shows_query', open_fn)
        self.assertNotIn('search_still_open', src)
        self.assertNotIn('click_search(px, py)\n    return search_box_holds', src)
        self.assertNotIn('key("Down")', open_fn)
        self.assertIn('_group_row_y', open_fn)
        self.assertIn('群聊', open_fn)
        group_open = open_fn[open_fn.index('# 群名'):]
        self.assertIn('clicks=1', group_open)
        self.assertIn('clicks=2', group_open)
        self.assertIn('row_dy = group_y - py', group_open)
        self.assertNotIn('key("Return")', group_open)
        self.assertIn('dismiss_search', group_open)
        self.assertNotIn('click_search_hit(px, py,', group_open)
        self.assertIn('ERR:wrong-chat', group_open)
        # Esc 统一走 esc():4.1.9+ 主窗口有焦点时 Esc 会收进托盘,esc() 会点托盘恢复
        self.assertIn('esc()', open_fn)
        self.assertNotIn('key("Escape")', open_fn)
        self.assertNotIn('px + w // 2', open_fn)
        self.assertNotIn('py + h // 2', open_fn)
        self.assertIn('clear_input()', send_fn)
        self.assertIn('already_on_chat', send_fn)
        self.assertNotIn('key("Escape")', send_fn)
        self.assertNotIn('esc()', send_fn)
        id_open = open_fn[open_fn.index('if searched_by_wechat_id'):open_fn.index('# 群名')]
        self.assertIn('key("Return")', id_open)
        self.assertNotIn('click_search_hit', id_open)
        self.assertIn('ERR:chat-not-opened', id_open)
        self.assertIn('_header_conflicts', id_open)
        self.assertEqual(id_open.count('key("Return")'), 1)
        self.assertIn('--repeat', src)
        self.assertIn('before = header_text', open_fn)
        self.assertIn('already_on_chat', open_fn)
        self.assertIn('def _header_box', src)
        self.assertIn('px + 8, py + 30, px + right, py + 68', src)
        self.assertIn('detached_chat_id', src)
        self.assertIn('_window_title_is', src)
        self.assertIn(r'\(\d+\)', src)

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

    def test_self_chat_searches_display_name_and_checks_header(self):
        with patch('config.wxid', return_value='wxid_me'):
            self.assertTrue(sender.is_self_chat('wxid_me'))
            self.assertFalse(sender.is_self_chat('wxid_wife'))
            self.assertEqual(sender.search_key('wxid_me', '🐮🐮🌱demo'),
                             ('🐮🐮🌱demo', '🐮🐮🌱demo'))
            r = sender.send_text('🐮🐮🌱demo', 'body', chat_username='wxid_me')
        self.assertEqual(r['status'], 'submitted')
        self.assertEqual(self.execute.call_args.args[3], '🐮🐮🌱demo')
        self.assertEqual(self.execute.call_args.args[5], '🐮🐮🌱demo')

    def test_header_ocr_typo_still_matches_expected_chat(self):
        src = Path('docker/wx_send_legacy.py').read_text(encoding='utf-8')
        start = src.index('def _ocr_title')
        end = src.index('\ndef header_crop_path')
        ns = {'__name__': 'header'}
        exec(compile(src[start:end], 'header.py', 'exec'), ns)
        self.assertTrue(ns['header_matches']('老浪one', '老婆one'))
        self.assertTrue(ns['header_matches']('老婆two', '老婆two'))
        self.assertTrue(ns['header_matches']('漂云', '漂云'))
        self.assertFalse(ns['header_matches']('老婆', '漂云'))
        self.assertFalse(ns['header_matches']('猫来了小队', '老婆two'))
        # 切号后顶栏「小乙🌱demo」：原图读成 EE'，放大后读成 小已sdemo。
        self.assertTrue(ns['header_matches']("EE'demo\n小已sdemo", '小乙🌱demo'))
        self.assertTrue(ns['header_matches']('小已sdemo', '小乙🌱demo'))
        self.assertFalse(ns['header_matches']("EE'demo", '小乙🌱demo'))
        self.assertFalse(ns['header_matches']('demo', '小乙🌱demo'))
        # 两个 demo 不能靠表情尾巴互认。小乙的顶栏不是 🐮🐮。
        self.assertFalse(ns['header_matches']('小已sdemo', '🐮🐮🌱demo'))
        self.assertFalse(ns['header_matches']("EE'demo", '🐮🐮🌱demo'))
        self.assertFalse(ns['header_matches']('小乙🌱demo', '🐮🐮🌱demo'))
        self.assertFalse(ns['header_matches']('demo', '🐮🐮🌱demo'))
        self.assertTrue(ns['header_matches']('🐮🐮🌱demo', '🐮🐮🌱demo'))
        self.assertTrue(ns['_header_conflicts']('小乙xyz', '🐮🐮🌱demo'))
        self.assertFalse(ns['_header_conflicts']("EE'demo", '🐮🐮🌱demo'))
        self.assertTrue(ns['searched_by_wechat_id']('alias2005'))
        self.assertTrue(ns['searched_by_wechat_id']('unique_a'))
        self.assertTrue(ns['searched_by_wechat_id']('abcdxy'))
        self.assertFalse(ns['searched_by_wechat_id']('二奶'))
        self.assertFalse(ns['searched_by_wechat_id']('same name'))
        self.assertFalse(ns['searched_by_wechat_id']('group@chatroom'))
        self.assertFalse(ns['searched_by_wechat_id']('Limit'))
        self.assertFalse(ns['searched_by_wechat_id']('ABC'))
        self.assertTrue(ns['searched_by_wechat_id']('aliasdemo1'))
        self.assertFalse(ns['searched_by_wechat_id']('wxid_abc123'))

    def test_group_row_ignores_web_results_and_chat_history(self):
        from PIL import Image
        src = Path('docker/wx_send_legacy.py').read_text(encoding='utf-8')
        start = src.index('def _ocr_rows')
        end = src.index('\ndef search_shows_query')
        ns = {'x': lambda *a, **k: SimpleNamespace(stdout=''), 'new_layout': lambda: False}
        exec(compile(src[start:end], 'group_row.py', 'exec'), ns)
        name = '南风闲话群'
        rows = [
            (20, '南风闲话群'),
            (52, '失忆后南风闲话群txt'),
            (84, '每天都在闲话群by'),
            (116, '每天都在闲话群by作者'),
            (150, '群聊'),
            (186, '南风闲话群'),
            (230, '搜索网络结果'),
            (270, 'demo'),
        ]
        self.assertEqual(ns['_section_label']('群聊'), 'group')
        self.assertEqual(ns['_section_label']('聊天记录'), 'history')
        self.assertEqual(ns['_section_label']('南风闲话群'), '')
        self.assertTrue(ns['_name_hits']('南风闲话群', name))
        self.assertFalse(ns['_name_hits']('失忆后南风闲话群txt', name))
        self.assertFalse(ns['_name_hits']('每天都在闲话群by', name))
        # 「群聊」和「搜索网络结果」之间只有一个头像，直接点它。
        panel = Image.new('RGB', (370, 600), (247, 247, 247))
        for y0 in (170,):
            for dy in range(36):
                for x0 in range(8, 46):
                    panel.putpixel((x0, y0 + dy), (180, 90, 70))
        self.assertEqual(ns['_group_row_from_rows'](rows, panel, name), 187)

    def test_group_row_finds_avatar_when_section_label_is_cropped(self):
        from PIL import Image
        src = Path('docker/wx_send_legacy.py').read_text(encoding='utf-8')
        start = src.index('def _ocr_rows')
        end = src.index('\ndef search_shows_query')
        ns = {'x': lambda *a, **k: SimpleNamespace(stdout=''), 'new_layout': lambda: False}
        exec(compile(src[start:end], 'group_row.py', 'exec'), ns)
        name = '南风闲话群'
        # 「群聊」被裁出画面。群在最上，下面是网络结果，再下面是单人头像的聊天记录。
        rows = [(90, name), (180, name), (230, '聊天记录'), (270, 'demo')]
        panel = Image.new('RGB', (370, 600), (247, 247, 247))
        for y0 in (76, 246):
            for dy in range(36):
                for x0 in range(8, 46):
                    panel.putpixel((x0, y0 + dy), (180, 90, 70))
        self.assertEqual(ns['_group_row_from_rows'](rows, panel, name), 93)
        # 裁图的 y 要加上裁图在屏幕上的起点。整屏图才用 box。
        saved = ns['_ocr_rows']
        ns['_ocr_rows'] = lambda _path: rows
        try:
            self.assertEqual(ns['_group_row_y'](panel, (0, 400, 370, 1000), 'unused', name), 93)
        finally:
            ns['_ocr_rows'] = saved

    def test_group_row_survives_garbled_web_heading(self):
        from PIL import Image
        src = Path('docker/wx_send_legacy.py').read_text(encoding='utf-8')
        start = src.index('def _ocr_rows')
        end = src.index('\ndef search_shows_query')
        ns = {'x': lambda *a, **k: SimpleNamespace(stdout=''), 'new_layout': lambda: False}
        exec(compile(src[start:end], 'group_row.py', 'exec'), ns)
        # 19:02 真实 OCR：「搜索网络结果」读成「网涅索络结果」，群名也被拆碎。
        rows = [(48, '群聊'), (88, '旨昌场'), (96, '南风闲话'),
                (136, '网'), (152, '索络结果'), (184, 'Q每天是闲话'), (560, '猫来了小队')]
        panel = Image.new('RGB', (370, 600), (247, 247, 247))
        for y0 in (86, 564):
            for dy in range(34):
                for x0 in range(8, 46):
                    panel.putpixel((x0, y0 + dy), (180, 90, 70))
        self.assertEqual(ns['_section_label']('网涅索络结果'), 'web')
        picked = ns['_group_row_from_rows'](rows, panel, '南风闲话群')
        self.assertGreaterEqual(picked, 86)
        self.assertLess(picked, 136)

    def test_group_row_refuses_when_group_section_missing(self):
        src = Path('docker/wx_send_legacy.py').read_text(encoding='utf-8')
        start = src.index('def _ocr_rows')
        end = src.index('\ndef search_shows_query')
        ns = {'x': lambda *a, **k: SimpleNamespace(stdout=''), 'new_layout': lambda: False}
        exec(compile(src[start:end], 'group_row.py', 'exec'), ns)
        from PIL import Image
        panel = Image.new('RGB', (370, 200), (247, 247, 247))
        rows = [(20, '南风闲话群'), (52, '失忆后南风闲话群txt')]
        self.assertIsNone(ns['_group_row_from_rows'](rows, panel, '南风闲话群'))

    def test_group_row_stops_at_the_search_card(self):
        from PIL import Image
        src = Path('docker/wx_send_legacy.py').read_text(encoding='utf-8')
        start = src.index('def _ocr_rows')
        end = src.index('\ndef search_shows_query')
        ns = {'x': lambda *a, **k: SimpleNamespace(stdout=''), 'new_layout': lambda: False}
        exec(compile(src[start:end], 'group_row.py', 'exec'), ns)
        # 浮层只有群这一行。裁图继续往下，扫进了会话列表的公众号头像。
        panel = Image.new('RGB', (370, 600), (222, 222, 222))
        for y in range(40, 139):
            for x in range(80, 330):
                panel.putpixel((x, y), (242, 242, 242))
        for dy in range(28):
            for x0 in range(12, 44):
                # 四宫格中间的浅色十字缝，下面列表里的单人头像没有这条缝。
                seam = dy == 14 or x0 == 27
                panel.putpixel((x0, 86 + dy), (236, 236, 236) if seam else (180, 90, 70))
                panel.putpixel((x0, 220 + dy), (20, 90, 200))
        # 真实 OCR 会把「群聊」裁掉，群名少最后一个字。
        rows = [(88, '旨昌场'), (96, '南风闲话')]
        picked = ns['_group_row_from_rows'](rows, panel, '南风闲话群')
        self.assertIsNotNone(picked)
        self.assertLess(picked, 150)

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

    def test_internal_wxid_alias_searches_nickname_instead(self):
        # 未设自定义微信号时 alias 是 wxid_xxx，搜索框搜不到，改搜昵称/备注。
        # aliasdemo1 这种用户自己设的微信号仍按微信号搜。
        with self.contacts([('wxid_foo', 'wxid_foo', '', '某人')]):
            self.assertEqual(sender.search_key('wxid_foo', '某人'), ('某人', '某人'))
            self.assertEqual(sender.search_key('wxid_foo', ''), ('某人', '某人'))
            # display 误传成内部号时，仍应落到库里的昵称
            self.assertEqual(sender.search_key('wxid_foo', 'wxid_foo'), ('某人', '某人'))
            r = sender.send_text('某人', 'body', chat_username='wxid_foo')
        self.assertEqual(r['status'], 'submitted')
        self.assertEqual(self.execute.call_args.args[3], '某人')
        self.assertEqual(self.execute.call_args.args[5], '某人')
        (Path(self.tmp.name) / 'contacts.sqlite3').unlink()
        with self.contacts([('wxid_friend0001', 'aliasdemo1', '', '小甲')]):
            self.assertEqual(sender.search_key('wxid_friend0001', '小甲'),
                             ('aliasdemo1', '小甲'))
        self.assertFalse(sender._searchable_alias('wxid_abc123'))
        self.assertTrue(sender._searchable_alias('aliasdemo1'))
        self.assertTrue(sender._searchable_alias('aliasdemo2'))

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
