"""Durable authority ownership and synchronous, audited context handover.

The UI queues these operations on a worker thread. Network callbacks never run
inside SQLite transactions. A source mutation lease is retired by the same
transaction that accepts its STOPPED proof, before target initialization starts.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import stat
import threading
import time
from contextlib import nullcontext
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Protocol, cast
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from oandafly.domain.errors import Conflict, SafetyFence, ValidationFailure
from oandafly.domain.policy import (
    EngineConfig,
    Environment,
    ProfileVerifier,
    VerifiedAccountProfile,
)
from oandafly.domain.primitives import StrictModel, canonical_json, require_utc, sha256_json, utc_now
from oandafly.storage.database import Database

__all__ = [
    "AccountStopEvidence", "AuthorityCoordinator", "AuthoritySnapshot", "ContextInitializer",
    "ContextKey", "DrainDriver", "EngineConfig", "NativeStopProofVerifier", "PositionUnits", "ReadinessProof",
    "ReadinessVerifier", "SafetyBarrier", "StopProof", "StopProofVerifier", "TradeUnits", "TransitionRecord",
    "VerifiedAccountProfile",
]

ProductTrack = Literal["TRADING", "DATA"]
TransitionState = Literal[
    "REQUESTED", "QUIESCING", "DRAINING", "RECONCILING", "STOPPED_OLD", "RESETTING",
    "INITIALIZING_NEW", "READY_UNARMED", "BLOCKED_DRAIN", "FAILED_INITIALIZATION",
    "RECOVERY_REQUIRED",
]
LeaseStatus = Literal["NONE", "ACTIVE", "RECOVERY", "RETIRED"]
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ContextKey(StrictModel):
    authority_id: Identifier
    environment: Environment = "SANDBOX"
    product_track: ProductTrack = "DATA"
    account_profile_id: Identifier = "unenrolled"
    epoch: Annotated[int, Field(strict=True, ge=0, le=9223372036854775806)] = 0

    @property
    def fingerprint(self) -> str:
        return sha256_json(self)


class TradeUnits(StrictModel):
    trade_id: Identifier
    current_units: Decimal


class PositionUnits(StrictModel):
    instrument: str
    long_units: Decimal
    short_units: Decimal

    @model_validator(mode="after")
    def side_signs(self) -> PositionUnits:
        if self.long_units < 0 or self.short_units > 0:
            raise ValueError("Position long units must be nonnegative and short units nonpositive")
        return self


class AccountStopEvidence(StrictModel):
    account_id: Identifier
    environment: Environment
    snapshot_transaction_id: Annotated[str, Field(pattern=r"^[0-9]{1,40}$")]
    covered_transaction_id: Annotated[str, Field(pattern=r"^[0-9]{1,40}$")]
    observed_at: datetime
    trades: tuple[TradeUnits, ...] = ()
    positions: tuple[PositionUnits, ...] = ()
    pending_order_ids: tuple[str, ...] = ()
    unknown_intent_ids: tuple[str, ...] = ()
    financing_finalized: Annotated[bool, Field(strict=True)]
    financing_evidence_sha256: Digest
    source_snapshot_sha256: Digest

    _utc = field_validator("observed_at")(require_utc)

    @model_validator(mode="after")
    def unique_native_keys(self) -> AccountStopEvidence:
        if len({x.trade_id for x in self.trades}) != len(self.trades):
            raise ValueError("Duplicate native Trade IDs in STOPPED proof")
        if len({x.instrument for x in self.positions}) != len(self.positions):
            raise ValueError("Duplicate native positions in STOPPED proof")
        return self

    def require_flat(self) -> None:
        if any(t.current_units != 0 for t in self.trades):
            raise SafetyFence("STOPPED requires every native trade to have zero units")
        if any(p.long_units != 0 or p.short_units != 0 for p in self.positions):
            raise SafetyFence("A net-zero hedge is not a gross-flat position")
        if self.pending_order_ids or self.unknown_intent_ids:
            raise SafetyFence("Pending orders or ambiguous dispatch prevent STOPPED")
        if not self.financing_finalized:
            raise SafetyFence("Unsettled financing prevents source retirement")
        if int(self.covered_transaction_id) < int(self.snapshot_transaction_id):
            raise SafetyFence("REST history coverage has not reached the source snapshot")


class StopProof(StrictModel):
    source_context_sha256: Digest
    observed_at: datetime
    complete_account_universe: Annotated[bool, Field(strict=True)]
    accounts: tuple[AccountStopEvidence, ...] = ()

    _utc = field_validator("observed_at")(require_utc)

    @model_validator(mode="after")
    def disjoint_scope(self) -> StopProof:
        keys = [(a.environment, a.account_id) for a in self.accounts]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate accounts cannot prove a complete disjoint scope")
        return self


class ReadinessProof(StrictModel):
    context_sha256: Digest
    profile_version: Annotated[int, Field(strict=True, ge=1)]
    observed_at: datetime
    evidence_sha256: Digest
    structural_gate_verified: Literal[True]
    account_reconciled: Literal[True]

    _utc = field_validator("observed_at")(require_utc)


class AuthoritySnapshot(StrictModel):
    context: ContextKey
    state: TransitionState
    armed: bool
    revision: int
    lease_status: LeaseStatus
    lease_epoch: int | None
    lease_profile_version: int | None
    transition_id: str | None
    requested_context: ContextKey | None


class TransitionRecord(StrictModel):
    transition_id: str
    source: ContextKey
    target: ContextKey
    state: TransitionState
    requested_at: datetime
    failure_code: str | None
    stopped_proof_sha256: str | None


class SafetyBarrier(Protocol):
    def has_unresolved_in(self, connection: sqlite3.Connection, authority_id: str) -> bool:
        """Include all pending entries, dispatched UNKNOWN and unfinished recovery."""


class StopProofVerifier(Protocol):
    def verify_stop_in(self, connection: sqlite3.Connection, source: ContextKey, proof: StopProof) -> None:
        """Bind supplied native snapshots to the current durable projection cut."""


class NativeStopProofVerifier:
    """Validate native cash/history and execution projections in the commit lock."""

    def verify_stop_in(self, connection: sqlite3.Connection, source: ContextKey, proof: StopProof) -> None:
        if source.product_track != "TRADING":
            return
        from oandafly.storage.ledger import Ledger

        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_accounts'").fetchone() is None:
            raise SafetyFence("Native ledger proof validation is unavailable")
        trades_exist = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_trades'").fetchone() is not None
        orders_exist = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_orders'").fetchone() is not None
        for account in proof.accounts:
            Ledger.validate_stop_in(connection, account.environment, account.account_id,
                                    account.snapshot_transaction_id, account.covered_transaction_id)
            scope = (account.environment, account.account_id)
            if trades_exist:
                for trade in connection.execute("SELECT current_units FROM execution_trades WHERE environment=? AND account_id=?", scope):
                    if Decimal(trade["current_units"]) != 0:
                        raise SafetyFence("A durable native trade projection is not gross-flat")
            if orders_exist and connection.execute("SELECT 1 FROM execution_orders WHERE environment=? AND account_id=? AND state NOT IN ('CANCELLED','FILLED','REJECTED') LIMIT 1", scope).fetchone() is not None:
                raise SafetyFence("A durable native order projection remains pending")


class DrainDriver(Protocol):
    def quiesce(self, source: ContextKey) -> None:
        """Stop entry production and join all producer tasks."""

    def drain(self, source: ContextKey) -> None:
        """Cancel entries and close every trade through guarded recovery routes."""

    def reconcile(self, source: ContextKey) -> StopProof:
        """Return a fresh full account snapshot plus REST-covered finality proof."""


class ContextInitializer(Protocol):
    def reset_transients(self, source: ContextKey, target: ContextKey) -> None:
        """Purge streams/pip caches/weights; preserve all financial audit history."""

    def initialize(self, target: ContextKey, profile: VerifiedAccountProfile | None) -> None:
        """Initialize observation and qualified resources without submitting orders."""


class ReadinessVerifier(Protocol):
    def verify(self, context: ContextKey, profile: VerifiedAccountProfile) -> ReadinessProof:
        """Independently validate current account, model, data and release gates."""


class _WindowsFileLockModule(Protocol):
    LK_NBLCK: int

    def locking(self, descriptor: int, mode: int, byte_count: int) -> None:
        """Lock the first byte of the canonical authority binding file."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS authority_state (
    authority_id TEXT PRIMARY KEY, context_json TEXT NOT NULL,
    state TEXT NOT NULL, armed INTEGER NOT NULL CHECK(armed IN (0,1)),
    revision INTEGER NOT NULL, lease_epoch INTEGER, lease_profile_version INTEGER,
    lease_status TEXT NOT NULL, owner_token TEXT NOT NULL,
    transition_id TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS verified_account_profiles (
    authority_id TEXT NOT NULL, profile_id TEXT NOT NULL, version INTEGER NOT NULL,
    profile_json TEXT NOT NULL, profile_hash TEXT NOT NULL,
    PRIMARY KEY(authority_id,profile_id,version));
