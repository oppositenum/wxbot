"""Replay synthetic conversations through the real coordinator; no live I/O."""
import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_send_safety import Isolated, forbidden
from core import bot, sender, send_ledger, reply_policy as policy, reply_inbox, reply_context
from core.legacy_sender import LegacySearchAdapter


def msg(i, text, own=False, **fields):
    return dict(local_id=i, type=1, content=text, is_self=own,
                sender='account-A' if own else 'chat-A', **fields)


class Decisions(unittest.TestCase):
    def test_closing_question_and_cancelled_batch(self):
        cases = [([msg(1, '好的')], 'observe'),
                 ([msg(1, '好的，还有一个问题？')], 'reply'),
                 ([msg(1, '三点可以吗？'), msg(2, '先别回了')], 'observe'),
                 ([msg(1, '三点可以吗？'), msg(2, '好的')], 'reply'),
                 ([msg(1, '不用回复了是什么意思？')], 'reply')]
        for batch, expected in cases:
            with self.subTest(batch=batch):
                self.assertEqual(policy.decide(batch, [], 'account-A').action, expected)

    def test_sender_identity_and_manual_answer(self):
        own = msg(2, '四点见', True); own['is_self'] = False
        self.assertEqual(policy.decide([own], [], 'account-A').reason, 'self_message')
        self.assertEqual(policy.decide([msg(1, '几点见？')], [own], 'account-A').reason,
                         'own_reply_after_source')
        self.assertEqual(policy.decide([msg(3, '地点呢？')], [own], 'account-A').action, 'reply')

    def test_similar_text_preserves_changed_facts(self):
        self.assertTrue(policy.near('好的，明天见！', '好的明天见'))
        self.assertTrue(policy.near('今天辛苦了你先好好休息一下有事情我们明天再慢慢聊',
                                   '今天辛苦了你先好好休息一下有事情我们明天再慢慢聊吧'))
        self.assertFalse(policy.near('我们约定明天15点在那个熟悉的地方见面', '我们约定明天16点在那个熟悉的地方见面'))
        self.assertFalse(policy.near('这个事情我现在可以帮你处理好稍后告诉你结果', '这个事情我现在不可以帮你处理好稍后告诉你结果'))

    def test_context_budget_preserves_latest_own_turn_and_current_request(self):
        turns, current, _ = reply_context.build([msg(1, '很久前'+('长文'*1000)), msg(2, '我方刚刚说的话', True)],
            [msg(3, '请解释上一句话')], account='account-A', is_group=False,
            render=lambda m:m['content'], name=lambda s:s, timestamp=lambda m:'刚才',
            scheduled=lambda m:False, max_history_chars=100)
        self.assertEqual(turns[-1]['role'], 'assistant')
        self.assertIn('我方刚刚说的话', turns[-1]['content'])
        self.assertNotIn('很久前', str(turns))
        self.assertIn('请解释上一句话', current)


