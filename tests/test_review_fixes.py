"""Isolated route/media/authorization regression tests. No network or real UI sends.

Run: python3 -B tests/test_review_fixes.py
"""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from core import llm, bot, media_read, read_access, tools, contacts, agent


def blocked(*args, **kwargs):
    raise AssertionError("real network/UI/model call forbidden in offline tests")


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wx-review-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        for target, value in (("config.account_dir", lambda: str(self.root)),
                              ("config.account_key", lambda: "wxid_accountA"),
                              ("config.wxid", lambda: "wxid_accountA"),
                              ("socket.create_connection", blocked),
                              ("socket.socket.connect", blocked),
                              ("subprocess.run", blocked),
                              ("subprocess.Popen", blocked),
                              ("core.llm._post", blocked),
                              ("core.llm.load_cfg", lambda: {}),
                              ("core.sender.send_text", blocked),
                              ("core.sender.send_image", blocked),
                              ("core.sender.send_at", blocked)):
            p = patch(target, value); p.start(); self.addCleanup(p.stop)
        bot._media_desc.clear()
        llm._ROUTE_LOG.clear()

    def access(self, chat="wxid_friend", members=None):
        members = members or [chat]
        return read_access.Access("wxid_accountA", str(self.root), chat,
                                  frozenset(members), tuple((m, m) for m in members))


class Routes(Isolated):
    cfg = {"provider": "gpt", "gpt": {"base_url": "https://gateway.invalid", "api_key": "SECRET_G", "model": "configured-gpt-name"},
           "claude": {"base_url": "https://gateway.invalid", "api_key": "SECRET_C", "model": "configured-claude-name"}}
    specs = [{"name": "lookup", "description": "test", "input_schema": {"type": "object", "properties": {}}}]

    def responses(self, values):
        self.requests = []
        values = iter(values)
        def post(url, headers, body, proxy):
            self.requests.append((url, copy.deepcopy(body)))
            return next(values)
        p = patch.object(llm, "_post", post); p.start(); self.addCleanup(p.stop)

    def gpt_call(self, arguments="{}", name="lookup"):
        return {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "call1", "type": "function", "function": {"name": name, "arguments": arguments}}]}}]}

    def gpt_text(self, text="done"):
        return {"choices": [{"message": {"content": text}}]}

    def test_gpt_with_claude_credentials_stays_gpt_and_finishes(self):
        self.responses([self.gpt_call(), self.gpt_text()])
        called = []
        out = llm.chat_tools("PRIVATE_PROMPT", [{"role": "user", "content": "PRIVATE_BODY"}], self.specs,
                             lambda n, a: called.append((n, a)) or "result", self.cfg, max_rounds=1)
        self.assertEqual(out, "done"); self.assertEqual(called, [("lookup", {})])
        self.assertTrue(all(u.endswith("chat/completions") and b["model"] == "configured-gpt-name" for u, b in self.requests))
        final = self.requests[-1][1]
        self.assertNotIn("tools", final)
        self.assertEqual(final["messages"][-1], {"role": "tool", "tool_call_id": "call1", "content": "result"})
        diag = json.dumps(llm.route_diagnostics())
        for secret in ("SECRET", "PRIVATE", "gateway.invalid"):
            self.assertNotIn(secret, diag)
        self.assertIn("tools_final", diag); self.assertIn("configured-gpt-name", diag)
        self.assertEqual(len({r["request_id"] for r in llm.route_diagnostics()}), 1)

    def test_explicit_independent_claude_final_route(self):
        self.responses([{"content": [{"type": "tool_use", "id": "c1", "name": "lookup", "input": {}}]},
                        {"content": [{"type": "text", "text": "done"}]}])
        cfg = dict(self.cfg, tools_provider="claude", tools_model="unchanged-custom-model")
        llm.chat_tools("sys", [], self.specs, lambda *a: "result", cfg, max_rounds=1)
        self.assertTrue(all(u.endswith("/messages") and b["model"] == "unchanged-custom-model" for u, b in self.requests))
        self.assertEqual(self.requests[-1][1]["messages"][-1]["content"][0]["tool_use_id"], "c1")

    def test_unsupported_adapter_never_calls_network_or_falls_back(self):
        with patch.object(llm, "TOOL_PROVIDERS", frozenset({"claude"})):
            with self.assertRaises(llm.CapabilityError):
                llm.chat_tools("sys", [], self.specs, blocked, self.cfg)

    def test_gateway_rejection_does_not_fallback(self):
        with patch.object(llm, "_post", side_effect=RuntimeError("HTTP 400")) as post:
            with self.assertRaises(RuntimeError):
                llm.chat_tools("sys", [], self.specs, blocked, self.cfg)
            self.assertEqual(post.call_count, 1)

    def test_main_chat_diagnostics_and_explicit_tools_do_not_change_chat(self):
        self.responses([self.gpt_text()])
        llm.chat("private", [], dict(self.cfg, tools_provider="claude"))
        self.assertEqual(llm.route_diagnostics()[0]["provider"], "gpt")
        self.assertEqual(llm.route_diagnostics()[0]["phase"], "chat")

    def test_invalid_or_unlisted_tool_never_dispatched(self):
        for args, name in (("not json", "lookup"), ("[]", "lookup"), ("{}", "not-listed")):
            self.responses([self.gpt_call(args, name), self.gpt_text()])
            llm.chat_tools("sys", [], self.specs, blocked, self.cfg, max_rounds=1)

    def test_empty_result_only_one_finalization(self):
        self.responses([self.gpt_text(""), self.gpt_text("done")])
        self.assertEqual(llm.chat_tools("sys", [], self.specs, blocked, self.cfg), "done")
        self.assertEqual(len(self.requests), 2)

    def test_no_fallback_after_empty_agent(self):
        msg = {"type": 1, "content": "hello", "sender": None}
        with patch.object(agent, "agent_config", return_value={"enabled": True, "tools": []}), \
             patch.object(agent, "run", return_value=""), patch.object(bot, "_sender_name", return_value="friend"), \
             patch.object(bot, "send_name_for", return_value="friend"), patch.object(llm, "chat", blocked):
            self.assertEqual(bot._ai_reply({"persona": "sys"}, "wxid_friend", msg, [msg]), "")


