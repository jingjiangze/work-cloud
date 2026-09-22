# -*- coding: utf-8 -*-
"""任务执行状态机与本地状态存储（Stage 1 / Stage 2）。

解决 RISK-B01（执行状态零持久化）：每次运行把"执行到了哪里"落盘，
供幂等检查（Stage 2）与每日执行历史（Stage 10）使用。

存储布局（data/ 目录，已加入 .gitignore）：
    data/{date}_{user_key}.json

文件结构：
    {
      "user": "<user_key>",
      "date": "2026-09-22",
      "tasks": {
        "checkin": {"state": "CHECKIN_SUCCESS", "time": "08:02:31", "message": "上班打卡成功"},
        ...
      },
      "updated_at": "2026-09-22T08:02:31"
    }

设计约束：
- 不改变任何用户配置格式；data/ 目录可随时删除（删除后仅失去幂等记忆，
  服务端去重检查仍然兜底）。
- 线程安全：模块级锁 + 临时文件原子替换。
- 兼容 Python 3.10（不使用 StrEnum）。
"""

import json
import logging
import os
import re
import threading
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 默认数据目录：项目根/data
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# 状态常量（Stage 1 计划中的状态集 + 失败细分）
STATE_INIT = "INIT"
STATE_LOGIN_SUCCESS = "LOGIN_SUCCESS"
STATE_CHECKIN_RUNNING = "CHECKIN_RUNNING"
STATE_CHECKIN_SUCCESS = "CHECKIN_SUCCESS"
STATE_REPORT_RUNNING = "REPORT_RUNNING"
STATE_REPORT_SUCCESS = "REPORT_SUCCESS"
STATE_FAILED = "FAILED"
STATE_DONE = "DONE"
STATE_SKIPPED = "SKIPPED"

# 任务名 -> 该任务"已完成"对应的成功状态（Stage 2 幂等判定依据）
_TASK_SUCCESS_STATES = {
    "login": STATE_LOGIN_SUCCESS,
    "checkin": STATE_CHECKIN_SUCCESS,
    "daily_report": STATE_REPORT_SUCCESS,
    "weekly_report": STATE_REPORT_SUCCESS,
    "monthly_report": STATE_REPORT_SUCCESS,
}

_RUNNING_STATES = {
    "checkin": STATE_CHECKIN_RUNNING,
    "daily_report": STATE_REPORT_RUNNING,
    "weekly_report": STATE_REPORT_RUNNING,
    "monthly_report": STATE_REPORT_RUNNING,
}

_file_lock = threading.Lock()


def derive_user_key(config) -> str:
    """从配置推导稳定的用户标识（用于文件名），不引入新配置项。

    优先级：userInfo.userId > config.user.phone > 配置文件名 > unknown。
    文件名安全化：仅保留字母数字下划线连字符。
    """
    for keys in ("userInfo.userId", "config.user.phone"):
        value = config.get_value(keys)
        if value:
            return re.sub(r"[^A-Za-z0-9_\-]", "_", str(value))[:64]
    path = getattr(config, "_path", None)
    if path:
        return re.sub(r"[^A-Za-z0-9_\-]", "_", os.path.splitext(os.path.basename(str(path)))[0])[:64]
    return "unknown"


def running_state_for(task: str) -> str:
    """任务名 -> 运行中状态；未知任务返回 INIT。"""
    return _RUNNING_STATES.get(task, STATE_INIT)


def success_state_for(task: str) -> str:
    """任务名 -> 成功状态；未知任务返回 DONE。"""
    return _TASK_SUCCESS_STATES.get(task, STATE_DONE)


class TaskStateStore:
    """基于 data/ 目录的每日任务状态存储。"""

    def __init__(self, data_dir: str = _DATA_DIR):
        self.data_dir = data_dir
        try:
            os.makedirs(self.data_dir, exist_ok=True)
        except OSError as e:
            logger.warning(f"状态目录创建失败，状态将不落盘: {e}")
            self.data_dir = None

    # ---------- 路径 ----------

    def _file_path(self, user_key: str, date: Optional[str] = None) -> Optional[str]:
        if not self.data_dir:
            return None
        date = date or datetime.now().strftime("%Y-%m-%d")
        return os.path.join(self.data_dir, f"{date}_{user_key}.json")

    # ---------- 读写 ----------

    def _load(self, path: Optional[str]) -> Dict[str, Any]:
        if not path:
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"状态文件读取失败（按空状态处理）: {path}: {e}")
            return {}

    def _save(self, path: Optional[str], data: Dict[str, Any]) -> None:
        if not path:
            return
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except OSError as e:
            logger.warning(f"状态文件写入失败: {path}: {e}")
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    # ---------- 对外 API ----------

    def mark(self, user_key: str, task: str, state: str,
             message: str = "", date: Optional[str] = None) -> None:
        """记录某用户某任务的状态（覆盖式更新当日该任务状态）。"""
        date = date or datetime.now().strftime("%Y-%m-%d")
        path = self._file_path(user_key, date)
        with _file_lock:
            data = self._load(path)
            data.setdefault("tasks", {})
            data["user"] = user_key
            data["date"] = date
            data["tasks"][task] = {
                "state": state,
                "time": datetime.now().strftime("%H:%M:%S"),
                "message": message,
            }
            data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._save(path, data)

    def get_task(self, user_key: str, task: str,
                 date: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """读取某用户某任务当日状态，不存在返回 None。"""
        path = self._file_path(user_key, date)
        with _file_lock:
            data = self._load(path)
        task_info = data.get("tasks", {}).get(task)
        return task_info if isinstance(task_info, dict) else None

    def is_done(self, user_key: str, task: str,
                date: Optional[str] = None) -> bool:
        """Stage 2 幂等判定：当日该任务是否已成功完成。"""
        info = self.get_task(user_key, task, date)
        if not info:
            return False
        return info.get("state") == success_state_for(task)

    def snapshot(self, user_key: str, date: Optional[str] = None) -> Dict[str, Any]:
        """读取某用户当日全部状态（Stage 10 执行历史的数据来源）。"""
        path = self._file_path(user_key, date)
        with _file_lock:
            return self._load(path)
