"""持续模式：群里不用 @ 也逐条接话；空闲自动关闭；与战斗模式互斥；[[SKIP]] 不发。"""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import continuous_mode, battle_mode, admin_commands as ac
from tests.test_admin_commands import Base

ROOM = 'room@chatroom'


class Store(Base):
    def test_enable_disable(self):
        self.assertFalse(continuous_mode.is_on(ROOM))
        continuous_mode.enable(ROOM)
        self.assertTrue(continuous_mode.is_on(ROOM))
        self.assertEqual(continuous_mode.active_chats(), [ROOM])
        self.assertTrue(continuous_mode.disable(ROOM))
        self.assertFalse(continuous_mode.is_on(ROOM))

    def test_idle_expiry_and_touch(self):
        continuous_mode.enable(ROOM, now=1000)
        continuous_mode.touch(ROOM, now=1000 + continuous_mode.IDLE_OFF - 10)
        self.assertEqual(continuous_mode.expire(now=1000 + continuous_mode.IDLE_OFF + 5), [])
        dead = continuous_mode.expire(now=1000 + 2 * continuous_mode.IDLE_OFF)
        self.assertEqual(dead, [ROOM])
        self.assertFalse(continuous_mode.is_on(ROOM))

    def test_mutually_exclusive_with_battle(self):
        battle_mode.enable(ROOM)
        continuous_mode.enable(ROOM)
        self.assertFalse(battle_mode.is_on(ROOM))
        ctx = self.ctx(chat=ROOM, is_group=True)
        with patch('core.bot.send_name_for', lambda u: 'Limit'):
            ac._cmd_battle_on('', ctx)
        self.assertTrue(battle_mode.is_on(ROOM))
        self.assertFalse(continuous_mode.is_on(ROOM))

    def test_commands(self):
        ctx = self.ctx(chat=ROOM, is_group=True)
        with patch('core.bot.send_name_for', lambda u: 'Limit'):
            on = ac._cmd_continuous_on('', ctx)
            self.assertIn('Limit', on)
            self.assertTrue(continuous_mode.is_on(ROOM))
            ac._cmd_continuous_off('', ctx)
        self.assertFalse(continuous_mode.is_on(ROOM))
        self.assertIn('/开启持续模式', ac._help_text())


class RunOnce(Base):
    def _drive(self, enabled, group_auto_reply=False):
        from core import bot, decrypt, messages, conversation_state, personalization
        store = {'msgs': [dict(local_id=1, type=1, is_self=False, sender='wxid_jx',
                               content='画蛇添足', create_time=0, at_me=False)]}
        enq = []
        patches = [
            patch.object(decrypt, 'run', lambda force=False: None),
            patch.object(messages, 'get_messages', lambda chat, limit=40: list(store['msgs']) if chat == ROOM else []),
            patch.object(messages, 'list_sessions', lambda limit=200: []),
            patch.object(conversation_state, 'observe', lambda *a, **k: None),
            patch.object(personalization, 'learn_live', lambda *a, **k: None),
            patch.object(bot, '_maybe_learn', lambda *a, **k: None),
            patch.object(bot, '_proactive_worker', lambda *a, **k: None),
            patch.object(bot, 'enqueue_pending',
                         lambda chat, m, msgs, rule, now: enq.append((m['content'], rule['name'])) or {'msgs': [m]}),
        ]
        for p in patches:
            p.start(); self.addCleanup(p.stop)
        if enabled:
            continuous_mode.enable(ROOM)
        rules = {'include_self': False, 'watch': [], 'admins': [], 'poll_interval': 5,
                 'group_auto_reply': group_auto_reply,
                 'rules': [{'name': 'r', 'match': {'type': 'at_me'}, 'action': {'type': 'reply_ai'}}]}
        state = bot.load_state()
        bot.load_pending(); bot._pending.clear()
        bot.run_once(rules, state, log=lambda *_: None)
        store['msgs'].append(dict(local_id=2, type=1, is_self=False, sender='wxid_jx',
                                  content='足智多谋', create_time=1, at_me=False))
        store['msgs'].append(dict(local_id=3, type=1, is_self=True, sender=self.account,
                                  content='谋事在人', create_time=2, at_me=False))
        bot.run_once(rules, state, log=lambda *_: None)
        return enq

    def test_unwatched_group_without_at_is_enqueued_when_on(self):
        self.assertEqual(self._drive(True), [('足智多谋', '持续模式')])

    def test_off_keeps_needing_at(self):
        self.assertEqual(self._drive(False, group_auto_reply=True), [])


class SkipToken(Base):
    def test_skip_is_not_sent(self):
        from core import bot, personalization
        continuous_mode.enable(ROOM)
        sent = []
        m = dict(local_id=5, type=1, is_self=False, sender='wxid_a', content='你们晚上吃啥', chat=ROOM)
        with patch('core.sender.preflight', lambda chat: None), \
             patch.object(personalization, 'resolve_persona', lambda *a, **k: {'persona': {'name': 'p', 'persona': 'x'}}), \
             patch.object(bot, '_ai_reply', lambda *a, **k: '[[SKIP]]'), \
             patch('core.sender.send_text', lambda *a, **k: sent.append(a) or {'status': 'confirmed'}):
            r = bot.do_action(bot._CONTINUOUS_RULE, m, ROOM, {}, lambda *_: None, batch_msgs=[m])
        self.assertEqual(r['status'], 'skipped')
        self.assertEqual(sent, [])


