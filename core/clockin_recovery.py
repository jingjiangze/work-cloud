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

import hashlib
import hmac
import logging
import os
import time
from urllib.parse import quote

from coreApi.MainLogicApi import RiskVerificationRequired

logger = logging.getLogger(__name__)


class TerminalPunchError(Exception):
    """自动化无法继续的终态错误（如人脸核验），不再重试。"""

POLL_ATTEMPTS = 0    # **不轮询**：实测每次提交都触发一次服务端人脸比对，
                     # 连续 4 次失败平台就把验证作废 ⇒ 只提交一次，等本人扫完由 retry 任务补提交
POLL_INTERVAL = 180
REGISTER_TTL_SEC = 30 * 60      # 登记号有效期 30 分钟
MAX_ROUNDS = 2
MAKEUP_MAX_PER_MONTH = 2   # 补卡每月上限（平台侧限制，保守取 2）                  # 两轮：第 1 轮跑满 30 分钟；登记号过期后才开第 2 轮（补卡），绝不提前发第二条链接


def build_click_link(register_url: str) -> str:
    """把 alipays:// 深链转成手机可直接点开的链接。"""
    return "https://ds.alipay.com/?scheme=" + quote(register_url, safe="")


def _trigger_buttons(kind):
    """一键重试按钮：token = HMAC(secret, 日期|类型|用途)，当天有效、跨天自动失效。"""
    secret = os.environ.get("GX_TRIGGER_SECRET") or ""
    base = (os.environ.get("GX_TRIGGER_BASE") or "").rstrip("/")
    if not secret or not base:
        return ""
    day = time.strftime("%Y-%m-%d")
    t = kind.upper()

    def _tok(purpose):
        msg = ("%s|%s|%s" % (day, t, purpose)).encode()
        return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:24]

    return ('<p><a href="%s/gx/retry?type=%s&t=%s">✅ 我已扫完，立即补打</a></p>'
            '<p><a href="%s/gx/new?type=%s&t=%s">🔄 链接失效？重新生成</a></p>'
            % (base, t, _tok("retry"), base, t, _tok("new")))


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
        ) + _trigger_buttons(kind)
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
    """带登记号轮询提交；返回 (rsp, attempt, face_seen, expired)。"""
    face_seen = False
    expired = False
    for attempt in range(1, attempts + 1):
        time.sleep(interval)
        try:
            rsp = api_client.submit_clock_in(
                dict(checkin_info, outRegisterNo=register_no))
            logger.info(f"304 已解除（第 {attempt} 次轮询成功）")
            return rsp, attempt, face_seen, False
        except RiskVerificationRequired:
            logger.warning(f"第 {attempt}/{attempts} 次轮询仍为 304，继续等待授权")
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            # 2026-10-27 更正：**「人脸核验失败」不是终态**，而是「本人还没扫码」的中间态。
            # 昨天（10-26）能打上卡的那次就是一直轮询、等人扫完脸后才成功的。
            # 之前把它当终态立刻中止（33 秒就杀），反而把唯一可行的路径掐死了。
            if "人脸核验" in msg:
                face_seen = True
                # 「还没扫」的中间态：继续等
                if attempt in (1, 5, 10, 20, 30):
                    logger.info(f"第 {attempt} 次轮询：等待本人完成人脸核验…")
            elif "验证已过期" in msg:
                # 2026-10-27 实测：本人扫码后返回的就是这个 —— 说明扫码**消费/作废**了登记号，
                # 而不是授权它。继续拿它提交只会再撞 46 次同样的错，还白白加重风控。
                logger.warning("验证已失效（多次提交触发人脸比对失败后平台作废），停止轮询")
                expired = True
                break
            else:
                logger.warning(f"第 {attempt} 次轮询异常: {exc}")
    return None, attempts, face_seen, expired


def _fresh_register(config):
    """取出仍在 30 分钟有效期内的登记号（跨运行复用）。

    ⭐这是**避免链接打架**的关键：每次 createAxdjk 都会让旧链接失效。
    实测 2026-10-27：78 分钟里创建了 6 个登记号，用户扫的链接被后续覆盖，
    扫完立刻变「验证已过期」——今天所有失败都是这么来的。
    """
    try:
        no = str(config.get_value("config.clockIn.outRegisterNo") or "").strip()
        ts = int(config.get_value("config.clockIn.outRegisterNoIssuedAt") or 0)
    except Exception:
        return None, 0
    if no and ts and (time.time() - ts) < REGISTER_TTL_SEC:
        return no, ts
    return None, 0


