"""Audited, idempotent OANDA native facts and a double-entry cash projection.

Stream observation and REST history coverage deliberately have different
watermarks. Transaction IDs are ordered decimal identifiers, not a consecutive
sequence. This module never equates position units to account-currency cash.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from oandafly.domain.errors import Conflict, SafetyFence, ValidationFailure
from oandafly.domain.financial import FINANCIAL
from oandafly.domain.instruments import currency_code, currency_pair
from oandafly.domain.primitives import canonical_json, decimal_text, money, require_utc, sha256_json, utc_now
from oandafly.storage.database import Database

_ZERO = Decimal(0)
_ID = re.compile(r"(?:0|[1-9][0-9]{0,39})\Z")
_NONCASH_TYPES = frozenset({
    "CREATE", "CLOSE", "REOPEN", "CLIENT_CONFIGURE", "CLIENT_CONFIGURE_REJECT",
    "TRANSFER_FUNDS_REJECT", "MARKET_ORDER", "MARKET_ORDER_REJECT",
    "FIXED_PRICE_ORDER", "LIMIT_ORDER", "LIMIT_ORDER_REJECT", "STOP_ORDER",
    "STOP_ORDER_REJECT", "MARKET_IF_TOUCHED_ORDER", "MARKET_IF_TOUCHED_ORDER_REJECT",
    "TAKE_PROFIT_ORDER", "TAKE_PROFIT_ORDER_REJECT", "STOP_LOSS_ORDER",
    "STOP_LOSS_ORDER_REJECT", "GUARANTEED_STOP_LOSS_ORDER",
    "GUARANTEED_STOP_LOSS_ORDER_REJECT", "TRAILING_STOP_LOSS_ORDER",
    "TRAILING_STOP_LOSS_ORDER_REJECT", "ORDER_CANCEL", "ORDER_CANCEL_REJECT",
    "ORDER_CLIENT_EXTENSIONS_MODIFY", "ORDER_CLIENT_EXTENSIONS_MODIFY_REJECT",
    "TRADE_CLIENT_EXTENSIONS_MODIFY", "TRADE_CLIENT_EXTENSIONS_MODIFY_REJECT",
    "MARGIN_CALL_ENTER", "MARGIN_CALL_EXTEND", "MARGIN_CALL_EXIT",
    "DELAYED_TRADE_CLOSURE", "RESET_RESETTABLE_PL",
})
_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_accounts (
 environment TEXT NOT NULL, account_id TEXT NOT NULL, currency TEXT NOT NULL,
 baseline_balance TEXT NOT NULL, baseline_cursor TEXT NOT NULL,
 baseline_observed_at TEXT NOT NULL, baseline_digest TEXT NOT NULL,
 balance TEXT NOT NULL, covered_cash_balance TEXT NOT NULL,
 covered_cursor TEXT NOT NULL, observed_head TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(environment, account_id));
CREATE TABLE IF NOT EXISTS ledger_native_facts (
 environment TEXT NOT NULL, account_id TEXT NOT NULL, transaction_id TEXT NOT NULL,
 transaction_type TEXT NOT NULL, payload TEXT NOT NULL, payload_digest TEXT NOT NULL,
 native_time TEXT, received_at TEXT NOT NULL, first_source TEXT NOT NULL,
 PRIMARY KEY(environment, account_id, transaction_id));
CREATE INDEX IF NOT EXISTS ledger_native_ordered_cut ON ledger_native_facts
 (environment,account_id,length(transaction_id),transaction_id);
CREATE TABLE IF NOT EXISTS ledger_fact_projection (
 environment TEXT NOT NULL, account_id TEXT NOT NULL, transaction_id TEXT NOT NULL,
 disposition TEXT NOT NULL, cash_delta TEXT NOT NULL, account_balance TEXT,
 PRIMARY KEY(environment, account_id, transaction_id),
 FOREIGN KEY(environment, account_id, transaction_id)
 REFERENCES ledger_native_facts(environment, account_id, transaction_id));
CREATE INDEX IF NOT EXISTS ledger_projection_ordered_cut ON ledger_fact_projection
 (environment,account_id,length(transaction_id),transaction_id);
CREATE TABLE IF NOT EXISTS ledger_cash_journal (
 environment TEXT NOT NULL, account_id TEXT NOT NULL, transaction_id TEXT NOT NULL,
 component TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('CASH','CONTRA')),
 currency TEXT NOT NULL, amount TEXT NOT NULL,
 PRIMARY KEY(environment, account_id, transaction_id, component, side),
 FOREIGN KEY(environment, account_id, transaction_id)
 REFERENCES ledger_native_facts(environment, account_id, transaction_id));
CREATE TABLE IF NOT EXISTS ledger_fences (
 environment TEXT NOT NULL, account_id TEXT NOT NULL, transaction_id TEXT NOT NULL,
 reason TEXT NOT NULL, evidence_digest TEXT NOT NULL, created_at TEXT NOT NULL,
 resolved_at TEXT,
 PRIMARY KEY(environment, account_id, transaction_id, reason, evidence_digest));
CREATE TABLE IF NOT EXISTS ledger_audit (
 event_id TEXT PRIMARY KEY, environment TEXT NOT NULL, account_id TEXT NOT NULL,
 kind TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_checkpoints (
 environment TEXT NOT NULL, account_id TEXT NOT NULL, snapshot_digest TEXT NOT NULL,
 transaction_id TEXT NOT NULL, balance TEXT NOT NULL, observed_at TEXT NOT NULL,
 verified INTEGER NOT NULL CHECK(verified IN (0,1)),
 PRIMARY KEY(environment, account_id, snapshot_digest));
CREATE TABLE IF NOT EXISTS ledger_financing_obligations (
 environment TEXT NOT NULL, account_id TEXT NOT NULL, obligation_id TEXT NOT NULL,
 estimated_amount TEXT NOT NULL, evidence_digest TEXT NOT NULL,
 created_at TEXT NOT NULL, settled_transaction_id TEXT, settled_amount TEXT,
 settlement_evidence TEXT, settled_at TEXT,
 PRIMARY KEY(environment, account_id, obligation_id));
CREATE TRIGGER IF NOT EXISTS ledger_native_facts_no_update BEFORE UPDATE ON ledger_native_facts
BEGIN SELECT RAISE(ABORT,'Native facts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS ledger_native_facts_no_delete BEFORE DELETE ON ledger_native_facts
BEGIN SELECT RAISE(ABORT,'Native facts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS ledger_cash_journal_no_update BEFORE UPDATE ON ledger_cash_journal
BEGIN SELECT RAISE(ABORT,'Cash journals are immutable'); END;
CREATE TRIGGER IF NOT EXISTS ledger_cash_journal_no_delete BEFORE DELETE ON ledger_cash_journal
BEGIN SELECT RAISE(ABORT,'Cash journals are immutable'); END;
CREATE TRIGGER IF NOT EXISTS ledger_baseline_no_update BEFORE UPDATE OF currency,baseline_balance,baseline_cursor,baseline_observed_at,baseline_digest ON ledger_accounts
BEGIN SELECT RAISE(ABORT,'Account baselines are immutable'); END;
CREATE TRIGGER IF NOT EXISTS ledger_audit_no_update BEFORE UPDATE ON ledger_audit
BEGIN SELECT RAISE(ABORT,'Audit events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS ledger_audit_no_delete BEFORE DELETE ON ledger_audit
BEGIN SELECT RAISE(ABORT,'Audit events are immutable'); END;
"""


