# -*- coding: utf-8 -*-
"""账户级任务开关（多账户改造 Stage 5 / Commit 05）。

任务决策链（任一层明确禁用 → SKIPPED 并记录原因）：

    Registry enabled
        ↓
    Task policy enabled      ← 本模块（账户独立开关）
        ↓
    Original config enabled  ← config.reportSettings.* / 现有逻辑
        ↓
    Preflight → Execute

规则：
- task_policy 未设置或该键缺省 → 不做决定，落到下一层（原配置旗标）
- task_policy 显式 false → 直接 SKIPPED（优先级高于配置旗标）
- task_policy 显式 true  → 仅表示"允许进入下一层"，不越过配置旗标
- 打卡（checkin）历史上无总开关，task_policy 是它的第一道开关；
  缺省时行为与旧版一致（默认执行，节假日/自定义日期逻辑照旧）
"""

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 策略键 → 任务标签（与 main/state_store 使用的任务名一致）
TASK_KEYS = ("checkin", "daily_report", "weekly_report", "monthly_report")
TASK_LABELS = {
    "checkin": "打卡",
    "daily_report": "日报提交",
    "weekly_report": "周报提交",
    "monthly_report": "月报提交",
}
# 策略键 → 原配置旗标路径（checkin 无旗标 → None）
CONFIG_FLAGS = {
    "checkin": None,
    "daily_report": "config.reportSettings.daily.enabled",
    "weekly_report": "config.reportSettings.weekly.enabled",
    "monthly_report": "config.reportSettings.monthly.enabled",
}


def normalize_policy(raw: Optional[Dict[str, Any]]) -> Dict[str, bool]:
    """清洗策略：仅保留已知键、bool 强转、None/未知键剔除。"""
    if not isinstance(raw, dict):
        return {}
    policy: Dict[str, bool] = {}
    for key in TASK_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        policy[key] = bool(value)
    extra = set(raw) - set(TASK_KEYS)
    if extra:
        logger.debug(f"task_policy 忽略未知键: {sorted(extra)}")
    return policy


def effective_enabled(task_key: str,
                      policy: Dict[str, bool],
                      config: Any) -> Tuple[bool, str]:
    """判定某任务是否允许执行。

    Returns:
        (enabled, reason)——enabled=False 时 reason 为 SKIPPED 记录原因。
    """
    if task_key not in TASK_KEYS:
        return False, f"未知任务键: {task_key}"

    # 第 1 层：账户 task_policy（显式 false 直接否决）
    if task_key in policy and not policy[task_key]:
        return False, f"账户任务开关关闭({task_key})"

    # 第 2 层：原配置旗标（checkin 无旗标，交由执行层既有逻辑处理）
    flag_path = CONFIG_FLAGS.get(task_key)
    if flag_path is not None:
        if not config.get_value(flag_path):
            label = TASK_LABELS[task_key]
            return False, f"用户未开启{label}功能"
    return True, ""


def resolve_decisions(policy: Optional[Dict[str, Any]],
                      config: Any) -> Dict[str, Tuple[bool, str]]:
    """一次性解析全部任务的决策（供 run() 构建任务清单）。"""
    normalized = normalize_policy(policy)
    return {key: effective_enabled(key, normalized, config)
            for key in TASK_KEYS}
