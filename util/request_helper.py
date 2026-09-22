# -*- coding: utf-8 -*-
"""统一网络请求包装（Stage 4）。

解决：
- RISK-C05：推送请求无 timeout，可无限阻塞工作线程；
- RISK-C04：重试分类靠"异常文本含中文"的脆弱启发式；
- RISK-B06 的缓解：只对确定未到达服务端的错误类做重试（连接/超时/5xx/429），
  4xx 与业务错误不重试，降低重复提交概率。

对外能力：
    is_retryable_exception(exc)  异常是否值得重试
    classify_exception(exc)      异常分类（与 auth_checker 分类口径一致）
    request_with_retry(...)      带超时/有限重试/指数退避的请求包装
    post_with_retry(...)         常用 POST 快捷方式
"""

import logging
import time
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# 默认策略：最多 3 次尝试（首次 + 2 次重试），连接 5s / 读取 10s
DEFAULT_MAX_RETRY = 3
DEFAULT_TIMEOUT: Tuple[float, float] = (5, 10)
DEFAULT_BACKOFF = 1.0  # 秒，指数递增

# 明确"请求未完成、重试安全"的异常：连接失败、超时、传输中断
_SAFE_RETRY_EXCEPTIONS = (
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ReadTimeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
)

# 异常分类（口径与 coreApi/auth_checker 保持一致）
EXC_TIMEOUT = "TIMEOUT"
EXC_NETWORK = "NETWORK_ERROR"
EXC_SERVER = "SERVER_ERROR"
EXC_RATE_LIMIT = "RATE_LIMITED"
EXC_CLIENT = "CLIENT_ERROR"
EXC_BUSINESS = "BUSINESS_ERROR"
EXC_UNKNOWN = "UNKNOWN_ERROR"


def _status_code(exc: Exception) -> Optional[int]:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) if response is not None else None


def is_retryable_exception(exc: Exception) -> bool:
    """判断异常是否应重试。原则：不确定服务端是否受理的，一律不重试。"""
    if isinstance(exc, _SAFE_RETRY_EXCEPTIONS):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        code = _status_code(exc)
        # 5xx / 429 可重试；4xx 客户端错误不重试
        return code is None or code >= 500 or code == 429
    return False


def classify_exception(exc: Exception) -> str:
    """把请求异常归类（用于日志与通知分级）。"""
    if isinstance(exc, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout)):
        return EXC_TIMEOUT
    if isinstance(exc, _SAFE_RETRY_EXCEPTIONS):
        return EXC_NETWORK
    if isinstance(exc, requests.exceptions.HTTPError):
        code = _status_code(exc)
        if code == 429:
            return EXC_RATE_LIMIT
        return EXC_SERVER if code is None or code >= 500 else EXC_CLIENT
    return EXC_UNKNOWN


def request_with_retry(
    method: str,
    url: str,
    *,
    session: Optional[requests.Session] = None,
    max_retry: int = DEFAULT_MAX_RETRY,
    timeout: Tuple[float, float] = DEFAULT_TIMEOUT,
    backoff: float = DEFAULT_BACKOFF,
    **kwargs,
) -> requests.Response:
    """带超时与有限重试的请求。

    - max_retry 为总尝试次数（含首次），重试之间指数退避；
    - 仅对 is_retryable_exception 的错误重试；
    - 超过次数后抛出最后一次异常（保留原类型，供上层分类）。
    """
    sender = session if session is not None else requests
    last_exc: Exception = RuntimeError("request_with_retry: 未发起请求")

    for attempt in range(1, max(1, max_retry) + 1):
        try:
            response = sender.request(method, url, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as e:
            last_exc = e
            category = classify_exception(e)
            if attempt >= max_retry or not is_retryable_exception(e):
                logger.error(
                    f"请求最终失败 [{category}] {method} {url}: {e} "
                    f"(尝试 {attempt}/{max_retry})")
                raise
            wait = backoff * (2 ** (attempt - 1))
            logger.warning(
                f"请求失败 [{category}] {method} {url}: {e}，"
                f"{wait:.1f}s 后重试 ({attempt}/{max_retry})")
            time.sleep(wait)

    raise last_exc


def post_with_retry(url: str, **kwargs) -> requests.Response:
    """POST 快捷方式（推送等无返回体敏感场景默认 2 次尝试）。"""
    kwargs.setdefault("max_retry", 2)
    return request_with_retry("POST", url, **kwargs)
