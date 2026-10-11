# -*- coding: utf-8 -*-
"""图片管理：上传前校验 + 上传结果记录（Stage 7）。

解决：
- RISK-B05：图片损坏/缺失静默降级为无图打卡且无记录；
- 上传结果不落盘，无法事后核对。

上传结果布局（data/uploads/，已 gitignore）：
    data/uploads/{date}_{user_key}.json
    {"date": "...", "user": "...", "records": [
        {"file": "images/a.jpg", "ok": true, "key": "...", "reason": ""}, ...]}

校验项：文件存在、大小 0~10MB、PIL 可打开且可解码、最小尺寸 50x50、
格式为 JPEG/PNG。
"""

import json
import logging
import os
import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

UPLOAD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "uploads")

MAX_FILE_BYTES = 10 * 1024 * 1024   # 原始文件上限 10MB（压缩由 FileUploader 处理）
MIN_SIDE_PIXELS = 50
ALLOWED_EXTENSIONS = (".png", ".jpg", ".jpeg")

_lock = threading.Lock()


def validate_image_file(image_path: str) -> Tuple[bool, str]:
    """校验单个图片文件。返回 (是否可用, 原因)。"""
    if not os.path.isfile(image_path):
        return False, "文件不存在"
    try:
        size = os.path.getsize(image_path)
    except OSError as e:
        return False, f"无法读取文件大小: {e}"
    if size == 0:
        return False, "文件为空"
    if size > MAX_FILE_BYTES:
        return False, f"文件过大（{size // 1024 // 1024}MB > 10MB）"
    if not image_path.lower().endswith(ALLOWED_EXTENSIONS):
        return False, "扩展名不支持"

    try:
        with Image.open(image_path) as img:
            img.verify()  # 校验文件完整性（解码头/校验和）
        # verify 后需重新打开校验尺寸（verify 会消耗文件对象）
        with Image.open(image_path) as img2:
            width, height = img2.size
    except (UnidentifiedImageError, OSError, ValueError) as e:
        return False, f"图片损坏或格式不支持: {e}"
    if width < MIN_SIDE_PIXELS or height < MIN_SIDE_PIXELS:
        return False, f"尺寸过小（{width}x{height} < {MIN_SIDE_PIXELS}px）"
    return True, ""


def filter_valid_images(image_paths: List[str]) -> Tuple[List[str], List[Dict[str, str]]]:
    """批量校验。返回 (可用路径列表, 不可用记录列表)。"""
    valid: List[str] = []
    invalid: List[Dict[str, str]] = []
    for path in image_paths:
        ok, reason = validate_image_file(path)
        if ok:
            valid.append(path)
        else:
            logger.warning(f"图片校验不通过，跳过: {path}: {reason}")
            invalid.append({"file": os.path.basename(path), "reason": reason})
    return valid, invalid


def record_upload_result(user_key: str, records: List[Dict[str, Any]],
                         upload_dir: str = UPLOAD_DIR,
                         date: Optional[str] = None) -> None:
    """记录一次上传的明细（成功 key / 失败原因）。"""
    if not records:
        return
    date = date or datetime.now().strftime("%Y-%m-%d")
    safe_user = re.sub(r"[^A-Za-z0-9_\-]", "_", user_key)[:64]
    path = os.path.join(upload_dir, f"{date}_{safe_user}.json")
    payload = {
        "date": date,
        "user": user_key,
        "records": records,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    with _lock:
        try:
            os.makedirs(upload_dir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError as e:
            logger.warning(f"上传结果记录失败: {path}: {e}")
