# -*- coding: utf-8 -*-
"""Stage 2 (Commit 02): AccountContext 单元测试。

验证：
- from_config 对文件配置自动注册账户并创建隔离目录
- from_config 对无 _path 的环境变量配置返回 None（legacy 降级）
- context 显式携带 account_id / user_key，目录按账户隔离
- run() 签名显式接收 context（静态检查）
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from core.account_context import AccountContext  # noqa: E402
from models import account_registry  # noqa: E402


def _write_config(user_dir: str, name: str) -> str:
    path = os.path.join(user_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"userInfo": {"phone": "13800001111"}}')
    return path


class _FakeConfig:
    """最小 ConfigManager 替身：只暴露 _path 与 get_value。"""

    def __init__(self, path):
        self._path = path

    def get_value(self, _key):
        return None


class TestAccountContext(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="acct_ctx_")
        self.user_dir = os.path.join(self.tmp, "user")
        self.registry_path = os.path.join(self.tmp, "data", "accounts",
                                          "index.json")
        os.makedirs(self.user_dir, exist_ok=True)

    def test_from_config_registers_and_creates_dirs(self):
        cfg_path = _write_config(self.user_dir, "user_a.json")
        ctx = AccountContext.from_config(
            _FakeConfig(cfg_path), user_dir=self.user_dir,
            registry_path=self.registry_path,
            data_dir=os.path.join(self.tmp, "data"))

        self.assertIsNotNone(ctx)
        self.assertTrue(ctx.account_id.startswith("acct_"))
        self.assertEqual(ctx.user_key, ctx.account_id)
        # 注册表里能按 account_id 查到，且绑定正确配置文件
        account = account_registry.get_account(
            ctx.account_id, registry_path=self.registry_path)
        self.assertIsNotNone(account)
        self.assertEqual(account.config_file, "user_a.json")
        # 隔离目录已创建
        for sub in ("state", "history", "risk", "logs", "reports",
                    "uploads", "session"):
            self.assertTrue(
                os.path.isdir(os.path.join(ctx.runtime_dir, sub)),
                f"missing dir: {sub}")

    def test_from_config_env_config_returns_none(self):
        """环境变量配置（无 _path）→ None，legacy 行为不变。"""
        self.assertIsNone(AccountContext.from_config(_FakeConfig(None)))

    def test_dirs_isolated_between_accounts(self):
        pa = _write_config(self.user_dir, "user_a.json")
        pb = _write_config(self.user_dir, "user_b.json")
        ctx_a = AccountContext.from_config(
            _FakeConfig(pa), user_dir=self.user_dir,
            registry_path=self.registry_path, data_dir=self.tmp)
        ctx_b = AccountContext.from_config(
            _FakeConfig(pb), user_dir=self.user_dir,
            registry_path=self.registry_path, data_dir=self.tmp)

        self.assertNotEqual(ctx_a.account_id, ctx_b.account_id)
        self.assertNotEqual(ctx_a.runtime_dir, ctx_b.runtime_dir)
        self.assertNotEqual(ctx_a.state_dir, ctx_b.state_dir)
        self.assertNotEqual(ctx_a.history_dir, ctx_b.history_dir)
        self.assertNotEqual(ctx_a.session_dir, ctx_b.session_dir)

    def test_same_config_reuses_account_id(self):
        cfg_path = _write_config(self.user_dir, "user_a.json")
        ctx1 = AccountContext.from_config(
            _FakeConfig(cfg_path), user_dir=self.user_dir,
            registry_path=self.registry_path, data_dir=self.tmp)
        ctx2 = AccountContext.from_config(
            _FakeConfig(cfg_path), user_dir=self.user_dir,
            registry_path=self.registry_path, data_dir=self.tmp)
        self.assertEqual(ctx1.account_id, ctx2.account_id)

    def test_run_signature_accepts_context(self):
        """run(config, context) 签名显式存在，禁止退回全局变量方案。"""
        import inspect
        import main
        sig = inspect.signature(main.run)
        self.assertIn("context", sig.parameters)
        self.assertTrue(sig.parameters["context"].default is None)


if __name__ == "__main__":
    unittest.main()
