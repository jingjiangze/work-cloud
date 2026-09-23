# -*- coding: utf-8 -*-
"""L6 — 风险事件本地台账。

以天为单位把"程序自身的异常行为"记录为可审计事件：
data/risk/YYYY-MM-DD.json

记录字段：time / user / task / event_type / stage / action / result。

标准事件类型：
- CAPTCHA_REQUIRED        行为验证码出现（含熔断）
- SUBMIT_UNKNOWN          提交结果未知（服务端是否受理不确定）
- NETWORK_RETRY           查询类请求网络重试
- AUTH_FAILURE            登录失败（含分类）
- DUPLICATE_PREVENTED     服务端/本地判定重复，已阻止重复提交
- SECOND_INSTANCE_BLOCKED 单实例锁拦截了并发进程

隐私红线（本模块强制执行）：
- 不记录密码、Token、完整账号、完整请求体、敏感响应数据；
- 所有字段强制截断，dict/list 等复杂结构直接拒绝（只收标量摘要）。
"""

import json
import logging
import os
import threading
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DEFAULT_RISK_DIR = os.path.join(_DATA_DIR, "risk")

EVENT_CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
EVENT_SUBMIT_UNKNOWN = "SUBMIT_UNKNOWN"
EVENT_NETWORK_RETRY = "NETWORK_RETRY"
EVENT_AUTH_FAILURE = "AUTH_FAILURE"
EVENT_DUPLICATE_PREVENTED = "DUPLICATE_PREVENTED"
EVENT_SECOND_INSTANCE_BLOCKED = "SECOND_INSTANCE_BLOCKED"
EVENT_CAPTCHA_CIRCUIT_BREAK = "CAPTCHA_CIRCUIT_BREAK"

# 明确禁止入库的字段名（防御性过滤，正常调用方不应传这些）
_FORBIDDEN_KEYS = {
    "password", "passwd", "pwd", "token", "authorization", "apikey",
    "api_key", "secret", "secretkey", "cookie", "phone", "mobile",
}

_MAX_FIELD_LEN = 200
_write_lock = threading.Lock()


def _mask_user(user_key: Any) -> str:
    """用户标识脱敏：长数字串（手机号等）保留前 3 后 2，避免完整账号入库。"""
    if not isinstance(user_key, str):
        return _sanitize_field(user_key)
    digits = sum(c.isdigit() for c in user_key)
    if len(user_key) >= 7 and digits / len(user_key) > 0.6:
        return f"{user_key[:3]}****{user_key[-2:]}"
    return user_key[:_MAX_FIELD_LEN]


def _sanitize_field(value: Any) -> str:
    """字段净化：仅收标量，截断到上限，拒绝疑似敏感内容。"""
    if value is None:
        return ""
    if not isinstance(value, (str, int, float, bool)):
        # 复杂结构（dict/list 等）一律不落盘——防止误传完整请求体/响应
        return f"<{type(value).__name__}>"
    text = str(value)
    lowered = text.lower()
    for forbidden in _FORBIDDEN_KEYS:
        if forbidden in lowered and "=" in text or f'"{forbidden}"' in lowered:
            return "<sanitized:可能含敏感字段>"
    return text[:_MAX_FIELD_LEN]


def _risk_path(risk_dir: str, date: str) -> str:
    return os.path.join(risk_dir, f"{date}.json")


# ---------- Stage 3: 线程级风险目录路由 ----------
# run() 在账户线程入口设置 set_active_risk_dir(context.risk_dir)，线程内
# 所有任务函数的 record_event 调用自动落入该账户的 risk/ 目录，无需逐层
# 透传。注意：这是目录路由覆盖，不是身份来源——user_key 始终显式传参；
# 同账户不同轮次运行在不同线程（ThreadPoolExecutor），互不干扰。
_active_risk_dir = threading.local()


def set_active_risk_dir(directory: Optional[str]) -> None:
    """绑定当前线程的风险事件目录（账户级隔离路由）。"""
    _active_risk_dir.value = directory


def get_active_risk_dir() -> Optional[str]:
    return getattr(_active_risk_dir, "value", None)


def _load(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"date": os.path.splitext(os.path.basename(path))[0], "events": []}


def _save(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def record_event(
    user_key: str = "unknown",
    task: str = "",
    event_type: str = "",
    stage: str = "",
    action: str = "",
    result: str = "",
    date: Optional[str] = None,
    risk_dir: Optional[str] = None,
) -> None:
    """记录一条风险事件。任何失败都不影响主流程（静默降级为日志）。"""
    if not event_type:
        return
    try:
        day = date or datetime.now().strftime("%Y-%m-%d")
        # 优先级：显式 risk_dir > 线程级账户目录（Stage 3 路由）> 全局默认
        directory = (risk_dir or get_active_risk_dir()
                     or DEFAULT_RISK_DIR)
        path = _risk_path(directory, day)
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "user": _mask_user(user_key),
            "task": _sanitize_field(task),
            "event_type": _sanitize_field(event_type),
            "stage": _sanitize_field(stage),
            "action": _sanitize_field(action),
            "result": _sanitize_field(result),
        }
        with _write_lock:
            data = _load(path)
            data["events"].append(entry)
            _save(path, data)
        logger.info(f"[RISK_EVENT] {event_type} task={task} stage={stage} action={action}")
    except Exception as e:  # 台账失败绝不阻断业务
        logger.warning(f"[RISK_EVENT] 记录失败（忽略）: {e}")


def daily_summary(
    date: Optional[str] = None,
    risk_dir: Optional[str] = None,
) -> dict:
    """汇总某日风险事件：总数、按类型计数、是否触发熔断。"""
    day = date or datetime.now().strftime("%Y-%m-%d")
    directory = risk_dir or DEFAULT_RISK_DIR
    data = _load(_risk_path(directory, day))
    events = data.get("events", [])

    by_type: dict = {}
    for ev in events:
        et = ev.get("event_type", "?")
        by_type[et] = by_type.get(et, 0) + 1

    return {
        "date": day,
        "total": len(events),
        "by_type": by_type,
        "circuit_breaker_triggered": by_type.get(EVENT_CAPTCHA_CIRCUIT_BREAK, 0) > 0,
    }
