# -*- coding: utf-8 -*-
"""账户级 Session 管理器（多账户改造 Stage 4）。

职责：
- 每个账户一条 AccountSession（account_id / token / obtained_at /
  last_verified_at），进程内登记簿，线程安全
- ApiClient 显式绑定 context 后，重登/验证只更新**本账户**会话，
  绝不影响其他账户
- token 绝不入日志、绝不出现在 snapshot() 等对外视图

边界说明：
- token 的静态落点仍是各账户 config.userInfo.token（login() 写回，
  与历史行为一致）；本管理器是进程内的会话登记与追踪层，
  不改变登录方式、不新增登录次数
- 持久化到 context.session_dir 留待后续 Stage（当前重启即重建，
  首轮 ensure_login 会重新登记/验证）
"""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_MANAGER: Optional["SessionManager"] = None
_DEFAULT_MANAGER_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class AccountSession:
    """单个账户的会话登记（token 不序列化、不入日志）。"""

    account_id: str
    token: str = ""
    obtained_at: str = field(default_factory=_now)
    last_verified_at: str = ""

    def touch_verified(self) -> None:
        self.last_verified_at = _now()

    def refresh_token(self, token: str) -> None:
        self.token = token or ""
        self.obtained_at = _now()


class SessionManager:
    """账户会话登记簿：A 的重登/失效只影响 A。"""

    def __init__(self):
        self._sessions: Dict[str, AccountSession] = {}
        self._lock = threading.Lock()

    # ---------- 登记 / 查询 ----------

    def register(self, account_id: str, token: str) -> AccountSession:
        """登记或刷新某账户会话（重登后调用，仅影响该账户）。"""
        with self._lock:
            session = self._sessions.get(account_id)
            if session is None:
                session = AccountSession(account_id=account_id)
                self._sessions[account_id] = session
            session.refresh_token(token)
            return session

    def get(self, account_id: str) -> Optional[AccountSession]:
        with self._lock:
            return self._sessions.get(account_id)

    def token(self, account_id: str) -> Optional[str]:
        with self._lock:
            session = self._sessions.get(account_id)
            return session.token if session else None

    # ---------- 状态维护 ----------

    def mark_verified(self, account_id: str) -> bool:
        """会话预检通过后调用；未登记的账户返回 False（不隐式创建）。"""
        with self._lock:
            session = self._sessions.get(account_id)
            if session is None:
                return False
            session.touch_verified()
            return True

    def invalidate(self, account_id: str) -> bool:
        """仅使该账户会话失效（重登/登出/认证失败），不影响其他账户。"""
        with self._lock:
            return self._sessions.pop(account_id, None) is not None

    # ---------- 对外视图（绝不含 token） ----------

    def snapshot(self) -> List[Dict[str, str]]:
        with self._lock:
            return [
                {
                    "account_id": s.account_id,
                    "obtained_at": s.obtained_at,
                    "last_verified_at": s.last_verified_at,
                }
                for s in self._sessions.values()
            ]


def default_session_manager() -> SessionManager:
    """进程级默认登记簿（ApiClient 重登回调与 run() 共用）。"""
    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is None:
        with _DEFAULT_MANAGER_LOCK:
            if _DEFAULT_MANAGER is None:
                _DEFAULT_MANAGER = SessionManager()
    return _DEFAULT_MANAGER
