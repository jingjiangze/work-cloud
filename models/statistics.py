# -*- coding: utf-8 -*-
"""生产统计汇总（Stage 11.2）。

聚合 data/history/*.json 台账，生成 data/statistics.json：

    {
      "total_runs": 30,        用户轮次总数
      "success_runs": 28,      无任何失败的用户轮次
      "failed_runs": 2,        含失败任务的用户轮次
      "total_tasks": 120,      任务总数（含 skip）
      "success_tasks": 110,
      "failed_tasks": 2,
      "skipped_tasks": 8,
      "avg_duration_sec": 35.2,
      "days": 7,               有台账的天数
      "date_range": ["2026-09-22", "2026-09-28"],
      "updated_at": "..."
    }

用途：
- 观察期（Stage 11）每日核对；
- 未来 autotask-platform 对接的数据源（与 docs/OPERATIONS.md 第 6 节呼应）。

CLI：
    python -m models.statistics            # 生成并打印
    python -m models.statistics --quiet    # 只写文件
"""

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
HISTORY_DIR = os.path.join(_DATA_DIR, "history")
OUTPUT_PATH = os.path.join(_DATA_DIR, "statistics.json")


def _status_counts(results: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {"success": 0, "fail": 0, "skip": 0, "unknown": 0}
    for r in results or []:
        status = r.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def aggregate(history_dir: str = HISTORY_DIR) -> Dict[str, Any]:
    """扫描台账目录并汇总。目录不存在时返回全零结构。"""
    stats: Dict[str, Any] = {
        "total_runs": 0,
        "success_runs": 0,
        "failed_runs": 0,
        "total_tasks": 0,
        "success_tasks": 0,
        "failed_tasks": 0,
        "skipped_tasks": 0,
        "avg_duration_sec": 0.0,
        "days": 0,
        "date_range": [],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if not os.path.isdir(history_dir):
        return stats

    durations: List[float] = []
    days: List[str] = []
    for filename in sorted(os.listdir(history_dir)):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(history_dir, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"统计跳过损坏台账: {path}: {e}")
            continue
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            continue
        days.append(filename[:-5])
        for entry in entries:
            results = entry.get("results") or []
            counts = _status_counts(results)
            stats["total_runs"] += 1
            if counts["fail"] > 0:
                stats["failed_runs"] += 1
            else:
                stats["success_runs"] += 1
            stats["total_tasks"] += len(results)
            stats["success_tasks"] += counts["success"]
            stats["failed_tasks"] += counts["fail"]
            stats["skipped_tasks"] += counts["skip"]
            try:
                durations.append(float(entry.get("duration_sec", 0)))
            except (TypeError, ValueError):
                pass

    stats["days"] = len(days)
    if days:
        stats["date_range"] = [min(days), max(days)]
    if durations:
        stats["avg_duration_sec"] = round(sum(durations) / len(durations), 1)
    return stats


def generate(history_dir: str = HISTORY_DIR,
             output_path: str = OUTPUT_PATH) -> Dict[str, Any]:
    """聚合并写入 statistics.json（best-effort，失败不影响主流程）。"""
    stats = aggregate(history_dir)
    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        tmp = output_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        os.replace(tmp, output_path)
    except OSError as e:
        logger.warning(f"统计文件写入失败: {output_path}: {e}")
    return stats


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="生成生产统计汇总 data/statistics.json")
    parser.add_argument("--quiet", action="store_true", help="只写文件不打印")
    args = parser.parse_args()
    result = generate()
    if not args.quiet:
        print(json.dumps(result, ensure_ascii=False, indent=2))
