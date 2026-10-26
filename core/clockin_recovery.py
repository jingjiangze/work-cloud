"""304（支付宝安全验证）自动闭环。

背景：平台对非官方客户端返回 `code=200, msg=304`，表示「需本人支付宝安全验证」，
此时打卡**并未提交**（旧代码把它当成成功，形成假绿）。

闭环流程：
  1. 提交 → 命中 304
  2. 调 `createAxdjk` 申请登记号（outRegisterNo）与支付宝深链（30 分钟有效）
  3. 把深链推送给人（点开即跳支付宝授权），同时落盘到 config.clockIn.outRegisterNo
  4. 每 30 秒带该登记号重试一次，最多 6 分钟
  5. 本人授权后的下一次轮询即成功；始终失败则抛出原始异常（不伪造成功）
"""

import logging
import time
from urllib.parse import quote

from coreApi.MainLogicApi import RiskVerificationRequired

logger = logging.getLogger(__name__)

POLL_ATTEMPTS = 12
POLL_INTERVAL = 30


def build_click_link(register_url: str) -> str:
    """把 alipays:// 深链转成手机可直接点开的链接。"""
    return "https://ds.alipay.com/?scheme=" + quote(register_url, safe="")


def _push_link(config, link: str, register_no: str, kind: str) -> bool:
    """把授权链接推送给人；失败只记日志，不阻断流程。"""
    try:
        from util.MessagePush import MessagePusher

        pusher = MessagePusher(config.get_value("config.pushNotifications") or [])
        html = (
            "<p><b>%s打卡被风控拦截</b>（304，需支付宝安全验证）。</p>"
            "<p><a href=\"%s\">👉 点这里完成支付宝授权</a></p>"
            "<p>登记号：<code>%s</code>（30 分钟内有效）</p>"
            "<p>授权后脚本会自动补打，无需再操作。</p>"
            % (kind or "本次", link, register_no)
        )
        pusher.push_custom("[ACTION] 工学云打卡需要支付宝验证", html, html)
        return True
    except Exception as exc:  # noqa: BLE001 - 推送失败不得影响打卡主流程
        logger.warning(f"推送支付宝验证链接失败（忽略）: {exc}")
        return False


def submit_with_recovery(
    api_client,
    checkin_info: dict,
    config,
    poll_attempts: int = POLL_ATTEMPTS,
    poll_interval: int = POLL_INTERVAL,
):
    """提交打卡；命中 304 时走「登记 → 推送 → 轮询重试」闭环。

    Returns:
        (response, recovered_after): 成功时返回服务端响应；
        recovered_after 为 None 表示一次通过，为整数表示第几次轮询才通过。
    Raises:
        RiskVerificationRequired: 轮询耗尽仍未解除。
    """
    try:
        return api_client.submit_clock_in(checkin_info), None
    except RiskVerificationRequired as first_hit:
        kind = str(checkin_info.get("type") or "")
        logger.warning(f"{kind}打卡触发 304（需支付宝验证），开始自动闭环")

        reg = api_client.create_alipay_register()
        register_no = reg["outRegisterNo"]
        link = build_click_link(reg["registerUrl"])
        config.update_config(register_no, "config", "clockIn", "outRegisterNo")
        logger.info(f"已申请支付宝登记号 {register_no}")

        pushed = _push_link(config, link, register_no, kind)
        logger.info("授权链接已推送" if pushed else "授权链接推送失败，仅记录日志")

        for attempt in range(1, poll_attempts + 1):
            time.sleep(poll_interval)
            try:
                rsp = api_client.submit_clock_in(
                    dict(checkin_info, outRegisterNo=register_no))
                logger.info(f"304 已解除（第 {attempt} 次轮询成功）")
                return rsp, attempt
            except RiskVerificationRequired:
                logger.warning(
                    f"第 {attempt}/{poll_attempts} 次轮询仍为 304，继续等待授权")
            except Exception as exc:  # noqa: BLE001 - 网络类异常交由上层判定
                logger.warning(f"第 {attempt} 次轮询异常: {exc}")

        logger.error("轮询耗尽仍未解除 304，打卡未提交（需本人完成支付宝授权）")
        raise first_hit
