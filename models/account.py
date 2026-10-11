# -*- coding: utf-8 -*-
"""Account 账户模型（多账户改造 Stage 1）。

Account 是系统的稳定主键载体：
- account_id（acct_xxxxxxxx）一旦分配不再改变，不依赖文件名/手机号；
- Account 本身不保存任何明文密码 / token / apiKey / push 配置，
  这些仍只存在于 user/*.json（配置文件）中。

schedule_profile / task_policy / notify_profile 为 Stage 5/6/14 预留字段，
本阶段仅做透传存储，不参与执行决策。
"""

import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


ACCOUNT_ID_PATTERN = re.compile(r"^acct_[A-Za-z0-9]{6,16}$")

# 注册表禁止保存的字段（防敏感信息入库，双层防御：写入前过滤）
_FORBIDDEN_FIELDS = {
    "password", "passwd", "pwd", "token", "authorization", "apikey",
    "api_key", "secret", "secretkey", "cookie", "push_token", "phone",
    "mobile", "account", "username",
}


def generate_account_id() -> str:
    """生成新 account_id：acct_ + 8 位十六进制随机串。"""
    return f"acct_{secrets.token_hex(4)}"


def is_valid_account_id(account_id: str) -> bool:
    return bool(ACCOUNT_ID_PATTERN.match(str(account_id or "")))


def _sanitize_profile(value: Any) -> Optional[Dict[str, Any]]:
    """profile 字段清洗：只接受 dict，且剔除敏感键（递归一层）。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("profile 字段必须是对象（dict）或 null")

    def clean(d: Dict[str, Any]) -> Dict[str, Any]:
        out = {}
        for k, v in d.items():
            if str(k).lower() in _FORBIDDEN_FIELDS:
                continue
            out[k] = clean(v) if isinstance(v, dict) else v
        return out

    return clean(value)


@dataclass
class Account:
    """账户注册表条目（不含任何凭据）。"""

    account_id: str
    display_name: str
    config_file: str          # 相对 user/ 目录的文件名，如 "me.json"
    enabled: bool = True
    schedule_profile: Optional[Dict[str, Any]] = None
    task_policy: Optional[Dict[str, Any]] = None
    notify_profile: Optional[Dict[str, Any]] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def __post_init__(self):
        # 任意入口（构造/from_dict）都做一次 profile 清洗，敏感键永不落盘
        self.schedule_profile = _sanitize_profile(self.schedule_profile)
        self.task_policy = _sanitize_profile(self.task_policy)
        self.notify_profile = _sanitize_profile(self.notify_profile)
        self.validate()

    # ---------- 校验 ----------

    def validate(self) -> None:
        if not is_valid_account_id(self.account_id):
            raise ValueError(f"非法 account_id: {self.account_id!r}（应为 acct_xxxxxxxx）")
        if not self.display_name or not str(self.display_name).strip():
            raise ValueError("display_name 不能为空")
        if not self.config_file or "/" in self.config_file or "\\" in self.config_file \
                or self.config_file.startswith("."):
            raise ValueError(f"非法 config_file: {self.config_file!r}（应为 user/ 下文件名）")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled 必须是布尔值")

    # ---------- 序列化 ----------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "display_name": self.display_name,
            "config_file": self.config_file,
            "enabled": self.enabled,
            "schedule_profile": self.schedule_profile,
            "task_policy": self.task_policy,
            "notify_profile": self.notify_profile,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Account":
        if not isinstance(data, dict):
            raise ValueError("Account 数据必须是对象")
        return cls(
            account_id=str(data.get("account_id", "")),
            display_name=str(data.get("display_name", "")),
            config_file=str(data.get("config_file", "")),
            enabled=bool(data.get("enabled", True)),
            schedule_profile=_sanitize_profile(data.get("schedule_profile")),
            task_policy=_sanitize_profile(data.get("task_policy")),
            notify_profile=_sanitize_profile(data.get("notify_profile")),
            created_at=str(data.get("created_at", "")) or
            datetime.now().isoformat(timespec="seconds"),
            updated_at=str(data.get("updated_at", "")) or
            datetime.now().isoformat(timespec="seconds"),
        )

    def touch(self) -> None:
        self.updated_at = datetime.now().isoformat(timespec="seconds")
