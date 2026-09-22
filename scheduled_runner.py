# -*- coding: utf-8 -*-
"""定时调度脚本：每日在配置窗口内随机时间执行主任务（Stage 5）。

变更（相对旧版 BASE_TIMES + 固定偏移）：
- 时间点改为"窗口"概念：在每个窗口 [start, end] 内均匀随机取一个触发时刻；
- 默认窗口 (09:00-09:10) / (18:30-18:40) 与旧版 09:00/18:30 + 0~10 分钟随机
  行为等价，老用户无感知；
- 支持通过环境变量 WORKCLOUD_SCHEDULE 覆盖窗口（可选，不改变用户配置格式）：
    WORKCLOUD_SCHEDULE='{"windows": [["08:00","09:30"], ["17:30","19:00"]]}'
- 避免每天固定同一分钟触发，降低大批量任务同时刻执行的稳定性风险。

用法不变：python scheduled_runner.py [--file name1 name2 ...]
"""

import json
import logging
import argparse
import os
import random
import time
import threading
from datetime import datetime, date, timedelta
from typing import List, Optional, Tuple

# 导入主任务执行函数
from main import execute_tasks

# 尝试导入主模块的日志上下文，失败则创建本地版本
try:
    from main import _log_ctx
except ImportError:
    _log_ctx = threading.local()

logger = logging.getLogger("scheduler")

# 设置调度器的日志标签
_log_ctx.tag = "SCHEDULER"

# 默认触发窗口：与旧版 BASE_TIMES=["09:00","18:30"] + MAX_OFFSET_MINUTES=10 等价
DEFAULT_WINDOWS: List[Tuple[str, str]] = [
    ("09:00", "09:10"),
    ("18:30", "18:40"),
]

# 环境变量覆盖（可选）
ENV_SCHEDULE_KEY = "WORKCLOUD_SCHEDULE"


def _parse_hhmm(text) -> Optional[Tuple[int, int]]:
    """解析 HH:MM，非法返回 None。"""
    try:
        parts = str(text).strip().split(":")
        hour, minute = int(parts[0]), int(parts[1])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (ValueError, IndexError, AttributeError):
        pass
    return None


def load_windows() -> List[Tuple[str, str]]:
    """加载触发窗口：环境变量 WORKCLOUD_SCHEDULE 优先，非法配置回退默认。"""
    raw = os.getenv(ENV_SCHEDULE_KEY, "").strip()
    if not raw:
        return list(DEFAULT_WINDOWS)
    try:
        data = json.loads(raw)
        windows = []
        for item in data.get("windows", []):
            if isinstance(item, (list, tuple)) and len(item) == 2:
                start, end = _parse_hhmm(item[0]), _parse_hhmm(item[1])
                if start and end and start <= end:
                    windows.append((f"{start[0]:02d}:{start[1]:02d}",
                                    f"{end[0]:02d}:{end[1]:02d}"))
                else:
                    logger.warning(f"忽略非法窗口: {item}")
        if windows:
            logger.info(f"使用环境变量 {ENV_SCHEDULE_KEY} 配置的触发窗口: {windows}")
            return windows
        logger.warning(f"{ENV_SCHEDULE_KEY} 中无有效窗口，回退默认窗口")
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        logger.warning(f"{ENV_SCHEDULE_KEY} 解析失败，回退默认窗口: {e}")
    return list(DEFAULT_WINDOWS)


def _random_time_in_window(day: date, window: Tuple[str, str]) -> datetime:
    """在窗口 [start, end] 内（含边界，分钟粒度）随机取一个时刻。"""
    start = _parse_hhmm(window[0])
    end = _parse_hhmm(window[1])
    start_min = start[0] * 60 + start[1]
    end_min = end[0] * 60 + end[1]
    minute_of_day = random.randint(start_min, end_min)
    return datetime.combine(day, datetime.min.time()).replace(
        hour=minute_of_day // 60, minute=minute_of_day % 60)


def generate_daily_schedule(day: date,
                            windows: List[Tuple[str, str]]) -> List[datetime]:
    """为指定日期在每个窗口内生成随机触发时间列表（按时间排序）。"""
    schedule = [_random_time_in_window(day, w) for w in windows]
    schedule.sort()
    logger.info("生成当日计划执行时间: " +
                ", ".join(d.strftime("%Y-%m-%d %H:%M") for d in schedule))
    return schedule


def get_next_run(now: datetime,
                 schedule: List[datetime]) -> Optional[datetime]:
    """从当日计划中获取下一次待执行时间"""
    for run_at in schedule:
        if run_at > now:
            return run_at
    return None


def run_loop(selected_files: Optional[List[str]]):
    """主循环：持续等待并在计划时间执行"""
    windows = load_windows()
    current_day = date.today()
    schedule = generate_daily_schedule(current_day, windows)

    while True:
        now = datetime.now()

        # 日期跨天后重新生成
        if now.date() != current_day:
            current_day = now.date()
            schedule = generate_daily_schedule(current_day, windows)

        next_run = get_next_run(now, schedule)
        if not next_run:
            # 当天全部执行完，准备下一天
            current_day = now.date() + timedelta(days=1)
            schedule = generate_daily_schedule(current_day, windows)
            next_run = get_next_run(datetime.now(), schedule)

        wait_seconds = (next_run - datetime.now()).total_seconds()
        if wait_seconds <= 0:
            # 保险：立即执行
            wait_seconds = 0

        logger.info(f"下一次执行时间: {next_run.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"(等待 {int(wait_seconds)} 秒)")

        # 分段等待，便于 Ctrl+C
        slept = 0
        try:
            while slept < wait_seconds:
                step = min(60, wait_seconds - slept)
                time.sleep(step)
                slept += step
        except KeyboardInterrupt:
            logger.info("收到中断信号，退出调度器")
            return

        # 执行任务
        try:
            logger.info("开始执行 main.execute_tasks")
            execute_tasks(selected_files)
            logger.info("本次执行完成")
        except KeyboardInterrupt:
            logger.info("收到中断信号，退出调度器")
            return
        except Exception as e:
            logger.exception(f"执行任务时发生异常: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="定时调度脚本：每日在触发窗口内随机时间执行主任务"
                    f"（可用环境变量 {ENV_SCHEDULE_KEY} 覆盖窗口）")
    parser.add_argument(
        "--file",
        type=str,
        nargs="+",
        help="指定要执行的配置文件名（不带路径和后缀），透传给主程序",
    )
    args = parser.parse_args()

    windows = load_windows()
    logger.info("调度器启动。触发窗口: " +
                ", ".join(f"{s}-{e}" for s, e in windows))
    try:
        run_loop(args.file)
    except KeyboardInterrupt:
        logger.info("调度器已退出")


if __name__ == "__main__":
    main()
