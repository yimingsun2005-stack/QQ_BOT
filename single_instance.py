"""Linux process lock keyed by the resolved configuration path."""
from __future__ import annotations

import os
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    pass


class SingleInstance:
    def __init__(self, config_path: Path) -> None:
        resolved = Path(config_path).resolve()
        self.path = resolved.with_name(resolved.name + '.lock')
        self._fd: int | None = None

    def __enter__(self) -> "SingleInstance":
        if os.name != 'posix':
            raise OSError('QQ_BOT 服务器版需要 Linux / POSIX 文件锁')
        import fcntl
        flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise AlreadyRunningError('Bot 已经在运行，请勿重复启动') from None
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        # Keep the inode: removing it allows concurrent processes to lock different files.
