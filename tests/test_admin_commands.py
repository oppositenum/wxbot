"""管理员聊天命令：解析、鉴权、命令→系统功能分发，以及 run_once 接入。

所有系统调用(发朋友圈/发消息/改设置/生图)均被 mock：不触碰真实微信、模型或网络。
"""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from core import account_session as sessions, admin_commands as ac


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.account = 'account-A'
        for target, value in [('config.ACCOUNTS_DIR', self.temp.name),
                              ('config.wxid', lambda: self.account),
                              ('core.account_session._identity_probe', None),
                              ('core.account_session._current', None)]:
            p = patch(target, value); p.start(); self.addCleanup(p.stop)
        self.session = sessions.capture()

    def ctx(self, chat='wxid_admin', is_group=False, content=''):
        m = dict(local_id=7, type=1, is_self=False, sender='wxid_admin', content=content)
        return dict(chat=chat, m=m, is_group=is_group, rules={'admins': ['wxid_admin']}, log=lambda *a: None)


class IsAdmin(unittest.TestCase):
    def test_membership(self):
        self.assertTrue(ac.is_admin('a', {'admins': ['a', 'b']}))
        self.assertFalse(ac.is_admin('c', {'admins': ['a', 'b']}))
        self.assertFalse(ac.is_admin('a', {}))
        self.assertFalse(ac.is_admin('', {'admins': ['']}))
        self.assertFalse(ac.is_admin(None, {'admins': ['a']}))


