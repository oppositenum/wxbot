"""Synthetic auto-reply batches only; no real WeChat or model requests."""
import copy
import unittest
from unittest.mock import patch

from test_send_safety import Isolated, forbidden
from core import bot, send_ledger


def pending(text, **fields):
    return {'rule': {'name': 'test', 'action': {'type': 'reply_ai'}},
            'msgs': [dict(local_id=1, type=1, is_self=False, content=text, **fields)]}


class Classification(unittest.TestCase):
    def test_standalone_closings(self):
        for text in ['好的', '明白了', '知道了', '收到！', ' 嗯嗯 ', '好的。👍',
                     'ＯＫ', 'okay!', '好的，谢谢', '收到，知道了', '晚安～', '先这样吧']:
            with self.subTest(text=text):
                self.assertTrue(bot._single_closing_reply(pending(text)))

    def test_questions_negation_and_new_content_are_kept(self):
        for text in ['好的？', '明白了吗', '知道了?', '还没明白', '不知道了',
                     '好的，明天几点？', '好的，帮我查一下', '收到的文件打不开',
                     '他说“好的”', '好的\n还有一件事', '晚安是什么意思', '好的[疑问]', '']:
            with self.subTest(text=text):
                self.assertFalse(bot._single_closing_reply(pending(text)))

    def test_multiple_messages_are_kept_even_if_last_is_closing(self):
        p = pending('明天几点见？'); p['msgs'] += pending('好的')['msgs']
        self.assertFalse(bot._single_closing_reply(p))
        p = pending('好的'); p['msgs'] += pending('知道了')['msgs']
        self.assertFalse(bot._single_closing_reply(p))

    def test_media_quotes_mentions_self_and_forward_are_kept(self):
        for fields in [{'type': 49}, {'type': 34}, {'type': 3}, {'is_self': True},
                       {'refer': {'content': '问题'}}, {'at_me': True}, {'quote_me': True}]:
            p = pending('好的'); p['msgs'][0].update(fields)
            self.assertFalse(bot._single_closing_reply(p))
        p = pending('好的'); p['rule']['action']['type'] = 'forward'
        self.assertFalse(bot._single_closing_reply(p))


class Integration(Isolated):
    def batch(self, text):
        p = pending(text); p.update(session=self.token, ctx=[])
        bot._pending['chat-A'] = p
        return p

    def test_skip_calls_neither_generation_send_nor_learning_and_is_durable(self):
        p = self.batch('好的'); replay = copy.deepcopy(p)
        with patch.object(bot, 'do_action', forbidden), patch.object(bot, '_maybe_learn', forbidden):
            bot.process_pending('chat-A', p, {}, lambda *a: None)
            self.assertNotIn('chat-A', bot._pending)
            self.assertEqual(send_ledger.result(p['job_id'])['reason'], 'single_closing_message')
            bot._pending['chat-A'] = replay
            bot.process_pending('chat-A', replay, {}, lambda *a: None)
        self.assertNotIn('chat-A', bot._pending)
        self.assertEqual(self.adapter.calls, 0)

    def test_new_message_after_skip_is_processed_normally(self):
        p = self.batch('知道了')
        bot.process_pending('chat-A', p, {}, lambda *a: None)
        next_msg = dict(local_id=2, type=1, is_self=False, content='明天几点见？')
        p = bot.enqueue_pending('chat-A', next_msg, [], pending('')['rule'], 100)
        with patch.object(bot, 'do_action', return_value={'status':'submitted', 'reason':'test'}) as action, patch.object(bot, '_maybe_learn'):
            bot.process_pending('chat-A', p, {}, lambda *a: None)
        action.assert_called_once(); self.assertNotIn('chat-A', bot._pending)

    def test_multiple_message_batch_reaches_reply_generation_intact(self):
        p = self.batch('明天几点见？')
        p['msgs'].append(dict(local_id=2, type=1, is_self=False, content='好的'))
        with patch.object(bot, 'do_action', return_value={'status':'submitted', 'reason':'test'}) as action, patch.object(bot, '_maybe_learn'):
            bot.process_pending('chat-A', p, {}, lambda *a: None)
        self.assertEqual(len(action.call_args.kwargs['batch_msgs']), 2)

    def test_existing_uncertain_batch_is_not_reclassified_or_discarded(self):
        p = self.batch('好的'); p['send_status'] = 'uncertain'
        with patch.object(bot, 'do_action', forbidden):
            bot.process_pending('chat-A', p, {}, lambda *a: None)
        self.assertIs(bot._pending['chat-A'], p)


if __name__ == '__main__':
    unittest.main()
