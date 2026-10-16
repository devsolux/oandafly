"""One-shot transport and a coverage-ordered, native-fact reducer.

The private transaction stream delivers observations, not a historical cursor.
Feed REST ``/transactions/sinceid`` results through the same reducer after any
disconnect. A quiet stream, a 404, or a reused client ID is never no-send proof.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol, cast
from uuid import UUID, uuid4

from oandafly.domain.financial import FINANCIAL
from oandafly.domain.errors import Conflict, SafetyFence, TransportAmbiguous, ValidationFailure
from oandafly.domain.primitives import canonical_json, decimal_text, money, sha256_json, utc_now
from oandafly.execution.outbox import (
    DispatchState,
    OrderRequest,
    Outbox,
    OutboxRecord,
    OutboxStatus,
    native_payload,
)
from oandafly.storage.ledger import Ledger


class RateGovernor(Protocol):
    async def acquire(self, *, kind: Literal["request", "connection"] = "request", deadline: float) -> None:
        """Acquire the process-shared request budget before its monotonic deadline."""


class OrderTransport(Protocol):
    async def prepare_order(self, account_id: str) -> None:
        """Acquire transport resources without mutation; the gateway validates afterward."""

    async def send_order_bytes(self, account_id: str, payload: bytes) -> Mapping[str, object]:
        """Send these exact bytes once. No serialization, mutation or retry is permitted."""


@dataclass(frozen=True, slots=True)
class Reduction:
    transaction_id: str
    duplicate: bool
    intent_id: str | None
    quarantined: bool


@dataclass(frozen=True, slots=True)
class _DispatchSlot:
    account_id: str
    request_hash: str
    payload: bytes
    payload_hash: str


def _mapping(value: object) -> Mapping[str, object] | None:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else None


def _native_id(value: object) -> str:
    if (not isinstance(value, str) or not value.isascii() or not value.isdigit()
            or len(value) > 40 or value != str(int(value))):
        raise ValidationFailure("native transaction and order identifiers must be decimal strings")
    return value


ENTRY_CREATES = frozenset({"MARKET_ORDER", "LIMIT_ORDER", "STOP_ORDER", "MARKET_IF_TOUCHED_ORDER"})
PROTECTION_CREATES = {
    "STOP_LOSS_ORDER": "stop_loss_order_id",
    "GUARANTEED_STOP_LOSS_ORDER": "stop_loss_order_id",
    "TAKE_PROFIT_ORDER": "take_profit_order_id",
    "TRAILING_STOP_LOSS_ORDER": "trailing_stop_order_id",
}


class OutboxReducer:
    PROJECTION_SCHEMA = """
    CREATE TABLE IF NOT EXISTS execution_projection_state (
        environment TEXT NOT NULL, account_id TEXT NOT NULL,
        baseline_cursor TEXT NOT NULL, baseline_json TEXT NOT NULL,
        applied_cursor TEXT NOT NULL, state TEXT NOT NULL,
        PRIMARY KEY(environment, account_id)
    );
    """

    def __init__(self, outbox: Outbox, *, ledger: Ledger) -> None:
        self.outbox = outbox
        self.db = outbox.db
        self.ledger = ledger
        self.db.initialize(self.PROJECTION_SCHEMA, component=__name__ + ".projection")

    def _fence_in(
        self, connection: sqlite3.Connection, environment: str, account_id: str,
        code: str, transaction: Mapping[str, object], record: OutboxRecord | None = None,
    ) -> None:
        identity = sha256_json({"environment": environment, "account": account_id, "code": code, "id": transaction.get("id")})
        connection.execute(
            "INSERT OR IGNORE INTO execution_fences VALUES(?,?,?,?,?,?,NULL,?)",
            (identity, None if record is None else record.request.context.authority_id,
             environment, account_id, code, canonical_json(dict(transaction)), utc_now().isoformat()),
        )

    def _find_record_in(
        self, connection: sqlite3.Connection, environment: str, account_id: str,
        transaction: Mapping[str, object],
    ) -> OutboxRecord | None:
        client: object = transaction.get("clientOrderID")
        extension = _mapping(transaction.get("clientExtensions"))
        if client is None and extension is not None:
            client = extension.get("id")
        by_client = None
        if isinstance(client, str):
            by_client = connection.execute(
                "SELECT * FROM outbox WHERE environment=? AND account_id=? AND client_id=?",
                (environment, account_id, client),
            ).fetchone()
        kind = str(transaction.get("type", ""))
        native_order_id = transaction.get("id") if kind in ENTRY_CREATES or kind.endswith("_ORDER_REJECT") else transaction.get("orderID")
        by_order = None
        if isinstance(native_order_id, str):
            by_order = connection.execute(
                "SELECT * FROM outbox WHERE environment=? AND account_id=? AND native_order_id=?",
                (environment, account_id, native_order_id),
            ).fetchone()
        if by_order is not None and by_client is not None and by_order["intent_id"] != by_client["intent_id"]:
            raise SafetyFence("native order and local client identity identify different intents")
        found = by_order if by_order is not None else by_client
        return None if found is None else self.outbox._record(found)

    def _validate_entry_fact(self, transaction: Mapping[str, object], record: OutboxRecord) -> None:
        if record.dispatch_state in {DispatchState.NOT_DISPATCHED, DispatchState.NOT_SENT}:
            raise SafetyFence("native fact claims a locally proven unsent intent")
        if transaction.get("instrument") not in (None, record.request.instrument):
            raise SafetyFence("native response instrument disagrees with prepared payload")
        if "units" in transaction:
            units = money(transaction["units"])  # type: ignore[arg-type]
            if units == 0 or units * record.request.units <= 0 or abs(units) > abs(record.request.units):
                raise SafetyFence("native entry units disagree with the prepared direction or quantity")
            if str(transaction.get("type", "")) in ENTRY_CREATES and units != record.request.units:
                raise SafetyFence("native creation quantity differs from the exact prepared request")
        if str(transaction.get("type", "")) in ENTRY_CREATES and transaction.get("positionFill") not in (None, "OPEN_ONLY"):
            raise SafetyFence("native creation changed the entry position-fill semantics")
        if transaction.get("type") == "ORDER_FILL":
            opened = _mapping(transaction.get("tradeOpened"))
            if opened is None or transaction.get("tradeReduced") is not None or transaction.get("tradesClosed"):
                raise SafetyFence("an OPEN_ONLY entry fill must create exposure without reducing other trades")
            _native_id(opened.get("tradeID"))
            if money(opened.get("units")) != money(transaction.get("units")):  # type: ignore[arg-type]
                raise SafetyFence("native entry fill and trade-open units disagree")
        extension = _mapping(transaction.get("clientExtensions"))
        if extension is not None and extension.get("id") not in (None, record.client_id):
            raise SafetyFence("native client extension disagrees with the prepared local UUID")

    def ingest(
        self, transaction: Mapping[str, object], *, environment: str, account_id: str,
        source: Literal["STREAM", "REST"] = "STREAM",
    ) -> Reduction:
        with self.db.transaction() as connection:
            return self.ingest_in(connection, transaction, environment=environment, account_id=account_id, source=source)

    def ingest_in(
        self, connection: sqlite3.Connection, transaction: Mapping[str, object], *,
        environment: str, account_id: str, source: Literal["STREAM", "REST"] = "STREAM",
    ) -> Reduction:
        identity = _native_id(transaction.get("id"))
        if transaction.get("accountID") not in (None, account_id):
            self._fence_in(connection, environment, account_id, "NATIVE_ACCOUNT_MISMATCH", transaction)
            return Reduction(identity, False, None, True)
        digest = sha256_json(dict(transaction))
        previous = connection.execute(
            "SELECT digest,reduced FROM execution_native_facts WHERE environment=? AND account_id=? AND transaction_id=?",
            (environment, account_id, identity),
        ).fetchone()
        # Ledger conflicts are durable, so do not raise and roll them back.
        ledger_fact = self.ledger.ingest_native_tx(connection, environment, account_id, transaction, source)
        if ledger_fact.quarantined or previous is not None and previous["digest"] != digest:
            self._fence_in(connection, environment, account_id, "QUARANTINED_NATIVE_FACT", transaction)
            return Reduction(identity, previous is not None, None, True)
        if previous is None:
            connection.execute(
                "INSERT INTO execution_native_facts VALUES(?,?,?,?,?,0,?)",
                (environment, account_id, identity, digest, canonical_json(dict(transaction)), source),
            )
        elif previous["reduced"] == -1:
            # Invalid facts remain durable evidence, never deferred executable work.
            return Reduction(identity, True, None, True)
        record: OutboxRecord | None = None
        connection.execute("SAVEPOINT execution_lineage")
        try:
            record = self._find_record_in(connection, environment, account_id, transaction)
            if record is not None:
                self._validate_entry_fact(transaction, record)
            # A native acknowledgment is evidence of transmission. It is not a
            # historical coverage proof and does not mutate projected positions.
            self._correlate_in(connection, transaction, environment, account_id, record)
        except (SafetyFence, ValidationFailure, ValueError, ArithmeticError) as exc:
            connection.execute("ROLLBACK TO execution_lineage")
            connection.execute("UPDATE execution_native_facts SET reduced=-1 WHERE environment=? AND account_id=? AND transaction_id=?",
                               (environment, account_id, identity))
            self._fence_in(connection, environment, account_id, type(exc).__name__.upper(), transaction, record)
            return Reduction(identity, previous is not None, None if record is None else record.intent_id, True)
        finally:
            connection.execute("RELEASE execution_lineage")
        # STREAM and individual REST order responses are observed facts only.
        # They never certify that all earlier reductions have been received.
        return Reduction(identity, previous is not None, None if record is None else record.intent_id, False)

    def seed_snapshot(self, *, environment: str, account_id: str, cursor: str,
                      trades: Sequence[Mapping[str, object]] = (),
                      orders: Sequence[Mapping[str, object]] = ()) -> None:
        with self.db.transaction() as connection:
            self.seed_snapshot_in(connection, environment=environment, account_id=account_id,
                                  cursor=cursor, trades=trades, orders=orders)

    def seed_snapshot_in(self, connection: sqlite3.Connection, *, environment: str,
                         account_id: str, cursor: str,
                         trades: Sequence[Mapping[str, object]] = (),
                         orders: Sequence[Mapping[str, object]] = ()) -> None:
        """Pin the exact original native execution cut; never reset on restart."""
        cursor = _native_id(cursor)
        baseline = canonical_json({"trades": list(trades), "orders": list(orders)})
        row = connection.execute("SELECT * FROM execution_projection_state WHERE environment=? AND account_id=?",
                                 (environment, account_id)).fetchone()
        if row is not None:
            if row["baseline_cursor"] != cursor or row["baseline_json"] != baseline:
                raise Conflict("Execution baseline is immutable; restore history rather than reset")
            return
        ledger = connection.execute("SELECT baseline_cursor FROM ledger_accounts WHERE environment=? AND account_id=?",
                                    (environment, account_id)).fetchone()
        if ledger is None or ledger["baseline_cursor"] != cursor:
            raise SafetyFence("Execution baseline must bind the exact ledger baseline")
        connection.execute("INSERT INTO execution_projection_state VALUES(?,?,?,?,?,'READY')",
                           (environment, account_id, cursor, baseline, cursor))
        self._restore_baseline_in(connection, environment, account_id, cursor, baseline)


    def _restore_baseline_in(self, connection: sqlite3.Connection, environment: str, account_id: str,
                             cursor: str, baseline: str) -> None:
        scope = (environment, account_id)
        connection.execute("DELETE FROM execution_orders WHERE environment=? AND account_id=?", scope)
        connection.execute("DELETE FROM execution_trades WHERE environment=? AND account_id=?", scope)
        snapshot = json.loads(baseline)
        for trade in snapshot["trades"]:
            identity = _native_id(trade.get("id"))
            from oandafly.domain.instruments import CurrencyInstrument
            CurrencyInstrument(name=trade["instrument"])
            units = money(trade["currentUnits"])
            initial = money(trade["initialUnits"])
            if units == 0 or units * initial <= 0 or abs(units) > abs(initial):
                raise ValidationFailure("Invalid baseline trade units")
            dependent = []
            for key in ("stopLossOrder", "takeProfitOrder", "trailingStopLossOrder"):
                value = (trade.get(key) or trade.get("guaranteedStopLossOrder")) if key == "stopLossOrder" else trade.get(key)
                dependent.append(None if value is None else _native_id(value.get("id")))
            connection.execute("INSERT INTO execution_trades VALUES(?,?,?,NULL,?,?,?,?,?,?,?)",
                               (*scope, identity, trade["instrument"], decimal_text(initial),
                                decimal_text(units), *dependent, cursor))
        for order in snapshot["orders"]:
            identity = _native_id(order.get("id"))
            if order.get("state", "PENDING") != "PENDING":
                raise ValidationFailure("Baseline orders must be native pending orders")
            kind = str(order["type"]) + "_ORDER"
            connection.execute("INSERT INTO execution_orders VALUES(?,?,?,NULL,?,?,?,?,?,?)",
                               (*scope, identity, order.get("tradeID"), order.get("instrument"), kind,
                                "PENDING", order.get("units"), cursor))

    def apply_history(self, *, environment: str, account_id: str, requested_from_id: str,
                      transactions: Sequence[Mapping[str, object]], last_transaction_id: str) -> bool:
        """Journal and project a complete unfiltered /sinceid response atomically.

        Transaction IDs are ordered identifiers, not a gapless numeric sequence.
        Returns False with a durable fence if covered history is contradictory.
        """
        with self.db.transaction() as connection:
            for transaction in sorted(transactions, key=lambda item: int(_native_id(item.get("id")))):
                self.ingest_in(connection, transaction, environment=environment, account_id=account_id, source="REST")
            self.ledger.apply_history_in(connection, environment, account_id, requested_from_id,
                                         transactions, last_transaction_id, unfiltered=True)
            return self._advance_projection_in(connection, environment, account_id)

    def _advance_projection_in(self, connection: sqlite3.Connection, environment: str, account_id: str,
                               *, rebuild: bool = False) -> bool:
        scope = (environment, account_id)
        ledger = connection.execute("SELECT baseline_cursor,covered_cursor FROM ledger_accounts WHERE environment=? AND account_id=?", scope).fetchone()
        if ledger is None:
            raise SafetyFence("Projection requires an enrolled ledger")
        state = connection.execute("SELECT * FROM execution_projection_state WHERE environment=? AND account_id=?", scope).fetchone()
        if state is None:
            if ledger["baseline_cursor"] != "0":
                raise SafetyFence("Nonzero ledger baseline requires its exact native execution snapshot")
            connection.execute("INSERT INTO execution_projection_state VALUES(?,?,'0',?,'0','READY')",
                               (*scope, canonical_json({"trades": [], "orders": []})))
            state = connection.execute("SELECT * FROM execution_projection_state WHERE environment=? AND account_id=?", scope).fetchone()
        frontier = str(state["applied_cursor"])
        covered = str(ledger["covered_cursor"])
        # Late predecessors below a previously applied cut force a canonical
        # rebuild; raw facts and ledger conflict evidence are never deleted.
        late = connection.execute("SELECT 1 FROM execution_native_facts WHERE environment=? AND account_id=? AND reduced=0 AND (length(transaction_id),transaction_id)>(?,?) AND (length(transaction_id),transaction_id)<=(?,?) LIMIT 1",
                                  (*scope, len(state["baseline_cursor"]), state["baseline_cursor"], len(frontier), frontier)).fetchone()
        rebuild = rebuild or late is not None
        connection.execute("SAVEPOINT canonical_projection")
        fact: Mapping[str, object] = {"id": covered}
        try:
            if rebuild:
                self._restore_baseline_in(connection, environment, account_id, state["baseline_cursor"], state["baseline_json"])
                frontier = str(state["baseline_cursor"])
                connection.execute("UPDATE execution_native_facts SET reduced=0 WHERE environment=? AND account_id=? AND reduced=1 AND (length(transaction_id),transaction_id)>(?,?)",
                                   (*scope, len(frontier), frontier))
            rows = connection.execute("SELECT transaction_id,payload_json,reduced FROM execution_native_facts WHERE environment=? AND account_id=? AND (length(transaction_id),transaction_id)>(?,?) AND (length(transaction_id),transaction_id)<=(?,?) ORDER BY length(transaction_id),transaction_id",
                                      (*scope, len(frontier), frontier, len(covered), covered))
            for row in rows:
                fact = json.loads(row["payload_json"])
                if row["reduced"] == -1:
                    raise SafetyFence("A quarantined fact prevents canonical projection")
                record = self._find_record_in(connection, environment, account_id, fact)
                if record is not None:
                    self._validate_entry_fact(fact, record)
                self._correlate_in(connection, fact, environment, account_id, record)
                if not self._project_in(connection, fact, environment, account_id, record):
                    raise SafetyFence("Covered history lacks a required opening trade")
                connection.execute("UPDATE execution_native_facts SET reduced=1 WHERE environment=? AND account_id=? AND transaction_id=?",
                                   (*scope, row["transaction_id"]))
            connection.execute("UPDATE execution_projection_state SET applied_cursor=?,state='READY' WHERE environment=? AND account_id=?",
                               (covered, *scope))
        except (SafetyFence, ValidationFailure, ValueError, ArithmeticError) as exc:
            connection.execute("ROLLBACK TO canonical_projection")
            connection.execute("UPDATE execution_projection_state SET state='FENCED' WHERE environment=? AND account_id=?", scope)
            self._fence_in(connection, environment, account_id, "COVERED_PROJECTION_" + type(exc).__name__.upper(), fact)
            return False
        finally:
            connection.execute("RELEASE canonical_projection")
        return True

    def require_caught_up(self, *, environment: str, account_id: str) -> str:
        with self.db.read() as connection:
            row = connection.execute("SELECT p.applied_cursor,p.state,l.covered_cursor,l.observed_head FROM execution_projection_state p JOIN ledger_accounts l USING(environment,account_id) WHERE environment=? AND account_id=?",
                                     (environment, account_id)).fetchone()
            if (row is None or row["state"] != "READY" or row["applied_cursor"] != row["covered_cursor"]
                    or int(row["observed_head"]) > int(row["covered_cursor"])):
                raise SafetyFence("Native execution projection awaits complete ordered history")
            if connection.execute("SELECT 1 FROM execution_fences WHERE environment=? AND account_id=? AND resolved_evidence IS NULL", (environment, account_id)).fetchone():
                raise SafetyFence("Native execution conflict is unresolved")
            return str(row["applied_cursor"])

    def _correlate_in(
        self, connection: sqlite3.Connection, transaction: Mapping[str, object],
        environment: str, account_id: str, record: OutboxRecord | None,
    ) -> None:
        if record is None:
            return
        kind = str(transaction.get("type", ""))
        identity = _native_id(transaction.get("id"))
        if kind in ENTRY_CREATES:
            order_id = identity
            if record.native_order_id is not None and record.native_order_id != order_id:
                raise SafetyFence("a local intent cannot acquire a second native order across epochs")
            # The ACK_BY_FILL state wins even if the CREATE is delivered later.
            connection.execute(
                """UPDATE outbox SET native_order_id=?,status=CASE WHEN status='PENDING_SUBMISSION'
                THEN 'ACKNOWLEDGED' ELSE status END,dispatch_state='RESPONDED',version=version+1,
                updated_at=? WHERE intent_id=?""", (order_id, utc_now().isoformat(), record.intent_id),
            )
            connection.execute(
                "UPDATE execution_orders SET intent_id=? WHERE environment=? AND account_id=? AND order_id=?",
                (record.intent_id, environment, account_id, order_id),
            )
        elif kind == "ORDER_FILL":
            order_id = _native_id(transaction.get("orderID"))
            if record.native_order_id not in (None, order_id):
                raise SafetyFence("fill belongs to a different native entry order")
            newest_fill = identity if record.native_fill_id is None else str(max(int(identity), int(record.native_fill_id)))
            connection.execute(
                """UPDATE outbox SET native_order_id=?,native_fill_id=?,
                status='ACK_BY_FILL',dispatch_state='RESPONDED',version=version+1,updated_at=? WHERE intent_id=?""",
                (order_id, newest_fill, utc_now().isoformat(), record.intent_id),
            )
            opened = _mapping(transaction.get("tradeOpened"))
            if opened is not None:
                connection.execute(
                    "UPDATE execution_trades SET intent_id=? WHERE environment=? AND account_id=? AND trade_id=?",
                    (record.intent_id, environment, account_id, _native_id(opened.get("tradeID"))),
                )
            if record.status != OutboxStatus.ACK_BY_FILL:
                self.outbox.event_in(connection, record.intent_id, "ACK_BY_FILL", identity)
        elif kind == "ORDER_CANCEL":
            # A cancellation following a partial fill does not erase the fill.
            connection.execute(
                """UPDATE outbox SET status=CASE WHEN status='ACK_BY_FILL' THEN status ELSE 'CANCELLED' END,
                dispatch_state='RESPONDED',reservation_active=CASE WHEN status='ACK_BY_FILL'
                THEN reservation_active ELSE 0 END,version=version+1,updated_at=? WHERE intent_id=?""",
                (utc_now().isoformat(), record.intent_id),
            )
            self.outbox.event_in(connection, record.intent_id, "NATIVE_CANCELLED", identity)
        elif kind.endswith("_ORDER_REJECT"):
            if record.status == OutboxStatus.ACK_BY_FILL:
                raise SafetyFence("native rejection conflicts with a proven fill")
            connection.execute(
                "UPDATE outbox SET status='REJECTED',dispatch_state='RESPONDED',reservation_active=0,version=version+1,updated_at=? WHERE intent_id=?",
                (utc_now().isoformat(), record.intent_id),
            )
            self.outbox.event_in(connection, record.intent_id, "NATIVE_REJECTED", identity)

    @FINANCIAL.calculation
    def _project_in(
        self, connection: sqlite3.Connection, transaction: Mapping[str, object],
        environment: str, account_id: str, record: OutboxRecord | None,
    ) -> bool:
        kind = str(transaction.get("type", ""))
        identity = _native_id(transaction.get("id"))
        scope = (environment, account_id)
        if kind in ENTRY_CREATES or kind in PROTECTION_CREATES:
            trade_id = transaction.get("tradeID")
            order_state = connection.execute(
                "SELECT state FROM execution_orders WHERE environment=? AND account_id=? AND order_id=?",
                (*scope, identity),
            ).fetchone()
            state = "PENDING" if order_state is None else str(order_state["state"])
            connection.execute(
                """INSERT INTO execution_orders VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(environment,account_id,order_id)
                DO UPDATE SET intent_id=COALESCE(excluded.intent_id,execution_orders.intent_id),
                trade_id=COALESCE(excluded.trade_id,execution_orders.trade_id),order_type=excluded.order_type""",
                (*scope, identity, None if record is None else record.intent_id, trade_id,
                 transaction.get("instrument"), kind, state,
                 None if "units" not in transaction else decimal_text(money(transaction["units"])), identity),  # type: ignore[arg-type]
            )
            # A delayed CREATE cannot reattach a protection already cancelled.
            if kind in PROTECTION_CREATES and isinstance(trade_id, str) and state == "PENDING":
                column = PROTECTION_CREATES[kind]
                connection.execute(
                    f"UPDATE execution_trades SET {column}=? WHERE environment=? AND account_id=? AND trade_id=?",
                    (identity, *scope, trade_id),
                )
            return True
        if kind == "ORDER_CANCEL":
            order_id = _native_id(transaction.get("orderID"))
            connection.execute(
                """INSERT INTO execution_orders VALUES(?,?,?,NULL,NULL,NULL,'UNKNOWN','CANCELLED',NULL,?)
                ON CONFLICT(environment,account_id,order_id) DO UPDATE SET
                state=CASE WHEN execution_orders.state='FILLED' THEN 'FILLED' ELSE 'CANCELLED' END,
                last_transaction_id=CASE WHEN
                (length(excluded.last_transaction_id),excluded.last_transaction_id)>
                (length(execution_orders.last_transaction_id),execution_orders.last_transaction_id)
                THEN excluded.last_transaction_id ELSE execution_orders.last_transaction_id END""",
                (*scope, order_id, identity),
            )
            for column in PROTECTION_CREATES.values():
                connection.execute(f"UPDATE execution_trades SET {column}=NULL WHERE environment=? AND account_id=? AND {column}=?",
                                   (*scope, order_id))
            return True
        if kind != "ORDER_FILL":
            return True
        order_id = _native_id(transaction.get("orderID"))
        opened = _mapping(transaction.get("tradeOpened"))
        reduced = _mapping(transaction.get("tradeReduced"))
        # Malformed optional subtrees are not equivalent to absent exposure.
        if any(key in transaction and _mapping(transaction[key]) is None
               for key in ("tradeOpened", "tradeReduced")):
            raise ValidationFailure("Native trade subtree must be an object")
        closed_value = transaction.get("tradesClosed", [])
        if not isinstance(closed_value, list):
            raise ValidationFailure("tradesClosed must be a list of native TradeReduce objects")
        closures = [_mapping(value) for value in closed_value]
        if any(value is None for value in closures):
            raise ValidationFailure("invalid TradeReduce subtree")
        reductions: list[tuple[Mapping[str, object], bool]] = [(cast(Mapping[str, object], value), True) for value in closures]
        if reduced is not None:
            reductions.append((reduced, False))
        # Check all dependencies before applying any deltas. A REST catch-up can
        # deliver an older opening after an already received closing observation.
        validated: list[tuple[str, Decimal]] = []
        seen_trades: set[str] = set()
        for subtree, closed in reductions:
            trade_id = _native_id(subtree.get("tradeID"))
            if trade_id in seen_trades:
                raise SafetyFence("A fill cannot reduce the same trade more than once")
            seen_trades.add(trade_id)
            trade = connection.execute(
                "SELECT current_units FROM execution_trades WHERE environment=? AND account_id=? AND trade_id=?",
                (*scope, trade_id),
            ).fetchone()
            if trade is None:
                return False
            current = money(trade["current_units"])
            quantity = abs(money(subtree.get("units")))  # type: ignore[arg-type]
            if quantity > abs(current) or quantity == 0:
                raise SafetyFence("native reduction exceeds the tracked gross trade size")
            if closed and quantity != abs(current):
                raise SafetyFence("native closed-trade quantity disagrees with its known gross size")
            remaining = Decimal("0") if closed else (abs(current) - quantity).copy_sign(current)
            validated.append((trade_id, remaining))
        if opened is not None:
            trade_id = _native_id(opened.get("tradeID"))
            if trade_id in seen_trades:
                raise SafetyFence("A fill cannot open and reduce the same native trade")
            units = money(opened.get("units"))  # type: ignore[arg-type]
            instrument = str(transaction.get("instrument", ""))
            from oandafly.domain.instruments import CurrencyInstrument
            CurrencyInstrument(name=instrument)
            if units == 0:
                raise ValidationFailure("native TradeOpen must have nonzero units")
            existing = connection.execute(
                "SELECT initial_units,instrument FROM execution_trades WHERE environment=? AND account_id=? AND trade_id=?",
                (*scope, trade_id),
            ).fetchone()
            if existing is not None:
                raise SafetyFence("a second native fill attempted to reopen an existing TradeID")
            connection.execute(
                "INSERT INTO execution_trades VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL,?)",
                (*scope, trade_id, None if record is None else record.intent_id, instrument,
                 decimal_text(units), decimal_text(units), identity),
            )
            for protection in connection.execute(
                "SELECT order_id,order_type FROM execution_orders WHERE environment=? AND account_id=? AND trade_id=? AND state='PENDING'",
                (*scope, trade_id),
            ).fetchall():
                if protection["order_type"] in PROTECTION_CREATES:
                    column = PROTECTION_CREATES[protection["order_type"]]
                    connection.execute(
                        f"UPDATE execution_trades SET {column}=? WHERE environment=? AND account_id=? AND trade_id=?",
                        (protection["order_id"], *scope, trade_id),
                    )
        for trade_id, remaining in validated:
            connection.execute(
                "UPDATE execution_trades SET current_units=?,last_transaction_id=CASE WHEN (length(?),?)>(length(last_transaction_id),last_transaction_id) THEN ? ELSE last_transaction_id END WHERE environment=? AND account_id=? AND trade_id=?",
                (decimal_text(remaining), identity, identity, identity, *scope, trade_id),
            )
        connection.execute(
            """INSERT INTO execution_orders VALUES(?,?,?,?,NULL,?,'UNKNOWN','FILLED',?,?)
            ON CONFLICT(environment,account_id,order_id) DO UPDATE SET state='FILLED',
            intent_id=COALESCE(excluded.intent_id,execution_orders.intent_id),last_transaction_id=excluded.last_transaction_id""",
            (*scope, order_id, None if record is None else record.intent_id, transaction.get("instrument"),
             None if "units" not in transaction else decimal_text(money(transaction["units"])), identity),  # type: ignore[arg-type]
        )
        return True

    def bind_response(self, intent_id: str, response: Mapping[str, object]) -> OutboxRecord:
        """Bind native lineage to this single dispatch, then reduce all its facts."""
        with self.db.transaction() as connection:
            record = self.outbox.get_in(connection, intent_id)
            create = _mapping(response.get("orderCreateTransaction"))
            rejection = _mapping(response.get("orderRejectTransaction"))
            root = create if create is not None else rejection
            if root is None:
                raise TransportAmbiguous("order response has no authoritative create or reject transaction")
            self._validate_entry_fact(root, record)
            root_id = _native_id(root.get("id"))
            if record.native_order_id is not None and record.native_order_id != root_id:
                raise SafetyFence("REST response conflicts with the stream's native order lineage")
            root_kind = str(root.get("type", ""))
            if create is not None and root_kind != f"{record.request.order_type}_ORDER":
                # MARKET_IF_TOUCHED uses exactly MARKET_IF_TOUCHED_ORDER.
                raise SafetyFence("REST create type differs from the prepared native request")
            connection.execute("UPDATE outbox SET native_order_id=? WHERE intent_id=?", (root_id, intent_id))
            transactions = [value for value in response.values() if isinstance(value, Mapping) and "type" in value and "id" in value]
            for value in sorted(transactions, key=lambda item: int(_native_id(item.get("id")))):
                transaction = cast(Mapping[str, object], value)
                if transaction.get("type") in {"ORDER_FILL", "ORDER_CANCEL"} and transaction.get("orderID") != root_id:
                    raise SafetyFence("REST response includes a fill/cancel for another order")
                result = self.ingest_in(
                    connection, transaction, environment=str(record.request.context.environment),
                    account_id=record.request.account_id, source="REST",
                )
                if result.quarantined:
                    # Persist the quarantine and keep UNKNOWN; do not roll it back.
                    connection.execute(
                        "UPDATE outbox SET dispatch_state='UNKNOWN',last_error='RESPONSE_QUARANTINED',version=version+1 WHERE intent_id=? AND status!='ACK_BY_FILL'",
                        (intent_id,),
                    )
            return self.outbox.get_in(connection, intent_id)


class NativeMutationGateway:
    def __init__(
        self, outbox: Outbox, *, transport: OrderTransport, governor: RateGovernor,
        reducer: OutboxReducer, timeout_seconds: float = 15.0,
    ) -> None:
        if not 0 < timeout_seconds <= 60:
            raise ValueError("dispatch timeout must be within (0,60] seconds")
        self.outbox = outbox
        self.transport = transport
        self.governor = governor
        self.reducer = reducer
        self.timeout_seconds = timeout_seconds

    async def submit_order(self, request: OrderRequest, *, intent_id: UUID) -> OutboxRecord:
        """Typed proposal -> durable reservation -> one immutable-byte invocation.

        Repeated intent identities retain their receipts; dispatch never retries
        a slot whose single transmission opportunity has already been consumed.
        """
        prepared = self.outbox.prepare(OrderRequest.model_validate(request), intent_id=intent_id)
        return await self.dispatch(prepared.intent_id)

    def _mark_ambiguous(self, intent_id: str, code: str) -> OutboxRecord:
        with self.outbox.db.transaction() as connection:
            # The gateway runs the final guard itself before send_order_bytes, so a
            # missing invocation marker proves no native bytes were transmitted.
            unsent = connection.execute(
                """UPDATE outbox SET status='CANCELLED',dispatch_state='NOT_SENT',reservation_active=0,last_error=?,
                version=version+1,updated_at=? WHERE intent_id=? AND dispatch_state='DISPATCH_STARTED'
                AND status='PENDING_SUBMISSION' AND invocation_marker IS NULL""",
                (code, utc_now().isoformat(), intent_id),
            ).rowcount
            # A proven fill/create/cancel/rejection is stronger than a transport
            # timeout arriving later in another thread. Never regress its state.
            connection.execute(
                "UPDATE outbox SET dispatch_state='UNKNOWN',last_error=?,version=version+1,updated_at=? WHERE intent_id=? AND dispatch_state='DISPATCH_STARTED' AND status='PENDING_SUBMISSION'",
                (code, utc_now().isoformat(), intent_id),
            )
            self.outbox.event_in(connection, intent_id,
                                 "FAILED_BEFORE_NATIVE_INVOCATION" if unsent else "TRANSPORT_RESULT_UNCERTAIN",
                                 detail={"code": code})
            return self.outbox.get_in(connection, intent_id)

    async def dispatch(self, intent_id: str) -> OutboxRecord:
        initial = self.outbox.get(intent_id)
        if initial.dispatch_state != DispatchState.NOT_DISPATCHED:
            raise SafetyFence("this intent has already consumed its single dispatch opportunity")
        deadline = time.monotonic() + self.timeout_seconds
        await self.governor.acquire(kind="request", deadline=deadline)
        nonce = str(uuid4())
        with self.outbox.db.transaction() as connection:
            record = self.outbox.get_in(connection, intent_id)
            self.outbox.validate_entry_in(connection, record)
            wire = canonical_json(native_payload(record.request, record.client_id)).encode("utf-8")
            stored = connection.execute("SELECT payload_json FROM outbox WHERE intent_id=?",
                                        (intent_id,)).fetchone()[0]
            if stored.encode("utf-8") != wire or hashlib.sha256(wire).hexdigest() != record.payload_hash:
                raise SafetyFence("Durable payload bytes disagree with the gateway serialization")
            slot = _DispatchSlot(record.request.account_id, sha256_json(record.request), wire, record.payload_hash)
            cursor = connection.execute(
                """UPDATE outbox SET dispatch_state='DISPATCH_STARTED',dispatch_nonce=?,version=version+1,
                updated_at=? WHERE intent_id=? AND dispatch_state='NOT_DISPATCHED' AND status='PENDING_SUBMISSION'""",
                (nonce, utc_now().isoformat(), intent_id),
            )
            if cursor.rowcount != 1:
                raise Conflict("another dispatch or cancellation consumed this intent")
            self.outbox.event_in(connection, intent_id, "DISPATCH_STARTED")
        invoked = False

        def before_send() -> None:
            nonlocal invoked
            if invoked:
                raise SafetyFence("transport attempted to invoke an order twice")
            with self.outbox.db.transaction() as connection:
                current = self.outbox.get_in(connection, intent_id)
                try:
                    if time.monotonic() >= deadline:
                        raise SafetyFence("dispatch deadline expired before native invocation")
                    current_wire = connection.execute("SELECT payload_json FROM outbox WHERE intent_id=?",
                                                      (intent_id,)).fetchone()[0].encode("utf-8")
                    if (slot.payload != current_wire or slot.payload_hash != current.payload_hash
                            or slot.request_hash != sha256_json(current.request)
                            or slot.account_id != current.request.account_id):
                        raise SafetyFence("stored wire bytes disagree with the prepared payload")
                    self.outbox.validate_entry_in(connection, current)
                except (SafetyFence, ValidationFailure, Conflict):
                    connection.execute(
                        """UPDATE outbox SET status='CANCELLED',dispatch_state='NOT_SENT',reservation_active=0,
                        last_error='FINAL_GUARD_PREVENTED_SEND',version=version+1 WHERE intent_id=?
                        AND dispatch_nonce=? AND invocation_marker IS NULL AND dispatch_state='DISPATCH_STARTED'""",
                        (intent_id, nonce),
                    )
                    self.outbox.event_in(connection, intent_id, "FINAL_GUARD_PREVENTED_SEND")
                    # Commit no-send evidence before propagating the rejection.
                    prevented = True
                else:
                    cursor = connection.execute(
                        """UPDATE outbox SET invocation_marker=?,version=version+1 WHERE intent_id=?
                        AND dispatch_nonce=? AND invocation_marker IS NULL AND dispatch_state='DISPATCH_STARTED'""",
                        (str(uuid4()), intent_id, nonce),
                    )
                    if cursor.rowcount != 1:
                        raise SafetyFence("final invocation marker CAS failed")
                    self.outbox.event_in(connection, intent_id, "NATIVE_INVOCATION_STARTED")
                    prevented = False
            if prevented:
                raise SafetyFence("final guarded boundary prevented native transmission")
            invoked = True

        try:
            remaining = max(0.001, deadline - time.monotonic())
            async with asyncio.timeout(remaining):
                await self.transport.prepare_order(slot.account_id)
                before_send()  # Owned by the gateway, never supplied by a transport.
                response = await self.transport.send_order_bytes(slot.account_id, slot.payload)
            return self.reducer.bind_response(intent_id, response)
        except asyncio.CancelledError:
            self._mark_ambiguous(intent_id, "TASK_CANCELLED")
            raise
        except Exception as exc:
            result = self._mark_ambiguous(intent_id, type(exc).__name__)
            if result.dispatch_state == DispatchState.UNKNOWN:
                raise TransportAmbiguous("native outcome is unknown; reconcile authoritative transaction history before any retry") from exc
            if result.dispatch_state == DispatchState.NOT_SENT:
                raise SafetyFence("native invocation was prevented and durably proven unsent") from exc
            return result
