"""Entitled EUCB historical observations and explicit EUR-leg derived valuations.

OANDA's dataset is the data source. A v20 midpoint never substitutes for a central
bank publication, and an inverse or cross is always labelled as locally derived.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
from pydantic import Field, field_validator, model_validator

from oandafly.adapters.endpoints import RATES_ORIGIN
from oandafly.adapters.governor import LocalRateGovernor
from oandafly.adapters.http import BoundedHTTPReader, ReadDocument
from oandafly.domain.errors import Conflict, RateLimited, SafetyFence, Unavailable
from oandafly.domain.instruments import currency_code
from oandafly.domain.primitives import StrictModel, canonical_json, money, require_utc, utc_now
from oandafly.security.credentials import CredentialReference, NativeVault
from oandafly.storage.database import Database

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rates_quote_budgets (
 credential_id TEXT NOT NULL, period_id TEXT NOT NULL, local_remaining INTEGER NOT NULL,
 remote_remaining INTEGER, configured_limit INTEGER NOT NULL, PRIMARY KEY(credential_id,period_id)
);
CREATE TABLE IF NOT EXISTS rates_audit_queries (
 query_id TEXT PRIMARY KEY, credential_id TEXT NOT NULL, period_id TEXT NOT NULL,
 query_json TEXT NOT NULL, reserved_quotes INTEGER NOT NULL, status TEXT NOT NULL,
 response_sha256 TEXT, response_body BLOB, result_json TEXT, created_at TEXT NOT NULL
);
"""