class Replay(Isolated):
    def setUp(self):
        super().setUp()
        self.newer = []
        for target, value in [
            ('core.reply_inbox.refresh', lambda: None),
            ('core.reply_inbox._active', None),
            ('core.conversation_state.latest', lambda *a, **k: list(self.newer)),
            ('core.bot.send_name_for', lambda s: s),
            ('core.bot._maybe_learn', lambda *a: None),
            ('core.personalization.resolve_persona', lambda *a: {'persona': {'name':'test', 'persona':'test'}}),
        ]:
            p = patch(target, value); p.start(); self.addCleanup(p.stop)

    def batch(self, text='今天天气很好', rows=None):
        p = dict(session=self.token, msgs=rows or [msg(1, text)], ctx=[],
                 rule={'name':'test', 'action':{'type':'reply_ai'}})
        bot._pending['chat-A'] = p
        return p

    def process(self, p, output='明天三点见'):
        with patch.object(bot, '_ai_reply', return_value=output):
            bot.process_pending('chat-A', p, {}, lambda *a: None)
        return p

    def test_fixed_batch_preserves_next_batch_while_sending(self):
        p = self.batch('三点可以吗？')
        bot._pending.pop('chat-A')
        def generate(*a, **k):
            self.newer = [msg(2, '改成四点吧')]
            bot.enqueue_pending('chat-A', self.newer[0], p['msgs']+self.newer, p['rule'], 100)
            return '明天三点见'
        with patch.object(bot, '_ai_reply', side_effect=generate):
            bot.process_pending('chat-A', p, {}, lambda *a: None)
        self.assertEqual(p['send_status'], 'confirmed')
        self.assertEqual(self.adapter.calls, 1)
        p2 = bot._pending['chat-A']
        self.assertEqual([m['local_id'] for m in p2['msgs']], [2])
        self.newer = []
        self.process(p2, '明天四点见')
        self.assertEqual(self.adapter.calls, 2)
        self.assertNotIn('chat-A', bot._pending)

    def test_manual_answer_arrives_during_generation(self):
        self.newer = [msg(2, '已经人工回答', True)]
        p = self.process(self.batch('怎么安排？'))
        self.assertEqual(p['reason'], 'own_reply_before_send')
        self.assertEqual(self.adapter.calls, 0)

    def test_reply_duplicate_across_distinct_jobs_is_skipped(self):
        sender.send_text('chat-A', '你今天也辛苦了早点休息吧', chat_username='chat-A')
        p = self.process(self.batch(), '你今天也辛苦了，早点休息吧！')
        self.assertEqual(p['reason'], 'recent_reply_duplicate')
        self.assertEqual(self.adapter.calls, 1)
        self.assertEqual(send_ledger.Ledger().decisions('account-A')[0]['reason'], p['reason'])

    def test_explicit_repeat_or_fact_question_keeps_complete_answer(self):
        sender.send_text('chat-A', '明天三点见', chat_username='chat-A')
        p = self.process(self.batch('再说一遍几点见？'))
        self.assertEqual(p['send_status'], 'confirmed')
        self.assertEqual(self.adapter.calls, 2)

    def test_duplicate_check_covers_followup_but_not_manual_send(self):
        sender.send_text('chat-A', '早点休息吧', chat_username='chat-A')
        with policy.scope('chat-A', [], [msg(1, '早点休息吧', True)], mode='nudge'):
            r = sender.send_text('chat-A', '早点休息吧', chat_username='chat-A')
        self.assertEqual(r['reason'], 'recent_reply_duplicate')
        sender.send_text('chat-A', '早点休息吧', chat_username='chat-A')
        self.assertEqual(self.adapter.calls, 2)

    def test_other_chat_send_cannot_bypass_changed_source_guard(self):
        self.newer = [msg(2, '先不要发了')]
        with policy.scope('chat-A', [msg(1, '发给别人')]):
            # Legacy transport permits another destination; guard still follows source.
            with patch.object(sender, '_adapter', LegacySearchAdapter()):
                r = sender.send_text('chat-B', '结果', chat_username='chat-B')
        self.assertEqual(r['status'], 'skipped')

    def test_unrelated_group_chatter_does_not_cancel_source_member(self):
        self.newer = [dict(msg(2, '无关聊天'), sender='member-B')]
        with policy.scope('group@chatroom', [dict(msg(1, '几点？'), sender='member-A')]):
            result = policy.before_dispatch('group@chatroom', 'text', '三点', send_ledger.Ledger(), 'new')
        self.assertIsNone(result)

    def test_explicit_forward_and_its_presend_retry_keep_their_rule(self):
        p = self.batch('要转发的内容')
        p['rule']['action'] = {'type':'forward', 'to':'chat-A', 'to_username':'chat-A'}
        self.adapter.open_ok = False
        with patch('core.conversation_state.latest', forbidden):
            bot.process_pending('chat-A', p, {}, lambda *a:None)
            self.assertEqual(p['send_status'], 'not_sent')
            send_ledger.Ledger().update(p['child_job_id'], 'not_sent', 'open_failed', retry_at=0)
            self.adapter.open_ok = True
            bot.process_pending('chat-A', p, {}, lambda *a:None)
        self.assertEqual(self.adapter.calls, 1)

    def test_snapshot_failure_recovers_without_regenerating_or_unguarded_retry(self):
        p = self.batch('几点见？')
        with patch('core.reply_inbox.refresh', side_effect=OSError('synthetic')):
            self.process(p)
        self.assertEqual(p['reason'], 'reply_snapshot_unavailable')
        child = p['child_job_id']; ledger = send_ledger.Ledger()
        ledger.update(child, 'deferred', p['reason'], retry_at=0)
        self.assertEqual(sender.retry(child)['status'], 'deferred')
        with patch.object(bot, '_ai_reply', forbidden):
            bot.process_pending('chat-A', p, {}, lambda *a: None)
        self.assertEqual(self.adapter.calls, 1)
        self.assertEqual(p['send_status'], 'confirmed')

    def test_snapshot_failure_retries_are_bounded_and_do_not_regenerate(self):
        p = self.batch('几点见？')
        with patch('core.reply_inbox.refresh', side_effect=OSError('synthetic')):
            self.process(p)
            child = p['child_job_id']; ledger = send_ledger.Ledger()
            with patch.object(bot, '_ai_reply', forbidden):
                for _ in range(5):
                    ledger.update(child, 'deferred', 'reply_snapshot_unavailable', retry_at=0)
                    bot.process_pending('chat-A', p, {}, lambda *a:None)
        self.assertEqual(ledger.get(child)['attempts'], 3)
        self.assertFalse(send_ledger.result(child)['retryable'])
        self.assertEqual(self.adapter.calls, 0)

    def test_new_messages_cannot_replay_uncertain_legacy_submission(self):
        p = self.batch('几点见？')
        with patch.object(sender, '_adapter', LegacySearchAdapter()), patch('core.docker_wx._exec',
                return_value=SimpleNamespace(returncode=1, stdout='timeout')) as execute:
            self.process(p)
            self.assertEqual(p['send_status'], 'uncertain')
            bot.process_pending('chat-A', p, {}, lambda *a:None)
            self.assertEqual(execute.call_count, 1)
        p2 = bot.enqueue_pending('chat-A', msg(2, '还有一个问题'), [], p['rule'], 100)
        self.assertEqual([m['local_id'] for m in p2['msgs']], [2])

    def test_stop_before_dispatch_cancels_without_ui(self):
        with patch('core.reply_inbox._active', lambda:False):
            p = self.process(self.batch())
        self.assertEqual(p['reason'], 'bot_stopped_before_send')
        self.assertEqual(self.adapter.calls, 0)

    def test_legacy_guard_is_inside_ui_lock_and_never_calls_script_on_stale_reply(self):
        self.newer = [msg(2, '不用回复了')]
        with patch.object(sender, '_adapter', LegacySearchAdapter()), patch('core.docker_wx._exec', forbidden):
            p = self.process(self.batch('几点见？'))
        self.assertEqual(p['send_status'], 'skipped')

    def test_partial_tool_send_never_requeues_old_input(self):
        p = self.batch('查一下并告诉我')
        def generate(*a, **kw):
            sender.send_text('chat-A', '查询结果', chat_username='chat-A')
            self.newer = [msg(2, '不用继续了')]
            return '旧的后续回答'
        with patch.object(bot, '_ai_reply', side_effect=generate):
            bot.process_pending('chat-A', p, {}, lambda *a:None)
        self.assertEqual(p['send_status'], 'skipped')
        p2 = bot.enqueue_pending('chat-A', self.newer[0], self.newer, p['rule'], 100)
        self.assertEqual(len(p2['msgs']), 1)
        self.assertEqual(self.adapter.calls, 1)

    def test_reader_continues_while_model_waits(self):
        entered, release, refreshed, stop = (threading.Event() for _ in range(4))
        p = self.batch('几点见？')
        def generate(*a, **kw):
            entered.set()
            if not release.wait(3):
                raise AssertionError('reader blocked behind model')
            return '三点见'
        def refresh():
            self.newer = [msg(2, '改成四点')]
            refreshed.set()
        with patch.object(bot, '_ai_reply', side_effect=generate), patch.object(reply_inbox, 'refresh', side_effect=refresh):
            worker = threading.Thread(target=bot.process_pending, args=('chat-A',p,{},lambda *a:None))
            reader = threading.Thread(target=reply_inbox._loop, args=(stop,))
            worker.start()
            try:
                self.assertTrue(entered.wait(2)); reader.start()
                self.assertTrue(refreshed.wait(2))
            finally:
                release.set(); stop.set()
                worker.join(3)
                if reader.ident: reader.join(3)
            self.assertFalse(worker.is_alive()); self.assertFalse(reader.is_alive())
        self.assertEqual(p['send_status'], 'confirmed')
        self.assertEqual(self.adapter.calls, 1)


