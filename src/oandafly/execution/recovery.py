"""Audited, one-shot native order cancellation and exact TradeID reduction.

Recovery permissions preserve the original source lease during drain. They never
permit a new entry, an account transfer, leverage changes, or a position reversal.
An ambiguous recovery invocation is retained until its native lineage is proved.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID, uuid4

from oandafly.domain.context import ContextKey
from oandafly.domain.errors import Conflict, SafetyFence, TransportAmbiguous, ValidationFailure
from oandafly.domain.primitives import StrictModel, decimal_text, money, sha256_json, utc_now
from oandafly.execution.outbox import Outbox
from oandafly.execution.reconciler import OutboxReducer, RateGovernor, _mapping, _native_id


class RecoveryTransport(Protocol):
    async def close_trade(
        self, account_id: str, trade_id: str, units: Decimal, *, before_send: Callable[[], None],
    ) -> Mapping[str, object]:
        """Close only the exact positive unit quantity of this immutable TradeID."""

    async def cancel_order(
        self, account_id: str, order_id: str, *, before_send: Callable[[], None],
    ) -> Mapping[str, object]:
        """Cancel this exact native order without replacing it."""


class RecoveryRecord(StrictModel):
    action_id: str
    context: ContextKey
    account_id: str
    profile_version: int
    operation: Literal["CLOSE_TRADE", "CANCEL_ORDER"]
    target_id: str
    units: Decimal | None
    source_revision: str
    state: Literal["PREPARED", "DISPATCH_STARTED", "UNKNOWN", "RESOLVED", "NOT_SENT"]
    invocation_marker: str | None
    native_root_id: str | None


class RecoveryRouter:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS execution_recovery (
        action_id TEXT PRIMARY KEY,
        authority_id TEXT NOT NULL,
        environment TEXT NOT NULL,
        account_id TEXT NOT NULL,
        context_json TEXT NOT NULL,
        profile_version INTEGER NOT NULL,
        operation TEXT NOT NULL,
        target_id TEXT NOT NULL,
        units TEXT,
        source_revision TEXT NOT NULL,
        intent_hash TEXT NOT NULL,
        state TEXT NOT NULL,
        invocation_marker TEXT,
        native_root_id TEXT,
        response_hash TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS execution_one_recovery_per_target
      ON execution_recovery(environment,account_id,operation,target_id)
      WHERE state IN ('PREPARED','DISPATCH_STARTED','UNKNOWN');
    """

    def __init__(
        self, outbox: Outbox, *, reducer: OutboxReducer,
        transport: RecoveryTransport, governor: RateGovernor, timeout_seconds: float = 15.0,
    ) -> None:
        if not 0 < timeout_seconds <= 60:
            raise ValueError("invalid recovery timeout")
        self.outbox = outbox
        self.db = outbox.db
        self.reducer = reducer
        self.transport = transport
        self.governor = governor
        self.timeout_seconds = timeout_seconds
        self.db.initialize(self.SCHEMA, component=__name__)

    @staticmethod
    def _record(row: object) -> RecoveryRecord:
        import sqlite3
        if not isinstance(row, sqlite3.Row):
            raise ValidationFailure("unknown local recovery action")
        return RecoveryRecord(
            action_id=row["action_id"], context=ContextKey.model_validate_json(row["context_json"]),
            account_id=row["account_id"], profile_version=row["profile_version"],
            operation=row["operation"], target_id=row["target_id"],
            units=None if row["units"] is None else money(row["units"]),
            source_revision=row["source_revision"], state=row["state"],
            invocation_marker=row["invocation_marker"], native_root_id=row["native_root_id"],
        )

    def get(self, action_id: str) -> RecoveryRecord:
        with self.db.read() as connection:
            return self._record(connection.execute("SELECT * FROM execution_recovery WHERE action_id=?", (action_id,)).fetchone())

    def prepare(
        self, *, context: ContextKey, account_id: str, profile_version: int,
        operation: Literal["CLOSE_TRADE", "CANCEL_ORDER"], target_id: str,
        units: Decimal | None = None, action_id: UUID | None = None,
    ) -> RecoveryRecord:
        # Literal annotations do not validate direct Python calls. Reject unknown
        # operations before persisting an unexecutable recovery reservation.
        if operation not in {"CLOSE_TRADE", "CANCEL_ORDER"}:
            raise ValidationFailure("Unknown recovery operation")
        identity = str(action_id or uuid4())
        _native_id(target_id)
        if operation == "CLOSE_TRADE" and (units is None or money(units) <= 0):
            raise ValidationFailure("a recovery close requires an explicit positive unit quantity")
        if operation == "CANCEL_ORDER" and units is not None:
            raise ValidationFailure("order cancellation does not accept a unit quantity")
        payload = {"context": context.model_dump(mode="json"), "account_id": account_id,
                   "profile_version": profile_version, "operation": operation, "target_id": target_id,
                   "units": None if units is None else decimal_text(units)}
        digest = sha256_json(payload)
        with self.db.transaction() as connection:
            previous = connection.execute("SELECT * FROM execution_recovery WHERE action_id=?", (identity,)).fetchone()
            if previous is not None:
                if previous["intent_hash"] != digest:
                    raise Conflict("a recovery UUID cannot be reused for another action")
                return self._record(previous)
            self.outbox.authority.assert_mutation(connection, context, profile_version, action="RECOVERY")
            profile = self.outbox.authority.profile_in(connection, context, profile_version)
            if account_id not in profile.account_ids:
                raise SafetyFence("recovery target account is outside the retained source profile")
            if operation == "CLOSE_TRADE":
                target = connection.execute(
                    "SELECT current_units,last_transaction_id FROM execution_trades WHERE environment=? AND account_id=? AND trade_id=?",
                    (str(context.environment), account_id, target_id),
                ).fetchone()
                if target is None or units is None or units > abs(money(target["current_units"])):
                    raise SafetyFence("recovery close exceeds the known exact trade exposure")
                revision = f"{target['last_transaction_id']}:{target['current_units']}"
            else:
                target = connection.execute(
                    "SELECT state,last_transaction_id,trade_id FROM execution_orders WHERE environment=? AND account_id=? AND order_id=?",
                    (str(context.environment), account_id, target_id),
                ).fetchone()
                if target is None or target["state"] != "PENDING":
                    raise SafetyFence("recovery cancellation requires a known pending native order")
                if target["trade_id"] is not None:
                    raise SafetyFence("protective orders must remain attached until their trade is closed")
                revision = f"{target['last_transaction_id']}:{target['state']}"
            now = utc_now().isoformat()
            connection.execute(
                "INSERT INTO execution_recovery VALUES(?,?,?,?,?,?,?,?,?,?,?,'PREPARED',NULL,NULL,NULL,?,?)",
                (identity, context.authority_id, str(context.environment), account_id, context.model_dump_json(),
                 profile_version, operation, target_id, None if units is None else decimal_text(units),
                 revision, digest, now, now),
            )
        return self.get(identity)

    def _validate_in(self, connection: object, record: RecoveryRecord) -> None:
        import sqlite3
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("recovery validation requires the shared SQL transaction")
        self.outbox.authority.assert_mutation(connection, record.context, record.profile_version, action="RECOVERY")
        if record.operation == "CLOSE_TRADE":
            row = connection.execute(
                "SELECT last_transaction_id,current_units FROM execution_trades WHERE environment=? AND account_id=? AND trade_id=?",
                (str(record.context.environment), record.account_id, record.target_id),
            ).fetchone()
            revision = None if row is None else f"{row['last_transaction_id']}:{row['current_units']}"
        else:
            row = connection.execute(
                "SELECT last_transaction_id,state FROM execution_orders WHERE environment=? AND account_id=? AND order_id=?",
                (str(record.context.environment), record.account_id, record.target_id),
            ).fetchone()
            revision = None if row is None else f"{row['last_transaction_id']}:{row['state']}"
        if revision != record.source_revision:
            raise SafetyFence("native recovery target changed after preparation")

    async def dispatch(self, action_id: str) -> RecoveryRecord:
        initial = self.get(action_id)
        if initial.state != "PREPARED":
            raise SafetyFence("recovery actions have exactly one dispatch opportunity")
        deadline = time.monotonic() + self.timeout_seconds
        await self.governor.acquire(kind="request", deadline=deadline)
        with self.db.transaction() as connection:
            record = self._record(connection.execute("SELECT * FROM execution_recovery WHERE action_id=?", (action_id,)).fetchone())
            self._validate_in(connection, record)
            if connection.execute(
                "UPDATE execution_recovery SET state='DISPATCH_STARTED',updated_at=? WHERE action_id=? AND state='PREPARED'",
                (utc_now().isoformat(), action_id),
            ).rowcount != 1:
                raise Conflict("another caller claimed this recovery action")
        invoked = False

        def before_send() -> None:
            nonlocal invoked
            if invoked:
                raise SafetyFence("recovery transport attempted a second invocation")
            with self.db.transaction() as connection:
                try:
                    if time.monotonic() >= deadline:
                        raise SafetyFence("recovery deadline expired")
                    self._validate_in(connection, record)
                except (SafetyFence, ValidationFailure, Conflict):
                    connection.execute(
                        "UPDATE execution_recovery SET state='NOT_SENT',updated_at=? WHERE action_id=? AND state='DISPATCH_STARTED' AND invocation_marker IS NULL",
                        (utc_now().isoformat(), action_id),
                    )
                    prevented = True
                else:
                    if connection.execute(
                        "UPDATE execution_recovery SET invocation_marker=? WHERE action_id=? AND invocation_marker IS NULL AND state='DISPATCH_STARTED'",
                        (str(uuid4()), action_id),
                    ).rowcount != 1:
                        raise SafetyFence("recovery final invocation CAS failed")
                    prevented = False
            if prevented:
                raise SafetyFence("recovery guard durably prevented native transmission")
            invoked = True

        returned = False
        try:
            async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                if record.operation == "CLOSE_TRADE":
                    if record.units is None:
                        raise SafetyFence("persisted close intent has no unit quantity")
                    response = await self.transport.close_trade(record.account_id, record.target_id, record.units, before_send=before_send)
                else:
                    response = await self.transport.cancel_order(record.account_id, record.target_id, before_send=before_send)
            returned = True
            if not invoked:
                raise TransportAmbiguous("recovery transport skipped its invocation guard")
            return self.bind_response(action_id, response)
        # Preserve process interrupts; only operational failures are ambiguous
        # transport outcomes. A crashed invocation stays fenced durably.
        except (Exception, asyncio.CancelledError) as exc:
            with self.db.transaction() as connection:
                # Without a committed invocation marker the transport failed before its
                # final guard, so nothing was transmitted. A transport that returned
                # without invoking the guard remains ambiguous.
                connection.execute(
                    "UPDATE execution_recovery SET state=CASE WHEN invocation_marker IS NULL AND ? THEN 'NOT_SENT' ELSE 'UNKNOWN' END,"
                    "updated_at=? WHERE action_id=? AND state='DISPATCH_STARTED'",
                    (int(not returned), utc_now().isoformat(), action_id),
                )
            if isinstance(exc, asyncio.CancelledError):
                raise
            if self.get(action_id).state == "NOT_SENT":
                raise SafetyFence("recovery invocation was proven unsent") from exc
            raise TransportAmbiguous("recovery outcome remains fenced until exact native lineage is reconciled") from exc

    def bind_response(self, action_id: str, response: Mapping[str, object]) -> RecoveryRecord:
        """Also accepts the exact response reconstructed from authoritative REST facts."""
        with self.db.transaction() as connection:
            record = self._record(connection.execute("SELECT * FROM execution_recovery WHERE action_id=?", (action_id,)).fetchone())
            if record.invocation_marker is None or record.state not in {"DISPATCH_STARTED", "UNKNOWN", "RESOLVED"}:
                raise SafetyFence("native lineage cannot bind an uninvoked recovery action")
            if record.operation == "CANCEL_ORDER":
                root = _mapping(response.get("orderCancelTransaction"))
                if root is None or root.get("type") != "ORDER_CANCEL" or root.get("orderID") != record.target_id:
                    raise SafetyFence("cancellation response does not identify the exact requested order")
                transactions = [root]
                root_id = _native_id(root.get("id"))
            else:
                root = _mapping(response.get("orderCreateTransaction"))
                fill = _mapping(response.get("orderFillTransaction"))
                if root is None or fill is None:
                    raise TransportAmbiguous("close response has no proven native creation and fill")
                close = _mapping(root.get("tradeClose"))
                if root.get("type") != "MARKET_ORDER" or root.get("positionFill") != "REDUCE_ONLY":
                    raise SafetyFence("recovery response is not a native reduce-only market close")
                if close is None or close.get("tradeID") != record.target_id:
                    raise SafetyFence("native close creation does not identify the exact requested trade")
                root_id = _native_id(root.get("id"))
                if fill.get("type") != "ORDER_FILL" or fill.get("orderID") != root_id or fill.get("tradeOpened") is not None:
                    raise SafetyFence("recovery fill could open exposure or belongs to another native order")
                reduced = _mapping(fill.get("tradeReduced"))
                closed = fill.get("tradesClosed", [])
                if not isinstance(closed, list):
                    raise ValidationFailure("invalid native closed-trade collection")
                subtrees = [_mapping(item) for item in closed]
                if reduced is not None:
                    subtrees.append(reduced)
                if len(subtrees) != 1 or subtrees[0] is None or subtrees[0].get("tradeID") != record.target_id:
                    raise SafetyFence("recovery touched a trade outside its exact target")
                if abs(money(subtrees[0].get("units"))) != record.units:  # type: ignore[arg-type]
                    raise SafetyFence("native recovery fill quantity differs from the exact authorized quantity")
                transactions = [root, fill]
            if record.native_root_id not in (None, root_id):
                raise SafetyFence("a recovery action cannot be rebound to another native root")
            if int(root_id) <= int(record.source_revision.split(":", 1)[0]):
                raise SafetyFence("historical native activity cannot resolve a later recovery invocation")
            for transaction in transactions:
                result = self.reducer.ingest_in(
                    connection, transaction, environment=str(record.context.environment),
                    account_id=record.account_id, source="REST",
                )
                if result.quarantined:
                    connection.execute("UPDATE execution_recovery SET state='UNKNOWN' WHERE action_id=?", (action_id,))
                    return self._record(connection.execute("SELECT * FROM execution_recovery WHERE action_id=?", (action_id,)).fetchone())
            connection.execute(
                "UPDATE execution_recovery SET state='RESOLVED',native_root_id=?,response_hash=?,updated_at=? WHERE action_id=?",
                (root_id, sha256_json(dict(response)), utc_now().isoformat(), action_id),
            )
        return self.get(action_id)

    def pending(self, authority_id: str) -> tuple[RecoveryRecord, ...]:
        with self.db.read() as connection:
            return tuple(self._record(row) for row in connection.execute(
                "SELECT * FROM execution_recovery WHERE authority_id=? AND state IN ('PREPARED','DISPATCH_STARTED','UNKNOWN')",
                (authority_id,),
            ).fetchall())
