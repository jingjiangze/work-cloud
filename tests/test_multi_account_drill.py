# -*- coding: utf-8 -*-
"""Stage 1-3 多账户本地演练（模拟响应，零真实业务请求）。

验收目标（用户要求）：
- A 执行、B 执行（真实执行链 run/execute 路径）
- A 状态只出现在 A，B 状态只出现在 B
- A 日志只记录 A，B 日志只记录 B
- A session/Client 不进入 B
- 旧入口 _execute_tasks_impl 兼容（registry 自动建表 + context 注入）

业务请求全部 mock（ApiClient/ensure_login/任务函数/preflight/推送），
但 ConfigManager→AccountContext→registry→TaskStateStore→history/risk
的落盘路径全部走真实代码。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import main  # noqa: E402
from models import account_registry  # noqa: E402


def _ok(result):
    return lambda *a, **k: {"status": "success", "message": "mock",
                            "task_type": result}


class _StubConfig:
    def __init__(self, path=None, config=None):
        self._path = path
        self._config = config or {}

    def get_value(self, key):
        return self._config.get(key)


class _FakeApiClient:
    """记录每个账户创建的 client 实例，验证不串线。"""
    created = []          # [(config_path, instance_id)]
    instances = []

    def __init__(self, config, context=None):
        self.config = config
        self.context = context
        self.token = f"token_{id(self)}"   # 模拟每实例独立 token
        _FakeApiClient.created.append((getattr(config, "_path", None),
                                       self.token))
        _FakeApiClient.instances.append(self)

    def fetch_internship_plan(self):
        return None


class _StubReport:
    should_stop = False
    benign = True

    @staticmethod
    def summary():
        return "ok"


class TestMultiAccountDrill(unittest.TestCase):
    def setUp(self):
        _FakeApiClient.created = []
        _FakeApiClient.instances = []
        self.tmp = tempfile.mkdtemp(prefix="drill_")
        self.user_dir = os.path.join(self.tmp, "user")
        self.data_dir = os.path.join(self.tmp, "data")
        os.makedirs(self.user_dir)
        for name in ("test_a", "test_b"):
            with open(os.path.join(self.user_dir, f"{name}.json"), "w",
                      encoding="utf-8") as f:
                f.write('{"userInfo": {"phone": "1390000000"}}')

    def _run_drill(self):
        """走真实 _execute_tasks_impl 装配 + run 执行链（业务层 mock）。"""
        config_map = {
            "test_a": {"userInfo": {"nikeName": "甲", "userType": "student"},
                       "planInfo": {"planId": "p1"}},
            "test_b": {"userInfo": {"nikeName": "乙", "userType": "student"},
                       "planInfo": {"planId": "p2"}},
        }

        def fake_config_manager(path=None, config=None):
            if path is not None:
                name = os.path.splitext(os.path.basename(path))[0]
                return _StubConfig(path=path, config=config_map.get(name, {}))
            return _StubConfig(config=config)

        def clock_in_side_effect(api_client, config, state_store, user_key):
            # 模拟任务内部产生风险事件——验证线程级路由落对目录
            from models.risk_ledger import record_event, EVENT_DUPLICATE_PREVENTED
            record_event(user_key, "打卡", EVENT_DUPLICATE_PREVENTED,
                         stage="mock", action="演练", result="ok")
            return {"status": "success", "message": "mock", "task_type": "打卡"}

        with mock.patch.object(main, "USER_DIR", self.user_dir), \
             mock.patch.object(main, "DATA_DIR", self.data_dir), \
             mock.patch.object(main, "REGISTRY_PATH", os.path.join(
                 self.data_dir, "accounts", "index.json")), \
             mock.patch.object(main, "ConfigManager", fake_config_manager), \
             mock.patch.object(main, "ApiClient", _FakeApiClient), \
             mock.patch.object(main, "ensure_login",
                               lambda c: (True, None, "ok")), \
             mock.patch.object(main, "run_preflight",
                               lambda c, s, u: _StubReport()), \
             mock.patch.object(main, "perform_clock_in", clock_in_side_effect), \
             mock.patch.object(main, "submit_daily_report", _ok("日报")), \
             mock.patch.object(main, "submit_weekly_report", _ok("周报")), \
             mock.patch.object(main, "submit_monthly_report", _ok("月报")), \
             mock.patch.object(main, "MessagePusher", lambda cfg: mock.Mock()):
            main._execute_tasks_impl(["test_a", "test_b"])

    def test_two_accounts_execute_and_isolate(self):
        self._run_drill()

        registry_path = os.path.join(self.data_dir, "accounts", "index.json")
        with open(registry_path, encoding="utf-8") as f:
            registry = json.load(f)
        accounts = registry["accounts"]
        self.assertEqual(len(accounts), 2)
        ids = {a["account_id"] for a in accounts}
        self.assertEqual(len(ids), 2)
        self.assertTrue(all(i.startswith("acct_") for i in ids))

        # 每账户目录独立且内容只含本账户
        dirs = {}
        today = None
        for acct in accounts:
            root = os.path.join(self.data_dir, "accounts", acct["account_id"])
            today = acct["account_id"]
            for sub in ("state", "history", "risk"):
                self.assertTrue(os.path.isdir(os.path.join(root, sub)),
                                f"{acct['account_id']}/{sub} missing")
            dirs[acct["account_id"]] = root

        # 历史：A 日志只记录 A，B 只记录 B（user 字段 == account_id）
        from models import execution_history
        import datetime as _dt
        day = _dt.datetime.now().strftime("%Y-%m-%d")
        for aid, root in dirs.items():
            hist = execution_history.load_day(
                day, history_dir=os.path.join(root, "history"))
            users = {e["user"] for e in hist.get("entries", [])}
            self.assertEqual(users, {aid}, f"{aid} 历史串线: {users}")

        # 风险事件落在本账户 risk/（线程级路由生效）
        for aid, root in dirs.items():
            risk_path = os.path.join(root, "risk", f"{day}.json")
            self.assertTrue(os.path.exists(risk_path), f"{aid} 风险事件丢失")
            with open(risk_path, encoding="utf-8") as f:
                events = json.load(f)["events"]
            # 台账对含数字标识统一脱敏（隐私正确）；用其自身 _mask_user 对齐期望
            from models.risk_ledger import _mask_user
            self.assertEqual({e["user"] for e in events},
                             {_mask_user(aid)})

        # Session/Client 不串线：两次 ApiClient 构造绑定两个不同配置文件，
        # 且每实例 token 互不相同
        cfg_paths = {os.path.basename(p) for p, _ in _FakeApiClient.created}
        self.assertEqual(cfg_paths, {"test_a.json", "test_b.json"})
        tokens = [t for _, t in _FakeApiClient.created]
        self.assertEqual(len(tokens), 2)
        self.assertNotEqual(tokens[0], tokens[1])

    def test_legacy_env_config_still_runs(self):
        """环境变量配置（无 _path）走 legacy 分支，不注册账户。"""
        config_map = {"env_only": {}}

        def fake_config_manager(path=None, config=None):
            if path is not None:
                name = os.path.splitext(os.path.basename(path))[0]
                return _StubConfig(path=path, config=config_map.get(name, {}))
            return _StubConfig(config=config)  # ENV 配置

        with mock.patch.object(main, "USER_DIR", self.user_dir), \
             mock.patch.object(main, "DATA_DIR", self.data_dir), \
             mock.patch.object(main, "REGISTRY_PATH", os.path.join(
                 self.data_dir, "accounts", "index.json")), \
             mock.patch.object(main, "ConfigManager", fake_config_manager), \
             mock.patch.object(main, "ApiClient", _FakeApiClient), \
             mock.patch.object(main, "ensure_login",
                               lambda c: (True, None, "ok")), \
             mock.patch.object(main, "run_preflight",
                               lambda c, s, u: _StubReport()), \
             mock.patch.object(main, "perform_clock_in", _ok("打卡")), \
             mock.patch.object(main, "submit_daily_report", _ok("日报")), \
             mock.patch.object(main, "submit_weekly_report", _ok("周报")), \
             mock.patch.object(main, "submit_monthly_report", _ok("月报")), \
             mock.patch.object(main, "MessagePusher", lambda cfg: mock.Mock()):
            # 模拟 USER 环境变量配置执行：直接以 ENV 配置调 run（legacy 路径）
            results = main.run(_StubConfig(config={"userInfo": {
                "nikeName": "环", "userType": "student"}}), context=None)
        self.assertTrue(any(r.get("task_type") == "打卡" for r in results))
        # ENV 配置未注册任何账户
        reg_path = os.path.join(self.data_dir, "accounts", "index.json")
        self.assertFalse(os.path.exists(reg_path))


if __name__ == "__main__":
    unittest.main()
