# -*- coding: utf-8 -*-
"""Stage 9 (Commit 09): 账户级结果验证测试。

验证：
- 状态规范化（历史小写词汇 → 四态）
- 跨轮 UNKNOWN 收敛：有记录判成功（状态回写成功态）、无记录放行
  （状态不动）、核验失败维持 UNKNOWN、非 UNKNOWN 任务不触碰
- run() 集成：预置 UNKNOWN 状态 → 跨轮收敛判成功 → 本轮幂等跳过；
  正常执行的任务结果带 verification 结论（进执行台账）
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from services.verification_service import (  # noqa: E402
    normalize_result_status, resolve_unknown_tasks, for_submit_result,
    STATUS_SUCCESS, STATUS_FAILED, STATUS_SKIPPED, STATUS_UNKNOWN,
)
from models.task_state import TaskStateStore, success_state_for  # noqa: E402
from models import account_registry  # noqa: E402
from models import execution_ledger  # noqa: E402
import main  # noqa: E402


class TestNormalize(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(normalize_result_status("success"), STATUS_SUCCESS)
        self.assertEqual(normalize_result_status("fail"), STATUS_FAILED)
        self.assertEqual(normalize_result_status("skip"), STATUS_SKIPPED)
        self.assertEqual(normalize_result_status("unknown"), STATUS_UNKNOWN)
        self.assertEqual(normalize_result_status("SKIPPED"), STATUS_SKIPPED)
        self.assertEqual(normalize_result_status(None), STATUS_UNKNOWN)

    def test_for_submit_result(self):
        self.assertTrue(for_submit_result("success").verified is True)
        self.assertTrue(for_submit_result("fail").verified is False)
        self.assertTrue(for_submit_result("skip").verified is None)
        self.assertTrue(for_submit_result("unknown").verified is None)


class _StubStateStore:
    """最小状态库替身（内存态，接口对齐 TaskStateStore）。"""

    def __init__(self, states):
        self._states = states  # {task: {"state": ...}}
        self.marked = []

    def get_task(self, user_key, task, date=None):
        return self._states.get(task)

    def mark(self, user_key, task, state, message=""):
        self._states[task] = {"state": state, "message": message}
        self.marked.append((task, state, message))


class _FlakyStateStore(_StubStateStore):
    def get_task(self, user_key, task, date=None):
        raise RuntimeError("boom")


class TestCrossRunResolve(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.event_fn = lambda *a, **k: self.events.append(a)

    def test_exists_marks_success(self):
        store = _StubStateStore({"日报提交": {"state": "UNKNOWN"}})
        resolved = resolve_unknown_tasks(
            store, "u", ["日报提交"],
            {"日报提交": lambda: True},
            success_state_for, record_event_fn=self.event_fn)
        v = resolved["日报提交"]
        self.assertTrue(v.verified is True)
        self.assertEqual(v.resolved_from, "unknown-cross-run")
        self.assertEqual(store.marked[0][1], success_state_for("日报提交"))
        self.assertEqual(len(self.events), 1)

    def test_not_exists_keeps_state(self):
        store = _StubStateStore({"日报提交": {"state": "UNKNOWN"}})
        resolved = resolve_unknown_tasks(
            store, "u", ["日报提交"],
            {"日报提交": lambda: False},
            success_state_for, record_event_fn=self.event_fn)
        self.assertTrue(resolved["日报提交"].verified is False)
        self.assertEqual(store.marked, [])  # 状态未动，放行重新提交

    def test_verify_error_stays_unknown(self):
        store = _StubStateStore({"日报提交": {"state": "UNKNOWN"}})
        def _bad():
            raise RuntimeError("network down")
        resolved = resolve_unknown_tasks(
            store, "u", ["日报提交"],
            {"日报提交": _bad}, success_state_for)
        self.assertTrue(resolved["日报提交"].verified is None)
        self.assertEqual(store._states["日报提交"]["state"], "UNKNOWN")

    def test_non_unknown_untouched_and_missing_fn_skipped(self):
        store = _StubStateStore({"打卡": {"state": "INIT"}})
        resolved = resolve_unknown_tasks(
            store, "u", ["打卡", "日报提交"],
            {"日报提交": lambda: True}, success_state_for)
        self.assertNotIn("打卡", resolved)       # 非 UNKNOWN 不触碰
        self.assertNotIn("日报提交", resolved)   # 无核验函数跳过

    def test_state_store_error_isolated(self):
        store = _FlakyStateStore({})
        resolved = resolve_unknown_tasks(
            store, "u", ["日报提交"],
            {"日报提交": lambda: True}, success_state_for)
        self.assertEqual(resolved, {})


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


class TestRunVerificationIntegration(unittest.TestCase):
    def test_unknown_resolved_then_idempotent_skip(self):
        tmp = tempfile.mkdtemp(prefix="verif_")
        user_dir = os.path.join(tmp, "user")
        data_dir = os.path.join(tmp, "data")
        registry_path = os.path.join(data_dir, "accounts", "index.json")
        os.makedirs(user_dir)
        with open(os.path.join(user_dir, "t1.json"), "w") as f:
            f.write("{}")
        account_registry.bootstrap_from_user_dir(
            user_dir=user_dir, registry_path=registry_path)
        acc = account_registry.list_accounts(user_dir, registry_path)[0]

        # 预置：上一轮日报 UNKNOWN（状态键与去重判定同款英文键）
        state_dir = os.path.join(data_dir, "accounts", acc.account_id, "state")
        TaskStateStore(data_dir=state_dir).mark(
            acc.account_id, "daily_report", "UNKNOWN", "上轮结果未知")

        config_data = {"userInfo": {"nikeName": "测", "userType": "teacher"},
                       "config": {"reportSettings": {"daily": {"enabled": True}}}}

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
             mock.patch.object(main, "_report_exists_on_server",
                               lambda *a, **k: True), \
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

        types = {r["task_type"]: r for r in results}
        # 跨轮收敛：UNKNOWN → 只读核验有记录 → 状态回写成功态
        #（真实 submit_report 的 is_done 随后幂等跳过；此处 mock 直通，
        #  断言收敛副作用而非 mock 的返回）
        snap = TaskStateStore(data_dir=state_dir).snapshot(acc.account_id)
        self.assertEqual(snap["tasks"]["daily_report"]["state"],
                         success_state_for("daily_report"))
        self.assertTrue(any(r["task_type"] == "日报提交" for r in results))
        # 验证结论进入台账
        import datetime as _dt
        today = _dt.datetime.now().strftime("%Y-%m-%d")
        ledger_dir = os.path.join(data_dir, "accounts", acc.account_id,
                                  "ledger")
        rec = execution_ledger.load_day(ledger_dir, today)["runs"][-1]
        ledger_types = {t["task_type"]: t for t in rec["tasks"]}
        self.assertTrue(ledger_types["打卡"]["verification"]["verified"] is True)
        self.assertEqual(ledger_types["打卡"]["verification"]["method"],
                         "submit+server-verify")


if __name__ == "__main__":
    unittest.main()
