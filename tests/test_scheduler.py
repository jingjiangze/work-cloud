# -*- coding: utf-8 -*-
"""Stage 6 (Commit 06): 账户级调度器测试。

验证：
- 窗口/profile 解析校验（非法 HH:MM、start>end、enabled 语义）
- 禁用账户 / profile.enabled=False → 永不调度
- 账户窗口优先于全局；缺省回落全局
- 到点触发判定：每时刻只触发一次、按时间排序
- scheduled_runner 装配：enabled 过滤、--file 过滤
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from core import scheduler as sched  # noqa: E402
from models.account import Account  # noqa: E402
import scheduled_runner as runner  # noqa: E402


def _acc(aid, enabled=True, profile=None, config_file=None):
    full_id = aid if aid.startswith("acct_") else f"acct_{aid}"
    return Account(account_id=full_id, display_name=aid,
                   config_file=config_file or f"{aid}.json",
                   enabled=enabled, schedule_profile=profile)


GLOBAL = [("12:30", "12:40")]


class TestParseAndNormalize(unittest.TestCase):
    def test_parse_hhmm(self):
        self.assertEqual(sched.parse_hhmm("09:05"), (9, 5))
        self.assertIsNone(sched.parse_hhmm("24:00"))
        self.assertIsNone(sched.parse_hhmm("bad"))
        self.assertIsNone(sched.parse_hhmm(None))

    def test_normalize_windows(self):
        windows = sched.normalize_windows(
            [["08:00", "09:30"], ["19:00", "08:00"], "bad", ["26:00", "27:00"]])
        self.assertEqual(windows, [("08:00", "09:30")])

    def test_normalize_schedule_profile(self):
        p = sched.normalize_schedule_profile(
            {"enabled": False, "windows": [["07:00", "07:10"]], "bogus": 1})
        self.assertEqual(p, {"enabled": False,
                             "windows": [("07:00", "07:10")]})
        self.assertEqual(sched.normalize_schedule_profile(None),
                         {"enabled": None, "windows": None})
        self.assertEqual(sched.normalize_schedule_profile({"windows": "bad"}),
                         {"enabled": None, "windows": None})


class TestAccountScheduling(unittest.TestCase):
    def test_disabled_account_never_scheduled(self):
        self.assertIsNone(
            sched.account_schedule_windows(_acc("aaa111", enabled=False), GLOBAL))

    def test_profile_disabled_never_scheduled(self):
        acc = _acc("aaa112", enabled=True, profile={"enabled": False})
        self.assertIsNone(sched.account_schedule_windows(acc, GLOBAL))

    def test_profile_windows_override_global(self):
        acc = _acc("aaa114", profile={"enabled": True,
                                 "windows": [["07:00", "07:10"]]})
        self.assertEqual(sched.account_schedule_windows(acc, GLOBAL),
                         [("07:00", "07:10")])

    def test_profile_absent_falls_back_to_global(self):
        acc = _acc("aaa113")
        self.assertEqual(sched.account_schedule_windows(acc, GLOBAL), GLOBAL)

    def test_build_schedules_excludes_disabled(self):
        accounts = [
            _acc("onacc01", enabled=True),
            _acc("offacc01", enabled=False),
            _acc("prooff01", enabled=True,
                 profile={"enabled": False}),
        ]
        plans = sched.build_account_schedules(datetime(2026, 9, 23).date(),
                                              accounts, GLOBAL)
        self.assertEqual(set(plans), {"acct_onacc01"})
        self.assertEqual(len(plans["acct_onacc01"]), 1)  # 每窗口一个时刻
        t = plans["acct_onacc01"][0]
        self.assertTrue(datetime(2026, 9, 23, 12, 30)
                        <= t <= datetime(2026, 9, 23, 12, 40))


class TestDueRuns(unittest.TestCase):
    def test_due_fires_once_in_order(self):
        t1 = datetime(2026, 9, 23, 12, 35)
        t2 = datetime(2026, 9, 23, 17, 32)
        plans = {"acctaa11": [t1, t2], "acctbb22": [t1]}
        fired = {}
        # 12:36 → 两个账户都到点
        due = sched.due_account_runs(plans, datetime(2026, 9, 23, 12, 36),
                                     fired)
        self.assertEqual(due, [("acctaa11", t1), ("acctbb22", t1)])
        # 17:33 → 仅 a 的第二时刻
        due = sched.due_account_runs(plans, datetime(2026, 9, 23, 17, 33),
                                     fired)
        self.assertEqual(due, [("acctaa11", t2)])
        # 重复检查 → 空转（不重复触发）
        due = sched.due_account_runs(plans, datetime(2026, 9, 23, 18, 0),
                                     fired)
        self.assertEqual(due, [])

    def test_due_not_yet_time(self):
        t1 = datetime(2026, 9, 23, 12, 35)
        fired = {}
        due = sched.due_account_runs({"a": [t1]},
                                     datetime(2026, 9, 23, 12, 34), fired)
        self.assertEqual(due, [])


class TestRunnerAssembly(unittest.TestCase):
    def test_enabled_account_files_filters(self):
        tmp = tempfile.mkdtemp(prefix="sched_")
        from models import account_registry
        user_dir = os.path.join(tmp, "user")
        reg_path = os.path.join(tmp, "data", "accounts", "index.json")
        os.makedirs(user_dir)
        for name in ("a1", "a2"):
            with open(os.path.join(user_dir, f"{name}.json"), "w") as f:
                f.write("{}")
        account_registry.bootstrap_from_user_dir(user_dir=user_dir,
                                                 registry_path=reg_path)
        accs = account_registry.list_accounts(user_dir, reg_path)
        by_cfg = {a.config_file: a for a in accs}
        account_registry.disable_account(
            by_cfg["a2.json"].account_id, user_dir, reg_path)

        with mock.patch.object(account_registry, "USER_DIR", user_dir), \
             mock.patch.object(account_registry, "REGISTRY_PATH", reg_path):
            pairs = runner._enabled_account_files(None)
            self.assertEqual({stem for _, stem in pairs}, {"a1"})
            pairs = runner._enabled_account_files(["a1", "a2"])
            self.assertEqual({stem for _, stem in pairs}, {"a1"})

    def test_env_global_windows(self):
        with mock.patch.dict(os.environ,
                             {sched.ENV_SCHEDULE_KEY:
                              '{"windows": [["06:00","06:30"]]}'},
                             clear=False):
            self.assertEqual(sched.load_global_windows(),
                             [("06:00", "06:30")])
        with mock.patch.dict(os.environ,
                             {sched.ENV_SCHEDULE_KEY: "not-json"},
                             clear=False):
            self.assertEqual(sched.load_global_windows(),
                             sched.DEFAULT_WINDOWS)


if __name__ == "__main__":
    unittest.main()
