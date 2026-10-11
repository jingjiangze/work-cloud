# -*- coding: utf-8 -*-
"""跨账户通知聚合（多账户改造 Stage 13）。

多账户错峰调度下，每账户每轮各自推送（现状，保留）；本模块提供
**每日跨账户聚合摘要**——当日全部计划执行完毕后发一条总览，避免
逐条翻看。默认关闭：仅当调度器环境变量 WK_DIGEST_PUSH（JSON 数组，
与 config.pushNotifications 同构）配置了启用的渠道时才推送。

数据源：data/accounts/{account_id}/ledger/{date}.json（Stage 8 执行
台账，只读聚合，不含任何敏感正文）。
"""

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ENV_DIGEST_PUSH = "WK_DIGEST_PUSH"

# overall → (emoji, 文案)
_OVERALL = {
    "success": ("✅", "成功"),
    "failed": ("❌", "失败"),
    "unknown": ("❓", "未知"),
    "skipped": ("⏭️", "跳过"),
}


def load_digest_push_config(env_value: Optional[str] = None) -> list:
    """读聚合推送配置（env WK_DIGEST_PUSH，JSON 数组）。未配置/非法 → 空列表。"""
    raw = env_value if env_value is not None else os.environ.get(ENV_DIGEST_PUSH, "")
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError as e:
        logger.warning(f"{ENV_DIGEST_PUSH} 不是合法 JSON，聚合摘要不推送: {e}")
        return []


def collect_day_summary(accounts_dir: str, date: str) -> Dict[str, Any]:
    """聚合某日全部账户执行台账。

    Returns:
        {"date", "accounts": [{"account_id", "display_name", "runs",
          "last_status", "last_time", "tasks": [{"task_type", "status"}],
          "failures": int}], "total", "failed_accounts", "unknown_accounts"}
        无任何台账时 accounts 为空列表。
    """
    summary: Dict[str, Any] = {
        "date": date, "accounts": [], "total": 0,
        "failed_accounts": 0, "unknown_accounts": 0,
    }
    if not os.path.isdir(accounts_dir):
        return summary
    for name in sorted(os.listdir(accounts_dir)):
        acct_dir = os.path.join(accounts_dir, name)
        ledger_path = os.path.join(acct_dir, "ledger", f"{date}.json")
        if not os.path.isfile(ledger_path):
            continue
        try:
            with open(ledger_path, "r", encoding="utf-8") as f:
                day = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"台账读取失败（跳过）{ledger_path}: {e}")
            continue
        runs = day.get("runs") if isinstance(day, dict) else None
        if not isinstance(runs, list) or not runs:
            continue
        last = runs[-1]
        tasks = [
            {"task_type": t.get("task_type", ""),
             "status": t.get("status", "")}
            for t in (last.get("tasks") or [])
        ]
        # 注册表显示名（缺省回退 account_id）
        display_name = name
        reg_path = os.path.join(accounts_dir, "index.json")
        if os.path.isfile(reg_path):
            try:
                with open(reg_path, "r", encoding="utf-8") as f:
                    reg = json.load(f)
                for item in reg.get("accounts", []):
                    if item.get("account_id") == name:
                        display_name = item.get("display_name") or name
                        break
            except (OSError, json.JSONDecodeError):
                pass
        entry = {
            "account_id": name,
            "display_name": display_name,
            "runs": len(runs),
            "last_status": last.get("status", "unknown"),
            "last_time": last.get("ended_at", ""),
            "tasks": tasks,
        }
        summary["accounts"].append(entry)
        if entry["last_status"] == "failed":
            summary["failed_accounts"] += 1
        elif entry["last_status"] == "unknown":
            summary["unknown_accounts"] += 1
    summary["total"] = len(summary["accounts"])
    return summary


def build_digest_markdown(summary: Dict[str, Any]) -> tuple:
    """聚合摘要 → (Markdown 文本, 通知等级)。"""
    date = summary.get("date", "")
    total = summary.get("total", 0)
    failed = summary.get("failed_accounts", 0)
    unknown = summary.get("unknown_accounts", 0)
    ok = total - failed - unknown
    level = "ERROR" if failed else ("WARNING" if unknown or not total
                                    else "INFO")
    parts = [f"# 工学云多账户日报（{date}）\n\n"]
    parts.append("## 📊 账户总览\n\n")
    if not total:
        parts.append("- 今日无任何账户执行记录\n")
    else:
        parts.append(f"- 账户数：{total}（成功 {ok} / 失败 {failed}"
                     f" / 未知 {unknown}）\n")
    parts.append("\n## 📝 各账户明细\n\n")
    for a in summary.get("accounts", []):
        emoji, label = _OVERALL.get(a.get("last_status", "unknown"),
                                    _OVERALL["unknown"])
        parts.append(f"### {emoji} {a.get('display_name', '')}"
                     f"（{a.get('account_id', '')}）\n\n")
        parts.append(f"- 最后一轮：{label}（{a.get('last_time', '')}，"
                     f"今日 {a.get('runs', 0)} 轮）\n")
        for t in a.get("tasks", []):
            t_emoji = {"success": "✅", "fail": "❌", "skip": "⏭️",
                       "unknown": "❓"}.get(t.get("status", "unknown"), "❓")
            parts.append(f"  - {t_emoji} {t.get('task_type', '')}"
                         f"：{t.get('status', '')}\n")
        parts.append("\n")
    return "".join(parts), level


def push_daily_digest(push_config: list, accounts_dir: str,
                      date: str = None) -> bool:
    """构建并推送当日聚合摘要。返回是否实际推送。

    任何失败只记日志，绝不影响调度器主流程。
    """
    date = date or datetime.now().strftime("%Y-%m-%d")
    if not push_config:
        return False
    try:
        from util.MessagePush import MessagePusher
        summary = collect_day_summary(accounts_dir, date)
        if not summary["total"]:
            logger.info("聚合摘要：今日无账户执行记录，不推送")
            return False
        markdown, level = build_digest_markdown(summary)
        pusher = MessagePusher(push_config)
        pusher.push_custom(f"📊 多账户日报 {date}", markdown, level=level)
        logger.info(f"聚合摘要已推送（{summary['total']} 账户）")
        return True
    except Exception as e:  # best-effort
        logger.warning(f"聚合摘要推送失败（忽略）: {e}")
        return False