def _rows(*pairs, t0=1000):
    return [dict(local_id=i + 1, type=1, is_self=mine, content=text, create_time=t0 + i)
            for i, (mine, text) in enumerate(pairs)]


class IdiomGame(unittest.TestCase):
    def test_needs_last_char_of_other_side(self):
        rows = _rows((False, '跳梁小丑'), (True, '丑态百出'), (False, '出人头地'))
        g = continuous_mode.idiom_game(rows, now=1010)
        self.assertEqual((g['last'], g['need'], g['mine']), ('出人头地', '地', False))
        self.assertFalse(continuous_mode.check_answer(g, '出神入化'))     # 真实翻车：接了首字
        self.assertFalse(continuous_mode.check_answer(g, '跳梁小丑'))     # 接不上
        self.assertFalse(continuous_mode.check_answer(g, '地大物博 历史悠久'))
        self.assertFalse(continuous_mode.check_answer(g, '事开头，重接'))
        self.assertTrue(continuous_mode.check_answer(g, '地大物博'))
        self.assertTrue(continuous_mode.check_answer(g, '这个字难住我了，我认输'))
        self.assertIn('「地」', continuous_mode.game_prompt(g))

    def test_homophone_links(self):
        rows = _rows((False, '一心一意'), (True, '意气风发'), (False, '发扬光大'))
        g = continuous_mode.idiom_game(rows, now=1010)
        self.assertTrue(continuous_mode.check_answer(g, '大展宏图'))
        try:
            import pypinyin  # noqa: F401
        except ImportError:
            self.skipTest('pypinyin not installed')
        self.assertTrue(continuous_mode.check_answer(g, '达官贵人'))   # 大/达 同音

    def test_own_last_means_their_turn(self):
        rows = _rows((False, '跳梁小丑'), (True, '丑态百出'), (False, '继续啊'))
        g = continuous_mode.idiom_game(rows, now=1010)
        self.assertTrue(g['mine'])
        self.assertIn('[[SKIP]]', continuous_mode.game_prompt(g))

    def test_wrong_own_answer_does_not_count(self):
        rows = _rows((False, '跳梁小丑'), (True, '丑态百出'), (False, '出人头地'),
                     (True, '出神入化'), (False, '你要以地开头了'))
        g = continuous_mode.idiom_game(rows, now=1010)
        self.assertEqual((g['need'], g['mine']), ('地', False))

    def test_four_char_chat_is_not_a_game(self):
        for chat in [('气死我了', '笑死我了'), ('好的收到', '明天见啦'), ('今天好热', '我也觉得')]:
            rows = _rows((False, chat[0]), (True, chat[1]))
            self.assertIsNone(continuous_mode.idiom_game(rows, now=1010), chat)
        self.assertIn('不是出题', continuous_mode.game_prompt(None))

    def test_chat_inside_game_is_ignored(self):
        rows = _rows((False, '跳梁小丑'), (True, '丑态百出'), (False, '气死我了'))
        g = continuous_mode.idiom_game(rows, now=1010)
        self.assertEqual(g['last'], '丑态百出')     # 「气死我了」接不上，当聊天
        self.assertTrue(g['mine'])

    def test_explicit_start_single_idiom(self):
        rows = _rows((True, '来玩成语接龙吧'), (False, '画蛇添足'))
        g = continuous_mode.idiom_game(rows, now=1010)
        self.assertEqual((g['last'], g['need']), ('画蛇添足', '足'))
        self.assertIsNone(continuous_mode.idiom_game(_rows((False, '画蛇添足')), now=1010))

    def test_mode_receipt_does_not_end_game(self):
        rows = _rows((True, '目瞪口呆'), (False, '呆若木鸡'), (True, '/开启持续模式'),
                     (True, '🔁 已对「Limit」开启持续模式：适合玩成语接龙。手动关发 /关闭持续模式。'))
        g = continuous_mode.idiom_game(rows, now=1010)
        self.assertEqual((g['last'], g['mine']), ('呆若木鸡', False))

    def test_stop_ends_game(self):
        rows = _rows((False, '跳梁小丑'), (True, '丑态百出'), (False, '出人头地'), (False, '我不玩了'))
        self.assertIsNone(continuous_mode.idiom_game(rows, now=1010))

    def test_stale_game_window(self):
        old = _rows((False, '跳梁小丑'), (True, '丑态百出'), t0=0)
        self.assertIsNone(continuous_mode.idiom_game(old, now=10_000))

    def test_at_and_punctuation_stripped(self):
        self.assertEqual(continuous_mode.idiom_of({'type': 1, 'content': '@🐮🐮🌱besos 先行一步。'}), '先行一步')


