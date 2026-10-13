"""Atomic local preparation and permanent, account-scoped intent identities."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from oandafly.domain.context import ContextKey
from oandafly.domain.errors import Conflict, SafetyFence, ValidationFailure
from oandafly.domain.financial import FINANCIAL
from oandafly.domain.policy import VerifiedAccountProfile
from oandafly.domain.primitives import StrictModel, canonical_json, decimal_text, money, sha256_json, utc_now
from oandafly.execution.risk import RiskEvidence, RiskRegistry
from oandafly.storage.database import Database


class AuthorityVerifier(Protocol):
    def profile_in(
        self, connection: sqlite3.Connection, context: ContextKey, version: int | None = None,
    ) -> VerifiedAccountProfile:
        """Resolve the immutable profile bound to this exact context lease."""

    def assert_mutation(
        self, connection: sqlite3.Connection, context: ContextKey,
        profile_version: int | None = None,
        action: Literal["ENTRY", "RECOVERY"] = "ENTRY",
    ) -> int:
        """Verify the still-owned authority lease inside the caller transaction."""


class OutboxStatus(StrEnum):
    PENDING_SUBMISSION = "PENDING_SUBMISSION"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    ACK_BY_FILL = "ACK_BY_FILL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class DispatchState(StrEnum):
    NOT_DISPATCHED = "NOT_DISPATCHED"
    DISPATCH_STARTED = "DISPATCH_STARTED"
    UNKNOWN = "UNKNOWN"
    RESPONDED = "RESPONDED"
    NOT_SENT = "NOT_SENT"


class OrderRequest(StrictModel):
    context: ContextKey
    account_id: str = Field(min_length=1, max_length=128)
    profile_version: int = Field(ge=1)
    instrument: str = Field(pattern=r"^[A-Z]{3}_[A-Z]{3}$")
    units: Decimal
    portfolio_target_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    order_type: Literal["MARKET", "LIMIT", "STOP", "MARKET_IF_TOUCHED"] = "MARKET"
    stop_price: Decimal = Field(gt=0)
    price: Decimal | None = Field(default=None, gt=0)
    price_bound: Decimal | None = Field(default=None, gt=0)
    take_profit_price: Decimal | None = Field(default=None, gt=0)
    trailing_stop_distance: Decimal | None = Field(default=None, gt=0)
    evidence: RiskEvidence

    @field_validator(
        "units", "stop_price", "price", "price_bound", "take_profit_price",
        "trailing_stop_distance", mode="before",
    )
    @classmethod
    def exact_financial_values(cls, value: object) -> Decimal | None:
        return None if value is None else money(value)  # type: ignore[arg-type]

    @model_validator(mode="after")
    def request_contract(self) -> OrderRequest:
        from oandafly.domain.instruments import CurrencyInstrument
        CurrencyInstrument(name=self.instrument)
        if str(self.context.product_track) != "TRADING":
            raise ValueError("DEVISEN DATA cannot contain execution requests")
        if self.units == 0:
            raise ValueError("zero-unit entry orders are forbidden")
        if self.profile_version != self.evidence.profile_version:
            raise ValueError("profile and risk evidence versions disagree")
        if self.order_type == "MARKET":
            if self.price is not None or self.price_bound is None:
                raise ValueError("market orders require priceBound and do not accept price")
        elif self.price is None:
            raise ValueError("a pending order requires an explicit trigger/limit price")
        if self.order_type in {"STOP", "MARKET_IF_TOUCHED"} and self.price_bound is None:
            raise ValueError("triggered market orders require a bounded adverse fill price")
        if self.order_type == "LIMIT" and self.price_bound is not None:
            raise ValueError("the v20 LimitOrderRequest does not accept priceBound")
        return self


class OutboxRecord(StrictModel):
    intent_id: str
    client_id: str
    request: OrderRequest
    status: OutboxStatus
    dispatch_state: DispatchState
    reservation: Decimal
    reservation_active: bool
    payload_hash: str
    payload: Mapping[str, object]
    native_order_id: str | None
    native_fill_id: str | None
    invocation_marker: str | None
    version: int


def _environment(context: ContextKey) -> str:
    return str(context.environment)


def native_payload(request: OrderRequest, client_id: str) -> dict[str, object]:
    order: dict[str, object] = {
        "type": request.order_type,
        "instrument": request.instrument,
        "units": decimal_text(request.units),
        "timeInForce": "FOK" if request.order_type == "MARKET" else "GTC",
        "positionFill": "OPEN_ONLY",
        "clientExtensions": {"id": client_id, "tag": "oandafly"},
        "tradeClientExtensions": {"id": f"{client_id}_trade", "tag": "oandafly"},
        "stopLossOnFill": {"price": decimal_text(request.stop_price), "timeInForce": "GTC"},
    }
    if request.price is not None:
        order["price"] = decimal_text(request.price)
    if request.price_bound is not None:
        order["priceBound"] = decimal_text(request.price_bound)
    if request.take_profit_price is not None:
        order["takeProfitOnFill"] = {"price": decimal_text(request.take_profit_price), "timeInForce": "GTC"}
    if request.trailing_stop_distance is not None:
        order["trailingStopLossOnFill"] = {
            "distance": decimal_text(request.trailing_stop_distance), "timeInForce": "GTC",
        }
    return {"order": order}


class Outbox:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS outbox (
        intent_id TEXT PRIMARY KEY,
        client_id TEXT NOT NULL,
        authority_id TEXT NOT NULL,
        environment TEXT NOT NULL,
        account_id TEXT NOT NULL,
        context_json TEXT NOT NULL,
        profile_version INTEGER NOT NULL,
        authority_revision INTEGER NOT NULL,
        request_json TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        reservation TEXT NOT NULL,
        reservation_active INTEGER NOT NULL CHECK(reservation_active IN (0,1)),
        status TEXT NOT NULL,
        dispatch_state TEXT NOT NULL,
        dispatch_nonce TEXT,
        invocation_marker TEXT,
        native_order_id TEXT,
        native_fill_id TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 0,
        UNIQUE(environment,account_id,client_id)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS outbox_native_order_identity
        ON outbox(environment,account_id,native_order_id)
        WHERE native_order_id IS NOT NULL;
    CREATE INDEX IF NOT EXISTS outbox_scope_status
        ON outbox(authority_id,dispatch_state,status);
    CREATE TABLE IF NOT EXISTS outbox_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        intent_id TEXT NOT NULL REFERENCES outbox(intent_id),
        event TEXT NOT NULL,
        native_transaction_id TEXT,
        detail_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS execution_fences (
        fence_id TEXT PRIMARY KEY,
        authority_id TEXT,
        environment TEXT NOT NULL,
        account_id TEXT NOT NULL,
        code TEXT NOT NULL,
        detail_json TEXT NOT NULL,
        resolved_evidence TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS execution_native_facts (
        environment TEXT NOT NULL,
        account_id TEXT NOT NULL,
        transaction_id TEXT NOT NULL,
        digest TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        reduced INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL,
        PRIMARY KEY(environment,account_id,transaction_id)
    );
    CREATE TABLE IF NOT EXISTS execution_orders (
        environment TEXT NOT NULL,
        account_id TEXT NOT NULL,
        order_id TEXT NOT NULL,
        intent_id TEXT REFERENCES outbox(intent_id),
        trade_id TEXT,
        instrument TEXT,
        order_type TEXT NOT NULL,
        state TEXT NOT NULL,
        units TEXT,
        last_transaction_id TEXT NOT NULL,
        PRIMARY KEY(environment,account_id,order_id)
    );
    CREATE TABLE IF NOT EXISTS execution_trades (
        environment TEXT NOT NULL,
        account_id TEXT NOT NULL,
        trade_id TEXT NOT NULL,
        intent_id TEXT REFERENCES outbox(intent_id),
        instrument TEXT NOT NULL,
        initial_units TEXT NOT NULL,
        current_units TEXT NOT NULL,
        stop_loss_order_id TEXT,
        take_profit_order_id TEXT,
        trailing_stop_order_id TEXT,
        last_transaction_id TEXT NOT NULL,
        PRIMARY KEY(environment,account_id,trade_id)
    );
    """

    def __init__(self, database: Database, authority: AuthorityVerifier, risk: RiskRegistry) -> None:
        self.db = database
        self.authority = authority
        self.risk = risk
        self.db.initialize(self.SCHEMA, component=__name__)

    def event_in(
        self, connection: sqlite3.Connection, intent_id: str, event: str,
        native_transaction_id: str | None = None, detail: dict[str, object] | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO outbox_events(intent_id,event,native_transaction_id,detail_json,created_at) VALUES(?,?,?,?,?)",
            (intent_id, event, native_transaction_id, canonical_json(detail or {}), utc_now().isoformat()),
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> OutboxRecord:
        import json
        return OutboxRecord(
            intent_id=row["intent_id"], client_id=row["client_id"],
            request=OrderRequest.model_validate_json(row["request_json"]),
            status=OutboxStatus(row["status"]), dispatch_state=DispatchState(row["dispatch_state"]),
            reservation=money(row["reservation"]), reservation_active=bool(row["reservation_active"]),
            payload_hash=row["payload_hash"], payload=json.loads(row["payload_json"]),
            native_order_id=row["native_order_id"], native_fill_id=row["native_fill_id"],
            invocation_marker=row["invocation_marker"], version=row["version"],
        )

    def get_in(self, connection: sqlite3.Connection, intent_id: str) -> OutboxRecord:
        row = connection.execute("SELECT * FROM outbox WHERE intent_id=?", (intent_id,)).fetchone()
        if row is None:
            raise ValidationFailure("unknown local order intent")
        return self._record(row)

    def get(self, intent_id: str) -> OutboxRecord:
        with self.db.read() as connection:
            return self.get_in(connection, intent_id)

    @FINANCIAL.calculation
    def prepare(self, request: OrderRequest, *, intent_id: UUID | None = None) -> OutboxRecord:
        """Phase A: local intent, payload, evidence and reservation commit together."""
        request = OrderRequest.model_validate_json(request.model_dump_json())
        identity = intent_id or uuid4()
        client_id = f"oaf_{identity.hex}"
        payload = native_payload(request, client_id)
        payload_hash = sha256_json(payload)
        serialized_request = request.model_dump_json()
        with self.db.transaction() as connection:
            existing = connection.execute("SELECT * FROM outbox WHERE intent_id=?", (str(identity),)).fetchone()
            if existing is not None:
                if existing["payload_hash"] != payload_hash or existing["request_json"] != serialized_request:
                    raise Conflict("a consumed intent UUID cannot be rebound to another request or epoch")
                return self._record(existing)
            if self.has_unresolved_in(connection, request.context.authority_id, ambiguity_only=True):
                raise SafetyFence("an ambiguous prior mutation fences new order preparation")
            authority_revision = self.authority.assert_mutation(
                connection, request.context, request.profile_version, action="ENTRY",
            )
            if authority_revision != request.evidence.authority_revision:
                raise SafetyFence("authority revision changed after the risk evidence cut")
            self._assert_profile_scope_in(connection, request)
            self.assert_no_fences_in(connection, request.context.authority_id, _environment(request.context), request.account_id)
            required = self.risk.validate_in(
                connection, authority_id=request.context.authority_id, environment=_environment(request.context),
                account_id=request.account_id, instrument=request.instrument, units=request.units,
                stop_price=request.stop_price, expected=request.evidence, now=utc_now(),
                entry_bound=request.price if request.order_type == "LIMIT" else request.price_bound,
            )
            now = utc_now().isoformat()
            connection.execute(
                """INSERT INTO outbox(intent_id,client_id,authority_id,environment,account_id,context_json,
                profile_version,authority_revision,request_json,payload_json,payload_hash,evidence_json,
                reservation,reservation_active,status,dispatch_state,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)""",
                (str(identity), client_id, request.context.authority_id, _environment(request.context),
                 request.account_id, request.context.model_dump_json(), request.profile_version,
                 authority_revision, serialized_request, canonical_json(payload), payload_hash,
                 request.evidence.model_dump_json(), decimal_text(required),
                 OutboxStatus.PENDING_SUBMISSION, DispatchState.NOT_DISPATCHED, now, now),
            )
            self.event_in(connection, str(identity), "PREPARED", detail={"payload_hash": payload_hash})
            return self.get_in(connection, str(identity))

    @staticmethod
    def assert_no_fences_in(connection: sqlite3.Connection, authority_id: str, environment: str, account_id: str) -> None:
        fence = connection.execute(
            "SELECT code FROM execution_fences WHERE resolved_evidence IS NULL AND environment=? AND account_id=? AND (authority_id=? OR authority_id IS NULL) LIMIT 1",
            (environment, account_id, authority_id),
        ).fetchone()
        if fence is not None:
            raise SafetyFence(f"execution fact fence remains unresolved: {fence['code']}")

    @FINANCIAL.calculation
    def validate_entry_in(self, connection: sqlite3.Connection, record: OutboxRecord) -> None:
        request = record.request
        if sha256_json(record.payload) != record.payload_hash or sha256_json(native_payload(request, record.client_id)) != record.payload_hash:
            raise SafetyFence("prepared payload hash or canonical request binding changed")
        if self.has_unresolved_in(connection, request.context.authority_id, ambiguity_only=True,
                                  exclude_intent_id=record.intent_id):
            raise SafetyFence("another uncertain invocation fences this prepared intent")
        revision = self.authority.assert_mutation(connection, request.context, request.profile_version, action="ENTRY")
        if revision != request.evidence.authority_revision:
            raise SafetyFence("authority changed after order preparation")
        self._assert_profile_scope_in(connection, request)
        self.assert_no_fences_in(connection, request.context.authority_id, _environment(request.context), request.account_id)
        required = self.risk.validate_in(
            connection, authority_id=request.context.authority_id, environment=_environment(request.context),
            account_id=request.account_id, instrument=request.instrument, units=request.units,
            stop_price=request.stop_price, expected=request.evidence, now=utc_now(), own_intent_id=record.intent_id,
            entry_bound=request.price if request.order_type == "LIMIT" else request.price_bound,
        )
        if required != record.reservation:
            raise SafetyFence("prepared reservation no longer matches independent risk calculations")

    def _assert_profile_scope_in(self, connection: sqlite3.Connection, request: OrderRequest) -> None:
        profile = self.authority.profile_in(connection, request.context, request.profile_version)
        if request.account_id not in profile.account_ids:
            raise SafetyFence("target account is outside the exact verified sub-account profile")
        if profile.api_binding == "V20":
            from oandafly.domain.decisions import PortfolioTarget
            from oandafly.portfolio.planner import OpenTrade, residual_plan
            if request.portfolio_target_sha256 is None:
                raise SafetyFence("Native entry requires an immutable approved portfolio target")
            target_row = connection.execute("SELECT target_json,suspended FROM portfolio_targets WHERE environment=? AND account_id=? AND instrument=?",
                                            (_environment(request.context), request.account_id, request.instrument)).fetchone()
            if target_row is None or target_row["suspended"]:
                raise SafetyFence("Portfolio target is absent or suspended")
            target = PortfolioTarget.model_validate_json(target_row["target_json"])
            if target.fingerprint != request.portfolio_target_sha256 or target.expires_at <= utc_now():
                raise SafetyFence("Portfolio target changed or expired before native entry")
            deployment = connection.execute("SELECT model_sha256 FROM model_deployments WHERE authority_id=?",
                                             (request.context.authority_id,)).fetchone()
            if deployment is None or deployment[0] != target.model_sha256:
                raise SafetyFence("Target does not belong to the deployed qualified model")
            trades = tuple(OpenTrade(row["trade_id"], request.instrument, money(row["current_units"]))
                           for row in connection.execute("SELECT trade_id,current_units FROM execution_trades WHERE environment=? AND account_id=? AND instrument=?",
                               (_environment(request.context), request.account_id, request.instrument)))
            residual = residual_plan(target.units, trades)
            if residual.operation != "ENTRY" or residual.units != request.units:
                raise SafetyFence("Prepared entry no longer equals the approved target residual")
        rule = profile.instrument_rule(request.instrument)
        row = connection.execute(
            "SELECT observation_json FROM execution_risk_observations WHERE authority_id=? AND environment=? AND account_id=? AND instrument=?",
            (request.context.authority_id, str(request.context.environment), request.account_id, request.instrument),
        ).fetchone()
        if row is None:
            raise SafetyFence("instrument risk observation is missing")
        from oandafly.execution.risk import RiskObservation
        observed = RiskObservation.model_validate_json(row["observation_json"]).instrument
        native = rule.instrument
        price_tick = Decimal("1").scaleb(-native.display_precision)
        if any(price is not None and price % price_tick for price in (
            request.price, request.price_bound, request.stop_price, request.take_profit_price,
            request.trailing_stop_distance,
        )):
            raise ValidationFailure("an order price exceeds the verified instrument displayPrecision")
        if (
            observed.trade_units_precision != native.trade_units_precision
            or observed.pip_location != native.pip_location
            or observed.minimum_trade_size != native.minimum_trade_size
            or observed.maximum_order_units > native.maximum_order_units
            or observed.requested_leverage > rule.maximum_leverage
            or any(tier.rate < native.margin_rate for tier in observed.margin_tiers)
            # Profile leverage can be stricter than the broker's base margin.
            or any(tier.rate * rule.maximum_leverage < 1 for tier in observed.margin_tiers)
        ):
            raise SafetyFence("risk observation widens the immutable verified instrument policy")

    def has_unresolved_in(
        self, connection: sqlite3.Connection, authority_id: str, *, ambiguity_only: bool = False,
        exclude_intent_id: str | None = None,
    ) -> bool:
        clause = "dispatch_state IN ('DISPATCH_STARTED','UNKNOWN')"
        if not ambiguity_only:
            clause += " OR status IN ('PENDING_SUBMISSION','ACKNOWLEDGED')"
        pending = connection.execute(
            f"SELECT 1 FROM outbox WHERE authority_id=? AND (? IS NULL OR intent_id!=?) AND ({clause}) LIMIT 1",
            (authority_id, exclude_intent_id, exclude_intent_id),
        ).fetchone() is not None
        if pending:
            return True
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_recovery'").fetchone():
            if connection.execute(
                "SELECT 1 FROM execution_recovery WHERE authority_id=? AND state IN ('PREPARED','DISPATCH_STARTED','UNKNOWN') LIMIT 1",
                (authority_id,),
            ).fetchone():
                return True
        if connection.execute(
            """SELECT 1 FROM execution_native_facts f WHERE f.reduced=0 AND EXISTS(
            SELECT 1 FROM outbox o WHERE o.authority_id=? AND o.environment=f.environment
            AND o.account_id=f.account_id) LIMIT 1""", (authority_id,),
        ).fetchone():
            return True
        return connection.execute(
            """SELECT 1 FROM execution_fences f WHERE f.resolved_evidence IS NULL AND
            (f.authority_id=? OR EXISTS(SELECT 1 FROM outbox o WHERE o.authority_id=?
            AND o.environment=f.environment AND o.account_id=f.account_id)) LIMIT 1""",
            (authority_id, authority_id),
        ).fetchone() is not None

    def cancel_prepared(self, intent_id: str) -> OutboxRecord:
        """Only a provably never-claimed request may be cancelled locally."""
        with self.db.transaction() as connection:
            record = self.get_in(connection, intent_id)
            if record.status == OutboxStatus.CANCELLED and record.dispatch_state == DispatchState.NOT_SENT:
                return record
            cursor = connection.execute(
                """UPDATE outbox SET status='CANCELLED',dispatch_state='NOT_SENT',reservation_active=0,
                updated_at=?,version=version+1 WHERE intent_id=? AND dispatch_state='NOT_DISPATCHED'
                AND status='PENDING_SUBMISSION' AND invocation_marker IS NULL AND dispatch_nonce IS NULL""",
                (utc_now().isoformat(), intent_id),
            )
            if cursor.rowcount != 1:
                raise SafetyFence("claimed or possibly transmitted orders require native reconciliation")
            self.event_in(connection, intent_id, "LOCAL_CANCELLED_NOT_SENT")
            return self.get_in(connection, intent_id)

    def open_trades(self, *, environment: str, account_id: str) -> tuple[dict[str, object], ...]:
        with self.db.read() as connection:
            rows = connection.execute(
                "SELECT * FROM execution_trades WHERE environment=? AND account_id=?", (environment, account_id),
            ).fetchall()
            return tuple(dict(row) for row in rows if money(row["current_units"]) != 0)

    def pending_orders(self, *, environment: str, account_id: str) -> tuple[dict[str, object], ...]:
        with self.db.read() as connection:
            return tuple(dict(row) for row in connection.execute(
                "SELECT * FROM execution_orders WHERE environment=? AND account_id=? AND state='PENDING'",
                (environment, account_id),
            ).fetchall())
