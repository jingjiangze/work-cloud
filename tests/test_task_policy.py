# -*- coding: utf-8 -*-
"""Stage 5 (Commit 05): 账户级任务开关测试。

验证：
- normalize_policy 清洗（未知键剔除、bool 强转）
- 决策链优先级：task_policy 显式 false > 原配置旗标；缺省则回落配置层
- registry.set_task_policy 持久化
- run() 集成：账户禁用 → 整体跳过；全部任务禁用 → 不发起登录；
  单任务禁用 → SKIPPED 带原因且状态落账户 state/ 目录
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import main  # noqa: E402
from models.task_policy import (  # noqa: E402
    normalize_policy, effective_enabled, resolve_decisions, TASK_KEYS,
)
from models import account_registry  # noqa: E402


class _StubConfig:
    def __init__(self, data=None, path=None):
        self._data = data or {}
        self._path = path

    def get_value(self, key):
        node = self._data
        for part in key.split("."):
            if not isinstance(node, dict):
                return None
            node = node.get(part)
        return node


class TestNormalizeAndDecide(unittest.TestCase):
    def test_normalize_policy(self):
        raw = {"checkin": 1, "daily_report": False, "weekly_report": None,
               "bogus": True}
        policy = normalize_policy(raw)
        self.assertEqual(policy, {"checkin": True, "daily_report": False})
        self.assertEqual(normalize_policy(None), {})
        self.assertEqual(normalize_policy("bad"), {})

    def test_policy_false_wins_over_config(self):
        config = _StubConfig({"config": {"reportSettings": {
            "daily": {"enabled": True}}}})
        ok, reason = effective_enabled(
            "daily_report", {"daily_report": False}, config)
        self.assertFalse(ok)
        self.assertIn("账户任务开关", reason)

    def test_policy_true_still_respects_config(self):
        """policy=true 只是放行，配置层关闭仍禁用（第三层否决）。"""
        config = _StubConfig({"config": {"reportSettings": {
            "weekly": {"enabled": False}}}})
        ok, reason = effective_enabled(
            "weekly_report", {"weekly_report": True}, config)
        self.assertFalse(ok)
        self.assertIn("用户未开启", reason)

    def test_absent_policy_falls_back_to_config(self):
        config = _StubConfig({"config": {"reportSettings": {
            "monthly": {"enabled": True}}}})
        ok, _ = effective_enabled("monthly_report", {}, config)
        self.assertTrue(ok)
        ok, _ = effective_enabled("checkin", {}, config)  # 打卡缺省默认执行
        self.assertTrue(ok)

    def test_resolve_all_keys(self):
        config = _StubConfig({"config": {"reportSettings": {
            "daily": {"enabled": True}, "weekly": {"enabled": False}}}})
        decisions = resolve_decisions({"checkin": False}, config)
        self.assertEqual(set(decisions), set(TASK_KEYS))
        self.assertFalse(decisions["checkin"][0])
        self.assertTrue(decisions["daily_report"][0])
        self.assertFalse(decisions["weekly_report"][0])


class TestRegistryPolicyPersistence(unittest.TestCase):
    def test_set_task_policy_roundtrip(self):
        tmp = tempfile.mkdtemp(prefix="tp_reg_")
        user_dir = os.path.join(tmp, "user")
        reg_path = os.path.join(tmp, "data", "accounts", "index.json")
        os.makedirs(user_dir)
        with open(os.path.join(user_dir, "u1.json"), "w") as f:
            f.write("{}")
        account_registry.bootstrap_from_user_dir(
            user_dir=user_dir, registry_path=reg_path)
        acc = account_registry.list_accounts(user_dir, reg_path)[0]

        account_registry.set_task_policy(
            acc.account_id, {"checkin": False, "bogus": 1},
            user_dir, reg_path)
        saved = account_registry.get_account(acc.account_id, user_dir, reg_path)
        self.assertEqual(saved.task_policy, {"checkin": False})

        account_registry.set_task_policy(acc.account_id, None,
                                         user_dir, reg_path)
        saved = account_registry.get_account(acc.account_id, user_dir, reg_path)
        self.assertIsNone(saved.task_policy)


def _ok(result):
    return lambda *a, **k: {"status": "success", "message": "mock",
                            "task_type": result}


class _FakeApiClient:
    created = 0

    def __init__(self, config, context=None):
        _FakeApiClient.created += 1

    def fetch_internship_plan(self):
        return None


class _StubReport:
    should_stop = False
    benign = True


class TestRunDecisionIntegration(unittest.TestCase):
    """run() 级集成：决策层在登录之前生效（不发无意义请求）。"""

    def setUp(self):
        _FakeApiClient.created = 0
        self.tmp = tempfile.mkdtemp(prefix="tp_run_")
        self.user_dir = os.path.join(self.tmp, "user")
        self.data_dir = os.path.join(self.tmp, "data")
        os.makedirs(self.user_dir)
        with open(os.path.join(self.user_dir, "t1.json"), "w") as f:
            f.write("{}")
        self.registry_path = os.path.join(self.data_dir, "accounts",
                                          "index.json")
        account_registry.bootstrap_from_user_dir(
            user_dir=self.user_dir, registry_path=self.registry_path)
        acc = account_registry.list_accounts(self.user_dir,
                                             self.registry_path)[0]
        self.account_id = acc.account_id

    def _run(self, config_data):
        def fake_cm(path=None, config=None):
            if path is not None:
                return _StubConfig(data=config_data, path=path)
            return _StubConfig(data=config_data)

        base = {"userInfo": {"nikeName": "测", "userType": "teacher"}}
        base.update(config_data or {})
        config_data = base
        with mock.patch.object(main, "USER_DIR", self.user_dir), \
             mock.patch.object(main, "DATA_DIR", self.data_dir), \
             mock.patch.object(main, "REGISTRY_PATH", self.registry_path), \
             mock.patch.object(main, "ConfigManager", fake_cm), \
             mock.patch.object(main, "ApiClient", _FakeApiClient), \
             mock.patch.object(main, "ensure_login",
                               lambda c: (True, None, "ok")), \
             mock.patch.object(main, "run_preflight",
                               lambda c, s, u: _StubReport()), \
             mock.patch.object(main, "perform_clock_in", _ok("打卡")), \
             mock.patch.object(main, "submit_daily_report", _ok("日报")), \
             mock.patch.object(main, "submit_weekly_report", _ok("周报提交")), \
             mock.patch.object(main, "submit_monthly_report", _ok("月报提交")), \
             mock.patch.object(main, "MessagePusher", lambda cfg: mock.Mock()):
            cfg = fake_cm(path=os.path.join(self.user_dir, "t1.json"))
            from core.account_context import AccountContext
            ctx = AccountContext.from_config(
                cfg, user_dir=self.user_dir,
                registry_path=self.registry_path, data_dir=self.data_dir)
            return main.run(cfg, context=ctx)

    def test_disabled_account_skips_entirely(self):
        account_registry.disable_account(self.account_id,
                                         self.user_dir, self.registry_path)
        results = self._run({})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "skip")
        self.assertIn("已禁用", results[0]["message"])
        self.assertEqual(_FakeApiClient.created, 0)  # 未发起任何登录

    def test_all_tasks_disabled_skips_without_login(self):
        account_registry.set_task_policy(
            self.account_id,
            {"checkin": False, "daily_report": False,
             "weekly_report": False, "monthly_report": False},
            self.user_dir, self.registry_path)
        results = self._run({})
        self.assertEqual(len(results), 4)
        self.assertTrue(all(r["status"] == "skip" for r in results))
        self.assertEqual(_FakeApiClient.created, 0)

    def test_single_task_disabled_records_skip_reason(self):
        account_registry.set_task_policy(
            self.account_id,
            {"checkin": True, "daily_report": False,
             "weekly_report": True, "monthly_report": True},
            self.user_dir, self.registry_path)
        # 原配置旗标显式开启周/月报（旗标缺失视为关闭，与旧版一致）
        config_data = {"userInfo": {"nikeName": "测", "userType": "teacher"},
                       "config": {"reportSettings": {
                           "weekly": {"enabled": True},
                           "monthly": {"enabled": True}}}}
        results = self._run(config_data)
        types = {r["task_type"]: r for r in results}
        self.assertEqual(types["日报提交"]["status"], "skip")
        self.assertIn("账户任务开关", types["日报提交"]["message"])
        # 启用的任务正常执行（teacher 身份，plan 检查跳过）
        self.assertEqual(types["打卡"]["status"], "success")
        self.assertEqual(types["周报提交"]["status"], "success")
        self.assertEqual(_FakeApiClient.created, 1)  # 登录正常发起一次

        # SKIPPED 状态落入账户 state/ 目录
        from models.task_state import TaskStateStore
        state_dir = os.path.join(self.data_dir, "accounts",
                                 self.account_id, "state")
        snap = TaskStateStore(data_dir=state_dir).snapshot(self.account_id)
        self.assertEqual(snap["tasks"]["日报提交"]["state"], "SKIPPED")
        self.assertIn("账户任务开关", snap["tasks"]["日报提交"]["message"])


if __name__ == "__main__":
    unittest.main()