def _mark_register(config, register_no, issued_at):
    config.update_config(register_no, "config", "clockIn", "outRegisterNo")
    config.update_config(int(issued_at), "config", "clockIn", "outRegisterNoIssuedAt")


def _try_makeup(api_client, checkin_info, config):
    """按月度配额尝试补卡（REPLACE 端点）。成功返回响应，否则抛异常。"""
    ym = time.strftime("%Y-%m")
    used = int(config.get_value("config.clockIn.makeupCount") or 0)
    mon = str(config.get_value("config.clockIn.makeupMonth") or "")
    if mon != ym:
        used, mon = 0, ym
    if used >= MAKEUP_MAX_PER_MONTH:
        logger.warning(f"本月补卡额度已用完（{used}/{MAKEUP_MAX_PER_MONTH}），跳过补卡")
        raise RiskVerificationRequired("打卡失败：平台要求人脸核验，且本月补卡额度已用完")

    logger.info(f"尝试补卡（本月第 {used + 1}/{MAKEUP_MAX_PER_MONTH} 次，REPLACE 端点）")
    resp = api_client.submit_clock_in_replace(checkin_info)
    logger.info(f"补卡结果: code={resp.get('code')} msg={resp.get('msg')}")
    if str(resp.get("code")) == "200":
        # 只有成功才消耗月度额度
        config.update_config(used + 1, "config", "clockIn", "makeupCount")
        config.update_config(ym, "config", "clockIn", "makeupMonth")
        logger.info(f"补卡成功（本月已用 {used + 1}/{MAKEUP_MAX_PER_MONTH}）")
        return resp
    raise RiskVerificationRequired(
        "补卡被平台拒绝: %s（若提示人脸核验/验证过期，说明尚未在新链接上完成扫脸）"
        % str(resp.get("msg"))[:100])


def _push_face_notice(config):
    """人脸核验提醒：每天只推一次。"""
    try:
        from util.MessagePush import MessagePusher

        today = time.strftime("%Y-%m-%d")
        if str(config.get_value("config.clockIn.faceNoticeDate") or "") == today:
            return
        pusher = MessagePusher(config.get_value("config.pushNotifications") or [])
        pusher.push_custom(
            "工学云打卡需人工处理（人脸核验）",
            "<p>平台要求<strong>人脸核验</strong>，自动化无法完成。</p>"
            "<p>请在官方工学云 APP 手动打卡；自动补卡会按月度配额尝试。</p>")
        config.update_config(today, "config", "clockIn", "faceNoticeDate")
        logger.info("人脸核验提醒已推送（当日仅此一次）")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"推送人脸核验提醒失败（忽略）: {exc}")