class Handlers(Base):
    def test_moment_publish_given_text(self):
        with patch('core.moments.save_draft', return_value={'id': 'x', 'revision': 1}) as sd, \
             patch('core.moments_jobs.enqueue_draft', return_value={'state': 'queued'}) as eq:
            out = ac._cmd_moment('今天天气不错', self.ctx())
        sd.assert_called_once()
        self.assertEqual(sd.call_args[0][0], dict(kind='publish', text='今天天气不错', assets=[]))
        eq.assert_called_once_with('x', 1)
        self.assertIn('今天天气不错', out)
        self.assertIn('queued', out)

    def test_moment_empty_autogenerates(self):
        with patch('core.moments.settings', return_value={'moods': ['平静']}), \
             patch('core.moments_ai.generate_post', return_value={'text': '自动文案', 'image_prompt': ''}) as gp, \
             patch('core.moments.save_draft', return_value={'id': 'y', 'revision': 1}) as sd, \
             patch('core.moments_jobs.enqueue_draft', return_value={'state': 'queued'}):
            out = ac._cmd_moment('', self.ctx())
        gp.assert_called_once_with(['平静'])
        self.assertEqual(sd.call_args[0][0]['text'], '自动文案')
        self.assertIn('自动文案', out)

    def test_moment_empty_with_image(self):
        with patch('core.moments.settings', return_value={'moods': ['平静']}), \
             patch('core.moments_ai.generate_post', return_value={'text': 'T', 'image_prompt': 'a cat'}), \
             patch('core.moments_jobs.make_publish_image', return_value='asset1') as mk, \
             patch('core.moments.save_draft', return_value={'id': 'z', 'revision': 1}) as sd, \
             patch('core.moments_jobs.enqueue_draft', return_value={'state': 'queued'}):
            out = ac._cmd_moment('', self.ctx())
        mk.assert_called_once_with('a cat')
        self.assertEqual(sd.call_args[0][0]['assets'], ['asset1'])
        self.assertIn('含配图', out)

    def test_moment_image_command(self):
        # 主题经大模型润色成正文；配图按润色后的画面描述生成，而非照抄输入
        with patch('core.moments.capabilities', return_value={'image_publish': True}), \
             patch('core.moments_ai.compose_from_topic',
                   return_value={'text': '润色后的正文', 'image_prompt': 'a cozy cat by the window'}) as cp, \
             patch('core.llm.gen_image', return_value=b'JPEGBYTES') as gi, \
             patch('core.llm.load_cfg', return_value={}), \
             patch('core.moments.upload', return_value={'id': 'aid'}) as up, \
             patch('core.moments.save_draft', return_value={'id': 'w', 'revision': 1}) as sd, \
             patch('core.moments_jobs.enqueue_draft', return_value={'state': 'queued'}):
            out = ac._cmd_moment_image('一只猫', self.ctx())
        cp.assert_called_once_with('一只猫')
        self.assertEqual(gi.call_args[0][0], 'a cozy cat by the window')   # 用画面描述生成，非原文
        up.assert_called_once()
        self.assertEqual(sd.call_args[0][0]['text'], '润色后的正文')
        self.assertEqual(sd.call_args[0][0]['assets'], ['aid'])
        self.assertIn('图文', out)

    def test_moment_image_no_capability(self):
        with patch('core.moments.capabilities', return_value={'image_publish': False, 'reason': '缺少工具'}):
            out = ac._cmd_moment_image('一只猫', self.ctx())
        self.assertIn('不支持生成配图', out)
        self.assertIn('缺少工具', out)

    def test_moment_image_gen_failure_is_honest(self):
        with patch('core.moments.capabilities', return_value={'image_publish': True}), \
             patch('core.moments_ai.compose_from_topic',
                   return_value={'text': 'T', 'image_prompt': 'P'}), \
             patch('core.llm.load_cfg', return_value={}), \
             patch('core.llm.gen_image', side_effect=RuntimeError('403 无权限')):
            out = ac._cmd_moment_image('一只猫', self.ctx())
        self.assertTrue(out.startswith('⚠️'))
        self.assertIn('配图生成失败', out)

    def test_sync(self):
        with patch('core.moments_jobs.refresh', return_value={'parsed': 10, 'added': 2, 'changed': 1}) as r:
            out = ac._cmd_sync('', self.ctx())
        r.assert_called_once()
        self.assertIn('新增 2', out)
        self.assertIn('更新 1', out)

    def test_status(self):
        with patch('core.moments.status', return_value={'count': 5, 'last_sync': None, 'automation_running': True}), \
             patch('core.moments.settings', return_value={'auto_comment': True, 'auto_publish': False, 'chat_reflection': False}):
            out = ac._cmd_status('', self.ctx())
        self.assertIn('缓存动态：5', out)
        self.assertIn('自动评论', out)

    def test_set_toggle(self):
        with patch('core.moments.settings', return_value={'revision': 5}), \
             patch('core.moments.save_settings', return_value={}) as ss:
            out = ac._cmd_set('自动评论 关', self.ctx())
        ss.assert_called_once_with({'auto_comment': False}, 5)
        self.assertIn('已关闭自动评论', out)

    def test_set_on(self):
        with patch('core.moments.settings', return_value={'revision': 0}), \
             patch('core.moments.save_settings', return_value={}) as ss:
            ac._cmd_set('自动发布 开', self.ctx())
        ss.assert_called_once_with({'auto_publish': True}, 0)

    def test_set_unknown_item(self):
        with patch('core.moments.save_settings') as ss:
            out = ac._cmd_set('乱七八糟 开', self.ctx())
        ss.assert_not_called()
        self.assertIn('未知设置项', out)

    def test_set_bad_value(self):
        with patch('core.moments.save_settings') as ss:
            out = ac._cmd_set('自动评论 也许', self.ctx())
        ss.assert_not_called()
        self.assertIn('开 或 关', out)

    def test_send_unique_contact(self):
        people = [{'username': 'wx1', 'name': '张三', 'nick_name': '', 'remark': '', 'alias': ''}]
        with patch('core.contacts.list_contacts', return_value=people), \
             patch('core.sendq.enqueue', return_value=('job1', 0)) as en:
            out = ac._cmd_send('张三 在吗', self.ctx())
        en.assert_called_once()
        self.assertEqual(en.call_args[0][0], 'text')
        self.assertEqual(en.call_args[1]['chat'], 'wx1')
        self.assertEqual(en.call_args[1]['content'], '在吗')
        self.assertIn('已发送给 张三', out)

    def test_send_ambiguous(self):
        people = [{'username': 'wx1', 'name': '张三', 'nick_name': '', 'remark': '', 'alias': ''},
                  {'username': 'wx2', 'name': '张四', 'nick_name': '', 'remark': '', 'alias': ''}]
        with patch('core.contacts.list_contacts', return_value=people), \
             patch('core.sendq.enqueue') as en:
            out = ac._cmd_send('张 在吗', self.ctx())
        en.assert_not_called()
        self.assertIn('匹配到多个', out)

    def test_send_not_found(self):
        with patch('core.contacts.list_contacts', return_value=[]), \
             patch('core.sendq.enqueue') as en:
            out = ac._cmd_send('李四 在吗', self.ctx())
        en.assert_not_called()
        self.assertIn('未找到联系人', out)

    def test_send_usage(self):
        out = ac._cmd_send('张三', self.ctx())
        self.assertIn('用法', out)

    def test_help_lists_all(self):
        out = ac._cmd_help('', self.ctx())
        for name in ('/朋友圈', '/同步朋友圈', '/设置', '/发', '/帮助'):
            self.assertIn(name, out)


