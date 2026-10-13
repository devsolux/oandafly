from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from oandafly.domain.errors import Conflict, SafetyFence, ValidationFailure
from oandafly.storage.database import Database
from tests.support import supported_database
from oandafly.storage.ledger import Ledger

D = Decimal
NOW = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("component", ["pl", "commission", "guaranteedExecutionFee"])
def test_journal_negation_preserves_every_decimal_digit(ledger: Ledger, db: Database, component: str) -> None:
    # Double-entry conservation must not depend on Python's default 28-digit context.
    amount = D("123456789012345678901234567890.123456789")
    event = fill("21", **{component: str(amount)})
    ledger.ingest("SANDBOX", "A", event)
    with db.read() as connection:
        rows = connection.execute("SELECT side,amount FROM ledger_cash_journal").fetchall()
    cash = next(D(row["amount"]) for row in rows if row["side"] == "CASH")
    contra = next(D(row["amount"]) for row in rows if row["side"] == "CONTRA")
    assert cash.copy_abs() == amount
    assert contra == cash.copy_negate()


def test_concurrent_native_replays_are_exactly_once_and_account_scoped(ledger: Ledger, db: Database) -> None:
    from concurrent.futures import ThreadPoolExecutor

    # Competing stream/REST writers share the same serialized financial commit.
    ledger.establish_baseline("SANDBOX", "B", "EUR", D(1), "10", NOW)
    events = [fill(str(index + 11), "0.01") for index in range(30)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda event: ledger.ingest("SANDBOX", "A", event), events * 4))
    assert ledger.balance("SANDBOX", "A").balance == D("1000.30")
    assert ledger.balance("SANDBOX", "B").balance == 1
    with db.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ledger_native_facts").fetchone()[0] == 30


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = supported_database(tmp_path / "ledger.sqlite3")
    yield database
    database.close()


@pytest.fixture
def ledger(db: Database) -> Ledger:
    result = Ledger(db)
    result.establish_baseline("SANDBOX", "A", "EUR", D("1000"), "10", NOW)
    return result


def fill(tx_id: str, pl: str = "0", financing: str = "0", **extra: object) -> dict[str, object]:
    return {"id": tx_id, "type": "ORDER_FILL", "accountID": "A", "instrument": "EUR_USD",
            "units": "100", "pl": pl, "financing": financing, **extra}


def test_root_cash_fees_and_nested_allocations_book_once(ledger: Ledger, db: Database) -> None:
    transaction = fill("20", "10", "-2", commission="1", guaranteedExecutionFee="0.5", accountBalance="1006.5",
                       halfSpreadCost="5", quotePL="11", tradesClosed=[{"tradeID": "2", "realizedPL": "10", "financing": "-2"}])
    first = ledger.ingest("SANDBOX", "A", transaction)
    second = ledger.ingest("SANDBOX", "A", transaction, "REST")
    assert first.cash_delta == D("6.5") and not first.duplicate
    assert second.duplicate and ledger.balance("SANDBOX", "A").balance == D("1006.5")
    result = ledger.apply_history("SANDBOX", "A", "10", [transaction], "20", unfiltered=True)
    assert result.history_caught_up
    with db.read() as conn:
        entries = conn.execute("SELECT * FROM ledger_cash_journal").fetchall()
        assert len(entries) == 8
        assert sum((D(row["amount"]) for row in entries), D(0)) == 0
        assert {row["currency"] for row in entries} == {"EUR"}
        assert all(D(row["amount"]) != 100 for row in entries)


def test_stream_head_is_not_history_coverage_and_gaps_are_legal(ledger: Ledger) -> None:
    later = fill("40", "5", accountBalance="1007")
    earlier = fill("25", "2", accountBalance="1002")
    ledger.ingest("SANDBOX", "A", later)
    pending = ledger.balance("SANDBOX", "A")
    assert pending.balance == 1005 and pending.covered_cursor == "10" and pending.observed_head == "40"
    assert not pending.history_caught_up and not pending.fences
    result = ledger.apply_history("SANDBOX", "A", "10", [later, earlier], "40", unfiltered=True)
    assert result.balance == 1007 and result.history_caught_up and not result.fences


