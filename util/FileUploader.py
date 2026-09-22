import os
import io
import logging
import random
from typing import Optional

from PIL import Image

from coreApi.FileUploadApi import upload
from util.image_manager import filter_valid_images, record_upload_result

logger = logging.getLogger(__name__)


def process_image(image_path: str) -> bytes:
    """
    读取并处理图片，确保格式为JPEG，且大小不超过1MB。
    通过动态调整JPEG压缩质量来控制文件大小。

    Args:
        image_path (str): 图片路径。

    Returns:
        bytes: 处理后的图片二进制数据。
    """
    # 打开原始图片
    with Image.open(image_path) as img:
        # 如果图片格式不是JPEG，则转换为RGB模式
        if (img.format is None) or (str(img.format).upper() != "JPEG"):
            img = img.convert("RGB")

        # 定义文件大小上限（1MB）
        max_size = 1 * 1024 * 1024

        # 初始化质量参数
        quality = 85
        min_quality = 5
        max_quality = 95

        # 使用二分查找方法优化质量压缩
        # 预先定义BytesIO对象，避免循环中重复创建（虽然差异不大，但更清晰）
        img_byte_arr = io.BytesIO()

        while max_quality - min_quality > 5:
            img_byte_arr.seek(0)
            img_byte_arr.truncate(0)
            img.save(img_byte_arr, format="JPEG", quality=quality)

            # 获取当前图片大小
            current_size = img_byte_arr.tell()

            # 根据当前大小调整压缩质量
            if current_size > max_size:
                # 如果太大，降低质量
                max_quality = quality
                quality = (min_quality + quality) // 2
            elif current_size < max_size:
                # 如果太小，可以尝试提高质量
                min_quality = quality
                quality = (max_quality + quality) // 2
            else:
                # 恰好等于目标大小，退出循环
                break

        # 最终保存并返回图片数据
        img_byte_arr.seek(0)
        img_byte_arr.truncate(0)
        img.save(img_byte_arr, format="JPEG", quality=quality)

        return img_byte_arr.getvalue()


def upload_img(token: str, snowFlakeId: str, userId: str, count: int,
               user_key: Optional[str] = None) -> str:
    """上传指定数量的处理后图片

    Args:
        token (str): 上传令牌。
        snowFlakeId (str): 组织ID。
        userId (str): 用户ID。
        count (int): 需要上传的图片数量。

    Returns:
        str: 上传成功的图片链接。
    """
    if count < 1:
        return ""

    # 获取图片文件夹路径
    # 使用abspath确保路径正确
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    images_dir = os.path.join(base_dir, "images")

    if not os.path.exists(images_dir):
        return ""

    # 获取所有符合条件的图片文件路径
    all_images = [
        os.path.join(images_dir, f) for f in os.listdir(images_dir)
        if f.lower().endswith((".png", ".jpg", ".jpeg"))
    ]

    # 如果图片数量不够，直接返回空 (或者上传所有可用的?)
    # 原逻辑是直接返回空，保持原样（记录告警，RISK-B05：不再完全静默）
    if len(all_images) < count:
        logger.warning(f"图库图片数量不足: 需要 {count} 张，实际 {len(all_images)} 张")
        return ""

    # 随机选择指定数量的图片
    selected_images = random.sample(all_images, count)

    # Stage 7: 上传前校验（存在/大小/损坏/尺寸），坏图跳过并记录
    valid_images, invalid_records = filter_valid_images(selected_images)
    upload_records = [{"file": r["file"], "ok": False, "key": "", "reason": r["reason"]}
                      for r in invalid_records]

    # 处理选中的图片并上传
    processed_images = []
    for img_path in valid_images:
        try:
            processed_images.append(process_image(img_path))
        except Exception as e:
            logger.warning(f"处理图片失败 {img_path}: {e}")
            upload_records.append({"file": os.path.basename(img_path),
                                   "ok": False, "key": "", "reason": f"压缩失败: {e}"})

    if not processed_images:
        record_upload_result(user_key or str(userId), upload_records)
        return ""

    result = upload(token, snowFlakeId, userId, processed_images)

    # Stage 7: 记录上传结果（成功 key / 失败原因）
    keys = [k for k in (result or "").split(",") if k]
    for idx, img_path in enumerate(valid_images[:len(processed_images)]):
        ok = idx < len(keys)
        upload_records.append({"file": os.path.basename(img_path), "ok": ok,
                               "key": keys[idx] if ok else "",
                               "reason": "" if ok else "上传失败（无返回 key）"})
    if len(keys) < len(processed_images):
        logger.warning(f"部分图片上传失败: 成功 {len(keys)}/{len(processed_images)}")
    record_upload_result(user_key or str(userId), upload_records)

    return result
