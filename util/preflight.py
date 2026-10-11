# -*- coding: utf-8 -*-
"""L5 — 启动前只读预检。

在真正执行任何提交前做一轮**轻量、只读**检查，任一关键条件不满足直接 STOP，
避免带着必败状态发起业务请求：

1. 配置完整性（离线，读内存）
2. 当前日期/时间合理性（离线）
3. 今日任务是否已全部成功（离线，读本地状态台账）
4. 会话状态（离线，仅检查 token 凭据存在；真实校验交给 ensure_login，不重复登录）
5. 网络是否可用（最多 1 次 TCP 连通探测，不发业务 HTTP 请求）
6. 并发同类任务（由 L1 单实例锁在上游保证；此处仅提示）

预检禁止事项（本模块严格遵守）：
- 不为预检增加大量 API 请求（全程 0 个业务接口调用）；
- 不重复登录、不获取验证码。

时间复杂度：0 次业务请求 + 至多 1 次 TCP connect。
"""

import logging
import socket
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 系统时间合理区间（本地执行器，超出即视为系统时钟异常，STOP）
MIN_REASONABLE_YEAR = 2020
MAX_REASONABLE_YEAR = 2040

DEFAULT_HOST = "api.moguding.net"
DEFAULT_PORT = 9000
NETWORK_PROBE_TIMEOUT = 3.0


@dataclass
class PreflightReport:
    """预检结果汇总。"""

    failures: List[str] = field(default_factory=list)      # 关键失败 → STOP（fail）
    benign_stops: List[str] = field(default_factory=list)  # 良性停止 → STOP（skip）
    warnings: List[str] = field(default_factory=list)      # 仅提示，不阻断
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def should_stop(self) -> bool:
        return bool(self.failures or self.benign_stops)

    @property
    def benign(self) -> bool:
        """良性停止（如任务已完成），以 skip 语义收敛。"""
        return not self.failures and bool(self.benign_stops)

    def summary(self) -> str:
        parts = self.failures + self.benign_stops
        return "；".join(parts) if parts else "预检通过"


def _check_config_completeness(config, report: PreflightReport) -> None:
    """检查 1：配置完整性（纯离线读取）。"""
    missing = []

    has_token = bool(config.get_value("userInfo.token"))
    has_credentials = bool(
        config.get_value("config.user.phone")
        and config.get_value("config.user.password")
    )
    if not has_token and not has_credentials:
        missing.append("登录凭据（userInfo.token 或 config.user.phone/password）")

    # 打卡开启时，定位信息必须完整
    if config.get_value("config.clockIn.enabled") is not False:
        for key in ("config.clockIn.location.latitude",
                    "config.clockIn.location.longitude",
                    "config.clockIn.location.address"):
            if not config.get_value(key):
                missing.append(key)

    # 报告类型开启时，AI 配置必须存在（否则生成必然失败）
    for rep in ("daily", "weekly", "monthly"):
        if config.get_value(f"config.reportSettings.{rep}.enabled"):
            if not config.get_value("config.ai.apikey"):
                missing.append(f"config.ai.apikey（{rep} 已开启）")
                break

    if missing:
        report.failures.append("配置缺失: " + ", ".join(missing))
    report.details["config_ok"] = not missing


def _check_time_sanity(report: PreflightReport) -> None:
    """检查 2：系统时间合理性（离线）。"""
    now = datetime.now()
    report.details["local_time"] = now.strftime("%Y-%m-%d %H:%M:%S")
    if not (MIN_REASONABLE_YEAR <= now.year <= MAX_REASONABLE_YEAR):
        report.failures.append(
            f"系统时间异常: {now.strftime('%Y-%m-%d')}，超出合理区间，"
            f"为避免错误日期提交已停止")


def _check_tasks_already_done(config, state_store, user_key,
                              report: PreflightReport) -> None:
    """检查 3：今日已启用的任务是否已全部成功（读本地状态台账）。"""
    if state_store is None or not user_key or user_key == "unknown":
        return

    enabled_tasks: List[str] = []
    if config.get_value("config.clockIn.enabled") is not False:
        enabled_tasks.append("checkin")
    for rep, task in (("daily", "daily_report"),
                      ("weekly", "weekly_report"),
                      ("monthly", "monthly_report")):
        if config.get_value(f"config.reportSettings.{rep}.enabled"):
            enabled_tasks.append(task)

    if not enabled_tasks:
        return

    done = [t for t in enabled_tasks if state_store.is_done(user_key, t)]
    report.details["tasks_done"] = f"{len(done)}/{len(enabled_tasks)}"
    if len(done) == len(enabled_tasks):
        report.benign_stops.append(
            "今日已启用任务均已成功（本地台账），无需重复执行")


def _check_session_credentials(config, report: PreflightReport) -> None:
    """检查 4：会话状态（仅离线检查凭据存在性，不做网络校验）。"""
    if config.get_value("userInfo.token"):
        report.details["session"] = "token-present"
    elif config.get_value("config.user.phone"):
        report.details["session"] = "will-login"
    else:
        report.warnings.append("无 token 且无手机号，登录阶段将失败")
        # 关键性已由配置完整性检查兜底，这里只提示


def _check_network(host: str, port: int, timeout: float,
                   report: PreflightReport) -> None:
    """检查 5：网络可用性（至多 1 次 TCP connect，不发业务请求）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            report.details["network"] = f"{host}:{port} reachable"
    except OSError as e:
        report.failures.append(
            f"网络不可达: {host}:{port} 连接失败（{e.__class__.__name__}），"
            f"为避免无意义重试已停止")
        report.details["network"] = f"unreachable: {e}"


def run_preflight(
    config,
    state_store=None,
    user_key: Optional[str] = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    network_probe: bool = True,
) -> PreflightReport:
    """执行启动前预检。任一关键条件不满足 → should_stop=True。"""
    report = PreflightReport()

    _check_config_completeness(config, report)
    _check_time_sanity(report)
    _check_tasks_already_done(config, state_store, user_key, report)
    _check_session_credentials(config, report)
    if network_probe and not report.failures:
        _check_network(host, port, NETWORK_PROBE_TIMEOUT, report)

    # 检查 6（并发同类任务）：由 L1 单实例锁在上游保证，此处仅记录说明
    report.details["concurrency"] = "enforced by L1 run lock upstream"

    for w in report.warnings:
        logger.warning(f"[PREFLIGHT] {w}")
    for f in report.failures:
        logger.error(f"[PREFLIGHT] {f}")
    for b in report.benign_stops:
        logger.info(f"[PREFLIGHT] {b}")
    if not report.should_stop:
        logger.info("[PREFLIGHT] 预检通过")
    return report
