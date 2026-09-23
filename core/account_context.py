# -*- coding: utf-8 -*-
"""AccountContext 账户执行上下文（多账户改造 Stage 2）。

目标：彻底消灭"靠全局变量判断当前账号"——任务执行的每一条数据路径都
显式携带 context：

    run(config, context) / context.account_id / context.state_dir ...

目录布局（Stage 3 运行时隔离）：
    data/accounts/{account_id}/
        state/      每日任务状态（TaskStateStore）
        history/    每日执行台账（execution_history）
        risk/       风险事件台账（risk_ledger）
        logs/       账户级结构化日志
        reports/    报告指纹与去重历史
        uploads/    图片上传结果记录
        session/    会话数据（Stage 4 预留）

兼容策略：from_config() 对 example*/_* 等样例文件与 USER 环境变量配置
返回 None（legacy 模式），旧入口行为完全不变。
"""

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from models import account_registry

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_DATA_DIR = os.path.join(_PROJECT_ROOT, "data")

ACCOUNT_SUBDIRS = ("state", "history", "risk", "logs", "reports",
                   "uploads", "session")


@dataclass
class AccountContext:
    """单个账户的执行上下文（数据隔离的根）。"""

    account_id: str
    display_name: str
    config: Any                      # ConfigManager（不序列化）
    data_root: str = _DEFAULT_DATA_DIR
    user_key: str = ""               # 新数据一律用 account_id

    def __post_init__(self):
        if not self.user_key:
            self.user_key = self.account_id

    # ---------- 目录 ----------

    @property
    def runtime_dir(self) -> str:
        return os.path.join(self.data_root, "accounts", self.account_id)

    @property
    def state_dir(self) -> str:
        return os.path.join(self.runtime_dir, "state")

    @property
    def history_dir(self) -> str:
        return os.path.join(self.runtime_dir, "history")

    @property
    def risk_dir(self) -> str:
        return os.path.join(self.runtime_dir, "risk")

    @property
    def log_dir(self) -> str:
        return os.path.join(self.runtime_dir, "logs")

    @property
    def report_dir(self) -> str:
        return os.path.join(self.runtime_dir, "reports")

    @property
    def uploads_dir(self) -> str:
        return os.path.join(self.runtime_dir, "uploads")

    @property
    def session_dir(self) -> str:
        return os.path.join(self.runtime_dir, "session")

    def ensure_dirs(self) -> None:
        for name in ACCOUNT_SUBDIRS:
            try:
                os.makedirs(os.path.join(self.runtime_dir, name), exist_ok=True)
            except OSError as e:
                logger.error(f"账户运行目录创建失败: {e}")

    # ---------- 构建 ----------

    @classmethod
    def from_account(cls, account, config: Any,
                     data_dir: Optional[str] = None) -> "AccountContext":
        """由 Account + ConfigManager 构建上下文。"""
        return cls(
            account_id=account.account_id,
            display_name=account.display_name,
            config=config,
            data_root=data_dir or _DEFAULT_DATA_DIR,
        )

    @classmethod
    def from_config(cls, config: Any,
                    user_dir: Optional[str] = None,
                    registry_path: Optional[str] = None,
                    data_dir: Optional[str] = None) -> Optional["AccountContext"]:
        """由 ConfigManager 构建上下文。

        - 按配置文件名在注册表查找；不存在则自动注册（get_or_register）
        - 样例文件 / 环境变量配置 → 返回 None（legacy 模式，行为不变）
        """
        path = getattr(config, "_path", None)
        if path is None:
            return None
        config_file = os.path.basename(str(path))
        account = account_registry.get_or_register_by_config(
            config_file,
            display_name=os.path.splitext(config_file)[0],
            user_dir=user_dir or account_registry.USER_DIR,
            registry_path=registry_path or account_registry.REGISTRY_PATH,
        )
        if account is None:
            return None
        ctx = cls.from_account(account, config, data_dir)
        ctx.ensure_dirs()
        return ctx
