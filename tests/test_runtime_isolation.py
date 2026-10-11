# -*- coding: utf-8 -*-
"""Stage 3 (Commit 03): 账户级运行目录隔离测试。

验证：
- 状态/历史/风险按 account_id 落入各自目录，互不串写
- 线程级风险目录路由：账户线程内 record_event 落入该账户 risk/
- legacy 路径（无 context）仍写旧目录
"""

import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from core.account_context import AccountContext  # noqa: E402
from models.risk_ledger import (  # noqa: E402
    record_event, set_active_risk_dir, get_active_risk_dir,
)
from models.task_state import TaskStateStore  # noqa: E402
from models import execution_history  # noqa: E402


class TestRuntimeIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rt_iso_")
        self.data_dir = os.path.join(self.tmp, "data")
        self.ctx_a = AccountContext(
            "acct_aaa11111", "账号A", config=None, data_root=self.data_dir)
        self.ctx_b = AccountContext(
            "acct_bbb22222", "账号B", config=None, data_root=self.data_dir)
        self.ctx_a.ensure_dirs()
        self.ctx_b.ensure_dirs()

    def test_state_files_isolated(self):
        store_a = TaskStateStore(data_dir=self.ctx_a.state_dir)
        store_b = TaskStateStore(data_dir=self.ctx_b.state_dir)
        today = datetime.now().strftime("%Y-%m-%d")

        store_a.mark(self.ctx_a.user_key, "打卡", "SUCCESS", "ok")
        store_b.mark(self.ctx_b.user_key, "打卡", "FAILED", "bad")

        a_state = store_a.snapshot(self.ctx_a.user_key, today)
        b_state = store_b.snapshot(self.ctx_b.user_key, today)
        self.assertEqual(a_state["tasks"]["打卡"]["state"], "SUCCESS")
        self.assertEqual(b_state["tasks"]["打卡"]["state"], "FAILED")

        a_files = set(os.listdir(self.ctx_a.state_dir))
        b_files = set(os.listdir(self.ctx_b.state_dir))
        self.assertTrue(a_files)
        self.assertFalse(a_files & b_files)  # 文件集完全不相交

    def test_history_isolated(self):
        today = datetime.now().strftime("%Y-%m-%d")
        execution_history.append_entry(
            self.ctx_a.user_key, [{"task_type": "打卡", "status": "success"}],
            "10:00:00", 1.0, history_dir=self.ctx_a.history_dir, date=today)
        execution_history.append_entry(
            self.ctx_b.user_key, [{"task_type": "打卡", "status": "fail"}],
            "10:00:01", 2.0, history_dir=self.ctx_b.history_dir, date=today)

        a_hist = execution_history.load_day(today,
                                            history_dir=self.ctx_a.history_dir)
        b_hist = execution_history.load_day(today,
                                            history_dir=self.ctx_b.history_dir)
        a_users = {e["user"] for e in a_hist.get("entries", [])}
        b_users = {e["user"] for e in b_hist.get("entries", [])}
        self.assertEqual(a_users, {self.ctx_a.user_key})
        self.assertEqual(b_users, {self.ctx_b.user_key})
        self.assertEqual(
            set(os.listdir(self.ctx_a.history_dir)),
            {f"{today}.json"})

    def test_risk_thread_routing(self):
        """账户线程内 record_event 自动落入该账户 risk/ 目录。"""
        def worker(ctx, event):
            set_active_risk_dir(ctx.risk_dir)
            try:
                record_event(ctx.user_key, "打卡", event)
            finally:
                set_active_risk_dir(None)

        ta = threading.Thread(target=worker, args=(self.ctx_a, "RISK_A"))
        tb = threading.Thread(target=worker, args=(self.ctx_b, "RISK_B"))
        ta.start(); tb.start(); ta.join(); tb.join()

        for ctx, expected in ((self.ctx_a, "RISK_A"), (self.ctx_b, "RISK_B")):
            path = os.path.join(
                ctx.risk_dir,
                f"{datetime.now().strftime('%Y-%m-%d')}.json")
            self.assertTrue(os.path.exists(path), f"missing {path}")
            import json
            with open(path, encoding="utf-8") as f:
                events = json.load(f)["events"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], expected)
            # account_id 是非敏感稳定主键，风险台账中原样保留（可审计）
            self.assertEqual(events[0]["user"], ctx.user_key)

    def test_risk_explicit_dir_wins(self):
        set_active_risk_dir(self.ctx_a.risk_dir)
        try:
            record_event("u", "t", "EV", risk_dir=self.ctx_b.risk_dir)
        finally:
            set_active_risk_dir(None)
        a_dir = os.listdir(self.ctx_a.risk_dir)
        self.assertEqual(a_dir, [])  # 显式参数优先于线程路由

    def test_legacy_default_dir_untouched(self):
        """无 context 的 legacy 存储仍指向项目 data/，行为不变。"""
        from models.task_state import _DATA_DIR
        store = TaskStateStore()
        self.assertEqual(store.data_dir, _DATA_DIR)
        self.assertIsNone(get_active_risk_dir())


if __name__ == "__main__":
    unittest.main()
