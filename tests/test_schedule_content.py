import json
import unittest
from datetime import datetime
from unittest.mock import patch
import test_moments as fixtures
from core import schedule as s

class ScheduleContent(unittest.TestCase):
    setUp=fixtures.Moments.setUp

    def test_separate_times_keep_separate_content(self):
        parsed=dict(action='create',target='我',tasks=[dict(title='早安',cron='30 6 * * *',prompt='早上好，该起床啦。',use_llm=False),dict(title='晚安',cron='30 21 * * *',prompt='记得刷牙洗漱，晚安。',use_llm=False)])
        ctx=dict(chat_username='friend',chat_display='朋友',requester_wxid='friend',requester_name='朋友',is_group=False)
        with patch.object(s.llm,'available',return_value=True),patch.object(s,'parse_nl',return_value=parsed),patch.object(s,'resolve_target',return_value=('friend',False,'朋友')):
            r=s.handle_nl('早上六点半叫起床，晚上九点半洗漱',ctx)
        self.assertTrue(r['ok']);tasks=s.load_tasks();self.assertEqual(len(tasks),2)
        self.assertEqual([(t['cron'],s._compute_text(t)) for t in tasks],[('30 6 * * *','早上好，该起床啦。'),('30 21 * * *','记得刷牙洗漱，晚安。')])
        self.assertTrue(all(t['creator_wxid']=='friend' for t in tasks))

    def test_invalid_second_entry_writes_nothing(self):
        parsed=dict(action='create',target='朋友',tasks=[dict(cron='30 6 * * *',prompt='起床'),dict(cron='30 25 * * *',prompt='洗漱')])
        with patch.object(s.llm,'available',return_value=True),patch.object(s,'parse_nl',return_value=parsed):
            self.assertFalse(s.handle_nl('创建')['ok'])
        self.assertEqual(s.load_tasks(),[])

    def test_same_content_multi_cron_still_supported(self):
        parsed=dict(action='create',target='朋友',crons=['0 10 * * *','0 20 * * *'],prompt='喝水啦',use_llm=False)
        with patch.object(s.llm,'available',return_value=True),patch.object(s,'parse_nl',return_value=parsed),patch.object(s,'resolve_target',return_value=('friend',False,'朋友')):
            self.assertTrue(s.handle_nl('每天十点二十点喝水')['ok'])
        self.assertEqual([t['prompt'] for t in s.load_tasks()],['喝水啦','喝水啦'])

    def test_model_receives_china_trigger_time_and_failure_never_returns_full_instructions(self):
        task=dict(prompt='早上起床，晚上洗漱',use_llm=True,cron='30 6 * * *',_trigger_at=datetime(2026,9,12,6,30,tzinfo=s.CHINA).timestamp())
        with patch.object(s.llm,'chat',return_value='早上好，该起床了') as model:
            self.assertEqual(s._compute_text(task),'早上好，该起床了')
            payload=json.loads(model.call_args.args[1][0]['content']);self.assertEqual(payload['trigger_time_china'],'2026-09-12 06:30')
        for answer in ['',None]:
            with patch.object(s.llm,'chat',return_value=answer):
                with self.assertRaises(ValueError):s._compute_text(task)
        with patch.object(s.llm,'chat',side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):s._compute_text(task)