def submit_with_recovery(
    api_client,
    checkin_info: dict,
    config,
    poll_attempts: int = POLL_ATTEMPTS,
    poll_interval: int = POLL_INTERVAL,
    max_rounds: int = MAX_ROUNDS,
):
    """提交打卡；需要支付宝/人脸验证时走「一个登记号 → 一条链接 → 等本人扫」的闭环。

    关键约定（2026-10-27 实测定稿）：
      * **同一时刻只允许存在一个有效登记号**：优先复用 config 里未过期的那个，
        绝不为了"重试"再申请新的——新登记号会让用户手上那条链接失效。
      * `CHECKIN_RETRY_ONLY=1`：只做一次提交（用于"本人已扫脸"后的补提交），
        **不申请登记号、不推送链接**。
      * 轮询里「人脸核验失败」= 还没扫，继续等；「验证已过期」= 登记号作废，本轮停。
    """
    retry_only = bool(os.environ.get("CHECKIN_RETRY_ONLY"))
    # mode=new：忽略已存登记号、忽略「当日需人脸核验」短路，强制申请新登记号并推链接
    force_new = bool(os.environ.get("CHECKIN_FORCE_NEW"))
    kind = str(checkin_info.get("type") or "")

    # ---- 1) 先试一次：复用现有有效登记号（没有就不带）
    reuse_no, reuse_ts = (None, 0) if force_new else _fresh_register(config)
    if force_new:
        logger.info("CHECKIN_FORCE_NEW=1：忽略已存登记号，强制申请新的并推送链接")
    probe = dict(checkin_info)
    if reuse_no:
        probe["outRegisterNo"] = reuse_no
        logger.info(f"复用现有登记号 {reuse_no}（剩余 "
                    f"{int((REGISTER_TTL_SEC - (time.time() - reuse_ts)) / 60)} 分钟），不新申请")
    try:
        return api_client.submit_clock_in(probe), None
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if not (("验证已过期" in msg) or ("304" in msg) or ("风险操作" in msg)
                or ("人脸核验" in msg)):
            raise
        logger.warning(f"{kind}打卡需要验证：{msg[:70]}")
        if retry_only:
            logger.info("CHECKIN_RETRY_ONLY=1：本次只做单次提交，不申请登记号、不推送链接")
            if "人脸核验" in msg:
                _push_face_notice(config)   # 记下当日需人脸核验，后续运行短路
            raise

    if (not force_new) and str(config.get_value("config.clockIn.faceNoticeDate") or "") == time.strftime("%Y-%m-%d"):
        # 当日已确认需人脸核验。补卡必须有「可扫的新链接」才有意义：
        # 若当前没有有效登记号（或已过期），先申请并推送一条，再走补卡。
        if not reuse_no:
            logger.info("当日无有效登记号 ⇒ 先申请新登记号并推送（补卡前必须可扫）")
            try:
                reg = api_client.create_alipay_register()
            except Exception as exc:  # noqa: BLE001
                logger.error(f"申请登记号失败: {exc}")
                raise
            reuse_no, reuse_ts = reg["outRegisterNo"], time.time()
            _mark_register(config, reuse_no, reuse_ts)
            _push_link(config, build_click_link(reg["registerUrl"]), reuse_no, kind, 1)
        logger.info("今日已确认需人脸核验 ⇒ 跳过轮询，直接按配额补卡")
        try:
            resp = _try_makeup(api_client, dict(checkin_info, outRegisterNo=reuse_no), config)
            return resp, 0
        except RiskVerificationRequired as exc:
            if "人脸核验" in str(exc):
                raise RiskVerificationRequired(
                    "补卡需要先在新链接上完成人脸核验（本次未扫）: %s" % str(exc)[:80])
            raise

    # ---- 2) 需要验证：只有在"没有可复用的登记号"或"现有登记号已作废"时才新建
    register_no, issued_at = reuse_no, reuse_ts
    err_msg = str(locals().get("msg", ""))
    # 复用的登记号被判过期 ⇒ 必须新建；否则沿用它继续等本人扫脸（避免链接打架）
    need_new = (not reuse_no) or ("验证已过期" in err_msg)
    if need_new:
        try:
            reg = api_client.create_alipay_register()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"申请登记号失败: {exc}")
            raise
        register_no, issued_at = reg["outRegisterNo"], time.time()
        _mark_register(config, register_no, issued_at)
        link = build_click_link(reg["registerUrl"])
        logger.info(f"已申请登记号 {register_no}")
        pushed = _push_link(config, link, register_no, kind, 1)
        logger.info("授权链接已推送" if pushed else "授权链接推送失败，仅记录日志")

    # ---- 3) 等本人完成验证（人脸核验失败=还没扫）
    rsp, attempt, face_seen, expired = _poll(api_client, checkin_info, register_no,
                                             poll_attempts, poll_interval)
    if rsp is not None:
        return rsp, attempt
    if not face_seen:
        raise RiskVerificationRequired(
            "已推送授权链接；本轮按设计不轮询（避免打死验证），"
            "等本人扫码后由 08:35/08:43 的 retry 任务补提交")
    _push_face_notice(config)
    if expired:
        logger.warning("验证已被平台作废 ⇒ 本次不尝试补卡（省额度），等下次重新申请登记号")
        raise RiskVerificationRequired("打卡失败：人脸核验导致验证作废（本次未消耗补卡额度）")
    resp = _try_makeup(api_client, dict(checkin_info, outRegisterNo=register_no), config)
    return resp, attempt
