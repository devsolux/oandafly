"""Durable account performance cuts with explicitly qualified cash-flow timing.

Cash journals remain exact. Return indexes are derived statistics, rounded once
to 28 decimal places with a recorded calculation policy. No authority pooling.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Annotated

from pydantic import BeforeValidator, Field, StrictBool, model_validator

from oandafly.domain.errors import Conflict, ValidationFailure
from oandafly.domain.primitives import StrictModel, decimal_text, money, require_utc, sha256_json
from oandafly.storage.database import Database
from oandafly.storage.ledger import native_id

Financial = Annotated[Decimal, BeforeValidator(money)]
_QUANTUM = Decimal("0.0000000000000000000000000001")
_POLICY = "TWR_END_FLOW_DECIMAL28_HALF_EVEN_V1"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_performance_cuts (
 environment TEXT NOT NULL, account_id TEXT NOT NULL, series_id TEXT NOT NULL,
 sequence INTEGER NOT NULL, native_cursor TEXT NOT NULL, observed_at TEXT NOT NULL,
 evidence_digest TEXT NOT NULL, input_digest TEXT NOT NULL,
 equity TEXT NOT NULL, ending_external_flow TEXT NOT NULL,
 return_index TEXT NOT NULL, high_water_index TEXT NOT NULL,
 drawdown_fraction TEXT NOT NULL, maximum_drawdown_fraction TEXT NOT NULL,
 calculation_policy TEXT NOT NULL, terminal INTEGER NOT NULL CHECK(terminal IN (0,1)),
 PRIMARY KEY(environment,account_id,series_id,sequence),
 UNIQUE(environment,account_id,series_id,input_digest));
CREATE TRIGGER IF NOT EXISTS account_performance_cuts_no_update BEFORE UPDATE ON account_performance_cuts
BEGIN SELECT RAISE(ABORT,'Performance audit cuts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS account_performance_cuts_no_delete BEFORE DELETE ON account_performance_cuts
BEGIN SELECT RAISE(ABORT,'Performance audit cuts are immutable'); END;
"""


class PerformanceCut(StrictModel):
    environment: str = Field(pattern=r"^(LIVE|SANDBOX)$")
    account_id: str = Field(min_length=1)
    series_id: str = Field(min_length=1)
    native_cursor: str
    observed_at: datetime
    equity: Financial = Field(ge=0)
    ending_external_flow: Financial = Decimal(0)
    source_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage_complete: StrictBool
    interval_flow_timing_verified: StrictBool

    @model_validator(mode="after")
    def _valid(self) -> PerformanceCut:
        require_utc(self.observed_at)
        native_id(self.native_cursor)
        if not self.coverage_complete or not self.interval_flow_timing_verified:
            raise ValueError("A time-weighted performance cut needs complete history and verified flow timing")
        return self


class PerformanceState(StrictModel):
    sequence: int
    return_index: Financial
    high_water_index: Financial
    drawdown_fraction: Financial
    maximum_drawdown_fraction: Financial
    terminal: StrictBool
    calculation_policy: str = _POLICY


class PerformanceTracker:
    """Persist qualified performance observations; context reset cannot erase them."""

    def __init__(self, db: Database) -> None:
        self.db = db
        db.initialize(_SCHEMA, component=__name__)

    def record(self, cut: PerformanceCut) -> PerformanceState:
        digest = sha256_json(cut.model_dump(mode="json"))
        key = (cut.environment, cut.account_id, cut.series_id)
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT * FROM account_performance_cuts WHERE environment=? AND account_id=? AND series_id=? "
                                    "AND input_digest=?", (*key, digest)).fetchone()
            if existing is not None:
                return self._state(existing)
            previous = conn.execute("SELECT * FROM account_performance_cuts WHERE environment=? AND account_id=? AND series_id=? "
                                    "ORDER BY sequence DESC LIMIT 1", key).fetchone()
            with localcontext() as ctx:
                ctx.prec = 160
                if previous is None:
                    if cut.equity <= 0 or cut.ending_external_flow != 0:
                        raise ValidationFailure("Initialize a performance series at positive post-funding equity with no interval flow")
                    index = high = Decimal(1)
                    drawdown = maximum = Decimal(0)
                    sequence = 1
                else:
                    if previous["terminal"]:
                        raise Conflict("An insolvent/fully redeemed performance series cannot silently resume")
                    if (cut.observed_at <= datetime.fromisoformat(previous["observed_at"])
                            or int(cut.native_cursor) < int(previous["native_cursor"])):
                        raise Conflict("Performance evidence must advance time without regressing native history")
                    previous_equity = money(previous["equity"])
                    pre_flow_equity = cut.equity - cut.ending_external_flow
                    if previous_equity <= 0 or pre_flow_equity < 0:
                        raise ValidationFailure("Cash-flow-adjusted equity cannot be negative in a live return series")
                    index = (money(previous["return_index"]) * pre_flow_equity / previous_equity).quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)
                    high = max(money(previous["high_water_index"]), index)
                    drawdown = ((high - index) / high).quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)
                    maximum = max(money(previous["maximum_drawdown_fraction"]), drawdown)
                    sequence = int(previous["sequence"]) + 1
            terminal = cut.equity == 0 or index == 0
            conn.execute("INSERT INTO account_performance_cuts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                *key, sequence, cut.native_cursor, require_utc(cut.observed_at).isoformat(), cut.source_evidence_digest,
                digest, decimal_text(cut.equity), decimal_text(cut.ending_external_flow), decimal_text(index),
                decimal_text(high), decimal_text(drawdown), decimal_text(maximum), _POLICY, int(terminal),
            ))
            return PerformanceState(sequence=sequence, return_index=index, high_water_index=high,
                                    drawdown_fraction=drawdown, maximum_drawdown_fraction=maximum, terminal=terminal)

    @staticmethod
    def _state(row: object) -> PerformanceState:
        # SQLite rows expose keyed access, kept local to avoid a second connection.
        import sqlite3

        if not isinstance(row, sqlite3.Row):
            raise Conflict("Invalid performance storage row")
        return PerformanceState(sequence=row["sequence"], return_index=money(row["return_index"]),
                                high_water_index=money(row["high_water_index"]), drawdown_fraction=money(row["drawdown_fraction"]),
                                maximum_drawdown_fraction=money(row["maximum_drawdown_fraction"]), terminal=bool(row["terminal"]),
                                calculation_policy=row["calculation_policy"])
