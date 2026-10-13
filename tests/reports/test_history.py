from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from oandafly.domain.errors import Conflict, ValidationFailure
from oandafly.reports.history import EventFilter, ReportEvent, ReportsService
from tests.support import supported_database

NOW = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def reports(tmp_path: Path):
    database = supported_database(tmp_path / "history.sqlite3")
    service = ReportsService(database)
    yield service
    database.close()


def event(index: int, **changes: object) -> ReportEvent:
    payload: dict[str, object] = {
        "source_key": f"SANDBOX:a:transaction:{index}",
        "occurred_at": NOW + timedelta(seconds=index),
        "environment": "SANDBOX", "account_id": "a", "kind": "SWAP_FEE_BOOKED",
        "currency": "EUR", "cash_amount": "-0.01", "coverage": "COMPLETE_VERIFIED",
        "transaction_id": str(index),
    }
    payload.update(changes)
    return ReportEvent.from_wire(payload)


def test_financing_projection_is_exact_idempotent_and_conflicts_are_rejected(reports: ReportsService) -> None:
    first = event(1, cash_amount="-0.000000000000000001")
    sequence = reports.record_event(first)
    assert reports.record_event(first) == sequence
    with pytest.raises(Conflict):
        reports.record_event(event(1, cash_amount="-0.02"))
    page = reports.page()
    assert page["row_count"] == 1
    assert page["cash_totals_by_original_currency"] == {"EUR": "-0.000000000000000001"}


def test_decimal_accumulation_does_not_round_to_default_context_precision(reports: ReportsService) -> None:
    reports.record_event(event(1, cash_amount="12345678901234567890123456789"))
    reports.record_event(event(2, cash_amount="0.000000000000000001"))
    assert reports.page()["cash_totals_by_original_currency"] == {"EUR": "12345678901234567890123456789.000000000000000001"}


def test_cash_currencies_and_exposure_units_never_pool(reports: ReportsService) -> None:
    reports.record_event(event(1, cash_amount="10.25"))
    reports.record_event(event(2, currency="USD", cash_amount="20.15"))
    reports.record_event(event(3, currency=None, cash_amount=None, instrument="EUR_USD", exposure_units="100000.12", kind="ACK_BY_FILL"))
    page = reports.page()
    assert page["cash_totals_by_original_currency"] == {"EUR": "10.25", "USD": "20.15"}
    assert page["units_total"] is None
    assert page["row_count"] == 3


def test_keyset_cut_is_immutable_despite_new_backdated_ingestion(reports: ReportsService) -> None:
    for index in range(1, 8):
        reports.record_event(event(index))
    first = reports.page(limit=3)
    reports.record_event(event(8, occurred_at=NOW - timedelta(days=1)))
    second = reports.page(cursor=str(first["next_cursor"]), limit=3)
    third = reports.page(cursor=str(second["next_cursor"]), limit=3)
    rows = [*first["rows"], *second["rows"], *third["rows"]]
    assert [row["sequence"] for row in rows] == [7, 6, 5, 4, 3, 2, 1]
    assert first["cutoff_sequence"] == second["cutoff_sequence"] == third["cutoff_sequence"] == 7
    assert third["next_cursor"] is None
    assert reports.page()["row_count"] == 8


def test_cursor_binds_exact_filters_and_rejects_tampering(reports: ReportsService) -> None:
    for index in range(1, 5):
        reports.record_event(event(index))
    first = reports.page(EventFilter(account_id="a"), limit=2)
    cursor = str(first["next_cursor"])
    with pytest.raises(ValidationFailure):
        reports.page(EventFilter(account_id="b"), cursor=cursor)
    with pytest.raises(ValidationFailure):
        reports.page(EventFilter(account_id="a"), cursor=("A" if cursor[0] != "A" else "B") + cursor[1:])
    with pytest.raises(ValidationFailure):
        reports.page(cursor="A" * 4097)


def test_cursor_survives_clean_restart(tmp_path: Path) -> None:
    path = tmp_path / "reports.sqlite3"
    database = supported_database(path)
    service = ReportsService(database)
    for index in range(1, 4):
        service.record_event(event(index))
    cursor = str(service.page(limit=1)["next_cursor"])
    database.close()
    database = supported_database(path)
    try:
        service = ReportsService(database)
        assert [row["sequence"] for row in service.page(cursor=cursor)["rows"]] == [2, 1]
    finally:
        database.close()


def test_export_matches_frozen_cut_and_neutralizes_spreadsheet_formulas(reports: ReportsService) -> None:
    reports.record_event(event(1, description="  =HYPERLINK(\"https://invalid\",\"click\")"))
    cut = reports.page()["cutoff_sequence"]
    reports.record_event(event(2))
    exported = reports.export_csv(cutoff_sequence=cut)
    rows = list(csv.DictReader(io.StringIO(exported["content"])))
    assert len(rows) == exported["row_count"] == 1
    assert rows[0]["description"].startswith("'  =")
    assert rows[0]["cash_amount"] == "'-0.01"
    assert exported["cutoff_sequence"] == 1
    with pytest.raises(ValidationFailure):
        reports.export_csv(max_rows=1)
    with pytest.raises(ValidationFailure):
        reports.export_csv(cutoff_sequence=99)


def test_event_insert_can_join_atomic_native_fact_transaction(reports: ReportsService) -> None:
    with pytest.raises(RuntimeError):
        with reports.database.transaction() as connection:
            reports.record_event(event(1), connection=connection)
            raise RuntimeError("inject crash before atomic commit")
    assert reports.page()["row_count"] == 0
    reports.record_event(event(1))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with reports.database.transaction() as connection:
            connection.execute("DELETE FROM report_events")


@pytest.mark.parametrize("changes", [
    {"cash_amount": 0.1}, {"cash_amount": "NaN"}, {"cash_amount": None},
    {"currency": None}, {"exposure_units": "100"}, {"instrument": "EUR_USD"},
    {"occurred_at": NOW.replace(tzinfo=None)}, {"unexpected": "extra"},
    {"instrument": "XAU_USD", "exposure_units": "1"}, {"currency": "BTC"},
])
def test_closed_exact_event_contract_rejects_invalid_values(changes: dict[str, object]) -> None:
    with pytest.raises((ValidationError, ValidationFailure)):
        event(1, **changes)


def test_filter_timestamps_normalize_offsets_and_exclude_other_scopes(reports: ReportsService) -> None:
    reports.record_event(event(1))
    reports.record_event(event(2, account_id="b"))
    reports.record_event(event(3))
    filters = EventFilter(account_id="a", start=NOW, end=NOW + timedelta(seconds=2))
    result = reports.page(filters)
    assert result["row_count"] == 1
    assert result["cash_totals_by_original_currency"] == {"EUR": "-0.01"}
    assert event(1, cash_amount=Decimal("0.0000000000000001")).cash_amount == Decimal("0.0000000000000001")


def test_empty_and_incomplete_reports_do_not_claim_complete_verification(reports: ReportsService) -> None:
    assert reports.page()["coverage"] == "PARTIAL"
    reports.record_event(event(1))
    assert reports.page()["coverage"] == "COMPLETE_VERIFIED"
    reports.record_event(event(2, coverage="UNKNOWN_UNIVERSE"))
    assert reports.page()["coverage"] == "UNKNOWN_UNIVERSE"
