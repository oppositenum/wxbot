import unittest
"""bot 回复发送方式切换(界面自动化 / 直发):离线,不连真微信。"""
from types import SimpleNamespace
from unittest.mock import patch

from test_send_safety import Isolated, forbidden
from core import sender, send_mode, send_ledger, docker_wx, direct_send
from core.legacy_sender import LegacySearchAdapter
from core.direct_sender import DirectSendAdapter

READY = {"attached": True, "logged_in": True}
DOWN = {"attached": False, "logged_in": False, "error": "微信没有在运行"}


class SendModeTest(Isolated):
    def setUp(self):
        super().setUp()
        self.legacy = LegacySearchAdapter()
        p = patch.object(sender, '_adapter', self.legacy)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(docker_wx, '_exec', return_value=SimpleNamespace(returncode=0, stdout='OK\n'))
        self.ui = p.start(); self.addCleanup(p.stop)

    def text(self, jid='job-1', body='hi'):
        return sender.send_text('demo', body, chat_username='wxid_a', job_id=jid, session=self.token)

    def test_default_mode_is_ui_and_does_not_touch_direct(self):
        self.assertEqual(send_mode.get()['mode'], 'ui')
        with patch('core.direct_send.send', forbidden), patch('core.direct_send.status', forbidden):
            r = self.text()
        self.assertTrue(r['ok'])
        self.assertEqual(r['reason'], 'legacy_ui_submitted')
        self.ui.assert_called_once()

    def test_direct_mode_sends_without_ui(self):
        send_mode.set('direct')
        with patch('core.direct_send.status', return_value=READY), \
             patch('core.direct_send.send', return_value={'ok': True}) as ds:
            r = self.text()
        self.assertTrue(r['ok'])
        self.assertEqual(r['reason'], 'direct_submitted')
        ds.assert_called_once_with('wxid_a', 'hi', 'demo', source='bot', at=None)
        self.ui.assert_not_called()

    def test_direct_unavailable_falls_back_to_ui_once(self):
        send_mode.set('direct', fallback=True)
        with patch('core.direct_send.status', return_value=DOWN), \
             patch('core.direct_send.send', forbidden):
            r = self.text()
        self.assertTrue(r['ok'])
        self.assertEqual(r['reason'], 'legacy_ui_submitted')
        self.ui.assert_called_once()
        self.assertEqual(send_ledger.Ledger().get('job-1')['status'], 'submitted')

    def test_direct_unavailable_without_fallback_is_not_sent(self):
        send_mode.set('direct', fallback=False)
        err = direct_send.DirectSendError('not_running', '微信没有在运行')
        with patch('core.direct_send.status', return_value=DOWN), \
             patch('core.direct_send.send', side_effect=err):
            r = self.text()
        self.assertFalse(r['ok'])
        self.assertEqual((r['status'], r['reason']), ('not_sent', 'direct_not_running'))
        self.ui.assert_not_called()

    def test_uncertain_direct_result_never_falls_back(self):
        send_mode.set('direct', fallback=True)
        err = direct_send.DirectSendError('timeout', '等待超时')
        with patch('core.direct_send.status', return_value=READY), \
             patch('core.direct_send.send', side_effect=err):
            r = self.text()
        self.assertEqual((r['status'], r['reason']), ('uncertain', 'direct_timeout'))
        self.ui.assert_not_called()
        # 同一 job 再来一次也不会重发
        with patch('core.direct_send.status', return_value=READY), \
             patch('core.direct_send.send', forbidden):
            self.assertEqual(self.text()['status'], 'uncertain')

    def test_image_is_staged_before_direct_send(self):
        send_mode.set('direct')
        src = self.tmp.name + '/a.png'
        with open(src, 'wb') as f:
            f.write(b'\x89PNG\r\n\x1a\n' + b'x' * 10)
        with patch('core.direct_send.status', return_value=READY), \
             patch('core.direct_send.stage_image', return_value='/w/direct_media/x.png') as st, \
             patch('core.direct_send.send_image', return_value={'ok': True}) as si:
            r = sender.send_image('demo', src, chat_username='wxid_a', job_id='img-1', session=self.token)
        self.assertTrue(r['ok'])
        st.assert_called_once()
        si.assert_called_once_with('wxid_a', '/w/direct_media/x.png', 'demo', 'a.png', source='bot')

    def test_at_mention_is_not_supported_by_direct(self):
        send_mode.set('direct')
        self.assertIsInstance(sender.current_adapter(), DirectSendAdapter)
        r = sender.preflight('g@chatroom', self.token, kind='at')
        self.assertEqual(r['reason'], 'direct_payload_not_supported')

    def test_at_and_quote_go_direct_when_version_supports_them(self):
        send_mode.set('direct')
        with patch('core.direct_send.at_supported', return_value=True), \
             patch('core.direct_send.quote_supported', return_value=True), \
             patch('core.direct_send.status', return_value=READY), \
             patch('core.direct_send.send', return_value={'ok': True}) as ds:
            self.assertTrue(sender.supports('g@chatroom', 'at'))
            self.assertFalse(sender.supports('wxid_a', 'at'))
            r1 = sender.send_at('群', 'g@chatroom', 'wxid_b', '黎深', '开会', session=self.token, job_id='job-at')
            r2 = sender.send_quote('群', 'g@chatroom', 7, '收到', session=self.token, job_id='job-q')
        self.assertEqual((r1['reason'], r2['reason']), ('direct_submitted', 'direct_submitted'))
        ds.assert_any_call('g@chatroom', '开会', '群', source='bot', at=['wxid_b'])
        ds.assert_any_call('g@chatroom', '收到', '群', source='bot', quote=7, at='auto')
        self.ui.assert_not_called()

    def test_quote_is_not_silently_turned_into_text(self):
        send_mode.set('direct')
        self.assertFalse(sender.supports('g@chatroom', 'quote'))
        with patch('core.direct_send.send', forbidden):
            r = sender.send_quote('群', 'g@chatroom', 7, '收到', session=self.token, job_id='job-q2')
        self.assertTrue(r['reason'].endswith('_payload_not_supported'), r['reason'])
        self.ui.assert_not_called()

    def test_bot_quotes_only_when_switched_on_and_supported(self):
        from core import bot
        msg = {'local_id': 9, 'is_self': False}
        send_mode.set('direct')
        self.assertFalse(bot._quote_reply('g@chatroom', msg))                     # 默认关
        send_mode.set(group_quote=True)
        self.assertTrue(send_mode.get()['group_quote'])
        self.assertFalse(bot._quote_reply('g@chatroom', msg))                     # 版本不支持
        with patch('core.direct_send.quote_supported', return_value=True):
            self.assertTrue(bot._quote_reply('g@chatroom', msg))
            self.assertFalse(bot._quote_reply('wxid_a', msg))                     # 私聊不引用
            self.assertFalse(bot._quote_reply('g@chatroom', dict(msg, is_self=True)))
            self.assertFalse(bot._quote_reply('g@chatroom', {'is_self': False}))

    def test_group_at_only_when_switched_on_and_several_people_talk(self):
        from core import bot
        now = 1000
        msg = {'local_id': 9, 'is_self': False, 'sender': 'wxid_b', 'sender_name': '黎深', 'create_time': now}
        alone = [dict(msg, local_id=8)]
        crowd = alone + [{'sender': 'wxid_c', 'is_self': False, 'create_time': now - 60}]
        old = [{'sender': 'wxid_c', 'is_self': False, 'create_time': now - 3600}]
        send_mode.set('direct')
        with patch('core.direct_send.at_supported', return_value=True):
            self.assertIsNone(bot._group_at_target('g@chatroom', msg, crowd))          # 默认关
            send_mode.set(group_at=True)
            self.assertEqual(bot._group_at_target('g@chatroom', msg, crowd), ('wxid_b', '黎深'))
            self.assertIsNone(bot._group_at_target('g@chatroom', msg, alone))          # 只有一个人在说
            self.assertIsNone(bot._group_at_target('g@chatroom', msg, old))            # 别人说话太久了
            self.assertIsNone(bot._group_at_target('wxid_b', msg, crowd))              # 私聊
        self.assertIsNone(bot._group_at_target('g@chatroom', msg, crowd))              # 版本不支持 @

    def test_draw_notice_quotes_request_only_when_switched_on(self):
        from core import bot
        msg = {'local_id': 9, 'is_self': False}
        send_mode.set('direct')
        with patch.object(sender, 'send_quote') as q, patch.object(sender, 'send_text') as t, \
             patch('core.direct_send.quote_supported', return_value=True):
            bot._draw_notice('wxid_a', 'A', msg, '在画了。')
            bot._draw_notice('wxid_a', 'A', msg, '在画了。', plain=False)
            q.assert_not_called(); t.assert_called_once()
            send_mode.set(draw_quote=True)
            bot._draw_notice('wxid_a', 'A', msg, '在画了。', plain=False)
            q.assert_called_once_with('A', 'wxid_a', 9, '在画了。')

    def test_unknown_flag_is_rejected(self):
        with self.assertRaises(ValueError):
            send_mode.set(nope=True)

    def test_injected_adapter_is_not_replaced(self):
        send_mode.set('direct')
        other = object()
        with patch.object(sender, '_adapter', other):
            self.assertIs(sender.current_adapter(), other)

    def test_bad_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            send_mode.set('nope')
        self.assertEqual(send_mode.get()['mode'], 'ui')


class VoiceGoesToUITest(unittest.TestCase):
    def test_voice_is_handed_to_ui_adapter(self):
        from core.direct_sender import DirectSendAdapter
        calls = []

        class UI:
            def submit(self, *a, **k):
                calls.append((a, k))
                return {"ok": True, "status": "submitted", "reason": "legacy_ui_submitted"}

        with patch("core.direct_send.voice_supported", return_value=False):
            r = DirectSendAdapter(fallback=UI()).submit("小A", "wxid_a", "voice", "/w/x.wav", job_id="j1")
        self.assertEqual(r["status"], "submitted")
        self.assertEqual(calls[0][0], ("小A", "wxid_a", "voice", "/w/x.wav"))
        self.assertEqual(calls[0][1]["job_id"], "j1")
