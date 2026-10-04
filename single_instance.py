"""Configuration-scoped process lock on Linux and Windows."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    pass


class SingleInstance:
    def __init__(self, config_path: Path) -> None:
        resolved = Path(config_path).resolve()
        self.path = resolved.with_name(resolved.name + ".lock")
        self.name = "Local\\QQ_BOT_" + hashlib.sha256(str(resolved).encode()).hexdigest()
        self._fd: int | None = None
        self._handle = None

    def __enter__(self) -> "SingleInstance":
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            kernel.CreateMutexW.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            handle = kernel.CreateMutexW(None, False, self.name)
            error = ctypes.get_last_error()
            if not handle:
                raise OSError(error, "无法创建单实例锁")
            if error == 183:
                kernel.CloseHandle(handle)
                raise AlreadyRunningError("Bot 已经在运行，请勿重复启动")
            self._handle, self._kernel = handle, kernel
        elif os.name == "posix":
            import fcntl
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise AlreadyRunningError("Bot 已经在运行，请勿重复启动") from None
            except BaseException:
                os.close(fd)
                raise
            self._fd = fd
        else:
            raise OSError("不支持的运行平台")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._handle is not None:
            self._kernel.CloseHandle(self._handle)
            self._handle = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        # Keep the inode so concurrent processes always lock the same file.
