# -*- coding: utf-8 -*-
"""账户级调度核心（多账户改造 Stage 6 / Commit 06）。

职责（纯函数，便于测试）：
- schedule_profile 解析与校验（enabled 开关 + windows 窗口列表）
- 窗口解析优先级：账户 schedule_profile.windows → 全局（env 覆盖 → 默认）
- 为启用账户生成当日随机触发时刻（随机性仅用于错开本地任务同时启动，
  不服务于任何规避平台检测的行为——见 Stage 6 工程约束）

调度语义：
- schedule_profile.enabled=False 的账户**永不调度**
- 未配置 schedule_profile 的账户使用全局窗口，行为与旧版一致
- 每个窗口 [start, end] 内均匀随机取一个分钟级时刻，按时间排序
"""

import json
import logging
import os
import random
from datetime import date as _date, datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 默认触发窗口：与生产定制等价（12:30 主执行，17:30 兜底重跑；
# 报告提交要求 hour>=12）
DEFAULT_WINDOWS: List[Tuple[str, str]] = [
    ("12:30", "12:40"),
    ("17:30", "17:40"),
]
ENV_SCHEDULE_KEY = "WORKCLOUD_SCHEDULE"


def parse_hhmm(text) -> Optional[Tuple[int, int]]:
    """解析 HH:MM，非法返回 None。"""
    try:
        parts = str(text).strip().split(":")
        hour, minute = int(parts[0]), int(parts[1])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (ValueError, IndexError, AttributeError):
        pass
    return None


def normalize_windows(raw) -> List[Tuple[str, str]]:
    """清洗窗口列表：非法项忽略，start>end 忽略。"""
    windows: List[Tuple[str, str]] = []
    for item in raw or []:
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            continue
        start, end = parse_hhmm(item[0]), parse_hhmm(item[1])
        if start and end and start <= end:
            windows.append((f"{start[0]:02d}:{start[1]:02d}",
                            f"{end[0]:02d}:{end[1]:02d}"))
        else:
            logger.warning(f"忽略非法调度窗口: {item}")
    return windows


def normalize_schedule_profile(raw) -> Dict[str, Optional[object]]:
    """清洗 schedule_profile：{"enabled": bool|None, "windows": [...] | None}。

    enabled 缺省为 None（= 跟随调度器整体运行，窗口用全局配置）；
    显式 False 表示该账户永不调度；windows 缺省/非法为 None（用全局）。
    """
    if not isinstance(raw, dict):
        return {"enabled": None, "windows": None}
    enabled = raw.get("enabled")
    if enabled is not None:
        enabled = bool(enabled)
    windows = normalize_windows(raw.get("windows")) or None
    return {"enabled": enabled, "windows": windows}


def load_global_windows() -> List[Tuple[str, str]]:
    """全局窗口：环境变量 WORKCLOUD_SCHEDULE 优先，非法/缺省回退默认。"""
    raw = os.getenv(ENV_SCHEDULE_KEY, "").strip()
    if not raw:
        return list(DEFAULT_WINDOWS)
    try:
        data = json.loads(raw)
        windows = normalize_windows(data.get("windows", []))
        if windows:
            logger.info(f"使用环境变量 {ENV_SCHEDULE_KEY} 的触发窗口: {windows}")
            return windows
        logger.warning(f"{ENV_SCHEDULE_KEY} 中无有效窗口，回退默认窗口")
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        logger.warning(f"{ENV_SCHEDULE_KEY} 解析失败，回退默认窗口: {e}")
    return list(DEFAULT_WINDOWS)


def _random_time_in_window(day: _date,
                           window: Tuple[str, str]) -> datetime:
    """在窗口 [start, end] 内（含边界，分钟粒度）随机取一个时刻。"""
    start, end = parse_hhmm(window[0]), parse_hhmm(window[1])
    start_min = start[0] * 60 + start[1]
    end_min = end[0] * 60 + end[1]
    minute_of_day = random.randint(start_min, end_min)
    return datetime.combine(day, datetime.min.time()).replace(
        hour=minute_of_day // 60, minute=minute_of_day % 60)


def generate_daily_schedule(day: _date,
                            windows: List[Tuple[str, str]]) -> List[datetime]:
    """为指定日期在每个窗口内生成随机触发时间列表（按时间排序）。"""
    schedule = [_random_time_in_window(day, w) for w in windows]
    schedule.sort()
    return schedule


def account_schedule_windows(account,
                             global_windows: List[Tuple[str, str]],
                             ) -> Optional[List[Tuple[str, str]]]:
    """解析单账户的调度窗口。

    Returns:
        None      —— 该账户永不调度（profile.enabled=False 或账户禁用）
        window 列表 —— 该账户当日使用的窗口（profile 优先，缺省用全局）
    """
    if not getattr(account, "enabled", True):
        return None
    profile = normalize_schedule_profile(
        getattr(account, "schedule_profile", None))
    if profile["enabled"] is False:
        return None
    return profile["windows"] or list(global_windows)


def build_account_schedules(day: _date,
                            accounts: list,
                            global_windows: List[Tuple[str, str]],
                            ) -> Dict[str, List[datetime]]:
    """为启用账户生成当日触发计划：{account_id: [datetime, ...]}。

    禁用账户 / profile.enabled=False 的账户不出现在结果中（永不调度）。
    """
    plans: Dict[str, List[datetime]] = {}
    for account in accounts:
        windows = account_schedule_windows(account, global_windows)
        if not windows:
            continue
        times = generate_daily_schedule(day, windows)
        plans[account.account_id] = times
        logger.info(
            f"账户 {account.account_id}({account.display_name}) 当日计划: "
            + ", ".join(t.strftime("%H:%M") for t in times))
    return plans


def due_account_runs(plans: Dict[str, List[datetime]],
                     now: datetime,
                     fired: Dict[str, int]) -> List[Tuple[str, datetime]]:
    """挑出到点待触发的 (account_id, run_at)（同一时刻只触发一次）。"""
    due: List[Tuple[str, datetime]] = []
    for account_id, times in plans.items():
        used = fired.get(account_id, 0)
        while used < len(times) and times[used] <= now:
            due.append((account_id, times[used]))
            used += 1
        if used != fired.get(account_id, 0):
            fired[account_id] = used
    due.sort(key=lambda item: item[1])
    return due
