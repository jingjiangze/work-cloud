# -*- coding: utf-8 -*-
"""Stage 8 (Commit 08): 账户级执行台账测试。

验证：
- run_id 格式（run_日期_时刻_账户_随机）与唯一性
- 台账按账户隔离落盘、load_day 往返、损坏重建
- run() 集成：一轮执行在账户 ledger/ 目录留下 run_id 记录，
  tasks 覆盖每个任务（含 SKIPPED 带原因），overall 状态正确
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from models import execution_ledger  # noqa: E402
from models import account_registry  # noqa: E402
import main  # noqa: E402


class TestRunId(unittest.TestCase):
    def test_format_and_embed_account(self):
        rid = execution_ledger.new_run_id("acct_ab12cd34")
        parts = rid.split("_")
        self.assertEqual(parts[0], "run")
        self.assertEqual(len(parts), 5)
        self.assertIn("acctab12cd34", parts[3])  # 账户嵌入（去下划线）
        self.assertEqual(len(parts[4]), 4)

    def test_uniqueness(self):
        ids = {execution_ledger.new_run_id("acct_x") for _ in range(50)}
        self.assertEqual(len(ids), 50)


class TestLedgerStore(unittest.TestCase):
    def test_roundtrip_and_isolation(self):
        tmp = tempfile.mkdtemp(prefix="ledger_")
        dir_a = os.path.join(tmp, "accounts", "acct_aaa11111", "ledger")
        dir_b = os.path.join(tmp, "accounts", "acct_bbb22222", "ledger")
        execution_ledger.append_run(dir_a, {
            "run_id": "run_1", "account_id": "acct_aaa11111",
            "user": "acct_aaa11111", "status": "success",
            "tasks": [{"task_type": "打卡", "status": "success"}]})
        execution_ledger.append_run(dir_b, {
            "run_id": "run_2", "account_id": "acct_bbb22222",
            "user": "acct_bbb22222", "status": "failed",
            "tasks": [{"task_type": "打卡", "status": "fail",
                       "message": "boom"}]})
        import datetime as _dt
        today = _dt.datetime.now().strftime("%Y-%m-%d")
        a = execution_ledger.load_day(dir_a, today)["runs"]
        b = execution_ledger.load_day(dir_b, today)["runs"]
        self.assertEqual([r["run_id"] for r in a], ["run_1"])
        self.assertEqual([r["run_id"] for r in b], ["run_2"])
        self.assertEqual(a[0]["tasks"][0]["status"], "success")
        self.assertEqual(b[0]["tasks"][0]["message"], "boom")

    def test_corruption_rebuild(self):
        tmp = tempfile.mkdtemp(prefix="ledger_bad_")
        import datetime as _dt
        today = _dt.datetime.now().strftime("%Y-%m-%d")
        path = os.path.join(tmp, f"{today}.json")
        with open(path, "w") as f:
            f.write("{broken json")
        execution_ledger.append_run(tmp, {"run_id": "run_x", "status": "skipped"})
        runs = execution_ledger.load_day(tmp, today)["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["run_id"], "run_x")

    def test_summarize(self):
        self.assertEqual(execution_ledger.summarize_results(
            [{"status": "success"}, {"status": "skip"}]), "success")
        self.assertEqual(execution_ledger.summarize_results(
            [{"status": "success"}, {"status": "fail"}]), "failed")
        self.assertEqual(execution_ledger.summarize_results(
            [{"status": "skip"}]), "skipped")


class _StubCfg:
    def __init__(self, data, path):
        self._data = data
        self._path = path

    def get_value(self, key):
        node = self._data
        for part in key.split("."):
            if not isinstance(node, dict):
                return None
            node = node.get(part)
        return node


class _FakeApiClient:
    def __init__(self, config, context=None):
        self.config = config

    def fetch_internship_plan(self):
        return None


class TestRunLedgerIntegration(unittest.TestCase):
    def test_run_writes_ledger(self):
        tmp = tempfile.mkdtemp(prefix="ledger_run_")
        user_dir = os.path.join(tmp, "user")
        data_dir = os.path.join(tmp, "data")
        registry_path = os.path.join(data_dir, "accounts", "index.json")
        os.makedirs(user_dir)
        with open(os.path.join(user_dir, "t1.json"), "w") as f:
            f.write("{}")
        account_registry.bootstrap_from_user_dir(
            user_dir=user_dir, registry_path=registry_path)
        acc = account_registry.list_accounts(user_dir, registry_path)[0]
        account_registry.set_task_policy(
            acc.account_id, {"monthly_report": False},
            user_dir, registry_path)

        config_data = {"userInfo": {"nikeName": "测", "userType": "teacher"},
                       "config": {"reportSettings": {
                           "weekly": {"enabled": True}}}}

        def _ok(t):
            return lambda *a, **k: {"status": "success", "message": "m",
                                    "task_type": t}

        with mock.patch.object(main, "USER_DIR", user_dir), \
             mock.patch.object(main, "DATA_DIR", data_dir), \
             mock.patch.object(main, "REGISTRY_PATH", registry_path), \
             mock.patch.object(main, "ApiClient", _FakeApiClient), \
             mock.patch.object(main, "ensure_login",
                               lambda c: (True, None, "ok")), \
             mock.patch.object(main, "run_preflight",
                               lambda c, s, u: type(
                                   "R", (), {"should_stop": False,
                                             "benign": True})()), \
             mock.patch.object(main, "perform_clock_in", _ok("打卡")), \
             mock.patch.object(main, "submit_daily_report", _ok("日报提交")), \
             mock.patch.object(main, "submit_weekly_report", _ok("周报提交")), \
             mock.patch.object(main, "submit_monthly_report", _ok("月报提交")), \
             mock.patch.object(main, "MessagePusher", lambda cfg: mock.Mock()):
            from core.account_context import AccountContext
            cfg = _StubCfg(config_data, os.path.join(user_dir, "t1.json"))
            ctx = AccountContext.from_config(
                cfg, user_dir=user_dir, registry_path=registry_path,
                data_dir=data_dir)
            results = main.run(cfg, context=ctx)

        # 台账落在该账户 ledger/ 目录，覆盖每个任务（含策略禁用的月报）
        import datetime as _dt
        today = _dt.datetime.now().strftime("%Y-%m-%d")
        ledger_dir = os.path.join(data_dir, "accounts", acc.account_id,
                                  "ledger")
        runs = execution_ledger.load_day(ledger_dir, today)["runs"]
        self.assertEqual(len(runs), 1)
        rec = runs[0]
        self.assertTrue(rec["run_id"].startswith("run_"))
        self.assertEqual(rec["account_id"], acc.account_id)
        self.assertEqual(rec["status"], "success")
        types = {t["task_type"]: t for t in rec["tasks"]}
        self.assertEqual(types["打卡"]["status"], "success")
        self.assertEqual(types["月报提交"]["status"], "skip")
        self.assertIn("账户任务开关", types["月报提交"]["message"])
        # Stage 9 已填充验证结论（成功任务带 method/verified）
        self.assertEqual(types["打卡"]["verification"]["verified"], True)
        self.assertEqual(types["打卡"]["verification"]["method"],
                         "submit+server-verify")
        # results 与台账同轮次
        self.assertEqual(len(results), len(rec["tasks"]))


if __name__ == "__main__":
    unittest.main()
