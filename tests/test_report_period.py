# -*- coding: utf-8 -*-
"""Stage 10 (Commit 10): 报告周期解析与记录生命周期测试。

验证：
- ReportPeriodResolver：day/month/week（API 周期优先、ISO 周兜底）、
  contains 边界、server_report_in_period（createTime 落区间口径）
- 周报去重摆脱 flag+1：createTime 在本周但 weeks 串不匹配 → 判已存在
- 报告元数据记录：账户目录隔离、字段齐全、不含正文
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from models.report_period import ReportPeriodResolver  # noqa: E402
from models import report_record  # noqa: E402
import main  # noqa: E402

NOW = datetime(2026, 9, 23, 15, 0, 0)  # 周三（ISO 2026-W39）


class TestReportPeriodResolver(unittest.TestCase):
    def test_day_period(self):
        p = ReportPeriodResolver.resolve("day", NOW)
        self.assertEqual(p.key, "2026-09-23")
        self.assertEqual(p.start_date, p.end_date)
        self.assertTrue(p.contains(datetime(2026, 9, 23, 8, 0, 0)))
        self.assertFalse(p.contains(datetime(2026, 9, 22, 23, 0, 0)))

    def test_month_period(self):
        p = ReportPeriodResolver.resolve("month", NOW)
        self.assertEqual(p.key, "2026-09")
        self.assertEqual(p.start_date.day, 1)
        self.assertEqual(p.end_date.day, 30)
        self.assertTrue(p.contains(datetime(2026, 9, 30, 23, 59, 0)))
        self.assertFalse(p.contains(datetime(2026, 10, 1, 0, 0, 0)))

    def test_week_iso_fallback(self):
        p = ReportPeriodResolver.resolve("week", NOW)  # 无 API 信息
        self.assertEqual(p.key, "2026-W39")
        self.assertEqual(p.start_date.weekday(), 0)   # 周一
        self.assertEqual(p.end_date.weekday(), 6)     # 周日
        self.assertTrue(p.contains(datetime(2026, 9, 21, 0, 0, 0)))
        self.assertTrue(p.contains(datetime(2026, 9, 27, 23, 0, 0)))
        self.assertFalse(p.contains(datetime(2026, 9, 28, 0, 0, 0)))

    def test_week_api_info_priority(self):
        p = ReportPeriodResolver.resolve("week", NOW, {
            "startTime": "2026-09-21", "endTime": "2026-09-27"})
        self.assertEqual(p.start_date.day, 21)
        self.assertEqual(p.end_date.day, 27)
        self.assertEqual(p.key, "2026-W39")

    def test_server_report_in_period(self):
        p = ReportPeriodResolver.resolve("week", NOW)
        inside = {"createTime": "2026-09-23 12:00:00"}
        outside = {"createTime": "2026-09-15 12:00:00"}
        self.assertTrue(ReportPeriodResolver.server_report_in_period(inside, p))
        self.assertFalse(ReportPeriodResolver.server_report_in_period(outside, p))
        self.assertFalse(ReportPeriodResolver.server_report_in_period({}, p))


class TestWeekDedupeFlagFree(unittest.TestCase):
    """周报核验摆脱 flag+1：createTime 落区间即判定存在。"""

    def test_exists_by_period_despite_flag_mismatch(self):
        api = mock.Mock()
        # 服务端 flag=2 → 第3周串不匹配；但 createTime 在本周
        api.get_submitted_reports_info.return_value = {
            "flag": 2,
            "data": [{"createTime": "2026-09-23 10:00:00",
                      "weeks": "第2周"}],
        }
        result = main._report_exists_on_server(
            api, "week", NOW, 3)
        self.assertTrue(result)  # 旧逻辑会 False（weeks 串不匹配）

    def test_not_exists_outside_period(self):
        api = mock.Mock()
        api.get_submitted_reports_info.return_value = {
            "flag": 5,
            "data": [{"createTime": "2026-09-01 10:00:00",
                      "weeks": "第2周"}],
        }
        self.assertFalse(main._report_exists_on_server(api, "week", NOW, 3))


class _Ctx:
    def __init__(self, aid, report_dir):
        self.account_id = aid
        self.report_dir = report_dir


class TestReportRecord(unittest.TestCase):
    def test_save_and_load_isolated(self):
        tmp = tempfile.mkdtemp(prefix="rrec_")
        ctx_a = _Ctx("acct_aaa11111", os.path.join(tmp, "a", "reports"))
        ctx_b = _Ctx("acct_bbb22222", os.path.join(tmp, "b", "reports"))
        period = ReportPeriodResolver.resolve("day", NOW).to_dict()

        rid = report_record.save_report_record(
            ctx_a, "day", period, content_hash="abcdef1234567890",
            preview="今天完成了实习日报的内容……", submit_status="success",
            verify_status=True, content_len=123)
        report_record.save_report_record(
            ctx_b, "day", period, content_hash="ffffffffffffffff",
            preview="B 账户内容", submit_status="success", verify_status=True)

        import datetime as _dt
        today = _dt.datetime.now().strftime("%Y-%m-%d")
        self.assertTrue(rid.startswith(f"rpt_{today}_day_"))
        records = report_record.load_day_records(ctx_a, today)
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["account_id"], "acct_aaa11111")
        self.assertEqual(rec["period"]["key"], "2026-09-23")
        self.assertEqual(rec["content_hash"], "abcdef1234567890")
        self.assertEqual(rec["verify_status"], True)
        # 不含正文（敏感内容不入元数据）
        self.assertNotIn("content", rec)
        self.assertNotIn("report_content", rec)
        # B 账户目录独立
        self.assertEqual(
            len(report_record.load_day_records(ctx_b, today)), 1)

    def test_legacy_without_context(self):
        tmp = tempfile.mkdtemp(prefix="rrec_legacy_")
        with mock.patch.object(report_record, "DEFAULT_META_DIR",
                               os.path.join(tmp, "meta")):
            report_record.save_report_record(
                None, "week", {"key": "2026-W39"},
                content_hash="1234", preview="p", submit_status="success",
                verify_status=None, user_key="legacyuser")
            path = os.path.join(tmp, "meta", "legacyuser", "week")
            files = os.listdir(path)
            self.assertEqual(len(files), 1)
            with open(os.path.join(path, files[0]), encoding="utf-8") as f:
                rec = json.load(f)
            self.assertIsNone(rec["account_id"])


if __name__ == "__main__":
    unittest.main()
