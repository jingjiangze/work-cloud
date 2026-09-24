# -*- coding: utf-8 -*-
"""Stage 13 (Commit 13): 跨账户通知聚合测试。

验证（伪造台账文件，不发真实请求）：
- collect_day_summary：多账户聚合、最近一轮口径、空台账/坏 JSON 安全、
  注册表显示名回退
- build_digest_markdown：等级判定（ERROR/WARNING/INFO/空日）与明细行
- load_digest_push_config：未配置/非法 JSON/合法数组
- MessagePusher.push_custom：渠道分发（打桩验证不外发）
"""

import json
import os
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from util import notification_digest as digest  # noqa: E402
from util.MessagePush import MessagePusher  # noqa: E402

AID_A = "acct_aaa11111"
AID_B = "acct_bbb22222"
TODAY = "2026-09-24"


def _write_ledger(accounts_dir, account_id, payload):
    d = os.path.join(accounts_dir, account_id, "ledger")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{TODAY}.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _run(status, tasks, ended="17:35:00"):
    return {"run_id": f"run_{status}", "status": status,
            "ended_at": ended, "tasks": tasks}


class TestCollectDaySummary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="digest13_")
        self.accounts_dir = os.path.join(self.tmp, "accounts")
        os.makedirs(self.accounts_dir)
        with open(os.path.join(self.accounts_dir, "index.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"accounts": [
                {"account_id": AID_A, "display_name": "账户A"},
                {"account_id": AID_B, "display_name": "账户B"},
            ]}, f, ensure_ascii=False)

    def test_multi_account_aggregation(self):
        _write_ledger(self.accounts_dir, AID_A, {"runs": [
            _run("success", [{"task_type": "打卡", "status": "success"}],
                 ended="12:35:00"),
            _run("failed", [
                {"task_type": "打卡", "status": "success"},
                {"task_type": "日报提交", "status": "fail"},
            ]),
        ]})
        _write_ledger(self.accounts_dir, AID_B, {"runs": [
            _run("success", [{"task_type": "打卡", "status": "success"}]),
        ]})
        s = digest.collect_day_summary(self.accounts_dir, TODAY)
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["failed_accounts"], 1)
        self.assertEqual(s["unknown_accounts"], 0)
        by_id = {a["account_id"]: a for a in s["accounts"]}
        # 最近一轮口径：A 失败、2 轮
        self.assertEqual(by_id[AID_A]["last_status"], "failed")
        self.assertEqual(by_id[AID_A]["runs"], 2)
        self.assertEqual(by_id[AID_A]["display_name"], "账户A")
        # B 成功
        self.assertEqual(by_id[AID_B]["last_status"], "success")

    def test_display_name_fallback_and_bad_json(self):
        # 无 index.json 条目的账户 → 回退 account_id
        _write_ledger(self.accounts_dir, "acct_ccc33333", {"runs": [
            _run("unknown", [])]})
        # 坏 JSON 不致崩溃
        d = os.path.join(self.accounts_dir, AID_A, "ledger")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{TODAY}.json"), "w") as f:
            f.write("{broken")
        s = digest.collect_day_summary(self.accounts_dir, TODAY)
        self.assertEqual(s["total"], 1)
        self.assertEqual(s["unknown_accounts"], 1)
        self.assertEqual(s["accounts"][0]["display_name"], "acct_ccc33333")

    def test_empty_day(self):
        s = digest.collect_day_summary(self.accounts_dir, TODAY)
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["accounts"], [])
        s2 = digest.collect_day_summary(os.path.join(self.tmp, "nope"), TODAY)
        self.assertEqual(s2["total"], 0)


class TestDigestMarkdown(unittest.TestCase):
    def setUp(self):
        self.accounts_dir = _mk_tmp()

    def test_levels_and_content(self):
        # 空日 → WARNING
        md, level = digest.build_digest_markdown(
            {"date": TODAY, "accounts": [], "total": 0,
             "failed_accounts": 0, "unknown_accounts": 0})
        self.assertEqual(level, "WARNING")
        self.assertIn("无任何账户执行记录", md)
        # 有失败账户 → ERROR，含任务明细
        s = digest.collect_day_summary(self.accounts_dir, TODAY)
        md, level = digest.build_digest_markdown(s)
        self.assertEqual(level, "ERROR")
        self.assertIn("失败 1", md)
        self.assertIn("日报提交", md)
        # 全成功 → INFO
        acc2 = os.path.join(self.accounts_dir, "..", "acc2")
        acc2 = os.path.normpath(acc2)
        os.makedirs(acc2, exist_ok=True)
        import shutil
        shutil.copytree(os.path.join(self.accounts_dir, AID_A),
                        os.path.join(acc2, AID_A))
        # 改成成功
        _write_ledger(acc2, AID_A, {"runs": [
            _run("success", [{"task_type": "打卡", "status": "success"}])]})
        s2 = digest.collect_day_summary(acc2, TODAY)
        _, level2 = digest.build_digest_markdown(s2)
        self.assertEqual(level2, "INFO")


def _mk_tmp():
    import tempfile
    d = tempfile.mkdtemp(prefix="digest13_md_")
    acc = os.path.join(d, "accounts")
    os.makedirs(acc)
    with open(os.path.join(acc, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"accounts": [{"account_id": AID_A,
                                 "display_name": "账户A"}]}, f)
    _write_ledger(acc, AID_A, {"runs": [
        _run("failed", [{"task_type": "日报提交", "status": "fail"}])]})
    return acc


class TestPushConfig(unittest.TestCase):
    def test_load_digest_push_config(self):
        self.assertEqual(digest.load_digest_push_config(""), [])
        self.assertEqual(digest.load_digest_push_config(None), [])
        self.assertEqual(digest.load_digest_push_config("not json"), [])
        self.assertEqual(digest.load_digest_push_config("{}"), [])
        cfg = [{"type": "NotifyX", "enabled": True}]
        self.assertEqual(digest.load_digest_push_config(json.dumps(cfg)), cfg)


class TestPushCustom(unittest.TestCase):
    def test_channel_dispatch(self):
        calls = []
        pusher = MessagePusher([
            {"type": "NotifyX", "enabled": True, "token": "x", "user": "u"},
            {"type": "PushPlus", "enabled": True, "token": "y"},
            {"type": "SMTP", "enabled": False},
        ])
        pusher._notifyx_push = (
            lambda cfg, t, c: calls.append(("notifyx", t, c)))
        pusher._pushplus_push = (
            lambda cfg, t, c: calls.append(("pushplus", t, c)))
        pusher.push_custom("标题", "# md", "<b>html</b>", level="ERROR")
        self.assertEqual([c[0] for c in calls], ["notifyx", "pushplus"])
        self.assertEqual(calls[0][1], "标题")
        self.assertEqual(calls[0][2], "# md")       # markdown 渠道
        self.assertEqual(calls[1][2], "<b>html</b>")  # html 渠道


if __name__ == "__main__":
    unittest.main()