class RatesEntitlement(StrictModel):
    credential_id: UUID
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_set: Literal["EUCB"] = "EUCB"
    period_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    period_start: datetime
    period_end: datetime
    quote_limit: int = Field(strict=True, ge=1, le=100_000_000)
    intraday_history_allowed: bool = False

    @field_validator("period_start", "period_end")
    @classmethod
    def aware_times(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def period_order(self) -> "RatesEntitlement":
        if self.period_start >= self.period_end:
            raise ValueError("Rates entitlement billing period is invalid")
        return self


class HistoricalFixingQuery(StrictModel):
    data_set: Literal["EUCB"] = "EUCB"
    base_currency: str
    quote_currency: str
    business_date: date
    cutoff: datetime
    allow_derived: bool = False
    carry_forward_days: int = Field(default=0, strict=True, ge=0, le=7)

    _currencies = field_validator("base_currency", "quote_currency")(currency_code)

    @field_validator("cutoff")
    @classmethod
    def boundary(cls, value: datetime) -> datetime:
        value = require_utc(value)
        if value.minute % 15 or value.second or value.microsecond:
            raise ValueError("Rates v2 historical date_time must lie on a 15-minute boundary")
        return value

    @model_validator(mode="after")
    def query_rules(self) -> "HistoricalFixingQuery":
        if self.base_currency == self.quote_currency:
            raise ValueError("Historical currency pair must contain two distinct currencies")
        if self.cutoff.date() != self.business_date:
            raise ValueError("Requested business date must match the UTC cutoff date")
        if self.cutoff >= utc_now():
            raise ValueError("Historical cutoff must precede the current time")
        if self.base_currency != "EUR" and not self.allow_derived:
            raise ValueError("EUR inverses and cross rates require explicit derived-rate selection")
        return self

    @property
    def eur_quotes(self) -> tuple[str, ...]:
        return tuple(sorted({self.base_currency, self.quote_currency} - {"EUR"}))


class FixingLeg(StrictModel):
    base_currency: Literal["EUR"] = "EUR"
    quote_currency: str
    midpoint: Decimal
    source_date: str
    publication_date: date
    publication_precision: Literal["DATE", "TIMESTAMP"]
    carried_days: int


class HistoricalFixing(StrictModel):
    query_id: UUID
    data_set: Literal["EUCB"] = "EUCB"
    base_currency: str
    quote_currency: str
    business_date: date
    requested_cutoff: datetime
    retrieved_at: datetime
    rate: Decimal
    derivation: Literal["PUBLISHED_EUR_LEG", "DERIVED_INVERSE", "DERIVED_CROSS"]
    coverage: Literal["COMPLETE_VERIFIED", "PARTIAL"]
    point_in_time_publication_verified: bool
    legs: tuple[FixingLeg, ...]
    response_sha256: str
    entitlement_evidence_sha256: str


class RatesAdapter:
    def __init__(
        self,
        *,
        db: Database,
        credential: CredentialReference,
        entitlement: RatesEntitlement,
        vault: NativeVault,
        governor: LocalRateGovernor,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        credential.authorize(purpose="RATES_OBSERVATION", environment=credential.environment)
        if governor.service != "OANDA_RATES":
            raise SafetyFence("Rates requires a separate canonical request quota")
        if credential.credential_id != entitlement.credential_id or (
            credential.entitlement_evidence_sha256 != entitlement.evidence_sha256
        ):
            raise SafetyFence("Rates credential does not match the enrolled entitlement")
        self.db = db
        self.credential = credential
        self.entitlement = entitlement
        self._http = BoundedHTTPReader(
            origins=frozenset({RATES_ORIGIN}),
            credential=credential,
            vault=vault,
            governor=governor,
            transport=transport,
        )
        db.initialize(_SCHEMA, component=__name__)
        with db.transaction() as connection:
            row = connection.execute(
                "SELECT configured_limit FROM rates_quote_budgets WHERE credential_id=? AND period_id=?",
                (str(credential.credential_id), entitlement.period_id),
            ).fetchone()
            if row is not None and row["configured_limit"] != entitlement.quote_limit:
                raise Conflict("Rates quota period cannot be silently reconfigured")
            connection.execute(
                "INSERT OR IGNORE INTO rates_quote_budgets VALUES(?,?,?,NULL,?)",
                (
                    str(credential.credential_id),
                    entitlement.period_id,
                    entitlement.quote_limit,
                    entitlement.quote_limit,
                ),
            )

    async def close(self) -> None:
        await self._http.close()

    def _authorize(self, query: HistoricalFixingQuery) -> None:
        self.credential.authorize(purpose="RATES_OBSERVATION", environment=self.credential.environment)
        if not self.entitlement.period_start <= utc_now() < self.entitlement.period_end:
            raise SafetyFence("Rates entitlement is outside its enrolled billing period")
        if (query.cutoff.hour or query.cutoff.minute) and not self.entitlement.intraday_history_allowed:
            raise SafetyFence("Non-midnight historical Rates calls require an explicit intraday entitlement")

    async def _metadata(self, query: HistoricalFixingQuery) -> None:
        document = await self._http.get(RATES_ORIGIN, "/v2/datasets/EUCB.json")
        rows = document.body.get("datasets")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise Unavailable("EUCB dataset metadata is incomplete")
        row = rows[0]
        if row.get("code") != "EUCB" or row.get("cross_currency") != "EUR":
            raise SafetyFence("EUCB dataset provenance or cross currency differs from the enrolled contract")
        quotables = row.get("quotables")
        if not isinstance(quotables, list) or any(not isinstance(item, str) for item in quotables):
            raise Unavailable("EUCB dataset has no verified quotable universe")
        if any(
            f"EUR/{code}" not in quotables and f"{code}/EUR" not in quotables for code in query.eur_quotes
        ):
            raise Unavailable("Requested national currency has no enrolled EUCB EUR leg")

    def _reserve(self, query: HistoricalFixingQuery, query_id: UUID) -> None:
        count = len(query.eur_quotes)
        with self.db.transaction() as connection:
            self._authorize(query)
            row = connection.execute(
                "SELECT * FROM rates_quote_budgets WHERE credential_id=? AND period_id=?",
                (str(self.credential.credential_id), self.entitlement.period_id),
            ).fetchone()
            if (
                row is None
                or row["local_remaining"] < count
                or (row["remote_remaining"] is not None and row["remote_remaining"] < count)
            ):
                raise RateLimited("Enrolled Rates quote budget is exhausted")
            connection.execute(
                "UPDATE rates_quote_budgets SET local_remaining=local_remaining-?, "
                "remote_remaining=CASE WHEN remote_remaining IS NULL THEN NULL ELSE remote_remaining-? END "
                "WHERE credential_id=? AND period_id=?",
                (count, count, str(self.credential.credential_id), self.entitlement.period_id),
            )
            connection.execute(
                "INSERT INTO rates_audit_queries(query_id,credential_id,period_id,query_json,reserved_quotes,status,created_at) "
                "VALUES(?,?,?,?,?,'REQUEST_RESERVED',?)",
                (
                    str(query_id),
                    str(self.credential.credential_id),
                    self.entitlement.period_id,
                    query.model_dump_json(),
                    count,
                    utc_now().isoformat(),
                ),
            )

    def _capture(self, query_id: UUID, document: ReadDocument) -> None:
        with self.db.transaction() as connection:
            changed = connection.execute(
                "UPDATE rates_audit_queries SET status='RESPONSE_RECEIVED',response_sha256=?,response_body=? "
                "WHERE query_id=? AND status='REQUEST_RESERVED'",
                (document.sha256, document.raw, str(query_id)),
            ).rowcount
            if changed != 1:
                raise Conflict("Rates response lost its unique durable query reservation")

    def _failure(self, query_id: UUID) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE rates_audit_queries SET status='UNVERIFIED_OR_UNKNOWN' WHERE query_id=? AND status IN ('REQUEST_RESERVED','RESPONSE_RECEIVED')",
                (str(query_id),),
            )

    @staticmethod
    def _publication(
        value: Any, query: HistoricalFixingQuery
    ) -> tuple[date, Literal["DATE", "TIMESTAMP"], int]:
        if not isinstance(value, str) or len(value) > 64:
            raise Unavailable("EUCB quote has no actual publication date")
        try:
            if len(value) == 10:
                published_date = date.fromisoformat(value)
                precision: Literal["DATE", "TIMESTAMP"] = "DATE"
            else:
                published = require_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
                if published > query.cutoff:
                    raise ValueError("Publication follows accounting cutoff")
                published_date = published.date()
                precision = "TIMESTAMP"
            age = (query.business_date - published_date).days
            if age < 0 or age > query.carry_forward_days:
                raise ValueError("Publication requires unapproved carry-forward")
            return published_date, precision, age
        except ValueError:
            raise Unavailable(
                "EUCB publication is beyond the requested cutoff or carry-forward policy"
            ) from None

    def _parse(
        self, body: dict[str, Any], query: HistoricalFixingQuery, query_id: UUID, digest: str
    ) -> HistoricalFixing:
        try:
            meta = body["meta"]
            params = meta["effective_params"]
            if (
                params["data_set"] != "EUCB"
                or params["base_currencies"] != ["EUR"]
                or set(params["quote_currencies"]) != set(query.eur_quotes)
                or len(params["quote_currencies"]) != len(query.eur_quotes)
                or meta["skipped_currency_pairs"]
            ):
                raise ValueError("Effective query differs from requested accounting scope")
            effective_text = params["date_time"]
            # Malformed upstream scalars must produce a sanitized typed failure,
            # not an AttributeError escaping the accounting provenance boundary.
            if not isinstance(effective_text, str):
                raise ValueError("Historical cutoff must be text")
            effective_cutoff = (
                datetime.combine(date.fromisoformat(effective_text), datetime.min.time(), tzinfo=timezone.utc)
                if isinstance(effective_text, str) and len(effective_text) == 10
                else require_utc(datetime.fromisoformat(effective_text.replace("Z", "+00:00")))
            )
            if effective_cutoff != query.cutoff:
                raise ValueError("Provider changed historical cutoff")
            rows = body["quotes"]
            if not isinstance(rows, list) or len(rows) != len(query.eur_quotes):
                raise ValueError("Missing historical EUR leg")
            legs: dict[str, FixingLeg] = {}
            for row in rows:
                code = row["quote_currency"]
                if row["base_currency"] != "EUR" or code not in query.eur_quotes or code in legs:
                    raise ValueError("Quote pair does not match requested EUR leg")
                if any(not isinstance(row[field], str) for field in ("bid", "ask", "midpoint")):
                    raise ValueError("Native rate must be a decimal string")
                bid, ask, midpoint = (money(row[field]) for field in ("bid", "ask", "midpoint"))
                if not Decimal(0) < bid <= midpoint <= ask:
                    raise ValueError("Invalid central-bank rate values")
                source = row["source_date"]
                publication, precision, age = self._publication(source, query)
                legs[code] = FixingLeg(
                    quote_currency=code,
                    midpoint=midpoint,
                    source_date=source,
                    publication_date=publication,
                    publication_precision=precision,
                    carried_days=age,
                )
            # Cross legs must reference the same publication day, never a mixed fixing.
            if len({leg.publication_date for leg in legs.values()}) != 1:
                raise ValueError("Cross-rate legs have different publication dates")
            with localcontext() as context:
                context.prec = 50
                numerator = (
                    Decimal(1) if query.quote_currency == "EUR" else legs[query.quote_currency].midpoint
                )
                denominator = (
                    Decimal(1) if query.base_currency == "EUR" else legs[query.base_currency].midpoint
                )
                rate = numerator / denominator
            precise = all(leg.publication_precision == "TIMESTAMP" for leg in legs.values())
            derivation: Literal["PUBLISHED_EUR_LEG", "DERIVED_INVERSE", "DERIVED_CROSS"] = (
                "PUBLISHED_EUR_LEG"
                if query.base_currency == "EUR"
                else "DERIVED_INVERSE"
                if query.quote_currency == "EUR"
                else "DERIVED_CROSS"
            )
            return HistoricalFixing(
                query_id=query_id,
                base_currency=query.base_currency,
                quote_currency=query.quote_currency,
                business_date=query.business_date,
                requested_cutoff=query.cutoff,
                retrieved_at=utc_now(),
                rate=rate,
                derivation=derivation,
                coverage="COMPLETE_VERIFIED" if precise else "PARTIAL",
                point_in_time_publication_verified=precise,
                legs=tuple(legs[code] for code in sorted(legs)),
                response_sha256=digest,
                entitlement_evidence_sha256=self.entitlement.evidence_sha256,
            )
        except (KeyError, TypeError, ValueError, ArithmeticError):
            raise Unavailable(
                "Historical EUCB response does not satisfy the accounting provenance contract"
            ) from None

    async def historical(self, query: HistoricalFixingQuery) -> HistoricalFixing:
        self._authorize(query)
        await self._metadata(query)
        query_id = uuid4()
        # Synchronous tiny reservation prevents cancellation losing the durable query ID.
        self._reserve(query, query_id)
        try:
            document = await self._http.get(
                RATES_ORIGIN,
                "/v2/rates/spot.json",
                params={
                    "data_set": "EUCB",
                    "base": "EUR",
                    "quote": ",".join(query.eur_quotes),
                    "date_time": query.cutoff.isoformat(timespec="minutes"),
                    "source_date": "true",
                },
            )
            self._capture(query_id, document)
            result = self._parse(document.body, query, query_id, document.sha256)
            with self.db.transaction() as connection:
                remaining = document.headers.get("remaining_quotes")
                if remaining is not None:
                    connection.execute(
                        "UPDATE rates_quote_budgets SET remote_remaining=CASE WHEN remote_remaining IS NULL "
                        "THEN ? ELSE MIN(remote_remaining,?) END WHERE credential_id=? AND period_id=?",
                        (
                            int(remaining),
                            int(remaining),
                            str(self.credential.credential_id),
                            self.entitlement.period_id,
                        ),
                    )
                connection.execute(
                    "UPDATE rates_audit_queries SET status='VERIFIED',response_sha256=?,response_body=?,result_json=? "
                    "WHERE query_id=? AND status='RESPONSE_RECEIVED'",
                    (
                        document.sha256,
                        document.raw,
                        canonical_json(result.model_dump(mode="json")),
                        str(query_id),
                    ),
                )
            return result
        except (Exception, asyncio.CancelledError):
            self._failure(query_id)
            raise
