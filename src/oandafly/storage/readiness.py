"""Fail immediately before touching a database lock during initialization."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from oandafly.domain.errors import Conflict


class InitializationFence:
    def __init__(self) -> None:
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._initializer: threading.Thread | None = None

    @contextmanager
    def initializing(self) -> Iterator[None]:
        with self._lock:
            if self._initializer is not None:
                raise Conflict("Database initialization already has an owner")
            self._ready.clear()
            self._initializer = threading.current_thread()
        try:
            yield
        finally:
            with self._lock:
                self._initializer = None

    def complete(self) -> None:
        with self._lock:
            if self._initializer is not None:
                raise Conflict("Database initialization is still running")
            self._ready.set()

    def require_ready(self) -> None:
        if not self._ready.is_set():
            raise Conflict("Worker initialization is not complete")

    def require_access(self) -> None:
        with self._lock:
            if not self._ready.is_set() and self._initializer is not threading.current_thread():
                raise Conflict("Database initialization is not complete")