CREATE TABLE IF NOT EXISTS context_transitions (
    transition_id TEXT PRIMARY KEY, authority_id TEXT NOT NULL,
    command_id TEXT NOT NULL, command_hash TEXT NOT NULL,
    source_json TEXT NOT NULL, target_json TEXT NOT NULL, state TEXT NOT NULL,
    requested_at TEXT NOT NULL, failure_code TEXT, stopped_proof_json TEXT,
    stopped_proof_sha256 TEXT, UNIQUE(authority_id,command_id));
CREATE TABLE IF NOT EXISTS authority_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, authority_id TEXT NOT NULL,
    revision INTEGER NOT NULL, event TEXT NOT NULL, body TEXT NOT NULL,
    occurred_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS authority_fences (
    fence_id TEXT PRIMARY KEY, authority_id TEXT NOT NULL, context_sha256 TEXT NOT NULL,
    code TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL,
    resolved_at TEXT, resolution_sha256 TEXT);
CREATE INDEX IF NOT EXISTS authority_open_fences
    ON authority_fences(authority_id,resolved_at);
CREATE TABLE IF NOT EXISTS authority_commands (
    authority_id TEXT NOT NULL, command_id TEXT NOT NULL, command_hash TEXT NOT NULL,
    command_kind TEXT NOT NULL, result_revision INTEGER NOT NULL,
    PRIMARY KEY(authority_id,command_id));
