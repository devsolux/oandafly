"""Persistent desired exposure and one-shot residual planning, not repeated signals."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid5, NAMESPACE_URL

from oandafly.domain.decisions import Action, PolicyDecision, PortfolioTarget
from oandafly.domain.errors import SafetyFence
from oandafly.domain.financial import FINANCIAL
from oandafly.domain.primitives import canonical_json, money, utc_now
from oandafly.storage.database import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_decisions (
 environment TEXT NOT NULL, account_id TEXT NOT NULL, instrument TEXT NOT NULL,
 model_sha256 TEXT NOT NULL, bar_end TEXT NOT NULL, decision_id TEXT NOT NULL,
 decision_json TEXT NOT NULL, outcome TEXT NOT NULL, recorded_at TEXT NOT NULL,
 PRIMARY KEY(environment,account_id,instrument,model_sha256,bar_end));
CREATE TABLE IF NOT EXISTS portfolio_targets (
 environment TEXT NOT NULL, account_id TEXT NOT NULL, instrument TEXT NOT NULL,
 generation INTEGER NOT NULL, target_json TEXT NOT NULL, suspended INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(environment,account_id,instrument));
CREATE TABLE IF NOT EXISTS portfolio_events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, environment TEXT NOT NULL,
 account_id TEXT NOT NULL, instrument TEXT NOT NULL, generation INTEGER,
 event TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS portfolio_decisions_immutable BEFORE UPDATE ON portfolio_decisions
BEGIN SELECT RAISE(ABORT,'Decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS portfolio_events_immutable BEFORE UPDATE ON portfolio_events
BEGIN SELECT RAISE(ABORT,'Portfolio events are immutable'); END;
"""


@dataclass(frozen=True)
class OpenTrade:
    trade_id: str
    instrument: str
    units: Decimal


@dataclass(frozen=True)
class PlanStep:
    operation: Literal["WAIT", "NOOP", "ENTRY", "CLOSE_TRADE"]
    units: Decimal = Decimal(0)
    trade_id: str | None = None
    reason: str = ""


@FINANCIAL.calculation
def residual_plan(target: Decimal, trades: tuple[OpenTrade, ...], *, pending: bool = False) -> PlanStep:
    """Close opposing trades first. Never disguise a reversal as OPEN_ONLY."""
    target = money(target)
    if len({trade.trade_id for trade in trades}) != len(trades):
        raise ValueError("A native trade cannot occur twice in a residual cut")
    if len({trade.instrument for trade in trades}) > 1:
        raise ValueError("Residual planning cannot mix instruments")
    if pending:
        return PlanStep("WAIT", reason="UNRESOLVED_OR_UNREFLECTED_INTENT")
    ordered = sorted((t for t in trades if t.units != 0), key=lambda t: int(t.trade_id))
    for trade in ordered:
        if target == 0 or trade.units * target < 0:
            return PlanStep("CLOSE_TRADE", abs(trade.units), trade.trade_id, "CLOSE_BEFORE_REVERSE_OR_FLATTEN")
    current = sum((trade.units for trade in ordered), Decimal(0))
    if abs(current) > abs(target):
        excess = abs(current) - abs(target)
        first = ordered[0]
        return PlanStep("CLOSE_TRADE", min(excess, abs(first.units)), first.trade_id, "REDUCE_TO_TARGET")
    if current == target:
        return PlanStep("NOOP", reason="TARGET_ALREADY_REACHED")
    return PlanStep("ENTRY", target - current, reason="OPEN_ONLY_RESIDUAL")


