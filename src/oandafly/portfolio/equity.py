"""Persistent loss limits using native NAV minus actual external cash flows.

This is an explicit fixed-endowment monetary loss control, not a time-weighted
return calculation. Deposits cannot earn model reward or reset a drawdown fence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from oandafly.domain.capabilities import RiskLimits
from oandafly.domain.financial import FINANCIAL
from oandafly.domain.primitives import decimal_text, money, require_utc
from oandafly.storage.database import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_equity_limits (
 environment TEXT NOT NULL, account_id TEXT NOT NULL,
 initial_equity TEXT NOT NULL, high_water_adjusted TEXT NOT NULL,
 utc_day TEXT NOT NULL, day_start_adjusted TEXT NOT NULL, day_start_equity TEXT NOT NULL,
 last_observed_at TEXT NOT NULL, last_frontier TEXT NOT NULL,
 hard_fence TEXT, PRIMARY KEY(environment,account_id));
CREATE TABLE IF NOT EXISTS portfolio_equity_cuts (
 environment TEXT NOT NULL, account_id TEXT NOT NULL, observed_at TEXT NOT NULL,
 frontier TEXT NOT NULL, nav TEXT NOT NULL, cumulative_external_flows TEXT NOT NULL,
 adjusted_nav TEXT NOT NULL, source_sha256 TEXT NOT NULL,
 PRIMARY KEY(environment,account_id,observed_at));
"""


@dataclass(frozen=True)
class EquityStatus:
    adjusted_nav: Decimal
    daily_loss: Decimal
    drawdown: Decimal
    hard_fence: str | None


class EquityLimits:
    def __init__(self, db: Database) -> None:
        self.db = db
        db.initialize(SCHEMA, component=__name__)

    @FINANCIAL.calculation
    def observe(self, *, environment: str, account_id: str, nav: Decimal,
                frontier: str, observed_at: datetime, source_sha256: str, limits: RiskLimits) -> EquityStatus:
        nav, observed_at = money(nav), require_utc(observed_at)
        scope = (environment, account_id)
        with self.db.transaction() as connection:
            flows = sum((money(row[0]) for row in connection.execute(
                "SELECT amount FROM ledger_cash_journal WHERE environment=? AND account_id=? AND component='EXTERNAL_TRANSFER' AND side='CASH' AND (length(transaction_id),transaction_id)<=(?,?)",
                (*scope, len(frontier), frontier))), Decimal(0))
            adjusted = nav - flows
            old = connection.execute("SELECT * FROM portfolio_equity_limits WHERE environment=? AND account_id=?", scope).fetchone()
            day = observed_at.date().isoformat()
            if old is None:
                if nav <= 0:
                    raise ValueError("Initial account equity must be positive")
                # The endowment is flow-adjusted like the drawdown it bounds, so a
                # deposit before the first observation cannot loosen the fence.
                initial, high, start, day_equity = adjusted, adjusted, adjusted, nav
                fence = None
            else:
                if observed_at < datetime.fromisoformat(old["last_observed_at"]) or int(frontier) < int(old["last_frontier"]):
                    raise ValueError("Equity evidence cannot move backward")
                initial = money(old["initial_equity"])
                high = max(money(old["high_water_adjusted"]), adjusted)
                start = adjusted if day != old["utc_day"] else money(old["day_start_adjusted"])
                day_equity = nav if day != old["utc_day"] else money(old["day_start_equity"])
                fence = old["hard_fence"]
            daily_loss = max(Decimal(0), start - adjusted)
            drawdown = max(Decimal(0), high - adjusted)
            if nav <= 0:
                fence = fence or "NONPOSITIVE_NATIVE_EQUITY"
            elif daily_loss >= day_equity * limits.maximum_daily_loss_fraction:
                fence = fence or "DAILY_LOSS_LIMIT"
            elif drawdown >= initial * limits.maximum_drawdown_fraction:
                fence = fence or "DRAWDOWN_LIMIT"
            connection.execute("INSERT INTO portfolio_equity_limits VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(environment,account_id) DO UPDATE SET high_water_adjusted=excluded.high_water_adjusted,utc_day=excluded.utc_day,day_start_adjusted=excluded.day_start_adjusted,day_start_equity=excluded.day_start_equity,last_observed_at=excluded.last_observed_at,last_frontier=excluded.last_frontier,hard_fence=excluded.hard_fence",
                (*scope, decimal_text(initial), decimal_text(high), day, decimal_text(start), decimal_text(day_equity), observed_at.isoformat(), frontier, fence))
            connection.execute("INSERT OR IGNORE INTO portfolio_equity_cuts VALUES(?,?,?,?,?,?,?,?)",
                (*scope, observed_at.isoformat(), frontier, decimal_text(nav), decimal_text(flows), decimal_text(adjusted), source_sha256))
            return EquityStatus(adjusted, daily_loss, drawdown, fence)