class Hints(unittest.TestCase):
    def test_wrong_char_attempt_gets_hint_and_game_continues(self):
        rows = _rows((False, '跳梁小丑'), (True, '丑态百出'), (False, '神采飞扬'))
        g = continuous_mode.idiom_game(rows, now=1010)
        self.assertEqual((g['mine'], g['attempt']), (True, '神采飞扬'))
        self.assertIn('[[NOT_IDIOM]]', continuous_mode.game_prompt(g))
        h = continuous_mode.hint(g)
        self.assertTrue(h.startswith('要用「出」'))
        # 提示发出后，对方改接对了：链照常延续
        rows += _rows((True, h), (False, '出人头地'), t0=1003)
        for i, r in enumerate(rows):
            r['local_id'] = i + 1
        g2 = continuous_mode.idiom_game(rows, now=1010)
        self.assertEqual((g2['last'], g2['mine'], g2['need']), ('出人头地', False, '地'))

    def test_fake_idiom_is_voided_after_hint(self):
        rows = _rows((False, '跳梁小丑'), (True, '丑态百出'), (False, '出来玩啊'))
        g = continuous_mode.idiom_game(rows, now=1010)
        self.assertEqual((g['last'], g['mine']), ('出来玩啊', False))   # 接上了字，但未必是成语
        self.assertTrue(continuous_mode.check_answer(g, '[[NOT_IDIOM]]'))
        h = continuous_mode.hint(g)
        self.assertEqual(h, '「出来玩啊」好像不是成语哦，换个「出」开头的试试～')
        rows.append(dict(local_id=4, type=1, is_self=True, content=h, create_time=1004))
        g2 = continuous_mode.idiom_game(rows, now=1010)
        self.assertEqual((g2['last'], g2['mine']), ('丑态百出', True))
        self.assertNotIn('出来玩啊', g2['used'])

    def test_repeat_attempt_hint(self):
        rows = _rows((False, '跳梁小丑'), (True, '丑态百出'), (False, '出人头地'),
                     (True, '地久天长'), (False, '跳梁小丑'))
        g = continuous_mode.idiom_game(rows, now=1010)
        self.assertIn('用过', continuous_mode.hint(g))

    def test_common_first_in_prompt(self):
        g = continuous_mode.idiom_game(_rows((False, '跳梁小丑'), (True, '丑态百出'), (False, '出人头地')), now=1010)
        self.assertIn('常见成语', continuous_mode.game_prompt(g))


class DoActionGame(Base):
    def _run(self, model_out, retry_outs=(), live=None):
        from core import bot, personalization, messages
        continuous_mode.enable(ROOM)
        ctx = _rows((False, '跳梁小丑'), (True, '丑态百出'), t0=__import__('time').time() - 5)
        m = dict(local_id=10, type=1, is_self=False, sender='wxid_jx', content='出人头地',
                 chat=ROOM, create_time=__import__('time').time())
        live = live if live is not None else ctx + [m]
        sent, retries = [], list(retry_outs)
        with patch('core.sender.preflight', lambda chat: None), \
             patch.object(personalization, 'resolve_persona', lambda *a, **k: {'persona': {'name': 'p', 'persona': 'x'}}), \
             patch.object(bot, '_ai_reply', lambda *a, **k: model_out), \
             patch.object(bot.llm, 'chat', lambda *a, **k: retries.pop(0) if retries else ''), \
             patch.object(messages, 'get_messages', lambda chat, limit=40: live), \
             patch.object(bot, 'send_name_for', lambda u: 'Limit'), \
             patch.object(bot, '_part_gap', lambda *a, **k: 0), \
             patch('core.sender.send_text', lambda t, text, **k: sent.append(text) or {'status': 'confirmed'}):
            r = bot.do_action(bot._CONTINUOUS_RULE, m, ROOM, {}, lambda *_: None,
                              context_msgs=ctx, batch_msgs=[m])
        return r, sent

    def test_valid_single_message(self):
        r, sent = self._run('地大物博\n[[NEXT]]\n博学多才')
        self.assertEqual(sent, ['地大物博'])

    def test_wrong_char_is_retried(self):
        r, sent = self._run('出神入化', retry_outs=['地久天长'])
        self.assertEqual(sent, ['地久天长'])

    def test_retry_still_wrong_sends_nothing(self):
        r, sent = self._run('出神入化', retry_outs=['化险为夷', '事开头，重接'])
        self.assertEqual(sent, [])
        self.assertEqual(r['status'], 'not_sent')

    def test_not_idiom_marker_sends_hint(self):
        r, sent = self._run('[[NOT_IDIOM]]')
        self.assertEqual(sent, ['「出人头地」好像不是成语哦，换个「出」开头的试试～'])

    def test_stale_answer_dropped(self):
        import time as _t
        live = _rows((False, '跳梁小丑'), (True, '丑态百出'), (False, '出人头地'),
                     (False, '地动山摇'), t0=_t.time() - 5)
        r, sent = self._run('地大物博', live=live)
        self.assertEqual(sent, [])
        self.assertEqual(r['reason'], 'continuous_stale')


if __name__ == '__main__':
    unittest.main()
