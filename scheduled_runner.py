import logging
import argparse
import random
import time
import threading
from datetime import datetime, date, timedelta
from typing import List, Optional

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

BASE_TIMES = ["09:00", "18:30"]  # 触发打卡的基础时间点
MAX_OFFSET_MINUTES = 10  # 去机器化，随机让时间点偏移 0~10 分钟


def generate_daily_schedule(day: date) -> List[datetime]:
    """为指定日期生成带随机偏移的执行时间列表"""
    schedule = []
    for t in BASE_TIMES:
        hour, minute = map(int, t.split(":"))
        offset = random.randint(0, MAX_OFFSET_MINUTES)
        dt = datetime.combine(day, datetime.min.time()).replace(
            hour=hour, minute=minute) + timedelta(minutes=offset)
        schedule.append(dt)
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
    current_day = date.today()
    schedule = generate_daily_schedule(current_day)

    while True:
        now = datetime.now()

        # 日期跨天后重新生成
        if now.date() != current_day:
            current_day = now.date()
            schedule = generate_daily_schedule(current_day)

        next_run = get_next_run(now, schedule)
        if not next_run:
            # 当天全部执行完，准备下一天
            current_day = now.date() + timedelta(days=1)
            schedule = generate_daily_schedule(current_day)
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
    parser = argparse.ArgumentParser(description="定时调度脚本：每日随机偏移执行主任务")
    parser.add_argument(
        "--file",
        type=str,
        nargs="+",
        help="指定要执行的配置文件名（不带路径和后缀），透传给主程序",
    )
    args = parser.parse_args()

    logger.info("调度器启动。基础时间点: " + ", ".join(BASE_TIMES) +
                f" (均加 0~{MAX_OFFSET_MINUTES} 分钟随机偏移)")
    try:
        run_loop(args.file)
    except KeyboardInterrupt:
        logger.info("调度器已退出")


if __name__ == "__main__":
    main()
