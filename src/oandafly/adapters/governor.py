"""Durable quotas shared by cooperating processes using the canonical store.

The broker's IP-wide limits also include unrelated applications and other hosts;
this local governor cannot certify those participants. Crashed stream reservations
remain occupied until an operator proves the corresponding connections retired.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Literal, Protocol
from uuid import UUID, uuid4

from oandafly.domain.errors import Conflict, RateLimited, SafetyFence, ValidationFailure
from oandafly.storage.database import Database

QuotaKind = Literal["request", "connection"]
_NANO = 1_000_000_000
_SCHEMA = """
CREATE TABLE IF NOT EXISTS egress_buckets (
 service TEXT NOT NULL, kind TEXT NOT NULL, rate INTEGER NOT NULL,
 capacity INTEGER NOT NULL, credits INTEGER NOT NULL, measured_ns INTEGER NOT NULL,
 clock_blocked INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(service, kind)
);
CREATE TABLE IF NOT EXISTS egress_boot_identity (service TEXT PRIMARY KEY, boot_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS egress_boot_audit (service TEXT NOT NULL, previous_boot_id TEXT, boot_id TEXT NOT NULL, measured_ns INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS egress_streams (
 lease_id TEXT PRIMARY KEY, service TEXT NOT NULL, owner_id TEXT NOT NULL,
 is_private INTEGER NOT NULL CHECK(is_private IN (0,1)), acquired_ns INTEGER NOT NULL
);
"""


class RateGovernor(Protocol):
    async def acquire(self, *, kind: QuotaKind = "request", deadline: float) -> None: ...


class ObservationGovernor(RateGovernor, Protocol):
    @property
    def service(self) -> str: ...
    def stream(self, *, private: bool) -> AbstractAsyncContextManager[str]: ...


class CredentialProbeGovernor:
    """Owner-local, low-rate GET probe budget; no persistence or stream rights."""

    service = "OANDA_V20"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next: dict[QuotaKind, float] = {"request": 0, "connection": 0}

    async def acquire(self, *, kind: QuotaKind = "request", deadline: float) -> None:
        now = time.monotonic()
        if kind not in self._next or not math.isfinite(deadline) or not 0 < deadline - now <= 60:
            raise RateLimited("Credential probe deadline or quota is invalid")
        with self._lock:
            grant = max(now, self._next[kind])
            if grant >= deadline:
                raise RateLimited("Credential probe capacity cannot meet the deadline")
            self._next[kind] = grant + (1.0 if kind == "connection" else 0.2)
        if grant > now:
            await asyncio.sleep(grant - now)
        if time.monotonic() >= deadline:
            raise RateLimited("Credential probe deadline expired")

    def stream(self, *, private: bool) -> AbstractAsyncContextManager[str]:
        raise SafetyFence("Credential probes have no streaming capability")


class LocalRateGovernor:
    def __init__(
        self,
        db: Database,
        bucket_key: Literal["OANDA_V20", "OANDA_RATES"] = "OANDA_V20",
        *,
        requests_per_second: int = 90,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        boot_identity: str | None = None,
    ) -> None:
        if bucket_key not in {"OANDA_V20", "OANDA_RATES"}:
            raise ValidationFailure("Unknown egress quota service")
        if type(requests_per_second) is not int or not 1 <= requests_per_second <= 90:
            raise ValidationFailure("Local request quota must be between 1 and 90 per second")
        self.db = db
        self.service = bucket_key
        self._clock = monotonic_ns
        self._owner_id = str(uuid4())
        db.initialize(_SCHEMA, component=__name__)
        now = self._clock()
        with db.transaction() as connection:
            if boot_identity is not None:
                UUID(boot_identity)
                previous = connection.execute("SELECT boot_id FROM egress_boot_identity WHERE service=?", (self.service,)).fetchone()
                if previous is not None and previous[0] != boot_identity:
                    # A changed Linux boot identity proves old kernel sockets are
                    # retired. Never interpret a same-boot clock regression this way.
                    connection.execute("DELETE FROM egress_streams WHERE service=?", (self.service,))
                    connection.execute("UPDATE egress_buckets SET credits=0,measured_ns=?,clock_blocked=0 WHERE service=?", (now, self.service))
                    connection.execute("INSERT INTO egress_boot_audit VALUES(?,?,?,?)", (self.service, previous[0], boot_identity, now))
                connection.execute("INSERT INTO egress_boot_identity VALUES(?,?) ON CONFLICT(service) DO UPDATE SET boot_id=excluded.boot_id", (self.service, boot_identity))
            for kind, rate in (("request", requests_per_second), ("connection", 1)):
                row = connection.execute(
                    "SELECT rate,capacity FROM egress_buckets WHERE service=? AND kind=?",
                    (self.service, kind),
                ).fetchone()
                if row is not None and (row["rate"] != rate or row["capacity"] != 1):
                    raise Conflict("Canonical egress quota policy differs across processes")
                connection.execute(
                    "INSERT OR IGNORE INTO egress_buckets(service,kind,rate,capacity,credits,measured_ns) "
                    "VALUES(?,?,?,?,?,?)",
                    (self.service, kind, rate, 1, _NANO, now),
                )

    def try_acquire(self, *, kind: QuotaKind = "request") -> float:
        """Atomically consume one token, or return seconds until it can be consumed."""
        if kind not in {"request", "connection"}:
            raise ValidationFailure("Unknown quota class")
        blocked = False
        delay = 0.0
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM egress_buckets WHERE service=? AND kind=?", (self.service, kind)
            ).fetchone()
            if row is None:
                raise SafetyFence("Canonical egress quota disappeared")
            now = self._clock()
            blocked = bool(row["clock_blocked"]) or now < row["measured_ns"]
            if blocked:
                connection.execute(
                    "UPDATE egress_buckets SET clock_blocked=1 WHERE service=? AND kind=?",
                    (self.service, kind),
                )
            else:
                credits = min(
                    _NANO * row["capacity"], row["credits"] + (now - row["measured_ns"]) * row["rate"]
                )
                if credits >= _NANO:
                    credits -= _NANO
                else:
                    delay = (_NANO - credits) / (row["rate"] * _NANO)
                connection.execute(
                    "UPDATE egress_buckets SET credits=?, measured_ns=? WHERE service=? AND kind=?",
                    (credits, now, self.service, kind),
                )
        if blocked:
            raise SafetyFence("Monotonic quota clock regressed; explicit connection retirement is required")
        return delay

    async def acquire(self, *, kind: QuotaKind = "request", deadline: float) -> None:
        if not math.isfinite(deadline) or deadline - time.monotonic() > 60:
            raise ValidationFailure("Quota deadline must be finite and at most 60 seconds away")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RateLimited("Local egress quota deadline expired")
            delay = await asyncio.to_thread(self.try_acquire, kind=kind)
            if delay == 0:
                if time.monotonic() >= deadline:
                    raise RateLimited("Local egress grant arrived after command deadline")
                return
            if delay >= remaining:
                raise RateLimited("Local egress quota cannot meet command deadline")
            await asyncio.sleep(min(delay, 0.05))

    def _claim_stream(self, private: bool) -> str:
        if self.service != "OANDA_V20":
            raise ValidationFailure("Rates REST subscription has no v20 stream capacity")
        lease_id = str(uuid4())
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(1-is_private),0) AS public "
                "FROM egress_streams WHERE service=?",
                (self.service,),
            ).fetchone()
            if row["total"] >= 20 or (not private and row["public"] >= 18):
                raise RateLimited("Local stream limit reached; private reconciliation capacity is reserved")
            connection.execute(
                "INSERT INTO egress_streams VALUES(?,?,?,?,?)",
                (lease_id, self.service, self._owner_id, int(private), self._clock()),
            )
        return lease_id

    def _release_stream(self, lease_id: str) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "DELETE FROM egress_streams WHERE lease_id=? AND service=? AND owner_id=?",
                (lease_id, self.service, self._owner_id),
            )

    @asynccontextmanager
    async def stream(self, *, private: bool) -> AsyncIterator[str]:
        # Claim is intentionally synchronous: tiny local atomic operation prevents
        # cancellation between a background claim's commit and receipt of its ID.
        lease_id = self._claim_stream(private)
        try:
            yield lease_id
        finally:
            self._release_stream(lease_id)
