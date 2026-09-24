# -*- coding: utf-8 -*-
"""报告记录生命周期（多账户改造 Stage 10 / Commit 10）。

每次报告成功提交后，在该账户 reports/{date}/{type}/ 目录保存一份
**元数据记录**（不含正文，仅指纹与状态——正文与截图属业务敏感内容，
不入看板/台账）：

    {
      "report_id":    "rpt_2026-09-24_week_ab12cd34",
      "account_id":   "acct_xxx",
      "report_type":  "day" | "week" | "month",
      "period":       {key/start_date/end_date}（ReportPeriod）,
      "generated_at": "YYYY-MM-DD HH:MM:SS",
      "content_hash": "...",        # 与 util/report_validator 同一哈希
      "preview":      "前 60 字符",
      "submit_status": "success" | "unknown",
      "verify_status": true | false | null,
    }

兼容：无 context（legacy）时记录进 data/reports_meta/{user_key}/。
"""

import json
import logging
import os
import threading
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_write_lock = threading.Lock()

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_META_DIR = os.path.join(_PROJECT_ROOT, "data", "reports_meta")


def save_report_record(context,
                       report_type: str,
                       period: Dict[str, Any],
                       content_hash: str,
                       preview: str,
                       submit_status: str,
                       verify_status: Optional[bool],
                       user_key: str = "unknown",
                       content_len: int = 0) -> Optional[str]:
    """保存报告元数据记录（best-effort，失败仅告警）。"""
    try:
        now = datetime.now()
        day = now.strftime("%Y-%m-%d")
        digest8 = (content_hash or "")[:8] or "nohash"
        report_id = f"rpt_{day}_{report_type}_{digest8}"

        if context is not None:
            base = os.path.join(context.report_dir, day, report_type)
            account_id = context.account_id
        else:
            base = os.path.join(DEFAULT_META_DIR, str(user_key), report_type)
            account_id = None

        record = {
            "report_id": report_id,
            "account_id": account_id,
            "report_type": report_type,
            "period": period,
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "content_hash": content_hash,
            "preview": (preview or "")[:60],
            "content_len": content_len,
            "submit_status": submit_status,
            "verify_status": verify_status,
        }
        path = os.path.join(base, f"{report_id}.json")
        with _write_lock:
            os.makedirs(base, exist_ok=True)
            tmp = path + f".tmp.{os.getpid()}.{threading.get_ident()}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        logger.info(f"[REPORT_RECORD] {report_id} 保存成功")
        return report_id
    except Exception as e:  # 元数据失败绝不影响业务
        logger.warning(f"[REPORT_RECORD] 保存失败（忽略）: {e}")
        return None


def load_day_records(context, day: str) -> list:
    """读取某账户某日全部报告元数据（供后续看板 Stage 11 使用）。"""
    if context is None:
        return []
    base = os.path.join(context.report_dir, day)
    records = []
    try:
        for rtype in sorted(os.listdir(base)):
            rdir = os.path.join(base, rtype)
            if not os.path.isdir(rdir):
                continue
            for name in os.listdir(rdir):
                if not name.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(rdir, name),
                              encoding="utf-8") as f:
                        records.append(json.load(f))
                except (OSError, ValueError):
                    continue
    except OSError:
        pass
    return records
