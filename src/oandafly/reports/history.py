"""Worker-owned reporting with immutable cuts and authenticated keyset cursors.

Reporting is a projection, never a balance authority. Cash and exposure units have
separate columns; summaries never add different currencies or pool free margin.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import re
import secrets
import sqlite3
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from oandafly.domain.errors import Conflict, ValidationFailure
from oandafly.domain.instruments import currency_code, currency_pair
from oandafly.domain.primitives import (
    StrictModel,
    canonical_json,
    decimal_text,
    money,
    require_utc,
    sha256_json,
    utc_now,
)
from oandafly.storage.database import Database

_SCHEMA = """
CREATE TABLE IF NOT EXISTS report_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS report_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT NOT NULL UNIQUE,
    payload_hash TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    environment TEXT NOT NULL,
    account_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    currency TEXT,
    cash_amount TEXT,
    instrument TEXT,
    exposure_units TEXT,
    transaction_id TEXT,
    coverage TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS report_event_scope
ON report_events(environment, account_id, sequence);
CREATE INDEX IF NOT EXISTS report_event_time ON report_events(occurred_at, sequence);
CREATE TRIGGER IF NOT EXISTS immutable_report_event_update
BEFORE UPDATE ON report_events BEGIN SELECT RAISE(ABORT, 'report events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_report_event_delete
BEFORE DELETE ON report_events BEGIN SELECT RAISE(ABORT, 'report events are immutable'); END;
"""


class ReportEvent(StrictModel):
    source_key: str = Field(min_length=1, max_length=256)
    occurred_at: datetime
    environment: Literal["LIVE", "SANDBOX", "SYNTHETIC:PAPER"]
    account_id: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    description: str = Field(default="", max_length=2048)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    cash_amount: Decimal | None = None
    instrument: str | None = Field(default=None, pattern=r"^[A-Z]{3}_[A-Z]{3}$")
    exposure_units: Decimal | None = None
    transaction_id: str | None = Field(default=None, max_length=128)
    coverage: Literal["COMPLETE_VERIFIED", "PARTIAL", "STALE", "INCONSISTENT", "UNKNOWN_UNIVERSE"] = "PARTIAL"

    @field_validator("currency")
    @classmethod
    def national_currency(cls, value: str | None) -> str | None:
        return None if value is None else currency_code(value)

    @field_validator("instrument")
    @classmethod
    def fx_pair(cls, value: str | None) -> str | None:
        return None if value is None else currency_pair(value)

    @field_validator("occurred_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        return require_utc(value)

    @field_validator("cash_amount", "exposure_units", mode="before")
    @classmethod
    def exact_decimal(cls, value: Any) -> Decimal | None:
        return None if value is None else money(value)

    @model_validator(mode="after")
    def coherent_bookkeeping(self) -> ReportEvent:
        if (self.currency is None) != (self.cash_amount is None):
            raise ValueError("cash requires its original currency and an exact amount")
        if (self.instrument is None) != (self.exposure_units is None):
            raise ValueError("exposure units require their currency pair")
        return self


class EventFilter(StrictModel):
    environment: Literal["LIVE", "SANDBOX", "SYNTHETIC:PAPER"] | None = None
    account_id: str | None = Field(default=None, min_length=1, max_length=128)
    kind: str | None = Field(default=None, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    start: datetime | None = None
    end: datetime | None = None

    @field_validator("start", "end")
    @classmethod
    def aware_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_utc(value)

    @model_validator(mode="after")
    def ordered_times(self) -> EventFilter:
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError("report start must not follow end")
        return self


def _timestamp(value: datetime) -> str:
    return require_utc(value).isoformat(timespec="microseconds")


def _exact_add(left: Decimal, right: Decimal) -> Decimal:
    left_tuple, right_tuple = left.as_tuple(), right.as_tuple()
    left_exponent, right_exponent = left_tuple.exponent, right_tuple.exponent
    if not isinstance(left_exponent, int) or not isinstance(right_exponent, int):
        raise ValidationFailure("nonfinite amount in report projection")
    precision = max(
        len(left_tuple.digits) + max(0, left_exponent - right_exponent),
        len(right_tuple.digits) + max(0, right_exponent - left_exponent),
    ) + 1
    with localcontext() as context:
        context.prec = max(28, precision)
        return left + right


def _filter_payload(filters: EventFilter) -> dict[str, Any]:
    return filters.model_dump(mode="json")


def _projection_coverage(observed: set[str]) -> str:
    if not observed:
        return "PARTIAL"
    for state in ("UNKNOWN_UNIVERSE", "INCONSISTENT", "STALE", "PARTIAL"):
        if state in observed:
            return state
    return "COMPLETE_VERIFIED"


def _safe_csv(value: object) -> str:
    """Make every exported cell literal, including negative financial amounts.

    Spreadsheet apps remove the leading apostrophe visually. The accompanying
    JSON API preserves unmodified canonical financial strings for ingestion.
    """
    result = "" if value is None else str(value)
    probe = result.lstrip(" \t\r\n\ufeff")
    return "'" + result if probe.startswith(("=", "+", "-", "@")) else result


class _BoundedCSVBuffer(io.StringIO):
    def __init__(self, maximum: int) -> None:
        super().__init__(newline="")
        self.maximum = maximum
        self.written_bytes = 0

    def write(self, value: str, /) -> int:
        size = len(value.encode("utf-8"))
        if self.written_bytes + size > self.maximum:
            raise ValidationFailure("CSV export exceeds its byte limit; narrow the filters")
        self.written_bytes += size
        return super().write(value)


class ReportsService:
    """Synchronous service: call from the worker executor, never the Tk thread."""

    def __init__(self, database: Database) -> None:
        self.database = database
        database.initialize(_SCHEMA, component=__name__)
        with database.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO report_metadata(key, value) VALUES ('cursor_key', ?)",
                (secrets.token_hex(32),),
            )
            row = connection.execute(
                "SELECT value FROM report_metadata WHERE key = 'cursor_key'"
            ).fetchone()
            if row is None:
                raise RuntimeError("report cursor key could not be initialized")
            key = str(row[0])
            if re.fullmatch(r"[0-9a-f]{64}", key) is None:
                raise ValidationFailure("corrupt report cursor key")
            self._cursor_key = bytes.fromhex(key)

    def record_event(
        self,
        event: ReportEvent,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        """Atomically append, or verify an exact duplicate from the same source."""
        if connection is None:
            with self.database.transaction() as owned:
                return self.record_event(event, connection=owned)
        payload_hash = sha256_json(event.model_dump(mode="json"))
        existing = connection.execute(
            "SELECT sequence, payload_hash FROM report_events WHERE source_key = ?",
            (event.source_key,),
        ).fetchone()
        if existing is not None:
            if not hmac.compare_digest(str(existing["payload_hash"]), payload_hash):
                raise Conflict("an existing report source key has different facts")
            return int(existing["sequence"])
        result = connection.execute(
            """INSERT INTO report_events(
                source_key, payload_hash, occurred_at, received_at, environment,
                account_id, kind, description, currency, cash_amount, instrument,
                exposure_units, transaction_id, coverage
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.source_key, payload_hash, _timestamp(event.occurred_at),
                _timestamp(utc_now()), event.environment, event.account_id, event.kind,
                event.description, event.currency,
                None if event.cash_amount is None else decimal_text(event.cash_amount),
                event.instrument,
                None if event.exposure_units is None else decimal_text(event.exposure_units),
                event.transaction_id, event.coverage,
            ),
        )
        if result.lastrowid is None:
            raise RuntimeError("report insertion returned no durable sequence")
        return int(result.lastrowid)

    def _encode_cursor(self, payload: dict[str, Any]) -> str:
        raw = canonical_json(payload).encode("utf-8")
        signature = hmac.digest(self._cursor_key, raw, "sha256")
        return base64.urlsafe_b64encode(signature + raw).decode("ascii").rstrip("=")

    def _decode_cursor(self, cursor: str, filters: EventFilter) -> tuple[int, int]:
        if len(cursor) > 4096 or re.fullmatch(r"[A-Za-z0-9_-]+", cursor) is None:
            raise ValidationFailure("invalid report cursor")
        try:
            decoded = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            signature, raw = decoded[:32], decoded[32:]
            if not hmac.compare_digest(signature, hmac.digest(self._cursor_key, raw, "sha256")):
                raise ValueError("signature mismatch")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) != {"v", "filter", "cut", "before"}:
                raise ValueError("cursor shape mismatch")
            cut, before = payload["cut"], payload["before"]
            if payload["v"] != 1 or payload["filter"] != sha256_json(_filter_payload(filters)):
                raise ValueError("cursor scope mismatch")
            if type(cut) is not int or type(before) is not int or not 0 < before <= cut:
                raise ValueError("cursor range mismatch")
            return cut, before
        except (ValueError, TypeError, KeyError, UnicodeDecodeError) as exc:
            raise ValidationFailure("invalid or mismatched report cursor") from exc

    @staticmethod
    def _where(filters: EventFilter, cut: int, before: int | None = None) -> tuple[str, list[object]]:
        terms = ["sequence <= ?"]
        parameters: list[object] = [cut]
        if before is not None:
            terms.append("sequence < ?")
            parameters.append(before)
        for column in ("environment", "account_id", "kind"):
            value = getattr(filters, column)
            if value is not None:
                terms.append(f"{column} = ?")
                parameters.append(value)
        if filters.start is not None:
            terms.append("occurred_at >= ?")
            parameters.append(_timestamp(filters.start))
        if filters.end is not None:
            terms.append("occurred_at <= ?")
            parameters.append(_timestamp(filters.end))
        return " AND ".join(terms), parameters

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, object]:
        keys = (
            "sequence", "occurred_at", "received_at", "environment", "account_id",
            "kind", "description", "currency", "cash_amount", "instrument",
            "exposure_units", "transaction_id", "coverage",
        )
        return {key: row[key] for key in keys}

    def page(
        self,
        filters: EventFilter | None = None,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValidationFailure("report page size must be between 1 and 500")
        effective_filter = EventFilter() if filters is None else filters
        with self.database.read() as connection:
            if cursor is None:
                cut = int(connection.execute("SELECT COALESCE(MAX(sequence), 0) FROM report_events").fetchone()[0])
                before = None
            else:
                cut, before = self._decode_cursor(cursor, effective_filter)
            where, parameters = self._where(effective_filter, cut, before)
            rows = connection.execute(
                f"SELECT * FROM report_events WHERE {where} ORDER BY sequence DESC LIMIT ?",
                [*parameters, limit + 1],
            ).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            next_cursor = None
            if has_more:
                next_cursor = self._encode_cursor({
                    "v": 1,
                    "filter": sha256_json(_filter_payload(effective_filter)),
                    "cut": cut,
                    "before": int(rows[-1]["sequence"]),
                })
            total_where, total_parameters = self._where(effective_filter, cut)
            totals: dict[str, Decimal] = {}
            count = 0
            coverage: set[str] = set()
            for row in connection.execute(
                f"SELECT currency, cash_amount, coverage FROM report_events WHERE {total_where}",
                total_parameters,
            ):
                count += 1
                coverage.add(str(row["coverage"]))
                if row["currency"] is not None:
                    currency = str(row["currency"])
                    totals[currency] = _exact_add(totals.get(currency, Decimal(0)), money(str(row["cash_amount"])))
            return {
                "rows": [self._event_dict(row) for row in rows],
                "next_cursor": next_cursor,
                "cutoff_sequence": cut,
                "row_count": count,
                "cash_totals_by_original_currency": {key: decimal_text(value) for key, value in sorted(totals.items())},
                "coverage": _projection_coverage(coverage),
                "filters": _filter_payload(effective_filter),
                "units_total": None,
                "disclosure": "Event projection only. Cash currencies and FX exposure units are separate; this is not account NAV.",
            }

    def export_csv(
        self,
        filters: EventFilter | None = None,
        *,
        cutoff_sequence: int | None = None,
        max_rows: int = 100_000,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> dict[str, object]:
        if type(max_rows) is not int or not 1 <= max_rows <= 100_000:
            raise ValidationFailure("export bound must be between 1 and 100000 rows")
        if type(max_bytes) is not int or not 256 <= max_bytes <= 64 * 1024 * 1024:
            raise ValidationFailure("export byte bound must be between 256 bytes and 64 MiB")
        effective_filter = EventFilter() if filters is None else filters
        if cutoff_sequence is not None and (type(cutoff_sequence) is not int or cutoff_sequence < 0):
            raise ValidationFailure("invalid export cutoff")
        fields = (
            "sequence", "occurred_at", "received_at", "environment", "account_id",
            "kind", "description", "currency", "cash_amount", "instrument",
            "exposure_units", "transaction_id", "coverage",
        )
        buffer = _BoundedCSVBuffer(max_bytes)
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(fields)
        count = 0
        with self.database.read() as connection:
            latest = int(connection.execute("SELECT COALESCE(MAX(sequence), 0) FROM report_events").fetchone()[0])
            cut = latest if cutoff_sequence is None else cutoff_sequence
            if cut > latest:
                raise ValidationFailure("export cutoff is beyond observed report history")
            where, parameters = self._where(effective_filter, cut)
            for row in connection.execute(
                f"SELECT * FROM report_events WHERE {where} ORDER BY sequence LIMIT ?",
                [*parameters, max_rows + 1],
            ):
                count += 1
                if count > max_rows:
                    raise ValidationFailure("export exceeds the requested row bound; narrow the filters")
                writer.writerow([_safe_csv(row[field]) for field in fields])
        content = buffer.getvalue()
        return {
            "content": content,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "row_count": count,
            "cutoff_sequence": cut,
            "media_type": "text/csv; charset=utf-8",
            "filename": f"oandafly_activity_{cut}.csv",
            "formula_escaping": "apostrophe-prefix for formula-leading cells, including negative financial text",
        }
