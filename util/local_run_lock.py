# -*- coding: utf-8 -*-
"""单实例运行锁（L1）。

解决：Windows 计划任务 + 手动启动叠加导致的多进程并发运行
（双重登录 / 双重提交 / 多 Session / 重复通知）。

实现：
- Windows: msvcrt.locking 对锁文件字节区间加内核级排他锁；
- POSIX:   fcntl.flock；
- 锁随进程退出（含异常退出/被杀）由操作系统自动释放 —— **不会死锁**，
  无需 PID 心跳或陈旧锁清理。

用法：
    lock = LocalRunLock(path)
    if not lock.acquire():
        logger.error("已有实例在运行")
        return
    try:
        ...
    finally:
        lock.release()
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

try:  # Windows
    import msvcrt  # noqa: F401
    _MSVCRT = msvcrt
except ImportError:  # POSIX
    _MSVCRT = None

try:
    import fcntl  # noqa: F401
    _FCNTL = fcntl
except ImportError:
    _FCNTL = None


class LocalRunLock:
    """基于锁文件字节区间的进程级排他锁。"""

    def __init__(self, path: str):
        self.path = path
        self._fh = None
        self.acquired = False

    def acquire(self) -> bool:
        """尝试获取锁；已被其他进程持有则立即返回 False（不等待）。"""
        if self.acquired:
            return True
        if _MSVCRT is None and _FCNTL is None:
            logger.warning("当前平台无文件锁支持（msvcrt/fcntl 均不可用），跳过单实例锁")
            return True  # 降级放行，避免在异常平台上完全不可用

        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            # "a+" 不会截断已有锁文件内容
            self._fh = open(self.path, "a+")
            self._fh.seek(0)
            if _MSVCRT is not None:
                _MSVCRT.locking(self._fh.fileno(), _MSVCRT.LK_NBLCK, 1)
            else:
                _FCNTL.flock(self._fh.fileno(), _FCNTL.LOCK_EX | _FCNTL.LOCK_NB)
        except OSError:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None
            return False

        # 写入 PID 仅作观测用途（锁的有效性不依赖它）
        try:
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(str(os.getpid()))
            self._fh.flush()
        except OSError:
            pass
        self.acquired = True
        logger.info(f"运行锁已获取: {self.path} (pid={os.getpid()})")
        return True

    def release(self) -> None:
        """释放锁（正常退出路径调用；进程异常退出由 OS 兜底释放）。"""
        if not self.acquired or self._fh is None:
            return
        try:
            self._fh.seek(0)
            if _MSVCRT is not None:
                _MSVCRT.locking(self._fh.fileno(), _MSVCRT.LK_UNLCK, 1)
            elif _FCNTL is not None:
                _FCNTL.flock(self._fh.fileno(), _FCNTL.LOCK_UN)
        except OSError as e:
            logger.warning(f"运行锁释放异常（进程退出仍会自动释放）: {e}")
        finally:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
            self.acquired = False
            logger.info("运行锁已释放")

    def __enter__(self) -> "LocalRunLock":
        acquired = self.acquire()
        if not acquired:
            raise RuntimeError(f"运行锁被其他进程持有: {self.path}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def default_lock_path(data_dir: Optional[str] = None) -> str:
    """默认锁文件路径：项目根/data/run.lock。"""
    base = data_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    return os.path.join(base, "run.lock")