"""


class _AuthorityFileLock:
    """Lifetime OS lock, with a durable canonical-store binding and no TTL."""

    def __init__(self, directory: Path, authority_id: str, database_path: Path) -> None:
        from oandafly.paths import secure_directory

        directory = secure_directory(directory)
        self._pid = os.getpid()
        self._binding = (directory, authority_id, database_path.absolute())
        for path in (directory, *directory.parents):
            if path.is_symlink():
                raise ValidationFailure("Authority lock directories cannot contain symbolic links")
        info = directory.stat()
        if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValidationFailure("Authority lock directory must be owner-only")
        self.path = directory / (hashlib.sha256(authority_id.encode()).hexdigest() + ".lock")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        self._fd = os.open(self.path, flags, 0o600)
        try:
            file_info = os.fstat(self._fd)
            self._identity = (file_info.st_dev, file_info.st_ino)
            if not stat.S_ISREG(file_info.st_mode) or file_info.st_nlink != 1:
                raise ValidationFailure("Authority lock must be a private regular file")
            if os.name != "nt" and (file_info.st_uid != os.getuid() or file_info.st_mode & 0o077):
                raise ValidationFailure("Authority lock permissions are unsafe")
            if os.name == "nt":
                from oandafly.security.windows_acl import private_file
                private_file(self.path)
            self._acquire()
            content = os.read(self._fd, 4097)
            binding = {"authority_id": authority_id, "database_path": str(database_path.absolute())}
            if content:
                if len(content) > 4096 or json.loads(content) != binding:
                    raise SafetyFence("Authority ID is permanently bound to another canonical state store")
            else:
                data = canonical_json(binding).encode()
                if os.write(self._fd, data) != len(data):
                    raise OSError("Incomplete authority lock binding write")
                os.fsync(self._fd)
        except BaseException:
            os.close(self._fd)
            self._fd = -1
            raise

    def _acquire(self) -> None:
        try:
            if os.name == "nt":
                msvcrt = cast(_WindowsFileLockModule, importlib.import_module("msvcrt"))
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise Conflict("Another local process owns this Authority ID") from exc

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def assert_held(self) -> None:
        if self._fd < 0 or self._pid != os.getpid():
            raise SafetyFence("Authority OS lock has been released")
        try:
            actual = self.path.stat(follow_symlinks=False)
        except OSError as exc:
            raise SafetyFence("Authority lock path disappeared") from exc
        if not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1 or (actual.st_dev, actual.st_ino) != self._identity:
            raise SafetyFence("Authority lock path was replaced or aliased")

    def assert_binding(self, directory: Path, authority_id: str, database_path: Path) -> None:
        self.assert_held()
        if self._binding != (directory.absolute(), authority_id, database_path.absolute()):
            raise SafetyFence("Pre-acquired ownership belongs to a different authority store")


class MutationAuthorizer(Protocol):
    def require(self, context: ContextKey, profile: VerifiedAccountProfile, action: str) -> None:
        """Check a current account-specific grant at the final mutation boundary."""


class AuthorityCoordinator:
    """One cooperating worker per authority in the fixed canonical lock directory.

    Different arbitrary lock directories cannot provide global exclusivity. The
    application must supply its fixed platform state directory, never GUI input.
    Active leases survive owner death as recovery-only claims until proven flat.
    """

    @staticmethod
    def acquire_ownership(directory: Path, authority_id: str, database_path: Path) -> _AuthorityFileLock:
        """Acquire the existing canonical authority lock before opening SQLite."""
        return _AuthorityFileLock(directory, authority_id, database_path)

    def __init__(
        self,
        db: Database,
        authority_id: str,
        lock_directory: Path,
        initial_context: ContextKey | None = None,
        *,
        barrier: SafetyBarrier | None = None,
        stop_verifier: StopProofVerifier | None = None,
        config: EngineConfig | None = None,
        ownership: _AuthorityFileLock | None = None,
        mutation_authorizer: MutationAuthorizer | None = None,
    ) -> None:
        initial = initial_context or ContextKey(authority_id=authority_id)
        if initial.authority_id != authority_id or initial.product_track != "DATA" or initial.epoch != 0:
            raise ValidationFailure("A new authority must start DATA / epoch 0 / unarmed")
        self.db = db
        self.authority_id = authority_id
        self.config = config or EngineConfig()
        self.mutation_authorizer = mutation_authorizer
        self.barrier = barrier
        self.stop_verifier = stop_verifier or NativeStopProofVerifier()
        self._owner_token = str(uuid4())
        self._pid = os.getpid()
        self._closed = False
        self._operation_lock = threading.Lock()
        self._file_lock = ownership or self.acquire_ownership(lock_directory, authority_id, db.path)
        try:
            self._file_lock.assert_binding(lock_directory, authority_id, db.path)
            db.initialize(_SCHEMA, component=__name__)
            with db.transaction() as connection:
                row = connection.execute("SELECT * FROM authority_state WHERE authority_id=?", (authority_id,)).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO authority_state VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (authority_id, initial.model_dump_json(), "READY_UNARMED", 0, 0, None, None,
                         "NONE", self._owner_token, None, utc_now().isoformat()),
                    )
                    self._audit(connection, 0, "AUTHORITY_CREATED", {"context": initial})
                else:
                    lease = str(row["lease_status"])
                    state = str(row["state"])
                    # An interrupted transition is recovered by a fresh reviewed
                    # drain, never continued from an assumed in-memory callback.
                    recovered_state = "RECOVERY_REQUIRED" if lease in {"ACTIVE", "RECOVERY"} or state != "READY_UNARMED" else state
                    recovered_lease = "RECOVERY" if lease in {"ACTIVE", "RECOVERY"} else lease
                    revision = int(row["revision"]) + 1
                    connection.execute(
                        "UPDATE authority_state SET armed=0,state=?,lease_status=?,owner_token=?,revision=?,updated_at=? WHERE authority_id=?",
                        (recovered_state, recovered_lease, self._owner_token, revision, utc_now().isoformat(), authority_id),
                    )
                    self._audit(connection, revision, "OWNER_REOPENED_UNARMED", {"previous_state": state, "state": recovered_state})
        except BaseException:
            self._file_lock.close()
            raise

    def _check_owner(self) -> None:
        if self._closed or self._pid != os.getpid():
            raise Conflict("Authority owner is closed or belongs to another process")
        self._file_lock.assert_held()

    def _row(self, connection: sqlite3.Connection) -> sqlite3.Row:
        self._check_owner()
        row = connection.execute("SELECT * FROM authority_state WHERE authority_id=?", (self.authority_id,)).fetchone()
        if row is None or row["owner_token"] != self._owner_token:
            raise SafetyFence("Authority ownership changed")
        return cast(sqlite3.Row, row)

    def _audit(self, connection: sqlite3.Connection, revision: int, event: str, body: object) -> None:
        connection.execute(
            "INSERT INTO authority_audit(authority_id,revision,event,body,occurred_at) VALUES(?,?,?,?,?)",
            (self.authority_id, revision, event, canonical_json(body), utc_now().isoformat()),
        )

    def _snapshot(self, connection: sqlite3.Connection, row: sqlite3.Row) -> AuthoritySnapshot:
        target = None
        if row["transition_id"]:
            transition = connection.execute("SELECT target_json FROM context_transitions WHERE transition_id=?", (row["transition_id"],)).fetchone()
            if transition is not None and row["state"] != "READY_UNARMED":
                target = ContextKey.model_validate_json(transition["target_json"])
        return AuthoritySnapshot(
            context=ContextKey.model_validate_json(row["context_json"]), state=row["state"],
            armed=bool(row["armed"]), revision=row["revision"], lease_status=row["lease_status"],
            lease_epoch=row["lease_epoch"], lease_profile_version=row["lease_profile_version"],
            transition_id=row["transition_id"], requested_context=target,
        )

    def snapshot(self) -> AuthoritySnapshot:
        with self.db.read() as connection:
            return self._snapshot(connection, self._row(connection))

    def snapshot_in(self, connection: sqlite3.Connection) -> AuthoritySnapshot:
        """Read authority state in a caller-owned atomic command transaction."""
        if not connection.in_transaction:
            raise ValidationFailure("An active database transaction is required")
        return self._snapshot(connection, self._row(connection))

    def assert_owner(self) -> None:
        """Public ownership capability check for the authenticated local IPC server."""
        with self.db.read() as connection:
            self._row(connection)

    def register_profile(self, profile: VerifiedAccountProfile, verifier: ProfileVerifier) -> None:
        self._check_owner()
        if profile.authority_id != self.authority_id:
            raise ValidationFailure("Profile belongs to another authority")
        verifier.verify(profile)
        profile.require_current()
        digest = sha256_json(profile)
        with self.db.transaction() as connection:
            row = self._row(connection)
            existing = connection.execute(
                "SELECT profile_hash FROM verified_account_profiles WHERE authority_id=? AND profile_id=? AND version=?",
                (self.authority_id, profile.profile_id, profile.version),
            ).fetchone()
            if existing is not None:
                if existing["profile_hash"] != digest:
                    raise Conflict("Immutable profile version was reused with different evidence")
                return
            if row["lease_status"] in {"ACTIVE", "RECOVERY"}:
                current = ContextKey.model_validate_json(row["context_json"])
                if current.account_profile_id == profile.profile_id:
                    raise Conflict("An active source profile cannot change before lease retirement")
            connection.execute("INSERT INTO verified_account_profiles VALUES(?,?,?,?,?)",
                               (self.authority_id, profile.profile_id, profile.version, profile.model_dump_json(), digest))
            self._audit(connection, int(row["revision"]), "PROFILE_VERIFIED", {"profile_id": profile.profile_id, "version": profile.version, "sha256": digest})

    def profile_in(self, connection: sqlite3.Connection, context: ContextKey, version: int | None = None) -> VerifiedAccountProfile:
        if context.authority_id != self.authority_id:
            raise SafetyFence("Cross-authority profile lookup is forbidden")
        if version is None:
            row = connection.execute("SELECT profile_json FROM verified_account_profiles WHERE authority_id=? AND profile_id=? ORDER BY version DESC LIMIT 1", (self.authority_id, context.account_profile_id)).fetchone()
        else:
            row = connection.execute("SELECT profile_json FROM verified_account_profiles WHERE authority_id=? AND profile_id=? AND version=?", (self.authority_id, context.account_profile_id, version)).fetchone()
        if row is None:
            raise SafetyFence("Target account profile has not been independently enrolled")
        profile = VerifiedAccountProfile.model_validate_json(row["profile_json"])
        if profile.environment != context.environment:
            raise SafetyFence("Profile environment does not match the context")
        return profile

    def profile(self, context: ContextKey, version: int | None = None) -> VerifiedAccountProfile:
        with self.db.read() as connection:
            self._row(connection)
            return self.profile_in(connection, context, version)

    def _transition(self, connection: sqlite3.Connection, transition_id: str) -> TransitionRecord:
        row = connection.execute("SELECT * FROM context_transitions WHERE transition_id=? AND authority_id=?", (transition_id, self.authority_id)).fetchone()
        if row is None:
            raise Conflict("Unknown context transition")
        return TransitionRecord(
            transition_id=row["transition_id"], source=ContextKey.model_validate_json(row["source_json"]),
            target=ContextKey.model_validate_json(row["target_json"]), state=row["state"],
            requested_at=datetime.fromisoformat(row["requested_at"]), failure_code=row["failure_code"],
            stopped_proof_sha256=row["stopped_proof_sha256"],
        )

    def transition(self, transition_id: str) -> TransitionRecord:
        with self.db.read() as connection:
            self._row(connection)
            return self._transition(connection, transition_id)

    def request_transition(self, target: ContextKey, expected_revision: int, command_id: str, *, deadline: datetime | None = None, connection: sqlite3.Connection | None = None) -> TransitionRecord:
        UUID(command_id)
        command_deadline = require_utc(deadline) if deadline is not None else None
        received_at = utc_now()
        if command_deadline is not None and command_deadline > received_at + timedelta(seconds=60):
            raise ValidationFailure("Command deadline may not exceed 60 seconds")
        monotonic_deadline = time.monotonic() + max(0, (command_deadline - received_at).total_seconds()) if command_deadline is not None else None
        digest = sha256_json({"target": target, "expected_revision": expected_revision, "deadline": command_deadline})
        if connection is not None and not connection.in_transaction:
            raise ValidationFailure("A supplied command connection must already be transactional")
        with self.db.transaction() if connection is None else nullcontext(connection) as connection:
            row = self._row(connection)
            existing = connection.execute("SELECT transition_id,command_hash FROM context_transitions WHERE authority_id=? AND command_id=?", (self.authority_id, command_id)).fetchone()
            if existing is not None:
                if existing["command_hash"] != digest:
                    raise Conflict("Transition command UUID was reused with a different payload")
                return self._transition(connection, existing["transition_id"])
            if command_deadline is not None:
                self._check_command_deadline(command_deadline, cast(float, monotonic_deadline), received_at)
            if row["revision"] != expected_revision:
                raise Conflict("Context preview is stale")
            if row["state"] not in {"READY_UNARMED", "RECOVERY_REQUIRED"}:
                raise Conflict("An existing handover must be completed or explicitly revised")
            source = ContextKey.model_validate_json(row["context_json"])
            if target.authority_id != self.authority_id or target.epoch != source.epoch + 1:
                raise ValidationFailure("Target must use the same Authority ID and exactly the next epoch")
            if target.product_track == "TRADING":
                self.profile_in(connection, target).require_current()
            transition_id = str(uuid4())
            revision = int(row["revision"]) + 1
            now = utc_now().isoformat()
            connection.execute("INSERT INTO context_transitions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (transition_id, self.authority_id, command_id, digest, source.model_dump_json(), target.model_dump_json(), "REQUESTED", now, None, None, None))
            connection.execute("UPDATE authority_state SET state='REQUESTED',armed=0,revision=?,transition_id=?,updated_at=? WHERE authority_id=?", (revision, transition_id, now, self.authority_id))
            self._audit(connection, revision, "REQUESTED", {"source": source, "target": target, "transition_id": transition_id})
            return self._transition(connection, transition_id)

    def _advance(self, transition_id: str, allowed: set[str], state: TransitionState, failure: str | None = None) -> TransitionRecord:
        with self.db.transaction() as connection:
            row = self._row(connection)
            transition = self._transition(connection, transition_id)
            if row["transition_id"] != transition_id or transition.state not in allowed:
                raise Conflict("Transition state changed while a callback was running")
            revision = int(row["revision"]) + 1
            connection.execute("UPDATE context_transitions SET state=?,failure_code=? WHERE transition_id=?", (state, failure, transition_id))
            connection.execute("UPDATE authority_state SET state=?,armed=0,revision=?,updated_at=? WHERE authority_id=?", (state, revision, utc_now().isoformat(), self.authority_id))
            self._audit(connection, revision, state, {"transition_id": transition_id, "failure_code": failure})
            return self._transition(connection, transition_id)

    def _has_unresolved(self, connection: sqlite3.Connection, context: ContextKey) -> bool:
        if self.barrier is not None:
            return self.barrier.has_unresolved_in(connection, self.authority_id)
        # Safe integration default, including pending local intents: zero
        # snapshot exposure does not establish absence of future fills.
        table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outbox'").fetchone()
        if table is not None:
            if connection.execute(
                "SELECT 1 FROM outbox WHERE authority_id=? AND (dispatch_state IN ('DISPATCH_STARTED','UNKNOWN') OR status IN ('PENDING_SUBMISSION','ACKNOWLEDGED')) LIMIT 1", (self.authority_id,),
            ).fetchone() is not None:
                return True
            # Omitting the injected barrier cannot hide unfinished recovery or
            # quarantined/deferred native facts from the source retirement gate.
            for name, query in (
                ("execution_recovery", "SELECT 1 FROM execution_recovery WHERE authority_id=? AND state IN ('PREPARED','DISPATCH_STARTED','UNKNOWN') LIMIT 1"),
                ("execution_fences", "SELECT 1 FROM execution_fences WHERE resolved_evidence IS NULL AND (authority_id=? OR authority_id IS NULL) LIMIT 1"),
                ("execution_native_facts", "SELECT 1 FROM execution_native_facts f WHERE reduced!=1 AND EXISTS (SELECT 1 FROM outbox o WHERE o.authority_id=? AND o.environment=f.environment AND o.account_id=f.account_id) LIMIT 1"),
            ):
                if (connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
                        and connection.execute(query, (self.authority_id,)).fetchone()):
                    return True
            return False
        if context.product_track == "TRADING":
            raise SafetyFence("No durable outbox barrier is installed for the trading source")
        return False

    def _validate_stop(self, connection: sqlite3.Connection, transition: TransitionRecord, proof: StopProof) -> None:
        now = utc_now()
        age = timedelta(seconds=self.config.proof_max_age_seconds)
        if proof.source_context_sha256 != transition.source.fingerprint or not proof.complete_account_universe:
            raise SafetyFence("STOPPED proof does not cover the exact source context")
        if not max(transition.requested_at, now - age) <= proof.observed_at <= now:
            raise SafetyFence("STOPPED proof is stale, future-dated or predates the transition")
        if transition.source.product_track == "TRADING":
            source_row = self._row(connection)
            version = source_row["lease_profile_version"]
            profile = self.profile_in(connection, transition.source, version)
            expected = {(profile.environment, account) for account in profile.account_ids}
            if {(a.environment, a.account_id) for a in proof.accounts} != expected:
                raise SafetyFence("STOPPED proof omits or adds a source account")
        elif proof.accounts:
            raise SafetyFence("DATA retirement must not manufacture mutation-account proofs")
        for account in proof.accounts:
            if not max(transition.requested_at, now - age) <= account.observed_at <= proof.observed_at:
                raise SafetyFence("Account proof is stale, inconsistent or future-dated")
            account.require_flat()
        if self._has_unresolved(connection, transition.source):
            raise SafetyFence("Durable pending or UNKNOWN dispatch blocks source retirement")
        if connection.execute("SELECT 1 FROM authority_fences WHERE authority_id=? AND resolved_at IS NULL LIMIT 1", (self.authority_id,)).fetchone() is not None:
            raise SafetyFence("An unresolved source safety fence blocks handover")
        self.stop_verifier.verify_stop_in(connection, transition.source, proof)

    def _retire(self, transition_id: str, proof: StopProof) -> None:
        with self.db.transaction() as connection:
            row = self._row(connection)
            transition = self._transition(connection, transition_id)
            if row["transition_id"] != transition_id or transition.state != "RECONCILING":
                raise Conflict("Only a reconciling source can be retired")
            self._validate_stop(connection, transition, proof)
            revision = int(row["revision"]) + 1
            digest = sha256_json(proof)
            connection.execute("UPDATE context_transitions SET state='STOPPED_OLD',stopped_proof_json=?,stopped_proof_sha256=? WHERE transition_id=?", (proof.model_dump_json(), digest, transition_id))
            connection.execute("UPDATE authority_state SET state='STOPPED_OLD',armed=0,lease_status='RETIRED',lease_epoch=NULL,lease_profile_version=NULL,revision=?,updated_at=? WHERE authority_id=?", (revision, utc_now().isoformat(), self.authority_id))
            self._audit(connection, revision, "SOURCE_LEASE_RETIRED", {"transition_id": transition_id, "proof_sha256": digest})

    def run_transition(self, transition_id: str, drain: DrainDriver, initializer: ContextInitializer) -> TransitionRecord:
        """Blocking worker barrier, never a Tk callback; failures remain durable.

        A failure result is returned so callers can show BLOCKED_DRAIN without
        discarding the stable transition identity or attempting target startup.
        """
        self._check_owner()
        if not self._operation_lock.acquire(blocking=False):
            raise Conflict("A context operation is already running")
        try:
            deadline = time.monotonic() + self.config.transition_timeout_seconds
            current = self.transition(transition_id)
            if current.state == "READY_UNARMED":
                return current
            if current.state in {"REQUESTED", "BLOCKED_DRAIN"}:
                try:
                    current = self._advance(transition_id, {"REQUESTED", "BLOCKED_DRAIN"}, "QUIESCING")
                    drain.quiesce(current.source)
                    self._require_before(deadline)
                    self._advance(transition_id, {"QUIESCING"}, "DRAINING")
                    drain.drain(current.source)
                    self._require_before(deadline)
                    self._advance(transition_id, {"DRAINING"}, "RECONCILING")
                    proof = drain.reconcile(current.source)
                    self._require_before(deadline)
                    self._retire(transition_id, proof)
                except Exception as exc:
                    return self._advance(transition_id, {"QUIESCING", "DRAINING", "RECONCILING"}, "BLOCKED_DRAIN", type(exc).__name__)
            elif current.state not in {"STOPPED_OLD", "RESETTING", "FAILED_INITIALIZATION"}:
                raise Conflict("Interrupted source stages require a new reviewed recovery transition")
            try:
                current = self._advance(transition_id, {"STOPPED_OLD", "RESETTING", "FAILED_INITIALIZATION"}, "RESETTING")
                initializer.reset_transients(current.source, current.target)
                self._require_before(deadline)
                self._advance(transition_id, {"RESETTING"}, "INITIALIZING_NEW")
                target_profile = self.profile(current.target) if current.target.product_track == "TRADING" else None
                if target_profile is not None:
                    target_profile.require_current()
                initializer.initialize(current.target, target_profile)
                self._require_before(deadline)
                with self.db.transaction() as connection:
                    row = self._row(connection)
                    if row["transition_id"] != transition_id or row["state"] != "INITIALIZING_NEW" or row["lease_status"] != "RETIRED":
                        raise SafetyFence("Target initialization cannot overlap an old mutation lease")
                    self._validate_target_commit(connection, current, target_profile)
                    revision = int(row["revision"]) + 1
                    trading = current.target.product_track == "TRADING"
                    connection.execute("UPDATE authority_state SET context_json=?,state='READY_UNARMED',armed=0,revision=?,lease_epoch=?,lease_profile_version=?,lease_status=?,updated_at=? WHERE authority_id=?",
                                       (current.target.model_dump_json(), revision, current.target.epoch if trading else None, target_profile.version if target_profile is not None else None, "ACTIVE" if trading else "NONE", utc_now().isoformat(), self.authority_id))
                    connection.execute("UPDATE context_transitions SET state='READY_UNARMED',failure_code=NULL WHERE transition_id=?", (transition_id,))
                    self._audit(connection, revision, "TARGET_READY_UNARMED", {"transition_id": transition_id, "context": current.target, "lease_acquired": trading})
                    return self._transition(connection, transition_id)
            except Exception as exc:
                return self._advance(transition_id, {"RESETTING", "INITIALIZING_NEW"}, "FAILED_INITIALIZATION", type(exc).__name__)
        finally:
            self._operation_lock.release()

    @staticmethod
    def _require_before(deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise SafetyFence("Context transition exceeded its original monotonic deadline")

    def _validate_target_commit(self, connection: sqlite3.Connection, transition: TransitionRecord, profile: VerifiedAccountProfile | None) -> None:
        if self._has_unresolved(connection, transition.source):
            raise SafetyFence("A late source fact reopened the dispatch fence")
        if connection.execute("SELECT 1 FROM authority_fences WHERE authority_id=? AND resolved_at IS NULL LIMIT 1", (self.authority_id,)).fetchone() is not None:
            raise SafetyFence("A late safety fence blocks target activation")
        self._validate_retired_source_cut(connection, transition.transition_id)
        if profile is not None:
            profile.require_current()
            current = self.profile_in(connection, transition.target)
            if sha256_json(current) != sha256_json(profile):
                raise SafetyFence("Target profile changed during initialization")

    def _validate_retired_source_cut(self, connection: sqlite3.Connection, transition_id: str) -> None:
        row = connection.execute("SELECT source_json,stopped_proof_json FROM context_transitions WHERE authority_id=? AND transition_id=?", (self.authority_id, transition_id)).fetchone()
        if row is None or row["stopped_proof_json"] is None:
            raise SafetyFence("Target activation requires the durable source retirement proof")
        source = ContextKey.model_validate_json(row["source_json"])
        proof = StopProof.model_validate_json(row["stopped_proof_json"])
        self.stop_verifier.verify_stop_in(connection, source, proof)

    def revise_failed_target(self, transition_id: str, target: ContextKey, expected_revision: int, *, deadline: datetime | None = None, connection: sqlite3.Connection | None = None) -> TransitionRecord:
        """Explicitly revise an uninitialized target while the source stays retired."""
        command_deadline = require_utc(deadline) if deadline is not None else None
        received_at = utc_now()
        if command_deadline is not None and command_deadline > received_at + timedelta(seconds=60):
            raise ValidationFailure("Command deadline may not exceed 60 seconds")
        monotonic_deadline = time.monotonic() + max(0, (command_deadline - received_at).total_seconds()) if command_deadline is not None else None
        if connection is not None and not connection.in_transaction:
            raise ValidationFailure("A supplied command connection must already be transactional")
        with self.db.transaction() if connection is None else nullcontext(connection) as connection:
            row = self._row(connection)
            if command_deadline is not None:
                self._check_command_deadline(command_deadline, cast(float, monotonic_deadline), received_at)
            old = self._transition(connection, transition_id)
            if row["revision"] != expected_revision or row["transition_id"] != transition_id:
                raise Conflict("Failed-target preview is stale")
            if old.state != "FAILED_INITIALIZATION" or row["lease_status"] != "RETIRED":
                raise SafetyFence("Only an uninitialized target with a retired source can be revised")
            if target.authority_id != self.authority_id or target.epoch != old.target.epoch:
                raise ValidationFailure("Revised target must retain the reserved authority and epoch")
            if target.product_track == "TRADING":
                self.profile_in(connection, target).require_current()
            revision = int(row["revision"]) + 1
            connection.execute("UPDATE context_transitions SET target_json=? WHERE transition_id=?", (target.model_dump_json(), transition_id))
            connection.execute("UPDATE authority_state SET revision=?,updated_at=? WHERE authority_id=?", (revision, utc_now().isoformat(), self.authority_id))
            self._audit(connection, revision, "FAILED_TARGET_REVISED", {"transition_id": transition_id, "target": target})
            return self._transition(connection, transition_id)

    def assert_mutation(self, connection: sqlite3.Connection, context: ContextKey, profile_version: int | None = None, action: Literal["ENTRY", "RECOVERY"] = "ENTRY") -> int:
        row = self._row(connection)
        actual = ContextKey.model_validate_json(row["context_json"])
        if actual != context or context.product_track != "TRADING":
            raise SafetyFence("Mutation requires the exact active TRADING ContextKey")
        if row["lease_status"] not in {"ACTIVE", "RECOVERY"} or row["lease_epoch"] != context.epoch:
            raise SafetyFence("Mutation lease is absent, retired or belongs to another epoch")
        if profile_version is not None and row["lease_profile_version"] != profile_version:
            raise SafetyFence("Mutation profile version is stale")
        profile = self.profile_in(connection, context, int(row["lease_profile_version"]))
        from oandafly.domain.capabilities import RuntimeMode
        mode = RuntimeMode(self.config.runtime_mode)
        mode.require_environment(str(context.environment))
        if mode == RuntimeMode.SHADOW:
            raise SafetyFence("SHADOW has no entry, close or cancel capability")
        if profile.api_binding == "PAPER":
            if mode not in {RuntimeMode.PAPER, RuntimeMode.REPLAY}:
                raise SafetyFence("Synthetic execution is isolated to PAPER/REPLAY")
        else:
            if not mode.broker_mutation or self.mutation_authorizer is None:
                raise SafetyFence("Broker execution requires an independently verified authorization grant")
            self.mutation_authorizer.require(context, profile, action)
        if action == "ENTRY":
            if row["state"] != "READY_UNARMED" or not row["armed"] or row["lease_status"] != "ACTIVE":
                raise SafetyFence("Entry mutation requires explicit arming of the ready active lease")
            profile.require_current()
            if connection.execute("SELECT 1 FROM authority_fences WHERE authority_id=? AND resolved_at IS NULL LIMIT 1", (self.authority_id,)).fetchone() is not None:
                raise SafetyFence("A safety fence blocks entry mutation")
        elif action == "RECOVERY":
            if row["state"] not in {"READY_UNARMED", "REQUESTED", "QUIESCING", "DRAINING", "RECONCILING", "BLOCKED_DRAIN", "RECOVERY_REQUIRED"}:
                raise SafetyFence("Recovery is restricted to the unretired source lease")
        else:
            raise ValidationFailure("Unknown mutation capability")
        return int(row["revision"])

    def arm(self, expected_revision: int, command_id: str, readiness: ReadinessVerifier, deadline: datetime) -> AuthoritySnapshot:
        UUID(command_id)
        deadline = require_utc(deadline)
        now = utc_now()
        received_at = now
        monotonic_deadline = time.monotonic() + (deadline - received_at).total_seconds()
        if deadline <= now or deadline > now + timedelta(seconds=60):
            raise SafetyFence("Arming requires an unexpired command deadline of at most 60 seconds")
        if not self._operation_lock.acquire(blocking=False):
            raise Conflict("A context operation is already running")
        try:
            snapshot = self.snapshot()
            digest = sha256_json({"expected_revision": expected_revision, "deadline": deadline})
            with self.db.read() as connection:
                previous = connection.execute("SELECT command_hash FROM authority_commands WHERE authority_id=? AND command_id=?", (self.authority_id, command_id)).fetchone()
                if previous is not None:
                    if previous["command_hash"] != digest:
                        raise Conflict("Arm command UUID was reused with a different payload")
                    return self._snapshot(connection, self._row(connection))
            if snapshot.revision != expected_revision or snapshot.state != "READY_UNARMED" or snapshot.context.product_track != "TRADING" or snapshot.lease_status != "ACTIVE":
                raise SafetyFence("Arming requires the reviewed current ready TRADING lease")
            profile = self.profile(snapshot.context, snapshot.lease_profile_version)
            profile.require_current()
            proof = readiness.verify(snapshot.context, profile)
            with self.db.transaction() as connection:
                row = self._row(connection)
                now = utc_now()
                self._check_command_deadline(deadline, monotonic_deadline, received_at)
                if now >= deadline or row["revision"] != expected_revision or row["state"] != "READY_UNARMED" or row["lease_status"] != "ACTIVE":
                    raise SafetyFence("Arming deadline or authority state changed during validation")
                if proof.context_sha256 != snapshot.context.fingerprint or proof.profile_version != profile.version:
                    raise SafetyFence("Readiness proof belongs to another context/profile")
                if not now - timedelta(seconds=self.config.proof_max_age_seconds) <= proof.observed_at <= now:
                    raise SafetyFence("Readiness evidence is stale or future-dated")
                if self._has_unresolved(connection, snapshot.context):
                    raise SafetyFence("Pending or UNKNOWN dispatch blocks arming")
                if connection.execute("SELECT 1 FROM authority_fences WHERE authority_id=? AND resolved_at IS NULL LIMIT 1", (self.authority_id,)).fetchone() is not None:
                    raise SafetyFence("Unresolved safety fences block arming")
                if row["transition_id"] is None:
                    raise SafetyFence("Arming requires a durable source retirement transition")
                self._validate_retired_source_cut(connection, str(row["transition_id"]))
                profile.require_current(now)
                revision = int(row["revision"]) + 1
                connection.execute("UPDATE authority_state SET armed=1,revision=?,updated_at=? WHERE authority_id=?", (revision, now.isoformat(), self.authority_id))
                connection.execute("INSERT INTO authority_commands VALUES(?,?,?,?,?)", (self.authority_id, command_id, digest, "ARM", revision))
                self._audit(connection, revision, "EXPLICITLY_ARMED", {"command_id": command_id, "readiness_sha256": sha256_json(proof)})
                return self._snapshot(connection, self._row(connection))
        finally:
            self._operation_lock.release()

    def resume_verified(self, expected_revision: int, readiness: ReadinessVerifier) -> AuthoritySnapshot:
        """Reacquire entry eligibility after full native recovery; caller must then arm.

        Preserves the source account and epoch. It cannot erase a pending intent,
        unresolved fence, or an interrupted context switch. It never asserts flat.
        """
        if not self._operation_lock.acquire(blocking=False):
            raise Conflict("An authority operation is already running")
        try:
            snapshot = self.snapshot()
            if (snapshot.revision != expected_revision or snapshot.state != "RECOVERY_REQUIRED"
                    or snapshot.lease_status != "RECOVERY" or snapshot.requested_context is not None
                    and snapshot.requested_context != snapshot.context):
                raise SafetyFence("Only the same recovered trading context may resume")
            profile = self.profile(snapshot.context, snapshot.lease_profile_version)
            profile.require_current()
            if self.mutation_authorizer is None:
                raise SafetyFence("Unattended resume requires an independent authorization")
            self.mutation_authorizer.require(snapshot.context, profile, "ENTRY")
            proof = readiness.verify(snapshot.context, profile)
            with self.db.transaction() as connection:
                row = self._row(connection)
                if row["revision"] != expected_revision or self._has_unresolved(connection, snapshot.context):
                    raise SafetyFence("Recovery changed or unresolved mutations remain")
                if connection.execute("SELECT 1 FROM authority_fences WHERE authority_id=? AND resolved_at IS NULL",
                                      (self.authority_id,)).fetchone():
                    raise SafetyFence("A persistent authority fence prevents resume")
                now = utc_now()
                if (proof.context_sha256 != snapshot.context.fingerprint or proof.profile_version != profile.version
                        or not now - timedelta(seconds=self.config.proof_max_age_seconds) <= proof.observed_at <= now):
                    raise SafetyFence("Recovered readiness proof is stale or incorrectly scoped")
                revision = expected_revision + 1
                connection.execute("UPDATE authority_state SET state='READY_UNARMED',lease_status='ACTIVE',armed=0,revision=?,updated_at=? WHERE authority_id=?",
                                   (revision, now.isoformat(), self.authority_id))
                self._audit(connection, revision, "RECOVERY_VERIFIED_FOR_RESUME", {"proof_sha256": sha256_json(proof)})
                return self._snapshot(connection, self._row(connection))
        finally:
            self._operation_lock.release()

    @staticmethod
    def _check_command_deadline(deadline: datetime, monotonic_deadline: float, received_at: datetime) -> None:
        now = utc_now()
        if now < received_at or now >= deadline or time.monotonic() >= monotonic_deadline:
            raise SafetyFence("Command deadline expired or the wall clock moved backwards")

    def disarm(self, reason: str = "EXPLICIT_USER_COMMAND", *, connection: sqlite3.Connection | None = None) -> AuthoritySnapshot:
        if not reason or len(reason) > 200:
            raise ValidationFailure("A bounded disarm reason is required")
        if connection is not None and not connection.in_transaction:
            raise ValidationFailure("A supplied disarm connection must already be transactional")
        with self.db.transaction() if connection is None else nullcontext(connection) as connection:
            row = self._row(connection)
            revision = int(row["revision"]) + 1
            connection.execute("UPDATE authority_state SET armed=0,revision=?,updated_at=? WHERE authority_id=?", (revision, utc_now().isoformat(), self.authority_id))
            self._audit(connection, revision, "DISARMED", {"reason": reason})
            return self._snapshot(connection, self._row(connection))

    def require_recovery(self, reason: str, *, connection: sqlite3.Connection | None = None) -> AuthoritySnapshot:
        """Fence unexpected worker/control failure without inventing a flat book."""
        if not reason or len(reason) > 200:
            raise ValidationFailure("A bounded recovery reason is required")
        if connection is not None and not connection.in_transaction:
            raise ValidationFailure("A supplied recovery connection must already be transactional")
        with self.db.transaction() if connection is None else nullcontext(connection) as connection:
            row = self._row(connection)
            revision = int(row["revision"]) + 1
            lease_status = "RECOVERY" if row["lease_status"] in {"ACTIVE", "RECOVERY"} else row["lease_status"]
            connection.execute("UPDATE authority_state SET state='RECOVERY_REQUIRED',armed=0,lease_status=?,revision=?,updated_at=? WHERE authority_id=?",
                               (lease_status, revision, utc_now().isoformat(), self.authority_id))
            if row["transition_id"] is not None:
                connection.execute("UPDATE context_transitions SET state='RECOVERY_REQUIRED',failure_code=? WHERE transition_id=? AND authority_id=?",
                                   (reason, row["transition_id"], self.authority_id))
            self._audit(connection, revision, "CONTROL_RECOVERY_REQUIRED", {"reason": reason})
            return self._snapshot(connection, self._row(connection))

    def add_fence(self, code: str, detail: str, scope_context: ContextKey) -> str:
        if scope_context.authority_id != self.authority_id or not code or len(code) > 100 or len(detail) > 1000:
            raise ValidationFailure("Invalid scoped safety fence")
        fence_id = str(uuid4())
        with self.db.transaction() as connection:
            row = self._row(connection)
            connection.execute("INSERT INTO authority_fences VALUES(?,?,?,?,?,?,?,?)", (fence_id, self.authority_id, scope_context.fingerprint, code, detail, utc_now().isoformat(), None, None))
            revision = int(row["revision"]) + 1
            connection.execute("UPDATE authority_state SET armed=0,revision=? WHERE authority_id=?", (revision, self.authority_id))
            self._audit(connection, revision, "SAFETY_FENCE_OPENED", {"fence_id": fence_id, "code": code, "context": scope_context})
        return fence_id

    def resolve_fence(self, fence_id: str, evidence_hash: str, *, context: ContextKey) -> None:
        if len(evidence_hash) != 64 or any(c not in "0123456789abcdef" for c in evidence_hash):
            raise ValidationFailure("Fence resolution requires a SHA-256 evidence digest")
        with self.db.transaction() as connection:
            row = self._row(connection)
            fence = connection.execute("SELECT * FROM authority_fences WHERE fence_id=? AND authority_id=?", (fence_id, self.authority_id)).fetchone()
            if fence is None or fence["context_sha256"] != context.fingerprint:
                raise SafetyFence("A fence can only be resolved for its exact origin context")
            if fence["resolved_at"] is not None:
                if fence["resolution_sha256"] != evidence_hash:
                    raise Conflict("Fence resolution evidence is immutable")
                return
            revision = int(row["revision"]) + 1
            connection.execute("UPDATE authority_fences SET resolved_at=?,resolution_sha256=? WHERE fence_id=?", (utc_now().isoformat(), evidence_hash, fence_id))
            connection.execute("UPDATE authority_state SET revision=? WHERE authority_id=?", (revision, self.authority_id))
            self._audit(connection, revision, "SAFETY_FENCE_RESOLVED", {"fence_id": fence_id, "evidence_sha256": evidence_hash})

    def close(self) -> None:
        if self._closed:
            return
        if not self._operation_lock.acquire(blocking=False):
            raise Conflict("Cannot close the authority while a handover or arm operation runs")
        try:
            self.disarm("OWNER_CLOSED_UNARMED")
            self._closed = True
            self._file_lock.close()
        finally:
            self._operation_lock.release()
