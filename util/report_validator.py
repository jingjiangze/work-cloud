# -*- coding: utf-8 -*-
"""报告内容校验与重复检测（Stage 6）。

解决：
- AI 生成内容为空 / 过短 / 残留模板占位符（"xxxx"）时被原样提交；
- 连续周期提交完全相同的内容。

历史记录布局（reports/ 已 gitignore）：
    reports/history/{user_key}/{report_type}.json
    结构: {"records": [{"date": "...", "hash": "...", "preview": "..."}]}
    每条仅存指纹与归一化文本（去标点/空白，截断至 COMPARE_CHARS），
    不保存原始全文；保留最近 N 条（HISTORY_KEEP）。

重复判定：
    归一化文本（去空白/标点、小写）后：
    1) 与任一历史记录 SHA1 相同 → 完全重复；
    2) 与最近几条 difflib 相似度 >= DUPLICATE_RATIO → 高度相似。
"""

import difflib
import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_HISTORY_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports", "history")

HISTORY_KEEP = 10          # 每用户每类型保留的历史条数
DUPLICATE_RATIO = 0.90     # 相似度阈值
COMPARE_CHARS = 4000       # 归一化文本参与比较的最大长度
DEFAULT_MIN_LENGTH = 100
DEFAULT_MAX_LENGTH = 5000

# 模板残留 / 占位符特征
_PLACEHOLDER_PATTERNS = (
    re.compile(r"[xX]{3,}"),           # xxxx / XXX
    re.compile(r"[…\.]{6,}"),          # ......
    re.compile(r"[（(]\s*[）)]"),      # 空括号
    re.compile(r"某某|【】|\{\}"),
)

_normalize_re = re.compile(r"[\s\W_]+", re.UNICODE)

_lock = threading.Lock()


def normalize_text(text: str) -> str:
    """归一化：去空白/标点/下划线并小写，用于比较。"""
    return _normalize_re.sub("", text or "").lower()


def text_hash(text: str) -> str:
    return hashlib.sha1(normalize_text(text).encode("utf-8")).hexdigest()


def validate_report(content: str,
                    min_length: int = DEFAULT_MIN_LENGTH,
                    max_length: int = DEFAULT_MAX_LENGTH) -> Tuple[bool, List[str]]:
    """校验报告内容。返回 (是否通过, 问题列表)。"""
    issues: List[str] = []
    text = (content or "").strip()
    if not text:
        return False, ["内容为空"]
    if len(text) < min_length:
        issues.append(f"内容过短（{len(text)} < {min_length} 字）")
    if len(text) > max_length:
        issues.append(f"内容过长（{len(text)} > {max_length} 字）")
    for pattern in _PLACEHOLDER_PATTERNS:
        match = pattern.search(text)
        if match:
            issues.append(f"疑似模板占位符残留: '{match.group(0)[:12]}'")
            break
    return (not issues), issues


def _history_path(user_key: str, report_type: str) -> str:
    safe_user = re.sub(r"[^A-Za-z0-9_\-]", "_", user_key)[:64]
    safe_type = re.sub(r"[^A-Za-z0-9_\-]", "_", report_type)[:16]
    return os.path.join(_HISTORY_DIR, safe_user, f"{safe_type}.json")


def _load(path: str) -> List[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("records", []) if isinstance(data, dict) else []
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"报告历史读取失败（按空处理）: {path}: {e}")
        return []


def _save(path: str, records: List[dict]) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"records": records}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning(f"报告历史写入失败: {path}: {e}")


def check_duplicate(user_key: str, report_type: str, content: str,
                    history_dir: str = _HISTORY_DIR) -> Tuple[bool, Optional[str]]:
    """检测内容是否与历史重复。返回 (是否重复, 相似记录日期)。"""
    path = _history_path(user_key, report_type)
    if history_dir != _HISTORY_DIR:
        path = os.path.join(history_dir, user_key, f"{report_type}.json")
    records = _load(path)
    if not records:
        return False, None

    digest = text_hash(content)
    normalized = normalize_text(content)[:COMPARE_CHARS]
    for record in records:
        if record.get("hash") == digest:
            return True, record.get("date")
    for record in records[-5:]:
        old = record.get("preview_normalized")
        if old and difflib.SequenceMatcher(None, normalized, old, autojunk=False).ratio() >= DUPLICATE_RATIO:
            return True, record.get("date")
    return False, None


def record_report(user_key: str, report_type: str, content: str,
                  date: Optional[str] = None,
                  history_dir: str = _HISTORY_DIR) -> None:
    """报告成功提交后记录指纹（含归一化预览，供相似度比较）。"""
    path = _history_path(user_key, report_type)
    if history_dir != _HISTORY_DIR:
        path = os.path.join(history_dir, user_key, f"{report_type}.json")
    date = date or datetime.now().strftime("%Y-%m-%d")
    normalized = normalize_text(content)
    entry = {
        "date": date,
        "hash": text_hash(content),
        "preview": (content or "")[:60],
        "preview_normalized": normalize_text(content)[:COMPARE_CHARS],
    }
    with _lock:
        records = _load(path)
        records.append(entry)
        records = records[-HISTORY_KEEP:]
        _save(path, records)
