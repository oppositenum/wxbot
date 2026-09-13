"""陪伴型定时问候：免打扰窗口、正在聊天跳过、按时段生成、免前缀但仍被排除，及本机免鉴权。

所有外呼(LLM/发送/取消息/人设)均被 mock：不触碰真实微信、模型或网络。
"""
import os
import time
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from core import account_session as sessions, schedule


def _struct(hhmm):
    return time.strptime(f"2026-09-13 {hhmm}", "%Y-%m-%d %H:%M")


class Quiet(unittest.TestCase):
    def test_same_day_window(self):
        q = {"start": "13:00", "end": "14:00"}
        self.assertTrue(schedule._in_quiet(q, _struct("13:30")))
        self.assertTrue(schedule._in_quiet(q, _struct("13:00")))   # 含起点
        self.assertFalse(schedule._in_quiet(q, _struct("14:00")))  # 不含终点
        self.assertFalse(schedule._in_quiet(q, _struct("12:59")))

    def test_wrap_midnight(self):
        q = {"start": "23:00", "end": "08:00"}
        self.assertTrue(schedule._in_quiet(q, _struct("23:30")))
        self.assertTrue(schedule._in_quiet(q, _struct("00:10")))
        self.assertTrue(schedule._in_quiet(q, _struct("07:59")))
        self.assertFalse(schedule._in_quiet(q, _struct("08:00")))
        self.assertFalse(schedule._in_quiet(q, _struct("12:00")))

    def test_none_or_bad(self):
        self.assertFalse(schedule._in_quiet(None, _struct("03:00")))
        self.assertFalse(schedule._in_quiet({"start": "x", "end": "y"}, _struct("03:00")))
        self.assertFalse(schedule._in_quiet({"start": "09:00", "end": "09:00"}, _struct("09:00")))

    def test_period(self):
        self.assertEqual(schedule._period(2), "深夜")
        self.assertEqual(schedule._period(6), "清晨")
        self.assertEqual(schedule._period(9), "上午")
        self.assertEqual(schedule._period(20), "夜晚")
        self.assertEqual(schedule._period(23), "深夜")


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.account = "acct-greet"
        for target, value in [("config.ACCOUNTS_DIR", self.temp.name),
                              ("config.WORK_DIR", self.temp.name),  # 隔离 send-ledger.sqlite3,避免跨运行幂等命中
                              ("config.wxid", lambda: self.account),
                              ("core.account_session._identity_probe", None),
                              ("core.account_session._current", None)]:
            p = patch(target, value); p.start(); self.addCleanup(p.stop)
        os.makedirs(config.account_dir(), exist_ok=True)
        self.session = sessions.capture()

    def _task(self, **over):
        t = dict(id=1, send_uid="u1", title="定时问候", target="她", target_username="wxid_her",
                 target_display="她", is_group=False, cron="* * * * *", once_at=None,
                 prompt="", use_llm=True, persona=None, kind="greeting", quiet=None,
                 enabled=True, created="2026-09-13 09:00")
        t.update(over)
        return t


class TickSkip(Base):
    def _run_tick(self, task, recent=False, quiet_hit=False):
        schedule.save_tasks([task])
        fired = []
        with patch.object(schedule, "fire",
                          side_effect=lambda t, **k: fired.append(t) or {"status": "confirmed"}), \
             patch.object(schedule, "_recent_inbound", return_value=recent), \
             patch.object(schedule, "_in_quiet", return_value=quiet_hit):
            schedule.tick()
        return fired, schedule.load_tasks()[0]

    def test_normal_fires_and_marks(self):
        fired, saved = self._run_tick(self._task())
        self.assertEqual(len(fired), 1)
        self.assertIn("last_min", saved)          # 成功触发后置游标

    def test_quiet_skips_without_marking(self):
        fired, saved = self._run_tick(self._task(quiet={"start": "00:00", "end": "23:59"}), quiet_hit=True)
        self.assertEqual(fired, [])
        self.assertNotIn("last_min", saved)       # 跳过不置游标,下周期再看

    def test_recent_chat_skips(self):
        fired, saved = self._run_tick(self._task(), recent=True)
        self.assertEqual(fired, [])
        self.assertNotIn("last_min", saved)


class Content(Base):
    def test_compose_greeting_persona_and_period(self):
        captured = {}
        with patch("core.personalization.resolve_persona",
                   return_value={"persona": {"persona": "P", "name": "N"}}), \
             patch("core.personalization.role_context", return_value="ROLE-CTX"), \
             patch("core.llm.chat",
                   side_effect=lambda system, msgs, **k: captured.update(system=system) or "想你了～"):
            out = schedule._compute_greeting(self._task(_trigger_at=time.mktime(_struct("07:30"))))
        self.assertEqual(out, "想你了～")
        self.assertIn("ROLE-CTX", captured["system"])
        self.assertIn("陪", captured["system"])           # 陪伴框架(非提醒框架)

    def test_compute_text_routes_greeting(self):
        with patch.object(schedule, "_compute_greeting", return_value="早安") as g:
            self.assertEqual(schedule._compute_text(self._task()), "早安")
        g.assert_called_once()


class FireNoPrefix(Base):
    def test_greeting_send_omits_prefix_but_recorded(self):
        sent = {}
        with patch("core.sender.preflight", return_value=None), \
             patch("core.sender.send_text",
                   side_effect=lambda disp, text, **k: sent.update(text=text) or {"status": "confirmed", "reason": "ok"}), \
             patch("core.personalization.resolve_persona",
                   return_value={"persona": {"persona": "P", "name": "N"}}), \
             patch("core.personalization.role_context", return_value="ROLE"), \
             patch("core.llm.chat", return_value="陪你到天亮"):
            row = schedule.fire(self._task(), occurrence="cron:1", session=self.session)
        self.assertIn(row["status"], ("confirmed", "submitted"))
        self.assertEqual(sent["text"], "陪你到天亮")                 # 无【定时提醒】前缀
        self.assertFalse(sent["text"].startswith(schedule.PREFIX))
        # 无前缀,但靠执行记录仍被判定为定时消息(从而排除上下文/记忆/检索)
        msg = {"is_self": True, "content": "陪你到天亮", "create_time": int(time.time())}
        self.assertTrue(schedule.is_scheduled_msg(msg, chat="wxid_her"))


class AddGreeting(Base):
    def test_add_and_list(self):
        with patch.object(schedule, "resolve_target", return_value=("wxid_her", False, "她")):
            t, err = schedule.add_greeting(target="她", cron="0 * * * *",
                                           quiet={"start": "23:00", "end": "08:00"})
        self.assertIsNone(err)
        self.assertEqual(t["kind"], "greeting")
        self.assertTrue(t["use_llm"])
        self.assertEqual(t["quiet"]["start"], "23:00")
        self.assertEqual([g["id"] for g in schedule.list_greetings()], [t["id"]])


class Auth(unittest.TestCase):
    def test_local_env_bypasses_token(self):
        import server
        with patch.dict(os.environ, {"WXBOT_LOCAL": "1"}), \
             server.app.test_request_context("/"):
            self.assertTrue(server.local_management_access())
        with patch.dict(os.environ, {"WXBOT_LOCAL": "0", "WXBOT_LOCAL_ADMIN": "0"}), \
             server.app.test_request_context("/"):
            self.assertFalse(server.local_management_access())


if __name__ == "__main__":
    unittest.main()
