# -*- coding: utf-8 -*-
"""Stage 11 (Commit 11): 多账户看板数据层测试。

验证（伪造 data/ 文件，不发请求）：
- build_accounts_overview：注册表驱动、统计计数、任务标签统一、禁用账户
- build_account_detail：7 天序列/错误聚合/风险事件带日期/报告元数据/
  非法 account_id 拒绝（防目录穿越）
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import dashboard.app as dash  # noqa: E402

AID_A = "acct_aaa11111"
AID_B = "acct_bbb22222"
TODAY = datetime.now().strftime("%Y-%m-%d")


class TestDashboardAccounts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dash11_")
        dash.DATA_DIR = self.tmp
        dash.ACCOUNTS_DIR = os.path.join(self.tmp, "accounts")
        acc_dir_a = os.path.join(dash.ACCOUNTS_DIR, AID_A)
        os.makedirs(os.path.join(acc_dir_a, "state"), exist_ok=True)
        os.makedirs(os.path.join(acc_dir_a, "ledger"), exist_ok=True)
        os.makedirs(os.path.join(acc_dir_a, "risk"), exist_ok=True)
        rep = os.path.join(acc_dir_a, "reports", TODAY, "day")
        os.makedirs(rep, exist_ok=True)

        with open(os.path.join(dash.ACCOUNTS_DIR, "index.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"version": 1, "accounts": [
                {"account_id": AID_A, "display_name": "测试A",
                 "config_file": "a.json", "enabled": True,
                 "task_policy": {"checkin": True},
                 "schedule_profile": None},
                {"account_id": AID_B, "display_name": "测试B",
                 "config_file": "b.json", "enabled": False},
            ]}, f, ensure_ascii=False)

        with open(os.path.join(acc_dir_a, "state",
                               f"{TODAY}_{AID_A}.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"date": TODAY, "user": AID_A, "tasks": {
                "login": {"state": "LOGIN_SUCCESS"},
                "打卡": {"state": "SUCCESS"},
                "daily_report": {"state": "REPORT_SUCCESS"},
                "weekly_report": {"state": "SKIPPED", "message": "x"},
            }}, f, ensure_ascii=False)

        with open(os.path.join(acc_dir_a, "ledger", f"{TODAY}.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"date": TODAY, "runs": [
                {"run_id": "run_1", "account_id": AID_A, "status": "success",
                 "started_at": "09:30:00", "duration_sec": 12.5,
                 "tasks": [{"task_type": "打卡", "status": "success",
                            "verification": {"verified": True}}]},
            ]}, f, ensure_ascii=False)

        with open(os.path.join(acc_dir_a, "risk", f"{TODAY}.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"date": TODAY, "events": [
                {"time": "09:30:05", "user": AID_A, "task": "打卡",
                 "event_type": "DUPLICATE_PREVENTED", "action": "跳过",
                 "result": "ok"}]}, f, ensure_ascii=False)

        with open(os.path.join(rep, "rpt_test.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"report_id": "rpt_x", "account_id": AID_A,
                       "report_type": "day", "submit_status": "success",
                       "verify_status": True}, f, ensure_ascii=False)

    def test_overview_counts_and_cards(self):
        data = dash.build_accounts_overview()
        self.assertEqual(data["counts"]["total"], 2)
        self.assertEqual(data["counts"]["enabled"], 1)
        self.assertEqual(data["counts"]["success"], 1)  # A 今天 1 轮成功
        cards = {a["account_id"]: a for a in data["accounts"]}
        self.assertFalse(cards[AID_B]["enabled"])
        card_a = cards[AID_A]
        labels = {t["label"] for t in card_a["tasks"]}
        # 中文/英文状态键统一为看板标签
        self.assertEqual(labels, {"登录", "打卡", "日报", "周报"})
        self.assertEqual(card_a["last_run"]["status"], "success")
        self.assertEqual(card_a["last_run"]["run_id"], "run_1")

    def test_detail_aggregation(self):
        data = dash.build_account_detail(AID_A)
        self.assertIsNotNone(data)
        self.assertEqual(data["profile"]["display_name"], "测试A")
        self.assertEqual(data["profile"]["task_policy"], {"checkin": True})
        self.assertEqual(len(data["daily_series"]), 7)
        today_row = data["daily_series"][0]
        self.assertEqual(today_row["runs"], 1)
        self.assertEqual(today_row["success"], 1)
        self.assertEqual(len(data["risk_events"]), 1)
        self.assertEqual(data["risk_events"][0]["_date"], TODAY)  # 日期已标注
        self.assertEqual(len(data["reports"]), 1)
        self.assertEqual(data["reports"][0]["report_id"], "rpt_x")

    def test_invalid_account_id_rejected(self):
        self.assertIsNone(dash.build_account_detail("../etc"))
        self.assertIsNone(dash.build_account_detail("acct_short"))
        self.assertIsNone(dash.build_account_detail(""))

    def test_unknown_account_404_path(self):
        self.assertIsNone(dash.build_account_detail("acct_zzz99999"))


if __name__ == "__main__":
    unittest.main()
