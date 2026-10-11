# -*- coding: utf-8 -*-
"""Account Registry 单元测试（多账户改造 Stage 1 / Commit 01）。

运行：python -m unittest tests.test_account_registry -v
"""

import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import account_registry as reg  # noqa: E402
from models.account import Account, generate_account_id, is_valid_account_id  # noqa: E402


class AccountModelTest(unittest.TestCase):
    def test_account_id_format(self):
        aid = generate_account_id()
        self.assertTrue(is_valid_account_id(aid))
        self.assertFalse(is_valid_account_id("acct_"))
        self.assertFalse(is_valid_account_id("user_01"))
        self.assertFalse(is_valid_account_id("13800138000"))

    def test_account_no_secrets(self):
        """Account 序列化不得包含敏感字段（即使恶意传入也被剔除）。"""
        acc = Account(account_id=generate_account_id(), display_name="A",
                      config_file="a.json",
                      task_policy={"checkin": True,
                                   "password": "hack",
                                   "nested": {"token": "x", "ok": 1}})
        d = acc.to_dict()
        raw = json.dumps(d, ensure_ascii=False)
        self.assertNotIn("hack", raw)
        self.assertNotIn("token", raw)
        self.assertIn("checkin", raw)
        self.assertIn("ok", raw)

    def test_invalid_account_rejected(self):
        with self.assertRaises(ValueError):
            Account(account_id="bad id", display_name="A", config_file="a.json")
        with self.assertRaises(ValueError):
            Account(account_id=generate_account_id(), display_name="",
                    config_file="a.json")
        with self.assertRaises(ValueError):
            Account(account_id=generate_account_id(), display_name="A",
                    config_file="../escape.json")


class AccountRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.user_dir = os.path.join(self.tmp.name, "user")
        self.reg_path = os.path.join(self.tmp.name, "accounts", "index.json")
        os.makedirs(self.user_dir)
        # 两个"真实"配置 + 一个 example + 一个备份文件
        for name in ("me.json", "second.json", "example.json", "_bak.json"):
            with open(os.path.join(self.user_dir, name), "w",
                      encoding="utf-8") as f:
                json.dump({"config": {}}, f)

    def tearDown(self):
        self.tmp.cleanup()

    def test_bootstrap_skips_example(self):
        """引导扫描：跳过 example*/_*，只注册真实配置。"""
        accounts = reg.bootstrap_from_user_dir(self.user_dir, self.reg_path)
        names = sorted(a.config_file for a in accounts)
        self.assertEqual(names, ["me.json", "second.json"])
        for a in accounts:
            self.assertTrue(a.account_id.startswith("acct_"))
            self.assertTrue(a.enabled)

    def test_ensure_registry_idempotent(self):
        reg.ensure_registry(self.user_dir, self.reg_path)
        first = reg.list_accounts(self.user_dir, self.reg_path)
        reg.ensure_registry(self.user_dir, self.reg_path)
        second = reg.list_accounts(self.user_dir, self.reg_path)
        # 二次 ensure 不重建（ID 稳定）
        self.assertEqual([a.account_id for a in first],
                         [a.account_id for a in second])

    def test_add_get_update_disable(self):
        acc = reg.add_account(Account(account_id=generate_account_id(),
                                      display_name="测试A",
                                      config_file="me.json"),
                              self.user_dir, self.reg_path)
        self.assertEqual(reg.get_account(acc.account_id, self.user_dir,
                                         self.reg_path).display_name, "测试A")
        # config_file 查询
        self.assertEqual(reg.get_account_by_config("me.json", self.user_dir,
                                                   self.reg_path).account_id,
                         acc.account_id)
        # update
        reg.update_account(acc.account_id, self.user_dir, self.reg_path,
                           display_name="改名A")
        self.assertEqual(reg.get_account(acc.account_id, self.user_dir,
                                         self.reg_path).display_name, "改名A")
        # disable / enable
        self.assertFalse(reg.disable_account(acc.account_id, self.user_dir,
                                             self.reg_path).enabled)
        self.assertTrue(reg.enable_account(acc.account_id, self.user_dir,
                                           self.reg_path).enabled)

    def test_duplicate_rejected(self):
        acc = reg.add_account(Account(account_id=generate_account_id(),
                                      display_name="A", config_file="me.json"),
                              self.user_dir, self.reg_path)
        with self.assertRaises(ValueError):
            reg.add_account(Account(account_id=acc.account_id,
                                    display_name="B", config_file="other.json"),
                            self.user_dir, self.reg_path)
        with self.assertRaises(ValueError):
            reg.add_account(Account(account_id=generate_account_id(),
                                    display_name="B", config_file="me.json"),
                            self.user_dir, self.reg_path)

    def test_corrupt_registry_recovery(self):
        """注册表损坏 → 归档并重建，不抛异常。"""
        os.makedirs(os.path.dirname(self.reg_path), exist_ok=True)
        with open(self.reg_path, "w", encoding="utf-8") as f:
            f.write("{broken json!!!")
        accounts = reg.list_accounts(self.user_dir, self.reg_path)
        self.assertEqual(len(accounts), 2)  # 从 user/ 重新引导
        archives = [f for f in os.listdir(os.path.dirname(self.reg_path))
                    if f.startswith("index.json.corrupt-")]
        self.assertEqual(len(archives), 1)

    def test_concurrent_add_unique_ids(self):
        """并发注册：所有线程成功且 ID 唯一（锁生效）。"""
        results = []
        errors = []

        def worker(i):
            try:
                # 每线程注册不同 config_file（先落到 user/ 目录）
                name = f"conc_{i}.json"
                with open(os.path.join(self.user_dir, name), "w",
                          encoding="utf-8") as f:
                    json.dump({}, f)
                acc = reg.get_or_register_by_config(
                    name, f"并发{i}", self.user_dir, self.reg_path)
                results.append(acc.account_id)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors, errors)
        self.assertEqual(len(results), len(set(results)), "account_id 出现重复")

    def test_get_or_register_skips_example(self):
        """example 文件不自动注册（老入口行为完全不变）。"""
        self.assertIsNone(reg.get_or_register_by_config(
            "example.json", self.user_dir, self.reg_path))


if __name__ == "__main__":
    unittest.main()