class Dispatch(Base):
    def _receipts(self):
        rec = []
        return rec, [
            patch('core.sender.send_text', side_effect=lambda disp, text, **kw: rec.append(text) or {'status': 'confirmed'}),
            patch('core.bot.send_name_for', lambda u: '管理员'),
            patch('core.send_ledger.stable_id', lambda *a: 'sid'),
        ]

    def _msg(self, content):
        return dict(local_id=3, type=1, is_self=False, sender='wxid_admin', content=content)

    def test_non_slash_not_handled(self):
        rec, ps = self._receipts()
        for p in ps: p.start(); self.addCleanup(p.stop)
        self.assertFalse(ac.dispatch('wxid_admin', self._msg('你好啊'), False, {'admins': ['wxid_admin']}))
        self.assertEqual(rec, [])

    def test_unknown_command_replies_help(self):
        rec, ps = self._receipts()
        for p in ps: p.start(); self.addCleanup(p.stop)
        handled = ac.dispatch('wxid_admin', self._msg('/乱来'), False, {'admins': ['wxid_admin']})
        self.assertTrue(handled)
        self.assertEqual(len(rec), 1)
        self.assertIn('未知命令', rec[0])

    def test_known_command_dispatches_and_receipts(self):
        rec, ps = self._receipts()
        for p in ps: p.start(); self.addCleanup(p.stop)
        with patch('core.moments.save_draft', return_value={'id': 'x', 'revision': 1}), \
             patch('core.moments_jobs.enqueue_draft', return_value={'state': 'queued'}) as eq:
            handled = ac.dispatch('wxid_admin', self._msg('/朋友圈 你好世界'), False, {'admins': ['wxid_admin']})
        self.assertTrue(handled)
        eq.assert_called_once()
        self.assertIn('你好世界', rec[0])

    def test_handler_error_reports_failure(self):
        rec, ps = self._receipts()
        for p in ps: p.start(); self.addCleanup(p.stop)
        with patch('core.moments.save_draft', side_effect=ValueError('文字过长')):
            handled = ac.dispatch('wxid_admin', self._msg('/朋友圈 x'), False, {'admins': ['wxid_admin']})
        self.assertTrue(handled)
        self.assertTrue(rec[0].startswith('⚠️'))
        self.assertIn('文字过长', rec[0])


class RunOnceIntegration(Base):
    """验证 bot.run_once 把管理员 /命令 路由到 dispatch 且跳过普通回复。"""

    def _drive(self, rules, admin_sender, first_content, second_content):
        from core import bot, decrypt, messages, conversation_state, personalization
        CHAT = admin_sender
        store = {'msgs': [dict(local_id=1, type=1, is_self=False, sender=CHAT,
                               content='历史', create_time=0)]}
        calls = {'dispatch': [], 'enqueue': []}
        patches = [
            patch.object(decrypt, 'run', lambda force=False: None),
            patch.object(messages, 'get_messages', lambda chat, limit=40: list(store['msgs']) if chat == CHAT else []),
            patch.object(conversation_state, 'observe', lambda *a, **k: None),
            patch.object(personalization, 'learn_live', lambda *a, **k: None),
            patch.object(bot, '_maybe_learn', lambda *a, **k: None),
            patch.object(bot, '_proactive_worker', lambda *a, **k: None),
            patch.object(bot.admin_commands, 'dispatch',
                         lambda chat, m, ig, r, log: calls['dispatch'].append(m['content']) or True),
            patch.object(bot, 'enqueue_pending',
                         lambda chat, m, msgs, rule, now: calls['enqueue'].append(m['content']) or {'msgs': [m]}),
        ]
        for p in patches: p.start(); self.addCleanup(p.stop)
        state = bot.load_state()
        bot.load_pending(); bot._pending.clear()
        bot.run_once(rules, state, log=lambda *_: None)          # 首见:只记指针
        store['msgs'].append(dict(local_id=2, type=1, is_self=False, sender=CHAT,
                                  content=second_content, create_time=1))
        bot.run_once(rules, state, log=lambda *_: None)
        return calls

    def test_admin_slash_routes_to_dispatch_only(self):
        rules = {'include_self': False, 'watch': ['wxid_admin'], 'admins': ['wxid_admin'], 'poll_interval': 5,
                 'rules': [{'name': 'r', 'match': {'type': 'auto'}, 'action': {'type': 'reply_ai'}}]}
        calls = self._drive(rules, 'wxid_admin', '历史', '/朋友圈 测试')
        self.assertEqual(calls['dispatch'], ['/朋友圈 测试'])
        self.assertEqual(calls['enqueue'], [])

    def test_non_admin_slash_falls_through_to_reply(self):
        rules = {'include_self': False, 'watch': ['wxid_friend'], 'admins': ['wxid_admin'], 'poll_interval': 5,
                 'rules': [{'name': 'r', 'match': {'type': 'auto'}, 'action': {'type': 'reply_ai'}}]}
        calls = self._drive(rules, 'wxid_friend', '历史', '/朋友圈 测试')
        self.assertEqual(calls['dispatch'], [])
        self.assertEqual(calls['enqueue'], ['/朋友圈 测试'])


if __name__ == '__main__':
    unittest.main()
