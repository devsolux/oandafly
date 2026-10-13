"""Bound total detached work and reserve an executor for safety commands."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from functools import partial
from typing import ParamSpec, TypeVar

from oandafly.domain.errors import Conflict, RateLimited

P = ParamSpec("P")
R = TypeVar("R")
SAFETY_LANE: ContextVar[bool] = ContextVar("oandafly_safety_lane", default=False)


class AdmissionController:
    """Reservations include queued, running and disconnected commands.

    Only the authority's hard-coded dispatcher chooses a lane. No wire parameter
    grants priority. Reserved threads do not bypass database transaction locks.
    """

    def __init__(self, capacity: int = 16, safety_capacity: int = 4) -> None:
        if not 1 <= capacity <= 64 or not 1 <= safety_capacity <= 8:
            raise ValueError("Invalid command capacity")
        self._limits = (capacity, safety_capacity)
        self._counts = [0, 0]
        self._lock = threading.Lock()
        self._closed = False
        self._pools = (ThreadPoolExecutor(max_workers=4, thread_name_prefix="oandafly-command"),
                       ThreadPoolExecutor(max_workers=2, thread_name_prefix="oandafly-safety"))

    def acquire(self, safety: bool) -> None:
        with self._lock:
            if self._closed:
                raise Conflict("Worker admission is closed")
            lane = int(safety)
            if self._counts[lane] >= self._limits[lane]:
                raise RateLimited("Worker command capacity reached")
            self._counts[lane] += 1

    def release(self, safety: bool) -> None:
        with self._lock:
            if self._counts[int(safety)] <= 0:
                raise RuntimeError("Unbalanced command reservation")
            self._counts[int(safety)] -= 1

    async def run(self, function: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
        context = copy_context()
        operation = partial(function, *args, **kwargs)
        future = asyncio.get_running_loop().run_in_executor(
            self._pools[int(SAFETY_LANE.get())], context.run, operation)
        # Cancelling a waiter cannot make an executing native thread disappear.
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await asyncio.shield(future)
            raise

    async def run_safety(self, function: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
        """Worker-internal reconciliation port; never accepts a wire callback."""
        self.acquire(True)
        token = SAFETY_LANE.set(True)
        try:
            return await self.run(function, *args, **kwargs)
        finally:
            SAFETY_LANE.reset(token)
            self.release(True)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        for pool in self._pools:
            pool.shutdown(wait=True, cancel_futures=False)
