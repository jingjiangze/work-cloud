# -*- coding: utf-8 -*-
"""报告周期解析器（多账户改造 Stage 10 / Commit 10）。

目标：彻底摆脱"flag + 1"式的周期判断（服务端 flag 在重复提交/补偿场景
下不可靠），改用真实周期字段 + 开始/结束日期：

    daily   → 当日（start = end = 当天）
    weekly  → 服务端 weeks_date 的 startTime/endTime；缺省用 ISO 周
              （周一~周日），key = ISOyear-Wxx
    monthly → 自然月（1 号~月末），key = YYYY-MM

去重与核验统一口径：以服务端报告的 createTime 是否落在当前周期区间
为准（weeks 字符串匹配保留为兜底）。纯逻辑，无网络请求。
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReportPeriod:
    """一个报告周期（真实日期区间）。"""

    report_type: str        # day / week / month
    key: str                # 周期唯一键：2026-09-23 / 2026-W39 / 2026-09
    start_date: date
    end_date: date

    def contains(self, dt: datetime) -> bool:
        """服务端报告的 createTime 是否落在本周期内。"""
        if not isinstance(dt, datetime):
            try:
                dt = datetime.strptime(str(dt), "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                return False
        return self.start_date <= dt.date() <= self.end_date

    def to_dict(self) -> Dict[str, str]:
        return {
            "report_type": self.report_type,
            "key": self.key,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
        }


class ReportPeriodResolver:
    """统一周期解析入口（daily / weekly / monthly）。"""

    @staticmethod
    def resolve(report_type: str, now: datetime,
                week_info: Optional[Dict[str, Any]] = None) -> ReportPeriod:
        if report_type == "day":
            d = now.date()
            return ReportPeriod("day", d.isoformat(), d, d)

        if report_type == "month":
            start = now.date().replace(day=1)
            nxt = (start + timedelta(days=32)).replace(day=1)
            end = nxt - timedelta(days=1)
            return ReportPeriod("month", start.strftime("%Y-%m"), start, end)

        if report_type == "week":
            start = end = None
            # 优先服务端周期字段（startTime/endTime，YYYY-MM-DD）
            if isinstance(week_info, dict):
                start = ReportPeriodResolver._parse_day(week_info.get("startTime"))
                end = ReportPeriodResolver._parse_day(week_info.get("endTime"))
            if start is None or end is None:
                # ISO 周（周一~周日）
                iso = now.date().isocalendar()
                monday = now.date() - timedelta(days=now.date().weekday())
                start, end = monday, monday + timedelta(days=6)
                logger.debug(f"weeks_date 缺失，使用 ISO 周 {iso[0]}-W{iso[1]:02d}")
            key = f"{start.isocalendar()[0]}-W{start.isocalendar()[1]:02d}"
            return ReportPeriod("week", key, start, end)

        raise ValueError(f"未知报告类型: {report_type}")

    @staticmethod
    def _parse_day(value) -> Optional[date]:
        if not value:
            return None
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None

    @staticmethod
    def server_report_in_period(report: Dict[str, Any],
                                period: ReportPeriod) -> bool:
        """判断服务端报告记录是否属于当前周期。

        优先 createTime 落区间（真实日期口径）；week 类型若携带
        yearmonth/weeks 等键则不做字符串匹配（由调用方兜底）。
        """
        if not isinstance(report, dict):
            return False
        create = report.get("createTime")
        if not create:
            return False
        try:
            dt = datetime.strptime(str(create), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return False
        return period.contains(dt)
