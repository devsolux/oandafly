"""Lifetime store ownership acquired before SQLite can write a single byte."""

from __future__ import annotations

import importlib
import os
import stat
from pathlib import Path
from typing import Protocol, cast

from oandafly.domain.errors import Conflict, SafetyFence, ValidationFailure


class WindowsLockAPI(Protocol):
    LK_NBLCK: int
    def locking(self, descriptor: int, mode: int, count: int) -> None: ...


class StoreOwnerLock:
    def __init__(self, database: Path) -> None:
        self.path = database.with_name(database.name + ".owner.lock")
        self._pid = os.getpid()
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.set_inheritable(self._fd, False)
            info = os.fstat(self._fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValidationFailure("Store ownership requires an unaliased regular lock file")
            if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise ValidationFailure("Store ownership file must be private")
            if os.name == "nt":
                from oandafly.security.windows_acl import private_file
                private_file(self.path)
            self._identity = (info.st_dev, info.st_ino)
            try:
                if os.name == "nt":
                    api = cast(WindowsLockAPI, importlib.import_module("msvcrt"))
                    api.locking(self._fd, api.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise Conflict("Another process owns this state store") from exc
            self.assert_held()
        except BaseException:
            self.close()
            raise

    def assert_held(self) -> None:
        if self._fd < 0 or os.getpid() != self._pid:
            raise SafetyFence("Store owner is closed or belongs to another process")
        info = self.path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != self._identity):
            raise SafetyFence("Store ownership path changed")

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1
