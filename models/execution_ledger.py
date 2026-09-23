# -*- coding: utf-8 -*-
"""账户级执行台账（多账户改造 Stage 8 / Commit 08）。

每次运行生成 run_id：
    run_{YYYYMMDD}_{HHMMSS}_{account_id}_{4位随机}

记录（可回答：哪个账号？哪一天？哪一轮？哪个任务？什么时候开始？
是否成功？为什么失败？）：
    run_id / account_id / user / date / started_at / ended_at /
    duration_sec / status(overall) / trigger / tasks[]（task_type,
    status, message, attempt, verification）

存储：data/accounts/{account_id}/ledger/{date}.json（legacy 无 context
时写 data/ledger/{date}.json，user 字段保留原 user_key）。
写路径与 execution_history 同款：线程锁 + 原子写 + 损坏重建。
verification 字段由 Stage 9 结果验证填充，当前为 None。
"""

import json
import logging
import os
import secrets
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_write_lock = threading.Lock()

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LEDGER_DIR = os.path.join(_PROJECT_ROOT, "data", "ledger")


def _sanitize_key(key: str) -> str:
    """run_id 内嵌 key 仅保留字母数字（acct_xxx → acctxxx）。"""
    return "".join(c for c in str(key or "unknown") if c.isalnum()) or "unknown"


def new_run_id(key: str) -> str:
    now = datetime.now()
    return (f"run_{now.strftime('%Y%m%d_%H%M%S')}_"
            f"{_sanitize_key(key)}_{secrets.token_hex(2)}")


def _ledger_path(ledger_dir: str, date: str) -> str:
    return os.path.join(ledger_dir, f"{date}.json")


def _load(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("runs"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"date": os.path.splitext(os.path.basename(path))[0], "runs": []}


def _save(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def append_run(ledger_dir: str, run: Dict[str, Any],
               date: Optional[str] = None) -> None:
    """追加一条运行记录（best-effort，失败仅告警不阻断业务）。"""
    try:
        day = date or datetime.now().strftime("%Y-%m-%d")
        path = _ledger_path(ledger_dir, day)
        entry = {
            "run_id": run.get("run_id", ""),
            "account_id": run.get("account_id"),
            "user": run.get("user", "unknown"),
            "started_at": run.get("started_at", ""),
            "ended_at": run.get("ended_at", ""),
            "duration_sec": run.get("duration_sec"),
            "status": run.get("status", ""),
            "trigger": run.get("trigger", ""),
            "tasks": run.get("tasks", []),
        }
        with _write_lock:
            data = _load(path)
            data["runs"].append(entry)
            _save(path, data)
        logger.info(f"[LEDGER] run_id={entry['run_id']} status={entry['status']} "
                    f"tasks={len(entry['tasks'])}")
    except Exception as e:  # 台账失败绝不影响主流程
        logger.warning(f"[LEDGER] 记录失败（忽略）: {e}")


def load_day(ledger_dir: str, date: str) -> Dict[str, Any]:
    path = _ledger_path(ledger_dir, date)
    return _load(path)


def summarize_results(results: List[Dict[str, Any]]) -> str:
    """整体状态：任一 fail → failed；有 success → success；否则 skipped。"""
    statuses = {str(r.get("status", "")).lower() for r in results}
    if "fail" in statuses:
        return "failed"
    if "success" in statuses:
        return "success"
    return "skipped"
