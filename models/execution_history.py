# -*- coding: utf-8 -*-
"""每日执行历史（Stage 10 / Commit 09）。

解决：每次调度执行后，把"当天哪些用户、哪些任务、结果如何、耗时多少"
聚合成一份历史台账，便于事后核对与平台化接入（Cloud Controller）。

布局（data/history/，已 gitignore）：
    data/history/YYYY-MM-DD.json
    {
      "date": "2026-09-22",
      "entries": [
        {
          "user": "<user_key>",
          "started_at": "08:02:31",
          "duration_sec": 12.4,
          "results": [{"task_type": "打卡", "status": "success", "message": "..."}, ...]
        }, ...
      ],
      "updated_at": "..."
    }

写策略：同一天多用户并发结束时各自追加（线程锁 + 原子替换）。
"""

import json
import logging
import os
import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

HISTORY_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "history")

_lock = threading.Lock()


def _file_path(history_dir: str, date: str) -> str:
    return os.path.join(history_dir, f"{re.sub(r'[^0-9-]', '_', date)}.json")


def append_entry(user_key: str,
                 results: List[Dict[str, Any]],
                 started_at: str,
                 duration_sec: float,
                 history_dir: str = HISTORY_DIR,
                 date: Optional[str] = None) -> None:
    """追加一条用户执行记录（best-effort，失败仅告警）。"""
    date = date or datetime.now().strftime("%Y-%m-%d")
    path = _file_path(history_dir, date)
    entry = {
        "user": user_key,
        "started_at": started_at,
        "duration_sec": round(float(duration_sec), 1),
        "results": results,
    }
    try:
        with _lock:
            data: Dict[str, Any] = {"date": date, "entries": []}
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict) and isinstance(loaded.get("entries"), list):
                        data = loaded
                except (OSError, json.JSONDecodeError) as e:
                    logger.warning(f"执行历史读取失败（重建）: {path}: {e}")
            # 同一用户同一天重复执行时覆盖旧条目（保留最新一次）
            data["entries"] = [e for e in data["entries"] if e.get("user") != user_key]
            data["entries"].append(entry)
            data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            os.makedirs(history_dir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
    except OSError as e:
        logger.warning(f"执行历史写入失败: {path}: {e}")


def load_day(date: Optional[str] = None,
             history_dir: str = HISTORY_DIR) -> Dict[str, Any]:
    """读取某天的执行历史（不存在返回空结构）。"""
    date = date or datetime.now().strftime("%Y-%m-%d")
    path = _file_path(history_dir, date)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"date": date, "entries": []}
    except FileNotFoundError:
        return {"date": date, "entries": []}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"执行历史读取失败: {path}: {e}")
        return {"date": date, "entries": []}
