"""单实例互斥锁：防止客户端重复启动（双开）。

Windows 使用命名内核对象（互斥体），由操作系统保证原子性：第二个进程
创建同名互斥体时 ``GetLastError`` 返回 ``ERROR_ALREADY_EXISTS``，据此
判定已有实例在运行。互斥体随进程句柄生命周期存在，进程崩溃时由系统
自动回收，不会留下死锁。

非 Windows 平台退化为 ``fcntl`` 文件锁（开发环境使用），同样由内核保证
排他性。
"""
from __future__ import annotations

import atexit
import os

from .config import APP_DIR

MUTEX_NAME = "Global\\DoubaoDesktopClient.SingleInstance"
LOCK_FILE = os.path.join(APP_DIR, ".singleinstance.lock")

ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    """进程级互斥守卫。

    用法::

        guard = SingleInstance()
        if not guard.acquire():
            # 已有实例在运行
            raise SystemExit(...)
        # ... 正常启动；进程退出时锁自动释放
    """

    def __init__(self, name: str = MUTEX_NAME):
        self.name = name
        self._handle = None      # Windows 互斥体句柄
        self._lock_fd = None     # POSIX 文件锁描述符

    @property
    def acquired(self) -> bool:
        return self._handle is not None or self._lock_fd is not None

    def acquire(self) -> bool:
        """尝试获取锁；成功返回 True，已有实例在运行返回 False。"""
        if self.acquired:
            return True
        if os.name == "nt":
            return self._acquire_windows()
        return self._acquire_posix()

    def release(self) -> None:
        """释放锁。进程正常退出时由 atexit 自动调用。"""
        if self._handle is not None:
            import ctypes
            try:
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except OSError:
                pass
            self._handle = None
        if self._lock_fd is not None:
            try:
                self._lock_fd.close()
                os.unlink(LOCK_FILE)
            except OSError:
                pass
            self._lock_fd = None

    # ---------------------------------------------------------------- Windows
    def _acquire_windows(self) -> bool:
        import ctypes
        try:
            handle = ctypes.windll.kernel32.CreateMutexW(None, False, self.name)
            if not handle:
                # 创建失败（如权限问题）：宁可放行也不能把程序挡死
                return True
            if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                ctypes.windll.kernel32.CloseHandle(handle)
                return False
            self._handle = handle
            atexit.register(self.release)
            return True
        except OSError:
            return True

    # ---------------------------------------------------------------- POSIX
    def _acquire_posix(self) -> bool:
        import fcntl
        try:
            fd = open(LOCK_FILE, "w")
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                fd.close()
            except Exception:  # noqa: BLE001
                pass
            return False
        fd.write(str(os.getpid()))
        fd.flush()
        self._lock_fd = fd
        atexit.register(self.release)
        return True
