from datetime import datetime, timezone
from pathlib import Path

import pytest

from oandafly.domain.errors import ValidationFailure
from oandafly.reports.history import ReportEvent, ReportsService
from tests.support import supported_database


def test_csv_byte_limit_counts_utf8_and_preserves_journal(tmp_path: Path) -> None:
    db = supported_database(tmp_path / "reports.db")
    reports = ReportsService(db)
    reports.record_event(ReportEvent(source_key="long-audit", occurred_at=datetime.now(timezone.utc),
        environment="SANDBOX", account_id="account", kind="AUDIT", description="€" * 500))
    with pytest.raises(ValidationFailure, match="byte limit"):
        reports.export_csv(max_bytes=1000)
    assert reports.export_csv(max_bytes=5000)["row_count"] == 1
    db.close()


@pytest.mark.parametrize("bound", [True, 1, 100000000])
def test_csv_rejects_invalid_resource_budget(tmp_path: Path, bound: int) -> None:
    db = supported_database(tmp_path / "reports.db")
    with pytest.raises(ValidationFailure, match="byte bound"):
        ReportsService(db).export_csv(max_bytes=bound)
    db.close()