class Media(Isolated):
    msg = {"type": 3, "local_id": 1, "server_id": 1, "content": "PRIVATE_XML_SCENE"}

    def setUp(self):
        super().setUp()
        from PIL import Image
        b = io.BytesIO(); Image.new("RGB", (2, 2), "red").save(b, "PNG"); self.png = b.getvalue()
        for target in ("core.imgdec.get_msg_image", "core.media.get_msg_voice", "core.media.get_msg_video_thumb",
                       "core.llm.describe_image", "core.llm.transcribe"):
            p = patch(target, blocked); p.start(); self.addCleanup(p.stop)

    def test_success_validated_bytes_mime_and_context(self):
        with patch("core.imgdec.get_msg_image", return_value=(self.png, "image/jpeg")), \
             patch.object(llm, "describe_image", return_value=json.dumps({"status": "success", "description": "red square"})) as vision:
            r = media_read.read("friend", self.msg, {})
        self.assertEqual(r.status, "success"); self.assertIn("red square", r.context())
        self.assertEqual(vision.call_args.kwargs["media_type"], "image/png")

    def test_missing_key_file_decode_reasons_do_not_request_model(self):
        for reason, want in (("no-img-key(details)", "missing_key"), ("no-dat", "file_unavailable"),
                             ("no-md5", "file_unavailable"), ("decode-failed", "decode_failed")):
            with patch("core.imgdec.get_msg_image", return_value=(None, reason)):
                r = media_read.read("friend", self.msg, {})
                self.assertEqual(r.status, want); self.assertNotIn("PRIVATE", r.context())
                self.assertIn("不得描述", r.context())

    def test_bad_image_header_or_truncation_never_sent_to_vision(self):
        for data in (b"not-an-image", self.png[:35]):
            with patch("core.imgdec.get_msg_image", return_value=(data, "image/png")):
                self.assertEqual(media_read.read("friend", self.msg, {}).status, "decode_failed")

    def test_api_failure_unconfigured_and_empty_are_distinct(self):
        with patch("core.imgdec.get_msg_image", return_value=(self.png, "image/png")):
            for error, want in ((RuntimeError("secret body"), "api_failed"), (llm.CapabilityError("key missing"), "not_configured")):
                with patch.object(llm, "describe_image", side_effect=error):
                    r = media_read.read("friend", self.msg, {})
                    self.assertEqual(r.status, want); self.assertNotIn("secret", r.context())
            for value in (None, " ", {}, "NONE"):
                with patch.object(llm, "describe_image", return_value=value):
                    self.assertEqual(media_read.read("friend", self.msg, {}).status, "invalid_result")

    def test_stt_off_no_decode_no_request(self):
        r = media_read.read("friend", dict(self.msg, type=34), {})
        self.assertEqual(r.status, "disabled")
        self.assertIn("语音未读取", r.context())

    def test_vision_unreadable_and_invalid_contract_are_not_success(self):
        with patch("core.imgdec.get_msg_image", return_value=(self.png, "image/png")):
            for raw, want in (({"status": "unreadable", "description": ""}, "unreadable"),
                              ({"status": "success", "description": ""}, "invalid_result"),
                              ({"description": "unverified text"}, "invalid_result"),
                              ([], "invalid_result")):
                with patch.object(llm, "describe_image", return_value=json.dumps(raw)):
                    result = media_read.read("friend", self.msg, {})
                    self.assertEqual(result.status, want)
                    self.assertIn("未读取", result.context())

    def test_voice_decode_failure_and_success(self):
        m = dict(self.msg, type=34)
        for reason, want in (("no-voice-data", "file_unavailable"), ("not-silk", "decode_failed"),
                             ("decode-error", "decode_failed"), ("no-silk-decoder", "decoder_unavailable")):
            with patch("core.media.get_msg_voice", return_value=(None, reason)):
                self.assertEqual(media_read.read("friend", m, {"enable_stt": True}).status, want)
        with patch("core.media.get_msg_voice", return_value=(b"wav", "audio/wav")), \
             patch.object(llm, "transcribe", return_value="hello") as stt:
            self.assertEqual(media_read.read("friend", m, {"enable_stt": True}).status, "success")
            self.assertEqual(stt.call_args.kwargs["filename"], "voice.wav")

    def test_video_only_claims_cover(self):
        with patch("core.media.get_msg_video_thumb", return_value=(self.png, "image/png")), \
             patch.object(llm, "describe_image", return_value=json.dumps({"status": "success", "description": "red square"})):
            r = media_read.read("friend", dict(self.msg, type=43), {})
            self.assertEqual(r.kind, "视频封面")

    def test_pure_media_failure_deterministic_no_reply_model(self):
        with patch.object(bot, "_media_result", return_value=media_read.Result("missing_key", "图片")), \
             patch.object(llm, "chat", blocked), patch.object(agent, "run", blocked):
            out = bot._ai_reply({"persona": "pretend you saw everything"}, "friend", self.msg, [self.msg])
        self.assertIn("没能读取", out); self.assertNotIn("missing_key", out)

    def test_mixed_text_failure_preserves_text_and_honesty(self):
        text = {"type": 1, "local_id": 2, "content": "请帮我安排明天的工作", "sender": None}
        with patch.object(bot, "_media_result", return_value=media_read.Result("api_failed", "图片")) as read, \
             patch.object(bot, "_sender_name", return_value="friend"), \
             patch.object(agent, "agent_config", return_value={"enabled": False}), \
             patch.object(llm, "chat", return_value="reply") as model:
            bot._ai_reply({"persona": "sys"}, "friend", self.msg, [self.msg, text], batch_msgs=[self.msg, text])
            prompt = str(model.call_args)
            self.assertIn(text["content"], prompt); self.assertIn("图片未读取", prompt)
            self.assertIn("禁止声称看过", prompt); self.assertNotIn("PRIVATE_XML", prompt)
            self.assertEqual(read.call_count, 1)

    def test_mixed_batch_text_before_many_images_is_not_lost(self):
        text = {"type": 1, "local_id": 99, "content": "本批文字不能丢", "sender": None}
        batch = [text] + [dict(self.msg, local_id=i, server_id=i) for i in range(1, 25)]
        with patch.object(bot, "_media_result", return_value=media_read.Result("api_failed", "图片")), \
             patch.object(bot, "_sender_name", return_value="friend"), \
             patch.object(agent, "agent_config", return_value={"enabled": False}), \
             patch.object(llm, "chat", return_value="reply") as model:
            bot._ai_reply({"persona": "sys"}, "friend", batch[-1], batch, batch_msgs=batch)
            self.assertIn(text["content"], str(model.call_args))

    def test_cache_is_account_scoped_and_only_success_cached(self):
        with patch.object(media_read, "read", return_value=media_read.Result("success", "图片", "a")) as reader:
            bot._media_result("friend", self.msg); bot._media_result("friend", self.msg)
            self.assertEqual(reader.call_count, 1)
            with patch.object(config, "account_key", return_value="wxid_other"):
                bot._media_result("friend", self.msg)
            self.assertEqual(reader.call_count, 2)
        bot._media_desc.clear()
        with patch.object(media_read, "read", return_value=media_read.Result("api_failed", "图片")) as reader:
            bot._media_result("friend", self.msg); bot._media_result("friend", self.msg)
            self.assertEqual(reader.call_count, 2)

    def test_passive_media_no_extra_model_calls(self):
        out = bot._passive_content("friend", self.msg)
        self.assertIn("未读取", out); self.assertNotIn("PRIVATE_XML", out)


