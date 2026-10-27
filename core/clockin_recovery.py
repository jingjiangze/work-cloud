"""304（支付宝安全验证）自动闭环 + 超时补卡。

背景：平台对非官方客户端返回 `code=200, msg=304`，表示「需本人支付宝安全验证」，
此时打卡**并未提交**（旧代码把它当成成功，形成假绿）。

闭环流程（每一轮）：
  1. 提交 → 命中 304
  2. 调 `createAxdjk` 申请登记号（outRegisterNo）与支付宝深链（30 分钟有效）
  3. 把深链推送给人（点开即跳支付宝授权），同时落盘到 config.clockIn.outRegisterNo
  4. 每 30 秒带该登记号重试一次，最多 6 分钟

**补卡（2026-10-27 新增）**：一轮轮询耗尽后，自动**再申请一次**登记号并重新推送
授权链接，再轮询一轮；最多 `max_rounds` 轮。这样「错过 30 分钟窗口」不需要人工去点
支付宝页面上的补卡按钮——链路会自己再发一条新的授权链接。

推送收敛：一旦推送过授权链接，就把 `config._action_pushed` 置位，
主流程收尾时不再重复推一条结果——**一次打卡只收一条消息**。
"""

import logging
import time
from urllib.parse import quote

from coreApi.MainLogicApi import RiskVerificationRequired

logger = logging.getLogger(__name__)

POLL_ATTEMPTS = 12
POLL_INTERVAL = 30
REGISTER_TTL_SEC = 30 * 60      # 登记号有效期 30 分钟
MAX_ROUNDS = 2                  # 第 1 轮 + 1 次补卡


def build_click_link(register_url: str) -> str:
    """把 alipays:// 深链转成手机可直接点开的链接。"""
    return "https://ds.alipay.com/?scheme=" + quote(register_url, safe="")


def _push_link(config, link: str, register_no: str, kind: str, round_no: int) -> bool:
    """把授权链接推送给人；失败只记日志，不阻断流程。"""
    try:
        from util.MessagePush import MessagePusher

        pusher = MessagePusher(config.get_value("config.pushNotifications") or [])
        title_suffix = "（补卡，第 %d 次）" % round_no if round_no > 1 else ""
        html = (
            "<p><b>%s打卡被风控拦截</b>（304，需支付宝安全验证）%s。</p>"
            "<p><a href=\"%s\">👉 点这里完成支付宝授权</a></p>"
            "<p>登记号：<code>%s</code>（30 分钟内有效）</p>"
            "<p>授权后脚本会自动补打，无需再操作；若仍未成功，下一轮会自动补卡并再发链接。</p>"
            % (kind or "本次", title_suffix, link, register_no)
        )
        pusher.push_custom("[ACTION] 工学云打卡需要支付宝验证", html, html)
        # 收敛：本次运行已推送可操作消息，收尾不再重复推结果
        try:
            config._action_pushed = True
        except Exception:
            pass
        return True
    except Exception as exc:  # noqa: BLE001 - 推送失败不得影响打卡主流程
        logger.warning(f"推送支付宝验证链接失败（忽略）: {exc}")
        return False


def _poll(api_client, checkin_info, register_no, attempts, interval):
    """带登记号轮询提交；成功返回 (rsp, attempt)，耗尽返回 (None, 0)。"""
    for attempt in range(1, attempts + 1):
        time.sleep(interval)
        try:
            rsp = api_client.submit_clock_in(
                dict(checkin_info, outRegisterNo=register_no))
            logger.info(f"304 已解除（第 {attempt} 次轮询成功）")
            return rsp, attempt
        except RiskVerificationRequired:
            logger.warning(f"第 {attempt}/{attempts} 次轮询仍为 304，继续等待授权")
        except Exception as exc:  # noqa: BLE001 - 网络类异常交由上层判定
            logger.warning(f"第 {attempt} 次轮询异常: {exc}")
    return None, 0


def _fresh_register(config):
    """取出 30 分钟内仍有效的登记号（跨运行复用）。

    实测（2026-10-27）：每次 304 都新申请登记号，用户手动授权的是**上一个**，
    于是「授权了却仍 304」。同一登记号 30 分钟内有效，所以复用它，
    让「用户点链接 → 下一次运行接着轮询」这条跨运行闭环成立。
    """
    try:
        no = str(config.get_value("config.clockIn.outRegisterNo") or "").strip()
        ts = int(config.get_value("config.clockIn.outRegisterNoIssuedAt") or 0)
    except Exception:
        return None, 0
    if no and ts and (time.time() - ts) < REGISTER_TTL_SEC:
        return no, ts
    return None, 0


def submit_with_recovery(
    api_client,
    checkin_info: dict,
    config,
    poll_attempts: int = POLL_ATTEMPTS,
    poll_interval: int = POLL_INTERVAL,
    max_rounds: int = MAX_ROUNDS,
):
    """提交打卡；命中 304 时走「登记 → 推送 → 轮询」闭环，超时自动补卡。

    Returns:
        (response, recovered_after): 成功时返回服务端响应；
        recovered_after 为 None 表示一次通过，为整数表示第几次轮询才通过。
    Raises:
        RiskVerificationRequired: 所有轮次（含补卡）都未解除。
    """
    try:
        return api_client.submit_clock_in(checkin_info), None
    except RiskVerificationRequired as first_hit:
        kind = str(checkin_info.get("type") or "")
        logger.warning(f"{kind}打卡触发 304（需支付宝验证），开始自动闭环")

        last_err = first_hit
        reuse_no, reuse_ts = _fresh_register(config)
        for round_no in range(1, max_rounds + 1):
            if round_no == 1 and reuse_no:
                register_no, issued_at = reuse_no, reuse_ts
                logger.info(f"复用仍有效的登记号 {register_no}（有效期剩余 "
                            f"{int((REGISTER_TTL_SEC - (time.time() - issued_at)) / 60)} 分钟），不再重新申请")
            else:
                issued_at = time.time()
                try:
                    reg = api_client.create_alipay_register()
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"第 {round_no} 轮申请登记号失败: {exc}")
                    break
                register_no = reg["outRegisterNo"]
                link = build_click_link(reg["registerUrl"])
                config.update_config(register_no, "config", "clockIn", "outRegisterNo")
                config.update_config(int(issued_at), "config", "clockIn",
                                     "outRegisterNoIssuedAt")
                logger.info(f"第 {round_no} 轮已申请登记号 {register_no}")
                pushed = _push_link(config, link, register_no, kind, round_no)
                logger.info("授权链接已推送" if pushed else "授权链接推送失败，仅记录日志")

            rsp, attempt = _poll(api_client, checkin_info, register_no,
                                 poll_attempts, poll_interval)
            if rsp is not None:
                return rsp, attempt

            elapsed = time.time() - issued_at
            expired = elapsed >= REGISTER_TTL_SEC
            logger.warning(
                f"第 {round_no} 轮轮询耗尽（已等 {elapsed / 60:.0f} 分钟，"
                f"登记号{'已过期' if expired else '仍在有效期'}）")
            if round_no < max_rounds:
                logger.info("自动补卡：重新申请登记号并再次推送授权链接")
                continue

        logger.error("所有轮次（含补卡）仍未解除 304，打卡未提交（需本人完成支付宝授权）")
        raise last_err