def test_stream_heartbeat_does_not_claim_missing_history(ledger: Ledger) -> None:
    ledger.record_heartbeat("SANDBOX", "A", "300")
    result = ledger.balance("SANDBOX", "A")
    assert result.covered_cursor == "10" and result.observed_head == "300"
    assert not result.history_caught_up


def test_immutable_baseline_and_prebaseline_no_double_booking(ledger: Ledger) -> None:
    ledger.ingest("SANDBOX", "A", fill("9", "500"))
    assert ledger.balance("SANDBOX", "A").balance == 1000
    ledger.establish_baseline("SANDBOX", "A", "EUR", D("1000"), "10", NOW)
    with pytest.raises(Conflict):
        ledger.establish_baseline("SANDBOX", "A", "EUR", D("1500"), "20", NOW)


def test_raw_early_fill_waits_for_baseline_but_is_not_receipt_quarantined(db: Database) -> None:
    ledger = Ledger(db)
    result = ledger.ingest("SANDBOX", "A", fill("20", "2"))
    assert result.disposition == "AWAITING_BASELINE" and not result.quarantined
    baseline = ledger.establish_baseline("SANDBOX", "A", "EUR", D(1000), "10", NOW)
    assert baseline.balance == 1002 and "NO_BASELINE" not in baseline.fences


def test_snapshot_covering_early_fill_does_not_book_it_again(db: Database) -> None:
    ledger = Ledger(db)
    ledger.ingest("SANDBOX", "A", fill("20", "2"))
    baseline = ledger.establish_baseline("SANDBOX", "A", "EUR", D(1002), "20", NOW)
    assert baseline.balance == 1002


def test_duplicate_identity_with_different_payload_is_durably_fenced(ledger: Ledger) -> None:
    ledger.ingest("SANDBOX", "A", fill("20", "2"))
    conflict = ledger.ingest("SANDBOX", "A", fill("20", "200"))
    assert conflict.quarantined
    state = ledger.balance("SANDBOX", "A")
    assert state.balance == 1002 and "NATIVE_IDENTITY_CONFLICT" in state.fences


@pytest.mark.parametrize("payload", [
    {"id": "20", "type": "FUTURE_FEE", "accountBalance": "990"},
    fill("20", "1", commission="-1"),
    fill("20", "NaN"),
    fill("20", "1", accountID="OTHER"),
    {"id": "20", "type": "ORDER_CANCEL", "financing": "-1"},
])
def test_unknown_or_malformed_financial_fact_fences_without_invented_cash(ledger: Ledger, payload: dict[str, object]) -> None:
    result = ledger.ingest("SANDBOX", "A", payload)
    assert result.quarantined
    assert ledger.balance("SANDBOX", "A").balance == 1000


def test_non_fx_event_preserves_native_cash_and_fences_account(ledger: Ledger) -> None:
    event = fill("20", "4", instrument="XAU_USD")
    result = ledger.ingest("SANDBOX", "A", event)
    assert result.quarantined and ledger.balance("SANDBOX", "A").balance == 1004
    assert "NON_FX_EVENT" in ledger.balance("SANDBOX", "A").fences


def test_root_daily_financing_never_sums_trade_children(ledger: Ledger) -> None:
    transaction: dict[str, object] = {"id": "20", "type": "DAILY_FINANCING", "financing": "-3",
        "accountBalance": "997", "positionFinancings": [{"instrument": "EUR_USD", "financing": "-3",
            "openTradeFinancings": [{"tradeID": "7", "financing": "-3"}]}]}
    result = ledger.apply_history("SANDBOX", "A", "10", [transaction], "20", unfiltered=True)
    assert result.balance == 997 and not result.fences


def test_cash_account_balance_discrepancy_only_after_complete_history(ledger: Ledger) -> None:
    bad = fill("20", "3", accountBalance="900")
    ledger.ingest("SANDBOX", "A", bad)
    assert not ledger.balance("SANDBOX", "A").fences
    result = ledger.apply_history("SANDBOX", "A", "10", [bad], "20", unfiltered=True)
    assert "CASH_BALANCE_MISMATCH" in result.fences and not result.history_caught_up