class Authorization(Isolated):
    def setUp(self):
        super().setUp()
        self.auth = self.access()
        self.ctx = {"chat": self.auth.chat, "read_access": self.auth}
        d = self.root / "profiles"; d.mkdir()
        facts = [{"id": str(i), "text": text, "status": status, "scope": scope, "expires_ts": expiry}
                 for i, (text, status, scope, expiry) in enumerate([
                     ("ALLOWED", "active", "chat:wxid_friend", None),
                     ("CANCELLED", "cancelled", "chat:wxid_friend", None),
                     ("EXPIRED", "active", "chat:wxid_friend", 1),
                     ("OTHER_SCOPE", "active", "group:room", None),
                     ("GLOBAL_BYPASS", "active", "global:anything", None)])]
        (d / "wxid_friend.json").write_text(json.dumps({"wxid": "wxid_friend", "summary": "CANCELLED EXPIRED OTHER_SCOPE", "facts": facts}))

    def test_missing_context_chat_and_model_forgery_denied(self):
        for ctx in (None, {}, {"chat": ""}, {"chat": "wxid_friend"},
                    {"chat": "", "read_access": self.auth}, {"chat": "wxid_friend", "read_access": self.auth.__dict__}):
            for name in ("search_history", "get_member_profile", "search_kb"):
                out = tools.run(name, {"query": "secret", "chat": "wxid_friend", "account": "wxid_accountA", "name_or_wxid": "wxid_friend"}, ctx)
                self.assertIn("拒绝", out)

    def test_direct_calls_cannot_bypass_run(self):
        self.assertIn("授权", tools.search_history("x", "wxid_friend"))
        self.assertIn("授权", tools.get_member_profile("wxid_friend", scope="chat:wxid_friend"))
        self.assertIn("授权", tools.search_kb("x"))

    def test_other_chat_and_same_account_other_person_denied(self):
        self.assertIn("只允许", tools.run("search_history", {"chat": "wxid_other", "query": "x"}, self.ctx))
        self.assertIn("明确成员", tools.run("get_member_profile", {"name_or_wxid": "wxid_other"}, self.ctx))

    def test_profile_summary_status_and_scope_cannot_bypass(self):
        out = tools.run("get_member_profile", {"name_or_wxid": "wxid_friend"}, self.ctx)
        self.assertIn("ALLOWED", out)
        for secret in ("CANCELLED", "EXPIRED", "OTHER_SCOPE", "GLOBAL_BYPASS"):
            self.assertNotIn(secret, out)

    def test_stale_other_account_denied_before_read(self):
        with patch.object(config, "account_key", return_value="wxid_accountB"), patch.object(read_access, "profile", blocked):
            self.assertIn("拒绝", tools.run("get_member_profile", {"name_or_wxid": "wxid_friend"}, self.ctx))

    def test_account_change_during_read_suppresses_return(self):
        original = read_access.profile
        def changed(access, member):
            value = original(access, member)
            p = patch.object(config, "account_key", return_value="wxid_accountB"); p.start(); self.addCleanup(p.stop)
            return value
        with patch.object(read_access, "profile", changed):
            self.assertIn("拒绝", tools.get_member_profile("wxid_friend", access=self.auth))

    def test_group_member_scope_and_wrong_scope(self):
        a = self.access("room@chatroom", ["wxid_friend"])
        self.assertNotIn("ALLOWED", tools.get_member_profile("wxid_friend", access=a))
        self.assertIn("不允许", tools.get_member_profile("wxid_friend", scope="chat:wxid_friend", access=a))

    def test_issue_requires_valid_server_identity(self):
        with patch.object(contacts, "list_contacts", return_value=[{"username": "wxid_friend", "name": "friend"}]):
            self.assertTrue(read_access.valid(read_access.issue("wxid_friend")))
            self.assertIsNone(read_access.issue("wxid_other"))
            self.assertIsNone(read_access.issue(""))
            with patch.object(config, "account_key", return_value="default"):
                self.assertIsNone(read_access.issue("wxid_friend"))

    def test_real_fixture_history_bound_to_account_and_excludes_schedules(self):
        d = self.root / "decrypted"; d.mkdir()
        con = sqlite3.connect(d / "message.db")
        con.execute("CREATE TABLE Name2Id(user_name TEXT)")
        con.executemany("INSERT INTO Name2Id VALUES(?)", [("wxid_accountA",), ("wxid_friend",)])
        table = "Msg_" + hashlib.md5(b"wxid_friend").hexdigest()
        con.execute("CREATE TABLE " + table + " (local_type INTEGER, real_sender_id INTEGER, create_time INTEGER, message_content TEXT)")
        con.executemany("INSERT INTO " + table + " VALUES(?,?,?,?)", [(1, 1, 1, "【定时提醒】secret"), (1, 2, 2, "find allowed"), (3, 2, 3, "private image xml")])
        con.commit(); con.close()
        out = tools.search_history("find", self.auth.chat, access=self.auth)
        self.assertIn("find allowed", out); self.assertNotIn("secret", out)
        # Global account directory may move mid-operation; pinned reader still opens only A.
        other = self.root / "other"; other.mkdir()
        with patch.object(config, "account_dir", return_value=str(other)):
            self.assertIn("授权", tools.search_history("find", self.auth.chat, access=self.auth))

    def test_private_management_is_explicit_separate_and_audited(self):
        import server
        client = server.app.test_client()
        with patch.dict(os.environ, {"WXBOT_ADMIN_READ_TOKEN": "ADMIN_TEST_SECRET"}), \
             patch.object(server.app.logger, "warning") as audit:
            self.assertEqual(client.get("/api/profiles/wxid_friend").status_code, 403)
            self.assertEqual(client.get("/api/kb").status_code, 403)
            response = client.get("/api/profiles/wxid_friend", headers={"X-Wxbot-Admin-Token": "ADMIN_TEST_SECRET"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("CANCELLED", response.json["summary"])  # owner view, never a model tool result
            self.assertNotIn("ADMIN_TEST_SECRET", str(audit.call_args_list))
            self.assertNotIn("CANCELLED", str(audit.call_args_list))

    def test_llm_settings_explicit_route_exposed_without_secrets(self):
        import server
        cfg = copy.deepcopy(Routes.cfg)
        cfg.update(tools_provider="claude", tools_model="custom-tool-model")
        with patch.object(llm, "load_cfg", return_value=cfg):
            response = server.app.test_client().get("/api/llm")
        self.assertEqual(response.json["tool_route"]["provider"], "claude")
        self.assertEqual(response.json["tool_route"]["model"], "custom-tool-model")
        self.assertNotIn("SECRET", response.get_data(as_text=True))

    def test_tool_config_roundtrip_writes_only_temporary_config(self):
        import server
        path = self.root / "llm_config.json"
        cfg = copy.deepcopy(Routes.cfg)
        path.write_text(json.dumps(cfg))
        with patch.object(llm, "CONFIG_FILE", str(path)), \
             patch.object(llm, "load_cfg", side_effect=lambda: json.loads(path.read_text())):
            client = server.app.test_client()
            response = client.post("/api/llm/config", json={"tools_provider": "claude", "tools_model": "literal-custom-name"})
            self.assertEqual(response.status_code, 200)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["gpt"], cfg["gpt"])
            self.assertEqual(saved["claude"], cfg["claude"])
            self.assertEqual(client.get("/api/llm").json["tool_route"]["model"], "literal-custom-name")
            self.assertEqual(client.post("/api/llm/config", json={"provider": ""}).status_code, 400)
            self.assertEqual(client.post("/api/llm/config", json={"tools_provider": "unknown"}).status_code, 400)
            self.assertEqual(client.post("/api/llm/config", json={"tools_model": {"api_key": "bad"}}).status_code, 400)
            client.post("/api/llm/config", json={"tools_provider": "", "tools_model": ""})
            self.assertEqual(client.get("/api/llm").json["tool_route"]["model"], "configured-gpt-name")


if __name__ == "__main__":
    unittest.main(verbosity=2)
