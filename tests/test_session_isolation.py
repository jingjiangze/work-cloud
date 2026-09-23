# -*- coding: utf-8 -*-
"""Stage 4 (Commit 04): 账户级 Session 隔离测试。

验证：
- 会话登记/验证/失效严格按 account_id 隔离：A 失效不影响 B
- token 绝不出现在对外视图 snapshot()
- ApiClient(context) 显式绑定账户；重登回调只登记本账户
- 未登记账户 mark_verified 不隐式创建
"""

import os
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from core.account_context import AccountContext  # noqa: E402
from coreApi.MainLogicApi import ApiClient  # noqa: E402
from services.session_manager import (  # noqa: E402
    AccountSession, SessionManager, default_session_manager,
)


class _StubConfig:
    def __init__(self, token=""):
        self._data = {"userInfo": {"token": token}} if token else {}

    def get_value(self, key):
        node = self._data
        for part in key.split("."):
            if not isinstance(node, dict):
                return None
            node = node.get(part)
        return node


def _ctx(tmp, name):
    return AccountContext(name, name, config=None, data_root=tmp)


class TestSessionIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sess_iso_")
        self.mgr = SessionManager()

    def test_register_and_verify_per_account(self):
        s_a = self.mgr.register("acct_a", "token_a")
        s_b = self.mgr.register("acct_b", "token_b")
        self.assertEqual(s_a.token, "token_a")
        self.assertTrue(self.mgr.mark_verified("acct_a"))
        self.assertTrue(self.mgr.mark_verified("acct_b"))
        snap = {s["account_id"]: s for s in self.mgr.snapshot()}
        self.assertTrue(snap["acct_a"]["last_verified_at"])
        self.assertNotIn("token", snap["acct_a"])  # token 不外泄

    def test_invalidate_only_target_account(self):
        self.mgr.register("acct_a", "token_a")
        self.mgr.register("acct_b", "token_b")
        self.assertTrue(self.mgr.invalidate("acct_a"))
        self.assertIsNone(self.mgr.get("acct_a"))
        self.assertEqual(self.mgr.token("acct_b"), "token_b")  # B 完好

    def test_mark_verified_unregistered_is_noop(self):
        self.assertFalse(self.mgr.mark_verified("acct_ghost"))
        self.assertIsNone(self.mgr.get("acct_ghost"))

    def test_relogin_refresh_only_one_session(self):
        """A token 失效重登 → 仅 A 的 obtained_at/token 更新，B 不动。"""
        s_a = self.mgr.register("acct_a", "token_a_old")
        s_b = self.mgr.register("acct_b", "token_b")
        obtained_b = s_b.obtained_at

        self.mgr.invalidate("acct_a")
        s_a2 = self.mgr.register("acct_a", "token_a_new")

        self.assertEqual(s_a2.token, "token_a_new")
        self.assertIsNot(s_a2, s_a)  # A 会话对象已重建
        self.assertEqual(self.mgr.token("acct_b"), "token_b")
        self.assertEqual(s_b.obtained_at, obtained_b)

    def test_api_client_binds_context_and_relogin_hook(self):
        """ApiClient(context) 显式绑定账户；login 钩子只登记本账户。"""
        mgr = default_session_manager()  # ApiClient 内部用的是默认登记簿
        mgr.invalidate("acct_x1")
        mgr.invalidate("acct_x2")

        ctx_a = _ctx(self.tmp, "acct_x1")
        ctx_b = _ctx(self.tmp, "acct_x2")
        client_a = ApiClient(_StubConfig(token="tok_x1"), context=ctx_a)
        client_b = ApiClient(_StubConfig(token=""), context=ctx_b)

        self.assertEqual(client_a.account_id, "acct_x1")
        self.assertEqual(client_b.account_id, "acct_x2")
        self.assertIsNot(client_a.session, client_b.session)  # 实例会话隔离

        # 模拟 A 重登成功（login 末尾钩子）
        client_a._on_token_obtained()
        self.assertEqual(mgr.token("acct_x1"), "tok_x1")
        self.assertIsNone(mgr.token("acct_x2"))  # B 未被登记/污染

    def test_api_client_legacy_without_context(self):
        """无 context（legacy）时 account_id 为 None，不触碰登记簿。"""
        mgr = default_session_manager()
        before = len(mgr.snapshot())
        client = ApiClient(_StubConfig(token="t"))
        client._on_token_obtained()  # 应静默跳过
        self.assertIsNone(client.account_id)
        self.assertEqual(len(mgr.snapshot()), before)


if __name__ == "__main__":
    unittest.main()
