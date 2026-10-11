# -*- coding: utf-8 -*-
"""Stage 12 (Commit 12): 受控账户管理 API 测试。

验证（不发起真实 HTTP 请求 / 不拉起真实子进程）：
- CSRF：会话绑定、恒定时间校验、无会话/错令牌拒绝
- 审计：_append_audit 落盘 + load_audit 读取（日期严格校验）
- perform_account_action：enable/disable/set_task_policy 经注册表生效；
  run 经 _spawn_run（打桩）；非法 ID / 未知动作 / 不存在账户的错误码
"""

import json
import os
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import dashboard.app as dash  # noqa: E402
from models import account_registry as reg  # noqa: E402
from models.account import Account, generate_account_id  # noqa: E402

AID_A = "acct_aaa11111"


class TestCsrf(unittest.TestCase):
    def test_csrf_session_binding(self):
        token = dash._new_session()
        csrf = dash._session_csrf(token)
        self.assertTrue(csrf)
        self.assertTrue(dash._csrf_valid(token, csrf))
        # 错令牌 / 空令牌 / 不存在的会话 一律拒绝
        self.assertFalse(dash._csrf_valid(token, csrf + "x"))
        self.assertFalse(dash._csrf_valid(token, ""))
        self.assertFalse(dash._csrf_valid("no-such-session", csrf))
        # 每个会话的 CSRF 独立
        token2 = dash._new_session()
        self.assertNotEqual(csrf, dash._session_csrf(token2))

    def test_csrf_after_logout(self):
        token = dash._new_session()
        csrf = dash._session_csrf(token)
        with dash._sessions_lock:
            dash._sessions.pop(token, None)
        self.assertFalse(dash._csrf_valid(token, csrf))


class TestAudit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dash12_audit_")
        self._orig = dash.AUDIT_DIR
        dash.AUDIT_DIR = self.tmp

    def tearDown(self):
        dash.AUDIT_DIR = self._orig

    def test_append_and_load(self):
        e1 = dash._append_audit("enable", AID_A, {"enabled": True})
        e2 = dash._append_audit("run", AID_B := "acct_bbb22222", {"pid": 1})
        day = e1["ts"][:10]
        audit = dash.load_audit(day)
        self.assertEqual([a["action"] for a in audit],
                         ["enable", "run"])
        self.assertEqual(audit[0]["detail"], {"enabled": True})
        self.assertEqual(audit[1]["account_id"], AID_B)
        self.assertEqual(audit, [e1, e2])

    def test_load_audit_bad_date(self):
        dash._append_audit("run", AID_A)
        self.assertEqual(dash.load_audit("../etc"), [])
        self.assertEqual(dash.load_audit(""), [])
        self.assertEqual(dash.load_audit("2026-13-99"), [])


class TestPerformAction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dash12_reg_")
        self.user_dir = os.path.join(self.tmp, "user")
        self.reg_path = os.path.join(self.tmp, "accounts", "index.json")
        os.makedirs(self.user_dir)
        # 审计也隔离到临时目录（动作会触发 _append_audit）
        self._orig_audit = dash.AUDIT_DIR
        dash.AUDIT_DIR = os.path.join(self.tmp, "audit")
        with open(os.path.join(self.user_dir, "a.json"), "w") as f:
            f.write("{}")
        aid = reg.add_account(
            Account(account_id=AID_A, display_name="测试A",
                    config_file="a.json"),
            user_dir=self.user_dir, registry_path=self.reg_path)
        self.assertTrue(aid)

    def test_enable_disable(self):
        resp, code = dash.perform_account_action(
            AID_A, "disable", registry_path=self.reg_path,
            user_dir=self.user_dir)
        self.assertEqual(code, 200)
        self.assertFalse(resp["enabled"])
        acc = reg.get_account(AID_A, registry_path=self.reg_path,
                              user_dir=self.user_dir)
        self.assertFalse(acc.enabled)
        resp, _ = dash.perform_account_action(
            AID_A, "enable", registry_path=self.reg_path,
            user_dir=self.user_dir)
        self.assertTrue(resp["enabled"])

    def test_set_task_policy(self):
        resp, code = dash.perform_account_action(
            AID_A, "set_task_policy",
            {"task_policy": {"checkin": True, "daily_report": False}},
            registry_path=self.reg_path, user_dir=self.user_dir)
        self.assertEqual(code, 200)
        acc = reg.get_account(AID_A, registry_path=self.reg_path,
                              user_dir=self.user_dir)
        self.assertEqual(acc.task_policy, {"checkin": True,
                                           "daily_report": False})

    def test_run_spawned(self):
        calls = []

        def fake_spawn(account):
            calls.append(account.account_id)
            return {"ok": True, "pid": 12345, "message": "spawned"}

        orig = dash._spawn_run
        dash._spawn_run = fake_spawn
        try:
            resp, code = dash.perform_account_action(
                AID_A, "run", registry_path=self.reg_path,
                user_dir=self.user_dir)
        finally:
            dash._spawn_run = orig
        self.assertEqual(code, 200)
        self.assertTrue(resp["ok"])
        self.assertEqual(calls, [AID_A])

    def tearDown(self):
        dash.AUDIT_DIR = self._orig_audit

    def test_errors(self):
        # 非法 ID（防穿越）
        for bad in ("../etc", "acct_!@@", "", "acct_x" * 10):
            resp, code = dash.perform_account_action(
                bad, "enable", registry_path=self.reg_path)
            self.assertEqual(code, 400)
        # 不存在的账户
        resp, code = dash.perform_account_action(
            "acct_zzz99999", "enable", registry_path=self.reg_path,
            user_dir=self.user_dir)
        self.assertEqual(code, 404)
        # 未知动作
        resp, code = dash.perform_account_action(
            AID_A, "delete_everything", registry_path=self.reg_path,
            user_dir=self.user_dir)
        self.assertEqual(code, 400)
        self.assertIn("unknown action", resp["error"])


if __name__ == "__main__":
    unittest.main()