class Inbox(Isolated):
    def setUp(self):
        super().setUp()
        p = patch.object(reply_inbox, '_stamp', None); p.start(); self.addCleanup(p.stop)

    def test_unchanged_source_does_not_repeat_decryption(self):
        with patch.object(reply_inbox, 'source_stamp', return_value='one'), patch.object(
                reply_inbox.decrypt, 'run', return_value={'message':'ok'}) as refresh:
            reply_inbox.refresh(); reply_inbox.refresh()
        self.assertEqual(refresh.call_count, 1)

    def test_stale_snapshot_is_never_marked_current(self):
        with patch.object(reply_inbox, 'source_stamp', return_value='one'), patch.object(
                reply_inbox.decrypt, 'run', return_value={'message':'stale-kept'}):
            with self.assertRaises(RuntimeError):reply_inbox.refresh()
        self.assertIsNone(reply_inbox._stamp)

    def test_source_change_during_refresh_requires_another_consistent_read(self):
        with patch.object(reply_inbox, 'source_stamp', side_effect=['one','two','two','two']), patch.object(
                reply_inbox.decrypt, 'run', return_value={'message':'ok'}) as refresh:
            reply_inbox.refresh()
        self.assertEqual(refresh.call_count, 2)

    def test_account_switch_invalidates_reader_cache(self):
        with patch.object(reply_inbox, 'source_stamp', return_value='one'), patch.object(
                reply_inbox.decrypt, 'run', return_value={'message':'ok'}) as refresh:
            reply_inbox.refresh()
            self.switch('account-B')
            reply_inbox.refresh()
        self.assertEqual(refresh.call_count, 2)


if __name__ == '__main__':
    unittest.main()
