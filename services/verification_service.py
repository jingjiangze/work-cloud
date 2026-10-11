# -*- coding: utf-8 -*-
"""账户级结果验证（多账户改造 Stage 9 / Commit 09）。

统一语义：HTTP 200 ≠ 任务成功。状态机：

    SUBMIT → RESPONSE → VERIFY → FINAL STATUS
    FINAL ∈ {SUCCESS, FAILED, SKIPPED, UNKNOWN}

规则（与 L3 `_resolve_submit` 同口径）：
- UNKNOWN 必须保留：提交已发出但结果未知时，不判成功也不判失败；
- 收敛手段只有**只读查询**（服务端是否有记录），绝不盲目重提交；
- 服务端有记录 → 判 SUCCESS（含跨轮收敛）；
- 服务端无记录 → 不改状态（本轮可依据"确认无记录"正常重新执行）；
- 只读查询失败 → 维持 UNKNOWN，等待下一轮。

本模块为纯逻辑（状态库与核验函数均注入），便于测试与复用。
"""

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# 规范四态
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED = "SKIPPED"
STATUS_UNKNOWN = "UNKNOWN"

# 结果串（历史小写词汇）→ 规范四态
_ALIASES = {
    "success": STATUS_SUCCESS,
    "fail": STATUS_FAILED,
    "failed": STATUS_FAILED,
    "skip": STATUS_SKIPPED,
    "skipped": STATUS_SKIPPED,
    "unknown": STATUS_UNKNOWN,
}


def normalize_result_status(status: Any) -> str:
    """历史结果串 → 规范四态；未知词汇原样大写返回。"""
    if not isinstance(status, str):
        return STATUS_UNKNOWN
    key = status.strip().lower()
    if key in _ALIASES:
        return _ALIASES[key]
    return status.strip().upper() or STATUS_UNKNOWN


@dataclass
class Verification:
    """单任务的验证结论（落入结果与执行台账）。"""

    method: str                      # e.g. "read-only-query" / "submit-response"
    verified: Optional[bool]         # True/False/None（无法判定）
    detail: str = ""
    resolved_from: str = ""          # 收敛来源（如 "unknown-cross-run"）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method": self.method,
            "verified": self.verified,
            "detail": self.detail,
            "resolved_from": self.resolved_from,
        }


def for_submit_result(status: str) -> Verification:
    """按提交路径的即时结果构造验证结论（L2/L3 内联核验另计）。"""
    norm = normalize_result_status(status)
    if norm == STATUS_SUCCESS:
        return Verification("submit+server-verify", True, "提交响应/L3核验判定")
    if norm == STATUS_FAILED:
        return Verification("submit-response", False, "服务端明确拒绝")
    if norm == STATUS_SKIPPED:
        return Verification("policy-or-dedupe", None, "任务被跳过，无需验证")
    return Verification("read-only-query", None, "结果未知，待只读核验收敛")


def resolve_unknown_tasks(state_store,
                          user_key: str,
                          task_labels: list,
                          verify_fns: Dict[str, Callable[[], bool]],
                          success_state_for: Callable[[str], str],
                          record_event_fn: Optional[Callable] = None,
                          ) -> Dict[str, Verification]:
    """跨轮 UNKNOWN 收敛：对上一轮结果未知的任务做只读核验。

    - 服务端有记录 → 状态置为该任务的成功态（本轮 is_done 生效，跳过重提交）
    - 服务端无记录 → 不改状态（本轮可正常重新执行，本轮 L3 会再核验）
    - 核验失败/无核验函数 → 维持 UNKNOWN
    任何失败都不抛出（收敛是尽力而为的只读动作）。
    """
    resolved: Dict[str, Verification] = {}
    for label in task_labels:
        verify_fn = verify_fns.get(label)
        if verify_fn is None:
            continue
        try:
            info = state_store.get_task(user_key, label)
        except Exception as e:
            logger.warning(f"[VERIFY] 读取状态失败（跳过 {label}）: {e}")
            continue
        if not info or info.get("state") != STATUS_UNKNOWN:
            continue
        try:
            exists = verify_fn()
        except Exception as e:
            resolved[label] = Verification(
                "read-only-query", None,
                f"核验失败，维持 UNKNOWN: {e}", "unknown-cross-run")
            logger.warning(f"[VERIFY] {label} 只读核验失败，维持 UNKNOWN: {e}")
            continue
        if exists:
            try:
                state_store.mark(user_key, label,
                                 success_state_for(label),
                                 "跨轮只读核验：服务端已有记录，判定成功")
            except Exception as e:
                logger.warning(f"[VERIFY] {label} 状态回写失败: {e}")
            resolved[label] = Verification(
                "read-only-query", True, "服务端已有记录，判定成功",
                "unknown-cross-run")
            if record_event_fn:
                record_event_fn(user_key, label, "SUBMIT_UNKNOWN",
                                stage="verify", action="跨轮收敛",
                                result="服务端已有记录，判定成功")
            logger.info(f"[VERIFY] {label} 跨轮收敛：服务端已有记录，判定成功")
        else:
            resolved[label] = Verification(
                "read-only-query", False, "服务端无记录，本轮允许重新提交",
                "unknown-cross-run")
            if record_event_fn:
                record_event_fn(user_key, label, "SUBMIT_UNKNOWN",
                                stage="verify", action="跨轮收敛",
                                result="服务端无记录，放行重新提交")
            logger.info(f"[VERIFY] {label} 跨轮收敛：服务端无记录，放行重新提交")
    return resolved
