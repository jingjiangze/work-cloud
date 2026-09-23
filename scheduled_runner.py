# -*- coding: utf-8 -*-
"""定时调度脚本（多账户改造 Stage 6）：按账户调度执行主任务。

变更（Stage 6，相对旧版全局窗口）：
- Scheduler → Account Registry → 过滤 enabled 账户 → 各账户 schedule_profile
  → 每账户独立生成当日随机触发时刻 → 到点按账户触发
- 账户 schedule_profile.enabled=False → 永不调度；
  windows 未配置 → 使用全局窗口（env 覆盖 → 默认 12:30-12:40 / 17:30-17:40）
- 到点只执行该账户（execute_tasks(selected_files=[配置文件名])），
  不再把所有用户捆在同一时刻全量执行
- 兼容：registry 为空时回退旧全局行为（一次执行全部）；--file 显式指定时
  仍按全局窗口执行指定文件
- 随机性仅用于错开本地任务同时启动，不用于任何规避平台检测的行为

用法不变：python scheduled_runner.py [--file name1 name2 ...]
"""

import logging
import os
import threading
import time
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple

from main import execute_tasks
from core import scheduler as sched
from models import account_registry

try:
    from main import _log_ctx
except ImportError:
    _log_ctx = threading.local()

logger = logging.getLogger("scheduler")
_log_ctx.tag = "SCHEDULER"

# ---- 兼容旧接口（历史调用方/测试可能直接引用） ----
DEFAULT_WINDOWS = sched.DEFAULT_WINDOWS
ENV_SCHEDULE_KEY = sched.ENV_SCHEDULE_KEY
load_windows = sched.load_global_windows
generate_daily_schedule = sched.generate_daily_schedule


def _parse_hhmm(text):
    return sched.parse_hhmm(text)


def get_next_run_time(now: datetime,
                      times: List[datetime]) -> Optional[datetime]:
    for run_at in times:
        if run_at > now:
            return run_at
    return None


def _enabled_account_files(selected_files: Optional[List[str]]) -> List[Tuple[str, str]]:
    """返回 [(account_id, config 文件名 stem)]，仅启用账户。

    selected_files 显式给定时仍按 registry 过滤（禁用账户不出列）。
    注：目录在调用期读取（而非默认参数定义期绑定），便于整体重定向。
    """
    user_dir = account_registry.USER_DIR
    registry_path = account_registry.REGISTRY_PATH
    account_registry.ensure_registry(user_dir=user_dir,
                                     registry_path=registry_path)
    accounts = account_registry.list_accounts(user_dir=user_dir,
                                              registry_path=registry_path)
    result = []
    for acc in accounts:
        if not acc.enabled:
            continue
        if selected_files and os.path.splitext(acc.config_file)[0] not in selected_files:
            continue
        result.append((acc.account_id, os.path.splitext(acc.config_file)[0]))
    return result


def run_loop(selected_files: Optional[List[str]]):
    """主循环：按账户计划等待并在到点时执行该账户任务。"""
    global_windows = load_windows()
    current_day = date.today()
    # fired: account_id → 已触发的当日时刻数（保证每时刻只触发一次）
    fired: Dict[str, int] = {}
    plans: Dict[str, List[datetime]] = {}
    legacy_schedule: List[datetime] = []
    legacy_fired = 0

    def rebuild(day: date):
        nonlocal plans, legacy_schedule, fired, legacy_fired
        fired, legacy_fired = {}, 0
        accounts = [
            acc for acc in account_registry.list_accounts()
            if sched.account_schedule_windows(acc, global_windows) is not None
        ]
        if selected_files or not accounts:
            # 显式 --file 或无注册账户：回退旧全局行为
            legacy_schedule = generate_daily_schedule(day, global_windows)
            plans = {}
            logger.info("使用全局窗口调度（legacy 模式）: "
                        + ", ".join(t.strftime("%H:%M") for t in legacy_schedule))
        else:
            legacy_schedule = []
            plans = sched.build_account_schedules(day, accounts, global_windows)
            if not plans:
                logger.warning("没有可调度的启用账户，今日空转")

    rebuild(current_day)

    while True:
        now = datetime.now()
        if now.date() != current_day:
            current_day = now.date()
            rebuild(current_day)

        # 找下一次触发时刻（账户计划优先，legacy 次之）
        next_run = None
        for times in plans.values():
            t = get_next_run_time(now, times)
            if t and (next_run is None or t < next_run):
                next_run = t
        if legacy_schedule:
            t = get_next_run_time(now, legacy_schedule)
            if t and (next_run is None or t < next_run):
                next_run = t

        if not next_run:
            # 当日全部执行完 → 准备下一天
            current_day = now.date() + timedelta(days=1)
            rebuild(current_day)
            continue

        wait_seconds = max(0, (next_run - datetime.now()).total_seconds())
        logger.info(f"下一次执行时间: {next_run.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"(等待 {int(wait_seconds)} 秒)")

        slept = 0
        try:
            while slept < wait_seconds:
                step = min(60, wait_seconds - slept)
                time.sleep(step)
                slept += step
        except KeyboardInterrupt:
            logger.info("收到中断信号，退出调度器")
            return

        # 到点：触发所有到时刻的账户（或 legacy 全量一次）
        now = datetime.now()
        try:
            due = sched.due_account_runs(plans, now, fired)
            for account_id, run_at in due:
                acc = account_registry.get_account(account_id)
                if acc is None or not acc.enabled:
                    continue  # 运行中被禁用 → 跳过
                file_stem = os.path.splitext(acc.config_file)[0]
                logger.info(f"触发账户 {account_id} ({file_stem}) "
                            f"计划时刻 {run_at.strftime('%H:%M')}")
                execute_tasks([file_stem])
            if legacy_schedule:
                while (legacy_fired < len(legacy_schedule)
                       and legacy_schedule[legacy_fired] <= now):
                    legacy_fired += 1
                    logger.info("触发全局执行（legacy 模式）")
                    execute_tasks(selected_files)
        except KeyboardInterrupt:
            logger.info("收到中断信号，退出调度器")
            return
        except Exception as e:
            logger.exception(f"执行任务时发生异常: {e}")


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="定时调度脚本：按账户计划执行主任务"
                    f"（可用环境变量 {ENV_SCHEDULE_KEY} 覆盖全局窗口）")
    parser.add_argument(
        "--file",
        type=str,
        nargs="+",
        help="指定要执行的配置文件名（不带路径和后缀），透传给主程序",
    )
    args = parser.parse_args()

    windows = load_windows()
    logger.info("调度器启动。全局触发窗口: " +
                ", ".join(f"{s}-{e}" for s, e in windows))
    try:
        run_loop(args.file)
    except KeyboardInterrupt:
        logger.info("调度器已退出")


if __name__ == "__main__":
    main()
