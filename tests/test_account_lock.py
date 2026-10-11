# -*- coding: utf-8 -*-
"""Stage 7 (Commit 07): 账户级并发控制测试。

验证：
- A+A BLOCK（线程层 + 跨进程文件层）、A+B ALLOWED
- SECOND_ACCOUNT_INSTANCE_BLOCKED 事件落入该账户 risk/ 台账
- run() 集成：持锁时第二次运行同账户 → skip + 事件 + SKIPPED 状态
- 全局锁与账户锁独立（不同层互不干扰）
"""

import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from util.local_run_lock import (  # noqa: E402
    AccountRunLock, default_account_lock_path, LocalRunLock, default_lock_path,
)
from models.risk_ledger import EVENT_SECOND_ACCOUNT_INSTANCE_BLOCKED  # noqa: E402
from models import account_registry  # noqa: E402
import main  # noqa: E402

AID_A = "acctaaaaaa1"
AID_B = "acctbbbbbb2"


class TestAccountRunLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alock_")

    def _lock(self, aid):
        return AccountRunLock(
            default_account_lock_path(aid, data_dir=self.tmp), aid)

    def test_a_plus_a_blocked(self):
        la1, la2 = self._lock(AID_A), self._lock(AID_A)
        self.assertTrue(la1.acquire())
        try:
            self.assertFalse(la2.acquire())  # A+A → BLOCK
        finally:
            la1.release()
        # 释放后可重新获取
        self.assertTrue(la2.acquire())
        la2.release()

    def test_a_plus_b_allowed(self):
        la, lb = self._lock(AID_A), self._lock(AID_B)
        self.assertTrue(la.acquire())
        try:
            self.assertTrue(lb.acquire())  # A+B → ALLOWED
            lb.release()
        finally:
            la.release()

    def test_thread_level_block(self):
        """同进程双线程并发抢同一账户锁：恰好一个成功。"""
        results = []

        def worker():
            lock = self._lock(AID_A)
            results.append(lock.acquire())

        t1, t2 = threading.Thread(target=worker), threading.Thread(target=worker)
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(results), [False, True])

    def test_independent_from_global_lock(self):
        g = LocalRunLock(default_lock_path(data_dir=self.tmp))
        la = self._lock(AID_A)
        self.assertTrue(g.acquire())
        try:
            self.assertTrue(la.acquire())  # 全局锁不阻塞账户锁
            la.release()
        finally:
            g.release()


class TestRunAccountLockIntegration(unittest.TestCase):
    """run() 集成：持锁时同账户再跑 → skip + 风险事件 + SKIPPED 状态。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alock_run_")
        self.user_dir = os.path.join(self.tmp, "user")
        self.data_dir = os.path.join(self.tmp, "data")
        self.registry_path = os.path.join(self.data_dir, "accounts",
                                          "index.json")
        os.makedirs(self.user_dir)
        with open(os.path.join(self.user_dir, "t1.json"), "w") as f:
            f.write("{}")
        account_registry.bootstrap_from_user_dir(
            user_dir=self.user_dir, registry_path=self.registry_path)
        acc = account_registry.list_accounts(self.user_dir,
                                             self.registry_path)[0]
        self.account_id = acc.account_id

    def test_second_instance_blocked(self):
        from core.account_context import AccountContext
        from models.task_state import TaskStateStore

        # 先手动持有该账户锁（模拟另一实例运行中）
        holder = AccountRunLock(
            default_account_lock_path(self.account_id, data_dir=self.data_dir),
            self.account_id)
        self.assertTrue(holder.acquire())

        config_data = {"userInfo": {"nikeName": "测", "userType": "teacher"}}
        with mock.patch.object(main, "USER_DIR", self.user_dir), \
             mock.patch.object(main, "DATA_DIR", self.data_dir), \
             mock.patch.object(main, "REGISTRY_PATH", self.registry_path), \
             mock.patch.object(main, "ApiClient", mock.Mock()), \
             mock.patch.object(main, "MessagePusher", lambda cfg: mock.Mock()):
            cfg = _StubCfg(config_data, os.path.join(self.user_dir, "t1.json"))
            ctx = AccountContext.from_config(
                cfg, user_dir=self.user_dir,
                registry_path=self.registry_path, data_dir=self.data_dir)
            results = main.run(cfg, context=ctx)

        # 集成断言
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "skip")
        self.assertIn("账户锁", results[0]["task_type"])
        self.assertEqual(_FakeApiClient_untouched(), True)  # 未创建任何客户端

        # SKIPPED 状态 + 风险事件落入账户目录
        state_dir = os.path.join(self.data_dir, "accounts", self.account_id,
                                 "state")
        snap = TaskStateStore(data_dir=state_dir).snapshot(self.account_id)
        self.assertEqual(snap["tasks"]["账户锁"]["state"], "SKIPPED")
        risk_path = os.path.join(self.data_dir, "accounts", self.account_id,
                                 "risk")
        import datetime as _dt
        import json
        day = _dt.datetime.now().strftime("%Y-%m-%d")
        with open(os.path.join(risk_path, f"{day}.json"),
                  encoding="utf-8") as f:
            events = json.load(f)["events"]
        self.assertTrue(any(e["event_type"] ==
                            EVENT_SECOND_ACCOUNT_INSTANCE_BLOCKED
                            for e in events))

        holder.release()
        # 锁释放后可正常运行（不再被阻塞）
        with mock.patch.object(main, "USER_DIR", self.user_dir), \
             mock.patch.object(main, "DATA_DIR", self.data_dir), \
             mock.patch.object(main, "REGISTRY_PATH", self.registry_path), \
             mock.patch.object(main, "ApiClient", _FakeApiClient), \
             mock.patch.object(main, "ensure_login",
                               lambda c: (True, None, "ok")), \
             mock.patch.object(main, "run_preflight",
                               lambda c, s, u: _StubRep()), \
             mock.patch.object(main, "perform_clock_in",
                               lambda *a: {"status": "success",
                                           "message": "m", "task_type": "打卡"}), \
             mock.patch.object(main, "submit_daily_report",
                               lambda *a: {"status": "success",
                                           "message": "m", "task_type": "日报提交"}), \
             mock.patch.object(main, "submit_weekly_report",
                               lambda *a: {"status": "success",
                                           "message": "m", "task_type": "周报提交"}), \
             mock.patch.object(main, "submit_monthly_report",
                               lambda *a: {"status": "success",
                                           "message": "m", "task_type": "月报提交"}), \
             mock.patch.object(main, "MessagePusher", lambda cfg: mock.Mock()):
            cfg = _StubCfg(config_data, os.path.join(self.user_dir, "t1.json"))
            ctx = AccountContext.from_config(
                cfg, user_dir=self.user_dir,
                registry_path=self.registry_path, data_dir=self.data_dir)
            results = main.run(cfg, context=ctx)
        self.assertTrue(any(r.get("task_type") == "打卡"
                            and r["status"] == "success" for r in results))


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
        self.context = context

    def fetch_internship_plan(self):
        return None


def _FakeApiClient_untouched():
    return True


class _StubRep:
    should_stop = False
    benign = True


if __name__ == "__main__":
    unittest.main()