def native_id(value: object) -> str:
    """Validate an OANDA decimal identifier without narrowing to int64."""
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValidationFailure("A canonical, nonnegative native transaction ID is required")
    return value


def _scope(environment: str, account_id: str) -> None:
    if environment not in {"LIVE", "SANDBOX"} or not account_id or len(account_id) > 160:
        raise ValidationFailure("Invalid native account scope")
    if any(ord(char) < 33 for char in account_id):
        raise ValidationFailure("Account identity contains whitespace or control characters")


def _field(data: Mapping[str, object], key: str, *, required: bool = False) -> Decimal:
    if key not in data:
        if required:
            raise ValidationFailure(f"Native financial field {key} is missing")
        return _ZERO
    value = data[key]
    if not isinstance(value, (str, Decimal, int)) or isinstance(value, bool):
        raise ValidationFailure(f"Native financial field {key} must be decimal text")
    return money(value)


def _components(data: Mapping[str, object]) -> tuple[dict[str, Decimal], str | None]:
    """Only root amounts book cash; nested allocations and spread costs are metadata."""
    kind = data.get("type")
    if kind == "TRANSFER_FUNDS":
        return {"EXTERNAL_TRANSFER": _field(data, "amount", required=True)}, None
    if kind == "ORDER_FILL":
        commission = _field(data, "commission")
        execution_fee = _field(data, "guaranteedExecutionFee")
        if commission < 0 or execution_fee < 0:
            raise ValidationFailure("OANDA charged fees must be nonnegative root quantities")
        return {
            "REALIZED_PL": _field(data, "pl", required=True),
            "FINANCING": _field(data, "financing", required=True),
            # Unary minus rounds under the ambient Decimal context; native cash
            # and its contra must retain every accepted digit independently.
            "COMMISSION": commission.copy_negate(),
            "GUARANTEED_EXECUTION_FEE": execution_fee.copy_negate(),
        }, None
    if kind == "DAILY_FINANCING":
        return {"FINANCING": _field(data, "financing", required=True)}, None
    if kind == "DIVIDEND_ADJUSTMENT":
        # Preserve actual native account cash while fencing the forbidden asset event.
        return {"UNSUPPORTED_ASSET_ADJUSTMENT": _field(data, "dividendAdjustment", required=True)}, "NON_FX_EVENT"
    if kind in _NONCASH_TYPES:
        if any(field in data for field in ("pl", "financing", "commission", "guaranteedExecutionFee", "dividendAdjustment")):
            raise ValidationFailure("Unexpected financial fields on a noncash native transaction")
        return {}, None
    return {}, "UNSUPPORTED_TRANSACTION_TYPE"