def test_atomic_reducer_failure_rolls_back_raw_and_money(ledger: Ledger, db: Database) -> None:
    with pytest.raises(RuntimeError, match="injected"):
        with db.transaction() as conn:
            ledger.ingest_native_tx(conn, "SANDBOX", "A", fill("20", "10"))
            raise RuntimeError("injected outbox reducer failure")
    assert ledger.balance("SANDBOX", "A").balance == 1000
    with db.read() as conn:
        assert conn.execute("SELECT count(*) FROM ledger_native_facts").fetchone()[0] == 0


def test_native_identity_is_account_environment_scoped(ledger: Ledger) -> None:
    ledger.establish_baseline("LIVE", "A", "EUR", D(2000), "10", NOW)
    ledger.ingest("SANDBOX", "A", fill("20", "2"))
    ledger.ingest("LIVE", "A", fill("20", "3"))
    assert ledger.balance("SANDBOX", "A").balance == 1002
    assert ledger.balance("LIVE", "A").balance == 2003


@pytest.mark.parametrize("start,end,transactions,unfiltered", [
    ("10", "20", [fill("20")], False),
    ("10", "9", [], True),
    ("10", "20", [fill("21")], True),
    ("10", "20", [fill("20"), fill("20")], True),
    ("9", "20", [fill("20")], True),
])
def test_invalid_history_does_not_advance_coverage(ledger: Ledger, start: str, end: str,
                                                 transactions: list[dict[str, object]], unfiltered: bool) -> None:
    with pytest.raises((ValidationFailure, Conflict)):
        ledger.apply_history("SANDBOX", "A", start, transactions, end, unfiltered=unfiltered)
    assert ledger.balance("SANDBOX", "A").covered_cursor == "10"


def test_very_large_native_ids_do_not_overflow_sql_integer(ledger: Ledger) -> None:
    huge = "184467440737095516160000"
    result = ledger.apply_history("SANDBOX", "A", "10", [fill(huge, "2")], huge, unfiltered=True)
    assert result.balance == 1002 and result.covered_cursor == huge


def test_checkpoint_preserves_baseline_and_flags_mismatch(ledger: Ledger) -> None:
    event = fill("20", "2", accountBalance="1002")
    ledger.apply_history("SANDBOX", "A", "10", [event], "20", unfiltered=True)
    assert ledger.checkpoint("SANDBOX", "A", D(1002), "20", NOW)
    assert not ledger.checkpoint("SANDBOX", "A", D(900), "20", NOW)
    assert ledger.balance("SANDBOX", "A").balance == 1002


def test_financing_estimate_replaced_by_attributed_root_once(ledger: Ledger) -> None:
    ledger.add_financing_obligation("SANDBOX", "A", "period-a", D("-2"), "a" * 64)
    assert ledger.balance("SANDBOX", "A").unbooked_financing == -2
    ledger.ingest("SANDBOX", "A", {"id": "20", "type": "DAILY_FINANCING", "financing": "-2.1"})
    ledger.settle_financing_obligation("SANDBOX", "A", "period-a", "20", D("-2.1"), "b" * 64)
    ledger.settle_financing_obligation("SANDBOX", "A", "period-a", "20", D("-2.1"), "b" * 64)
    state = ledger.balance("SANDBOX", "A")
    assert state.balance == D("997.9") and state.unbooked_financing == 0 and state.known_financing_settled


def test_financing_netted_zero_does_not_prove_all_obligations_settled(ledger: Ledger) -> None:
    ledger.add_financing_obligation("SANDBOX", "A", "p1", D(-2), "a" * 64)
    ledger.add_financing_obligation("SANDBOX", "A", "p2", D(2), "b" * 64)
    state = ledger.balance("SANDBOX", "A")
    assert state.unbooked_financing == 0 and not state.known_financing_settled


def test_financing_cannot_allocate_same_root_fee_twice(ledger: Ledger) -> None:
    for identifier in ("p1", "p2"):
        ledger.add_financing_obligation("SANDBOX", "A", identifier, D(-2), "a" * 64)
    ledger.ingest("SANDBOX", "A", {"id": "20", "type": "DAILY_FINANCING", "financing": "-2"})
    ledger.settle_financing_obligation("SANDBOX", "A", "p1", "20", D(-2), "b" * 64)
    with pytest.raises(ValidationFailure):
        ledger.settle_financing_obligation("SANDBOX", "A", "p2", "20", D(-2), "b" * 64)