class TargetStore:
    def __init__(self, db: Database) -> None:
        self.db = db
        db.initialize(SCHEMA, component=__name__)

    def seen(self, *, environment: str, account_id: str, instrument: str, model_sha256: str, bar_end: datetime) -> bool:
        with self.db.read() as connection:
            return connection.execute("SELECT 1 FROM portfolio_decisions WHERE environment=? AND account_id=? AND instrument=? AND model_sha256=? AND bar_end=?",
                (environment, account_id, instrument, model_sha256, bar_end.isoformat())).fetchone() is not None

    def accept(self, decision: PolicyDecision, *, environment: str, account_id: str,
               bar_end: datetime, units: Decimal | None, frontier: str, target_expires_at: datetime,
               outcome: str = "ACCEPTED") -> PortfolioTarget | None:
        decision = PolicyDecision.model_validate_json(decision.model_dump_json())
        now = utc_now()
        if decision.valid_until <= now or decision.observed_at > now or bar_end > decision.observed_at:
            raise SafetyFence("Cannot accept a stale or noncausal model decision")
        if units is not None:
            units = money(units)
        if decision.action == Action.FLATTEN and units != 0:
            raise ValueError("FLATTEN requires exactly zero target units")
        if decision.action in {Action.ABSTAIN, Action.KEEP} and units is not None:
            raise ValueError("ABSTAIN and KEEP cannot introduce a new target")
        if decision.action == Action.SET_TARGET and units is not None and units * Decimal(str(decision.preference)) <= 0:
            raise ValueError("Risk may tighten a target, never reverse the model's direction")
        key = (environment, account_id, decision.instrument)
        with self.db.transaction() as connection:
            old_decision = connection.execute("SELECT decision_id FROM portfolio_decisions WHERE environment=? AND account_id=? AND instrument=? AND model_sha256=? AND bar_end=?",
                (*key, decision.model_sha256, bar_end.isoformat())).fetchone()
            if old_decision is not None:
                # A fresh quote after restart is not permission to replay this bar.
                return None
            connection.execute("INSERT INTO portfolio_decisions VALUES(?,?,?,?,?,?,?,?,?)",
                (*key, decision.model_sha256, bar_end.isoformat(), decision.decision_id,
                 decision.model_dump_json(), outcome, now.isoformat()))
            old = connection.execute("SELECT * FROM portfolio_targets WHERE environment=? AND account_id=? AND instrument=?", key).fetchone()
            if decision.action == Action.KEEP:
                return None  # Preserve, but do not silently extend, an existing target's validity.
            if units is None or decision.action == Action.ABSTAIN:
                connection.execute("UPDATE portfolio_targets SET suspended=1 WHERE environment=? AND account_id=? AND instrument=?", key)
                return None
            if target_expires_at <= now:
                raise SafetyFence("The desired portfolio target has already expired")
            generation = 1 if old is None else old["generation"] + 1
            target = PortfolioTarget(environment=environment, account_id=account_id, instrument=decision.instrument,
                generation=generation, decision_id=decision.decision_id, model_sha256=decision.model_sha256,
                units=units, created_at=now, expires_at=target_expires_at, account_frontier=frontier)
            connection.execute("INSERT INTO portfolio_targets VALUES(?,?,?,?,?,0) ON CONFLICT(environment,account_id,instrument) DO UPDATE SET generation=excluded.generation,target_json=excluded.target_json,suspended=0",
                (*key, generation, target.model_dump_json()))
            connection.execute("INSERT INTO portfolio_events(environment,account_id,instrument,generation,event,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (*key, generation, "TARGET_ACCEPTED", canonical_json({"target": target.fingerprint}), now.isoformat()))
            return target

    def current(self, environment: str, account_id: str, instrument: str) -> PortfolioTarget | None:
        with self.db.read() as connection:
            row = connection.execute("SELECT target_json,suspended FROM portfolio_targets WHERE environment=? AND account_id=? AND instrument=?",
                (environment, account_id, instrument)).fetchone()
            if row is None or row["suspended"]:
                return None
            target = PortfolioTarget.model_validate_json(row["target_json"])
            return target if target.expires_at > utc_now() else None

    def suspend_all(self, environment: str, account_id: str, *, reason: str) -> None:
        with self.db.transaction() as connection:
            connection.execute("UPDATE portfolio_targets SET suspended=1 WHERE environment=? AND account_id=?", (environment, account_id))
            connection.execute("INSERT INTO portfolio_events(environment,account_id,instrument,generation,event,detail_json,created_at) VALUES(?,?,?,NULL,?,?,?)",
                (environment, account_id, "*", "TARGETS_SUSPENDED", canonical_json({"reason": reason}), utc_now().isoformat()))

    @staticmethod
    def action_id(target: PortfolioTarget, step: PlanStep) -> UUID:
        # At most one entry attempt and one reduction per TradeID for a target.
        # A rejection is not permission to manufacture a fresh equivalent intent.
        return uuid5(NAMESPACE_URL, "oandafly:target:" + target.fingerprint + ":" + step.operation + ":" + (step.trade_id or "ENTRY"))
