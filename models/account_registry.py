# -*- coding: utf-8 -*-
"""Account Registry 账户注册中心（多账户改造 Stage 1）。

存储：data/accounts/index.json（data/ 已 gitignore，不入库）

职责：
- 账户生命周期：list / get / add / update / enable / disable
- 兼容引导：registry 不存在时自动扫描 user/*.json（跳过 example*）建表，
  保证 `python main.py` 老用法零迁移直接可用
- 完整性：线程锁 + 跨进程 OS 文件锁 + 原子写（tmp + os.replace）
  + JSON 损坏保护（坏文件归档后重建）

安全红线：注册表不保存 password / token / apiKey / push token（Account
模型层同样过滤，这里是第二道防线）。
"""

import json
import logging
import os
import shutil
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from models.account import Account, generate_account_id

try:  # Windows
    import msvcrt

    def _lock_file(fh) -> None:
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock_file(fh) -> None:
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
except ImportError:  # POSIX
    import fcntl

    def _lock_file(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    def _unlock_file(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_PROJECT_ROOT, "data")
ACCOUNTS_DIR = os.path.join(DATA_DIR, "accounts")
REGISTRY_PATH = os.path.join(ACCOUNTS_DIR, "index.json")
USER_DIR = os.path.join(_PROJECT_ROOT, "user")

# 跳过不需要注册为账户的配置文件（样例/备份）
_SKIP_PREFIXES = ("example", "_", ".")

_registry_lock = threading.RLock()


# ---------- 底层文件操作（OS 文件锁 + 原子写） ----------

class _RegistryFileLock:
    """跨进程注册表文件锁（Windows msvcrt / POSIX flock），进程死亡自动释放。"""

    def __init__(self, path: str):
        self.path = path + ".lock"
        self._fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            _lock_file(self._fh)
        except OSError:
            pass  # 锁退化不影响单实例部署（全局单实例锁已兜底）
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._fh:
                try:
                    _unlock_file(self._fh)
                except OSError:
                    pass
                self._fh.close()
        finally:
            self._fh = None
        return False


def _load_raw(path: str) -> Optional[dict]:
    """读注册表；损坏时归档为 index.json.corrupt-<时间戳> 并返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("accounts"), list):
            return data
        logger.warning(f"注册表结构异常（按损坏处理）: {path}")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning(f"注册表读取失败（按损坏处理）: {e}")
    # 损坏 → 归档
    try:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.move(path, f"{path}.corrupt-{stamp}")
        logger.warning(f"损坏注册表已归档: {path}.corrupt-{stamp}")
    except OSError:
        pass
    return None


def _save_raw(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------- user/*.json 扫描引导 ----------

def scan_user_configs(user_dir: str = USER_DIR) -> List[str]:
    """扫描 user/*.json，返回可注册的配置文件名（跳过 example*/_* 等）。"""
    names = []
    try:
        if os.path.isdir(user_dir):
            for name in sorted(os.listdir(user_dir)):
                if not name.endswith(".json"):
                    continue
                if name.startswith(_SKIP_PREFIXES):
                    continue
                names.append(name)
    except OSError as e:
        logger.error(f"扫描用户目录失败: {e}")
    return names


def bootstrap_from_user_dir(user_dir: str = USER_DIR,
                            registry_path: str = REGISTRY_PATH) -> List[Account]:
    """registry 不存在时，扫描 user/*.json 自动建立注册表（老用法零迁移）。"""
    accounts: List[Account] = []
    for name in scan_user_configs(user_dir):
        accounts.append(Account(
            account_id=generate_account_id(),
            display_name=os.path.splitext(name)[0],
            config_file=name,
            enabled=True,
        ))
    if accounts:
        payload = {"version": 1, "accounts": [a.to_dict() for a in accounts]}
        _save_raw(registry_path, payload)
        logger.info(f"账户注册表已从 user/ 目录引导建立: {len(accounts)} 个账户")
    return accounts


# ---------- 对外 API ----------

def ensure_registry(user_dir: str = USER_DIR,
                    registry_path: str = REGISTRY_PATH) -> None:
    """确保注册表存在（不存在则从 user/ 引导）。"""
    with _registry_lock, _RegistryFileLock(registry_path):
        if _load_raw(registry_path) is None:
            bootstrap_from_user_dir(user_dir, registry_path)


def list_accounts(user_dir: str = USER_DIR,
                  registry_path: str = REGISTRY_PATH) -> List[Account]:
    ensure_registry(user_dir, registry_path)
    with _registry_lock:
        data = _load_raw(registry_path) or {"accounts": []}
    out = []
    for item in data.get("accounts", []):
        try:
            out.append(Account.from_dict(item))
        except ValueError as e:
            logger.warning(f"跳过非法账户条目: {e}")
    return out


def get_account(account_id: str,
                user_dir: str = USER_DIR,
                registry_path: str = REGISTRY_PATH) -> Optional[Account]:
    for acc in list_accounts(user_dir, registry_path):
        if acc.account_id == account_id:
            return acc
    return None


def get_account_by_config(config_file: str,
                          user_dir: str = USER_DIR,
                          registry_path: str = REGISTRY_PATH) -> Optional[Account]:
    """按配置文件名查账户（main.py 集成用）。"""
    if not config_file:
        return None
    target = os.path.basename(str(config_file))
    for acc in list_accounts(user_dir, registry_path):
        if acc.config_file == target:
            return acc
    return None


def add_account(account: Account,
                user_dir: str = USER_DIR,
                registry_path: str = REGISTRY_PATH) -> Account:
    """新增账户；account_id 唯一性检查 + config_file 唯一性检查。"""
    if not isinstance(account, Account):
        account = Account.from_dict(account)
    with _registry_lock, _RegistryFileLock(registry_path):
        data = _load_raw(registry_path) or {"version": 1, "accounts": []}
        existing = data.setdefault("accounts", [])
        for item in existing:
            if item.get("account_id") == account.account_id:
                raise ValueError(f"account_id 已存在: {account.account_id}")
            if item.get("config_file") == account.config_file:
                raise ValueError(f"config_file 已被账户 "
                                 f"{item.get('account_id')} 占用: {account.config_file}")
        account.touch()
        existing.append(account.to_dict())
        _save_raw(registry_path, data)
    logger.info(f"账户已注册: {account.account_id} ({account.display_name})")
    return account


def get_or_register_by_config(config_file: str,
                              display_name: str = "",
                              user_dir: str = USER_DIR,
                              registry_path: str = REGISTRY_PATH) -> Optional[Account]:
    """main.py 集成入口：按配置文件取账户；不存在且可注册时自动注册。

    example*/_* 等被扫描跳过的文件返回 None（保持老行为完全不变）。
    """
    target = os.path.basename(str(config_file or ""))
    if not target:
        return None
    if any(target.startswith(p) for p in _SKIP_PREFIXES):
        return None
    acc = get_account_by_config(target, user_dir, registry_path)
    if acc:
        return acc
    # 扫描范围之外的文件（如 USER 环境变量配置）不自动注册
    if target not in scan_user_configs(user_dir):
        return None
    return add_account(Account(
        account_id=generate_account_id(),
        display_name=display_name or os.path.splitext(target)[0],
        config_file=target,
        enabled=True,
    ), user_dir, registry_path)


def update_account(account_id: str,
                   user_dir: str = USER_DIR,
                   registry_path: str = REGISTRY_PATH,
                   **fields) -> Optional[Account]:
    """更新账户字段（display_name/enabled/schedule_profile/task_policy/
    notify_profile）；account_id 与 config_file 不可通过本接口修改。"""
    mutable = {"display_name", "enabled", "schedule_profile",
               "task_policy", "notify_profile"}
    bad = set(fields) - mutable
    if bad:
        raise ValueError(f"不可修改的字段: {', '.join(sorted(bad))}")
    with _registry_lock, _RegistryFileLock(registry_path):
        data = _load_raw(registry_path)
        if not data:
            raise ValueError("注册表不存在或为空")
        for item in data.get("accounts", []):
            if item.get("account_id") != account_id:
                continue
            account = Account.from_dict(item)
            for k, v in fields.items():
                setattr(account, k, v)
            account.touch()
            item.clear()
            item.update(account.to_dict())
            _save_raw(registry_path, data)
            return account
    return None


def enable_account(account_id: str,
                   user_dir: str = USER_DIR,
                   registry_path: str = REGISTRY_PATH) -> Optional[Account]:
    return update_account(account_id, user_dir, registry_path, enabled=True)


def disable_account(account_id: str,
                    user_dir: str = USER_DIR,
                    registry_path: str = REGISTRY_PATH) -> Optional[Account]:
    return update_account(account_id, user_dir, registry_path, enabled=False)


def set_task_policy(account_id: str,
                    policy: Optional[Dict[str, Any]],
                    user_dir: str = USER_DIR,
                    registry_path: str = REGISTRY_PATH) -> Optional[Account]:
    """设置账户任务开关（Stage 5：经 models.task_policy.normalize_policy
    清洗后落库；传 None/{} 表示清空策略、全部交回原配置旗标）。"""
    from models.task_policy import normalize_policy
    return update_account(account_id, user_dir, registry_path,
                          task_policy=normalize_policy(policy) or None)