@dataclass(frozen=True, slots=True)
class NativeFactResult:
    transaction_id: str
    duplicate: bool
    disposition: str
    cash_delta: Decimal
    quarantined: bool


@dataclass(frozen=True, slots=True)
class LedgerBalance:
    environment: str
    account_id: str
    currency: str
    balance: Decimal
    baseline_cursor: str
    covered_cursor: str
    observed_head: str
    revision: int
    fences: tuple[str, ...]
    unbooked_financing: Decimal
    open_financing_obligations: int = 0

    @property
    def history_caught_up(self) -> bool:
        return int(self.covered_cursor) >= int(self.observed_head) and not self.fences

    @property
    def known_financing_settled(self) -> bool:
        return self.open_financing_obligations == 0


class Ledger:
    """One immutable baseline, immutable native facts, and replayable cash journals."""

    def __init__(self, db: Database) -> None:
        self.db = db
        db.initialize(_SCHEMA, component=__name__)

    @staticmethod
    def _fence(conn: sqlite3.Connection, environment: str, account_id: str,
               transaction_id: str, reason: str, evidence: object) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO ledger_fences VALUES(?,?,?,?,?,?,NULL)",
            (environment, account_id, transaction_id, reason, sha256_json(evidence), utc_now().isoformat()),
        )

    @staticmethod
    def _audit(conn: sqlite3.Connection, environment: str, account_id: str,
               kind: str, body: object) -> None:
        conn.execute("INSERT INTO ledger_audit VALUES(?,?,?,?,?,?)", (
            str(uuid4()), environment, account_id, kind, canonical_json(body), utc_now().isoformat(),
        ))

    def establish_baseline(self, environment: str, account_id: str, currency: str,
                           balance: Decimal, last_transaction_id: str,
                           observed_at: datetime) -> LedgerBalance:
        with self.db.transaction() as conn:
            self.establish_baseline_in(conn, environment, account_id, currency, balance,
                                       last_transaction_id, observed_at)
        return self.balance(environment, account_id)

    def establish_baseline_in(self, conn: sqlite3.Connection, environment: str, account_id: str,
                              currency: str, balance: Decimal, last_transaction_id: str,
                              observed_at: datetime) -> None:
        """Bind cash and execution baselines in the caller's single commit."""
        _scope(environment, account_id)
        currency_code(currency)
        balance = money(balance)
        cursor = native_id(last_transaction_id)
        observed = require_utc(observed_at).isoformat()
        digest = sha256_json({"currency": currency, "balance": decimal_text(balance),
                              "cursor": cursor, "observed_at": observed})
        existing = conn.execute("SELECT baseline_digest FROM ledger_accounts WHERE environment=? AND account_id=?",
                                (environment, account_id)).fetchone()
        if existing is not None:
            if existing["baseline_digest"] != digest:
                raise Conflict("An account baseline is immutable; reconcile history instead of resetting cash")
        else:
            conn.execute("INSERT INTO ledger_accounts(environment,account_id,currency,baseline_balance,baseline_cursor,"
                         "baseline_observed_at,baseline_digest,balance,covered_cash_balance,covered_cursor,observed_head,revision) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,0)", (
                environment, account_id, currency, decimal_text(balance), cursor,
                observed, digest, decimal_text(balance), decimal_text(balance), cursor, cursor,
            ))
            rows = conn.execute(
                "SELECT payload FROM ledger_native_facts WHERE environment=? AND account_id=? "
                "ORDER BY length(transaction_id), transaction_id", (environment, account_id),
            ).fetchall()
            for row in rows:
                payload: dict[str, object] = json.loads(row["payload"])
                self._project(conn, environment, account_id, payload)
            conn.execute("UPDATE ledger_fences SET resolved_at=? WHERE environment=? AND account_id=? "
                         "AND reason='NO_BASELINE' AND resolved_at IS NULL", (observed, environment, account_id))
            self._audit(conn, environment, account_id, "BASELINE_ESTABLISHED", {"digest": digest, "cursor": cursor})

    def ingest(self, environment: str, account_id: str, transaction: Mapping[str, object],
               source: Literal["STREAM", "REST", "PAPER"] = "STREAM") -> NativeFactResult:
        with self.db.transaction() as conn:
            return self.ingest_native_tx(conn, environment, account_id, transaction, source)

    def ingest_native_tx(self, conn: sqlite3.Connection, environment: str, account_id: str,
                         transaction: Mapping[str, object], source: str = "STREAM") -> NativeFactResult:
        """Atomic integration hook; the caller may reduce outbox/trade facts in this transaction."""
        _scope(environment, account_id)
        if source not in {"STREAM", "REST", "PAPER"}:
            raise ValidationFailure("Invalid native fact source")
        tx_id = native_id(transaction.get("id"))
        kind = transaction.get("type")
        if not isinstance(kind, str) or not re.fullmatch(r"[A-Z_]{1,80}", kind):
            raise ValidationFailure("Invalid native transaction type")
        payload = canonical_json(dict(transaction))
        digest = sha256_json(dict(transaction))
        row = conn.execute("SELECT payload_digest FROM ledger_native_facts WHERE environment=? AND account_id=? "
                           "AND transaction_id=?", (environment, account_id, tx_id)).fetchone()
        if row is not None:
            if row["payload_digest"] != digest:
                self._fence(conn, environment, account_id, tx_id, "NATIVE_IDENTITY_CONFLICT", {"incoming": digest})
                return NativeFactResult(tx_id, True, "IDENTITY_CONFLICT", _ZERO, True)
            projected = conn.execute("SELECT disposition,cash_delta FROM ledger_fact_projection "
                                     "WHERE environment=? AND account_id=? AND transaction_id=?",
                                     (environment, account_id, tx_id)).fetchone()
            if projected is None:
                raise Conflict("A persisted native fact has no atomic projection record")
            disposition = str(projected["disposition"])
            return NativeFactResult(tx_id, True, disposition, money(projected["cash_delta"]),
                                    disposition.startswith("QUARANTINED"))
        native_time = transaction.get("time")
        conn.execute("INSERT INTO ledger_native_facts VALUES(?,?,?,?,?,?,?,?,?)", (
            environment, account_id, tx_id, kind, payload, digest,
            native_time if isinstance(native_time, str) else None, utc_now().isoformat(), source,
        ))
        return self._project(conn, environment, account_id, dict(transaction))

    def _project(self, conn: sqlite3.Connection, environment: str, account_id: str,
                 transaction: Mapping[str, object]) -> NativeFactResult:
        tx_id = native_id(transaction.get("id"))
        row = conn.execute("SELECT * FROM ledger_accounts WHERE environment=? AND account_id=?",
                           (environment, account_id)).fetchone()
        issue: str | None = None
        parts: dict[str, Decimal] = {}
        after_balance: Decimal | None = None
        try:
            if transaction.get("accountID", account_id) != account_id:
                raise ValidationFailure("A routed fact names a different native account")
            parts, issue = _components(transaction)
            if "accountBalance" in transaction:
                after_balance = _field(transaction, "accountBalance")
        except (ValidationFailure, ValueError, ArithmeticError) as exc:
            issue = "INVALID_NATIVE_FINANCIAL_FACT"
            self._fence(conn, environment, account_id, tx_id, issue, {"error": str(exc)})
        instruments: list[object] = []
        if "instrument" in transaction:
            instruments.append(transaction["instrument"])
        financing_positions = transaction.get("positionFinancings", [])
        if isinstance(financing_positions, list):
            instruments.extend(item.get("instrument") for item in financing_positions if isinstance(item, dict))
        for instrument in instruments:
            try:
                if not isinstance(instrument, str):
                    raise ValueError("Native instrument is not text")
                currency_pair(instrument)
            except ValueError:
                issue = issue or "NON_FX_EVENT"
        if row is not None and int(row["baseline_cursor"]) < int(tx_id) <= int(row["covered_cursor"]):
            issue = issue or "HISTORY_COVERAGE_CONTRADICTION"
        if issue is not None:
            self._fence(conn, environment, account_id, tx_id, issue, dict(transaction))
        with FINANCIAL.local() as ctx:
            ctx.prec = 512
            delta = sum(parts.values(), _ZERO)
        if row is None:
            disposition = "QUARANTINED" if issue else "AWAITING_BASELINE"
            self._fence(conn, environment, account_id, tx_id, "NO_BASELINE", {"transaction_id": tx_id})
        elif int(tx_id) <= int(row["baseline_cursor"]):
            disposition = "QUARANTINED_COVERED_BY_BASELINE" if issue else "COVERED_BY_BASELINE"
        elif issue is not None and issue not in {"NON_FX_EVENT", "HISTORY_COVERAGE_CONTRADICTION"}:
            disposition = "QUARANTINED"
        else:
            existing = conn.execute("SELECT disposition FROM ledger_fact_projection WHERE environment=? AND account_id=? "
                                    "AND transaction_id=?", (environment, account_id, tx_id)).fetchone()
            if existing is not None and existing["disposition"] in {"BOOKED", "NONCASH", "QUARANTINED_BOOKED"}:
                return NativeFactResult(tx_id, True, str(existing["disposition"]), delta, issue is not None)
            for component, amount in parts.items():
                if amount == 0:
                    continue
                # Sign reversal is exact even for values wider than 28 digits.
                for side, signed in (("CASH", amount), ("CONTRA", amount.copy_negate())):
                    conn.execute("INSERT INTO ledger_cash_journal VALUES(?,?,?,?,?,?,?)", (
                        environment, account_id, tx_id, component, side, row["currency"], decimal_text(signed),
                    ))
            with FINANCIAL.local() as ctx:
                ctx.prec = 512
                updated = money(row["balance"]) + delta
            conn.execute("UPDATE ledger_accounts SET balance=?,revision=revision+1 WHERE environment=? AND account_id=?",
                         (decimal_text(updated), environment, account_id))
            disposition = "QUARANTINED_BOOKED" if issue else ("BOOKED" if parts else "NONCASH")
        conn.execute("INSERT INTO ledger_fact_projection VALUES(?,?,?,?,?,?) ON CONFLICT(environment,account_id,transaction_id) "
                     "DO UPDATE SET disposition=excluded.disposition,cash_delta=excluded.cash_delta,account_balance=excluded.account_balance", (
                         environment, account_id, tx_id, disposition, decimal_text(delta),
                         None if after_balance is None else decimal_text(after_balance),
                     ))
        if row is not None and int(tx_id) > int(row["observed_head"]):
            conn.execute("UPDATE ledger_accounts SET observed_head=? WHERE environment=? AND account_id=?",
                         (tx_id, environment, account_id))
        return NativeFactResult(tx_id, False, disposition, delta, issue is not None)

    def record_heartbeat(self, environment: str, account_id: str, last_transaction_id: str) -> None:
        _scope(environment, account_id)
        cursor = native_id(last_transaction_id)
        with self.db.transaction() as conn:
            row = conn.execute("SELECT observed_head FROM ledger_accounts WHERE environment=? AND account_id=?",
                               (environment, account_id)).fetchone()
            if row is None:
                raise Conflict("A baseline is required before observing account heartbeats")
            if int(cursor) > int(row["observed_head"]):
                conn.execute("UPDATE ledger_accounts SET observed_head=? WHERE environment=? AND account_id=?",
                             (cursor, environment, account_id))

    def apply_history(self, environment: str, account_id: str, requested_from_id: str,
                      transactions: Sequence[Mapping[str, object]], last_transaction_id: str,
                      *, unfiltered: bool) -> LedgerBalance:
        """Commit complete unfiltered history; streams never advance coverage."""
        with self.db.transaction() as conn:
            self.apply_history_in(conn, environment, account_id, requested_from_id,
                                  transactions, last_transaction_id, unfiltered=unfiltered)
        return self.balance(environment, account_id)

    def apply_history_in(self, conn: sqlite3.Connection, environment: str, account_id: str,
                         requested_from_id: str, transactions: Sequence[Mapping[str, object]],
                         last_transaction_id: str, *, unfiltered: bool) -> None:
        """Share the worker's atomic native-facts / projection commit."""
        if not conn.in_transaction:
            raise ValidationFailure("Historical coverage requires an active SQL transaction")
        _scope(environment, account_id)
        start, end = native_id(requested_from_id), native_id(last_transaction_id)
        if not unfiltered or int(end) < int(start):
            raise ValidationFailure("Only a complete, unfiltered forward REST history cut can advance coverage")
        ids = [native_id(tx.get("id")) for tx in transactions]
        if len(set(ids)) != len(ids) or any(not int(start) < int(tx_id) <= int(end) for tx_id in ids):
            raise ValidationFailure("REST history contains duplicate or out-of-cut native identities")
        if int(end) > int(start) and (not ids or max(map(int, ids)) != int(end)):
            raise ValidationFailure("An advancing unfiltered REST cut must contain its reported native head")
        row = conn.execute("SELECT * FROM ledger_accounts WHERE environment=? AND account_id=?",
                           (environment, account_id)).fetchone()
        if row is None or row["covered_cursor"] != start:
            raise Conflict("History response does not extend the current covered frontier")
        known = {str(item[0]) for item in conn.execute(
            "SELECT transaction_id FROM ledger_native_facts WHERE environment=? AND account_id=? "
            "AND (length(transaction_id),transaction_id)>(?,?) AND (length(transaction_id),transaction_id)<=(?,?)",
            (environment, account_id, len(start), start, len(end), end),
        )}
        if not known.issubset(set(ids)):
            raise ValidationFailure("An unfiltered REST response omitted an already observed native fact")
        for transaction in sorted(transactions, key=lambda item: int(native_id(item.get("id")))):
            self.ingest_native_tx(conn, environment, account_id, transaction, "REST")
        covered_cash = self._verify_balances(conn, environment, account_id, end)
        conn.execute("UPDATE ledger_accounts SET covered_cursor=?,covered_cash_balance=?,observed_head=?,revision=revision+1 "
                     "WHERE environment=? AND account_id=?", (
                         end, decimal_text(covered_cash), str(max(int(end), int(row["observed_head"]))), environment, account_id,
                     ))
        self._audit(conn, environment, account_id, "UNFILTERED_HISTORY_COVERED", {"from": start, "through": end, "count": len(ids)})

    @staticmethod
    def validate_stop_in(conn: sqlite3.Connection, environment: str, account_id: str,
                         snapshot_transaction_id: str, covered_transaction_id: str) -> None:
        """Revalidate ledger evidence inside the lease-retirement SQL transaction.

        The context verifier separately proves the complete native trade, position
        and order universe. This check defeats a new fill/fee arriving after its
        snapshot and requires every known financing obligation to be attributed.
        """
        _scope(environment, account_id)
        snapshot, covered = native_id(snapshot_transaction_id), native_id(covered_transaction_id)
        row = conn.execute("SELECT baseline_cursor,covered_cursor,observed_head FROM ledger_accounts "
                           "WHERE environment=? AND account_id=?", (environment, account_id)).fetchone()
        if row is None:
            raise SafetyFence("STOPPED requires the enrolled native account ledger")
        if not (int(row["baseline_cursor"]) <= int(snapshot) <= int(covered) <= int(row["covered_cursor"])
                and int(row["observed_head"]) <= int(snapshot)):
            raise SafetyFence("A later native fact or incomplete history invalidates the source STOPPED snapshot")
        if conn.execute("SELECT 1 FROM ledger_fences WHERE environment=? AND account_id=? AND resolved_at IS NULL LIMIT 1",
                        (environment, account_id)).fetchone() is not None:
            raise SafetyFence("An unresolved native ledger fence prevents source retirement")
        if conn.execute("SELECT 1 FROM ledger_financing_obligations WHERE environment=? AND account_id=? "
                        "AND settled_transaction_id IS NULL LIMIT 1", (environment, account_id)).fetchone() is not None:
            raise SafetyFence("Unattributed financing prevents source retirement")

    def _verify_balances(self, conn: sqlite3.Connection, environment: str, account_id: str,
                         through: str) -> Decimal:
        account = conn.execute("SELECT covered_cash_balance,covered_cursor FROM ledger_accounts WHERE environment=? AND account_id=?",
                               (environment, account_id)).fetchone()
        if account is None:
            raise Conflict("Missing account baseline")
        start = str(account["covered_cursor"])
        rows = conn.execute("SELECT * FROM ledger_fact_projection WHERE environment=? AND account_id=? "
                            "AND (length(transaction_id),transaction_id)>(?,?) AND (length(transaction_id),transaction_id)<=(?,?) "
                            "ORDER BY length(transaction_id),transaction_id",
                            (environment, account_id, len(start), start, len(through), through)).fetchall()
        running = money(account["covered_cash_balance"])
        for row in rows:
            tx_id = int(row["transaction_id"])
            if row["disposition"] in {"BOOKED", "QUARANTINED_BOOKED", "NONCASH"}:
                with FINANCIAL.local() as ctx:
                    ctx.prec = 512
                    running += money(row["cash_delta"])
            if row["account_balance"] is not None and running != money(row["account_balance"]):
                self._fence(conn, environment, account_id, str(tx_id), "CASH_BALANCE_MISMATCH", {
                    "projected": decimal_text(running), "native": row["account_balance"], "covered_through": str(through),
                })
        return running

    def balance(self, environment: str, account_id: str) -> LedgerBalance:
        _scope(environment, account_id)
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM ledger_accounts WHERE environment=? AND account_id=?",
                               (environment, account_id)).fetchone()
            if row is None:
                raise Conflict("Account cash is unavailable until an immutable native baseline is enrolled")
            fences = tuple(str(item[0]) for item in conn.execute(
                "SELECT DISTINCT reason FROM ledger_fences WHERE environment=? AND account_id=? AND resolved_at IS NULL ORDER BY reason",
                (environment, account_id),
            ))
            with FINANCIAL.local() as ctx:
                ctx.prec = 512
                obligations = conn.execute(
                    "SELECT estimated_amount FROM ledger_financing_obligations WHERE environment=? AND account_id=? "
                    "AND settled_transaction_id IS NULL", (environment, account_id),
                ).fetchall()
                unbooked = sum((money(item[0]) for item in obligations), _ZERO)
            return LedgerBalance(environment, account_id, str(row["currency"]), money(row["balance"]),
                                 str(row["baseline_cursor"]), str(row["covered_cursor"]), str(row["observed_head"]),
                                 int(row["revision"]), fences, unbooked, len(obligations))

    def checkpoint(self, environment: str, account_id: str, balance: Decimal,
                   transaction_id: str, observed_at: datetime) -> bool:
        """Retain an immutable audit cut; a newer snapshot never overwrites the baseline."""
        _scope(environment, account_id)
        amount, cursor = money(balance), native_id(transaction_id)
        observed = require_utc(observed_at).isoformat()
        digest = sha256_json({"balance": decimal_text(amount), "cursor": cursor, "observed_at": observed})
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM ledger_accounts WHERE environment=? AND account_id=?",
                               (environment, account_id)).fetchone()
            if row is None:
                raise Conflict("Missing account baseline")
            verified = int(row["baseline_cursor"]) <= int(cursor) <= int(row["covered_cursor"])
            if verified:
                with FINANCIAL.local() as ctx:
                    ctx.prec = 512
                    if cursor == row["covered_cursor"]:
                        expected = money(row["covered_cash_balance"])
                    else:
                        expected = money(row["baseline_balance"])
                        for item in conn.execute("SELECT transaction_id,amount FROM ledger_cash_journal WHERE environment=? AND account_id=? "
                                                 "AND side='CASH'", (environment, account_id)):
                            if int(item["transaction_id"]) <= int(cursor):
                                expected += money(item["amount"])
                if expected != amount:
                    verified = False
                    self._fence(conn, environment, account_id, cursor, "CHECKPOINT_MISMATCH", {"snapshot": digest})
            conn.execute("INSERT OR IGNORE INTO ledger_checkpoints VALUES(?,?,?,?,?,?,?)", (
                environment, account_id, digest, cursor, decimal_text(amount), observed, int(verified),
            ))
            return verified

    def add_financing_obligation(self, environment: str, account_id: str, obligation_id: str,
                                 estimated_amount: Decimal, evidence_digest: str) -> None:
        """Record a signed, unbooked estimate with a durable evidence identity."""
        _scope(environment, account_id)
        amount = money(estimated_amount)
        if amount == 0 or not obligation_id or not re.fullmatch(r"[0-9a-f]{64}", evidence_digest):
            raise ValidationFailure("Financing accrual needs a stable identity and SHA-256 evidence")
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM ledger_accounts WHERE environment=? AND account_id=?",
                            (environment, account_id)).fetchone() is None:
                raise Conflict("A financing obligation requires an enrolled account")
            old = conn.execute("SELECT estimated_amount,evidence_digest FROM ledger_financing_obligations "
                               "WHERE environment=? AND account_id=? AND obligation_id=?",
                               (environment, account_id, obligation_id)).fetchone()
            if old is not None:
                if money(old[0]) != amount or old[1] != evidence_digest:
                    raise Conflict("An accrued financing identity cannot be reused with different evidence")
                return
            conn.execute("INSERT INTO ledger_financing_obligations VALUES(?,?,?,?,?,?,NULL,NULL,NULL,NULL)", (
                environment, account_id, obligation_id, decimal_text(amount), evidence_digest, utc_now().isoformat(),
            ))

    def settle_financing_obligation(self, environment: str, account_id: str, obligation_id: str,
                                    transaction_id: str, allocated_amount: Decimal,
                                    settlement_evidence: str) -> None:
        """Replace an estimate by its root booking exactly once; retain estimate variance.

        This attribution requires a qualified financing calendar/reconciler. Merely
        matching the amount is not proof that an arbitrary fee belongs to a period.
        """
        _scope(environment, account_id)
        tx_id, amount = native_id(transaction_id), money(allocated_amount)
        if not re.fullmatch(r"[0-9a-f]{64}", settlement_evidence):
            raise ValidationFailure("Financing settlement needs independent attribution evidence")
        with self.db.transaction() as conn:
            obligation = conn.execute("SELECT * FROM ledger_financing_obligations WHERE environment=? AND account_id=? AND obligation_id=?",
                                      (environment, account_id, obligation_id)).fetchone()
            if obligation is None:
                raise Conflict("Unknown financing obligation")
            if obligation["settled_transaction_id"] is not None:
                if (obligation["settled_transaction_id"] == tx_id and money(obligation["settled_amount"]) == amount
                        and obligation["settlement_evidence"] == settlement_evidence):
                    return
                raise Conflict("Financing obligation is already attributed")
            root = conn.execute("SELECT amount FROM ledger_cash_journal WHERE environment=? AND account_id=? AND transaction_id=? "
                                "AND component='FINANCING' AND side='CASH'", (environment, account_id, tx_id)).fetchone()
            if root is None:
                raise Conflict("Financing must be posted by a native root transaction before attribution")
            root_amount = money(root[0])
            with FINANCIAL.local() as ctx:
                ctx.prec = 512
                allocated = sum((money(row[0]) for row in conn.execute(
                    "SELECT settled_amount FROM ledger_financing_obligations WHERE environment=? AND account_id=? AND settled_transaction_id=?",
                    (environment, account_id, tx_id),
                )), _ZERO)
                if amount == 0 or amount.is_signed() != root_amount.is_signed() or abs(allocated + amount) > abs(root_amount):
                    raise ValidationFailure("Financing allocation exceeds or opposes the root booking")
            conn.execute("UPDATE ledger_financing_obligations SET settled_transaction_id=?,settled_amount=?,settlement_evidence=?,settled_at=? "
                         "WHERE environment=? AND account_id=? AND obligation_id=?", (
                             tx_id, decimal_text(amount), settlement_evidence, utc_now().isoformat(), environment, account_id, obligation_id,
                         ))
            self._audit(conn, environment, account_id, "FINANCING_ESTIMATE_REPLACED", {
                "obligation_id": obligation_id, "transaction_id": tx_id,
                "estimated": obligation["estimated_amount"], "booked": decimal_text(amount), "evidence": settlement_evidence,
            })
