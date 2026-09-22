# -*- coding: utf-8 -*-
"""登录检查层（Stage 3）。

解决 RISK-C03（登录失败不分类）：把登录/会话校验的失败拆分为可归因的类别，
便于用户通过通知直接定位问题，也便于后续统计各类失败频率。

分类常量：
    LOGIN_OK                 登录/会话正常
    LOGIN_PASSWORD_ERROR     凭据错误（手机号/密码错误）
    LOGIN_CAPTCHA_ERROR      验证码获取或校验失败
    LOGIN_TIMEOUT            请求超时
    LOGIN_NETWORK_ERROR      网络不可达 / DNS / SSL 等连接层错误
    LOGIN_SERVER_ERROR       服务端 5xx 或业务码异常
    LOGIN_UNKNOWN_ERROR      其他未归类错误

不改变登录方式与协议；只做"检查 + 分类 + 明确的错误信息"。
"""

import logging
from typing import Tuple

import requests

logger = logging.getLogger(__name__)

LOGIN_OK = "LOGIN_OK"
LOGIN_PASSWORD_ERROR = "LOGIN_PASSWORD_ERROR"
LOGIN_CAPTCHA_ERROR = "LOGIN_CAPTCHA_ERROR"
LOGIN_TIMEOUT = "LOGIN_TIMEOUT"
LOGIN_NETWORK_ERROR = "LOGIN_NETWORK_ERROR"
LOGIN_SERVER_ERROR = "LOGIN_SERVER_ERROR"
LOGIN_UNKNOWN_ERROR = "LOGIN_UNKNOWN_ERROR"

# 各分类对应的用户可读说明
CATEGORY_MESSAGES = {
    LOGIN_PASSWORD_ERROR: "登录失败：手机号或密码错误，请检查配置",
    LOGIN_CAPTCHA_ERROR: "登录失败：验证码获取/识别失败（多为网络或接口变更，稍后重试）",
    LOGIN_TIMEOUT: "登录失败：请求超时（网络波动，稍后重试）",
    LOGIN_NETWORK_ERROR: "登录失败：网络不可达（检查本机网络/代理/DNS）",
    LOGIN_SERVER_ERROR: "登录失败：服务端异常（平台侧问题，稍后重试）",
    LOGIN_UNKNOWN_ERROR: "登录失败：未知错误",
}

# 平台返回的凭据类错误关键词（msg 文本匹配）
_PASSWORD_KEYWORDS = ("密码", "账号或密码", "用户名或密码", "password", "账号不存在", "用户不存在")
# 验证码类关键词
_CAPTCHA_KEYWORDS = ("验证码", "滑块", "captcha", "点选")


def classify_login_error(exc: Exception) -> str:
    """把登录过程中的异常归类为上述分类常量。"""
    # 网络层：先按异常类型，再看消息文本
    if isinstance(exc, requests.exceptions.Timeout):
        return LOGIN_TIMEOUT
    if isinstance(exc, (requests.exceptions.ConnectionError,
                        requests.exceptions.SSLError,
                        requests.exceptions.ChunkedEncodingError)):
        return LOGIN_NETWORK_ERROR

    msg = str(exc) or ""
    msg_lower = msg.lower()

    # requests 的 HTTP 状态类异常（有 HTTP 响应说明服务端可达）
    if isinstance(exc, requests.exceptions.HTTPError):
        code = getattr(exc.response, "status_code", None) if exc.response is not None else None
        if code and code == 429:
            return LOGIN_CAPTCHA_ERROR
        return LOGIN_SERVER_ERROR

    # 超时/网络关键词（_post_request 会把异常文本重新包装，需兜底文本判断）
    if "timeout" in msg_lower or "timed out" in msg_lower:
        return LOGIN_TIMEOUT
    if any(k in msg_lower for k in ("connection", "ssl", "dns", "max retries", "network", "代理", "网络")):
        return LOGIN_NETWORK_ERROR

    # 验证码关键词
    if any(k in msg for k in _CAPTCHA_KEYWORDS):
        return LOGIN_CAPTCHA_ERROR

    # 凭据类关键词（业务 msg 含中文）
    if any(k in msg or k in msg_lower for k in _PASSWORD_KEYWORDS):
        return LOGIN_PASSWORD_ERROR

    return LOGIN_UNKNOWN_ERROR


def user_message(category: str, detail: str = "") -> str:
    """分类 -> 用户可读消息（附原始错误详情）。"""
    base = CATEGORY_MESSAGES.get(category, CATEGORY_MESSAGES[LOGIN_UNKNOWN_ERROR])
    return f"{base} [{detail}]" if detail else base


def ensure_login(api_client) -> Tuple[bool, str, str]:
    """登录/会话检查入口（不改变登录方式）。

    流程：
      1. token 存在 → 用轻量接口 get_upload_token 做会话有效性预检
         （若失效，_post_request 内部会自动重登）；
      2. token 不存在 → 直接 login()。

    Returns:
        (ok, category, message)
        ok=True  时 category=LOGIN_OK，message 为空。
    """
    try:
        has_token = bool(api_client.config.get_value("userInfo.token"))
        if has_token:
            # 轻量会话预检：该接口只读、无副作用；token 失效时内部自动重登
            token = api_client.get_upload_token()
            if token:
                return True, LOGIN_OK, ""
        else:
            api_client.login()
            return True, LOGIN_OK, ""
    except Exception as e:  # noqa: BLE001 - 统一分类后上抛给调用方展示
        category = classify_login_error(e)
        message = user_message(category, str(e))
        logger.error(f"登录检查失败 [{category}]: {e}")
        return False, category, message

    # token 预检通过但返回为空等边界情况
    return False, LOGIN_SERVER_ERROR, user_message(LOGIN_SERVER_ERROR, "会话预检返回空")