def test_metal_cannot_be_home_currency(db: Database) -> None:
    with pytest.raises(ValueError):
        Ledger(db).establish_baseline("SANDBOX", "A", "XAU", D(1), "1", NOW)


def test_stopped_check_revalidates_native_head_atomically(ledger: Ledger, db: Database) -> None:
    with db.transaction() as conn:
        Ledger.validate_stop_in(conn, "SANDBOX", "A", "10", "10")
    ledger.ingest("SANDBOX", "A", fill("20", "2"))
    with db.transaction() as conn:
        with pytest.raises(SafetyFence, match="later native fact"):
            Ledger.validate_stop_in(conn, "SANDBOX", "A", "10", "10")


def test_stopped_check_counts_offsetting_financing_obligations(ledger: Ledger, db: Database) -> None:
    ledger.add_financing_obligation("SANDBOX", "A", "p1", D(-2), "a" * 64)
    ledger.add_financing_obligation("SANDBOX", "A", "p2", D(2), "b" * 64)
    with db.transaction() as conn:
        with pytest.raises(SafetyFence, match="financing"):
            Ledger.validate_stop_in(conn, "SANDBOX", "A", "10", "10")


def test_raw_and_cash_journal_are_immutable_in_database(ledger: Ledger, db: Database) -> None:
    ledger.ingest("SANDBOX", "A", fill("20", "2"))
    for statement in ("UPDATE ledger_native_facts SET payload='{}'", "DELETE FROM ledger_cash_journal",
                      "UPDATE ledger_accounts SET baseline_balance='0'", "DELETE FROM ledger_audit"):
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction() as conn:
                conn.execute(statement)
    assert ledger.balance("SANDBOX", "A").balance == 1002


def test_rest_history_cannot_omit_its_head_or_known_stream_fact(ledger: Ledger) -> None:
    with pytest.raises(ValidationFailure, match="reported native head"):
        ledger.apply_history("SANDBOX", "A", "10", [], "20", unfiltered=True)
    ledger.ingest("SANDBOX", "A", fill("20", "2"))
    with pytest.raises(ValidationFailure, match="omitted"):
        ledger.apply_history("SANDBOX", "A", "10", [fill("30", "3")], "30", unfiltered=True)
    assert ledger.balance("SANDBOX", "A").covered_cursor == "10"


def test_new_fact_inside_previously_covered_range_is_fenced(ledger: Ledger) -> None:
    ledger.apply_history("SANDBOX", "A", "10", [fill("30", "3")], "30", unfiltered=True)
    result = ledger.ingest("SANDBOX", "A", fill("20", "2"))
    assert result.quarantined and ledger.balance("SANDBOX", "A").balance == 1005
    assert "HISTORY_COVERAGE_CONTRADICTION" in ledger.balance("SANDBOX", "A").fences


def test_incremental_covered_cash_is_independent_of_future_stream_cash(ledger: Ledger) -> None:
    ledger.apply_history("SANDBOX", "A", "10", [fill("20", "2", accountBalance="1002")], "20", unfiltered=True)
    ahead = fill("40", "4", accountBalance="1009")
    ledger.ingest("SANDBOX", "A", ahead)
    intermediate = ledger.apply_history("SANDBOX", "A", "20", [fill("30", "3", accountBalance="1005")], "30", unfiltered=True)
    assert intermediate.balance == 1009 and not intermediate.history_caught_up and not intermediate.fences
    complete = ledger.apply_history("SANDBOX", "A", "30", [ahead], "40", unfiltered=True)
    assert complete.balance == 1009 and complete.history_caught_up


def test_current_schema_reopens_without_changing_cash(ledger: Ledger, db: Database) -> None:
    ledger.apply_history("SANDBOX", "A", "10", [fill("20", "2", accountBalance="1002")], "20", unfiltered=True)
    path = db.path
    db.close()
    reopened = supported_database(path)
    try:
        current = Ledger(reopened)
        assert current.balance("SANDBOX", "A").balance == 1002
        result = current.apply_history("SANDBOX", "A", "20", [fill("30", "3", accountBalance="1005")], "30", unfiltered=True)
        assert result.balance == 1005 and result.history_caught_up
    finally:
        reopened.close()
