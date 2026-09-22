# -*- coding: utf-8 -*-
"""结构化 JSON 日志（Stage 8）。

解决 RISK-C06：脚本型日志、无文件落盘、异常无堆栈。

布局：
    logs/YYYY/MM/DD/app.log     每天一个文件，JSON Lines 格式

每行格式：
    {"time": "...", "level": "INFO", "logger": "main",
     "user": "MAIN|张*", "message": "...", "traceback": "..."(可选)}

说明：
- 控制台日志保持原有的人类可读格式不变；
- 文件日志与控制台共用 userTag 上下文（_log_ctx），多用户并发可区分；
- 写文件失败不影响主流程（best-effort）；
- logs/ 已加入 .gitignore；
- 环境变量 WORKCLOUD_NO_FILE_LOG=1 可关闭文件日志。
"""

import json
import logging
import os
import traceback
from datetime import datetime
from typing import Optional

# 默认日志根目录：项目根/logs
_LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")

_ENV_DISABLE = "WORKCLOUD_NO_FILE_LOG"


class JsonFormatter(logging.Formatter):
    """把 LogRecord 格式化为单行 JSON（JSON Lines）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created).strftime(
                "%Y-%m-%d %H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "user": getattr(record, "userTag", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["traceback"] = "".join(
                traceback.format_exception(*record.exc_info))
        try:
            return json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            payload["message"] = str(payload["message"])
            return json.dumps(payload, ensure_ascii=False)


def setup_file_logging(base_dir: str = _LOG_DIR,
                       level: int = logging.INFO) -> Optional[logging.Handler]:
    """为 root logger 挂载按日期分目录的 JSON 文件处理器。

    - 已挂载（handler 存在同名标记）时不重复挂载；
    - 目录创建/写文件失败时静默降级为仅控制台日志；
    - 返回挂载的 handler（未挂载返回 None）。
    """
    if os.getenv(_ENV_DISABLE, "").strip() in ("1", "true", "True"):
        logging.getLogger(__name__).info("文件日志已通过环境变量关闭")
        return None

    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, "_workcloud_json_file", False):
            return handler  # 已挂载

    try:
        day_dir = datetime.now().strftime("%Y/%m/%d")
        log_path = os.path.join(base_dir, day_dir, "app.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler._workcloud_json_file = True  # type: ignore[attr-defined]
        handler.setFormatter(JsonFormatter())
        handler.setLevel(level)
        root.addHandler(handler)
        return handler
    except OSError as e:
        logging.getLogger(__name__).warning(f"文件日志初始化失败，仅使用控制台日志: {e}")
        return None
