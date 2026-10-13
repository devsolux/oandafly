from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from oandafly.accounts.performance import PerformanceCut, PerformanceTracker
from oandafly.domain.errors import Conflict
from tests.support import supported_database

D = Decimal
NOW = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)


def cut(sequence: int, equity: str, flow: str = "0", **overrides: object) -> PerformanceCut:
    return PerformanceCut.model_validate({"environment": "SANDBOX", "account_id": "A", "series_id": "qualified-1",
        "native_cursor": str(sequence), "observed_at": NOW + timedelta(seconds=sequence),
        "equity": D(equity), "ending_external_flow": D(flow), "source_evidence_digest": "a" * 64,
        "coverage_complete": True, "interval_flow_timing_verified": True, **overrides})


def test_end_interval_deposit_is_not_profit_and_watermark_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "performance.sqlite3"
    db = supported_database(path)
    tracker = PerformanceTracker(db)
    tracker.record(cut(1, "100"))
    first_gain = tracker.record(cut(2, "110"))
    deposit = tracker.record(cut(3, "210", "100"))
    assert first_gain.return_index == D("1.1") and deposit.return_index == D("1.1")
    db.close()
    db = supported_database(path)
    tracker = PerformanceTracker(db)
    drawdown = tracker.record(cut(4, "189"))
    assert drawdown.return_index == D("0.99") and drawdown.high_water_index == D("1.1")
    assert drawdown.drawdown_fraction == D("0.1") and drawdown.maximum_drawdown_fraction == D("0.1")
    assert tracker.record(cut(4, "189")) == drawdown
    db.close()


def test_zero_equity_terminates_series_and_cannot_auto_resume(tmp_path: Path) -> None:
    db = supported_database(tmp_path / "performance.sqlite3")
    tracker = PerformanceTracker(db)
    tracker.record(cut(1, "100"))
    state = tracker.record(cut(2, "0"))
    assert state.terminal and state.maximum_drawdown_fraction == 1
    with pytest.raises(Conflict):
        tracker.record(cut(3, "100", "100"))
    db.close()


@pytest.mark.parametrize("override", [{"coverage_complete": False}, {"interval_flow_timing_verified": False}])
def test_unknown_intraperiod_cashflow_timing_rejects_performance(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        cut(1, "100", **override)
