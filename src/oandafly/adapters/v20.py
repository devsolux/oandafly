"""OANDA v20 observation contract; mutation capability is intentionally absent."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
from pydantic import Field, field_validator, model_validator

from oandafly.adapters.endpoints import Environment, V20Endpoints
from oandafly.adapters.governor import ObservationGovernor
from oandafly.adapters.http import BoundedHTTPReader, ReadDocument
from oandafly.domain.errors import SafetyFence, Unavailable, ValidationFailure
from oandafly.domain.financial import FINANCIAL
from oandafly.domain.instruments import CurrencyInstrument, currency_code, currency_pair
from oandafly.domain.primitives import StrictModel, money, utc_now
from oandafly.security.credentials import CredentialReference, NativeVault, account_segment
from oandafly.storage.ledger import native_id


def transaction_id(value: Any) -> str:
    # Use the ledger's canonical identity contract; aliases such as 001 cannot
    # establish a different replay frontier for transaction 1.
    try:
        return native_id(value)
    except ValidationFailure:
        raise Unavailable("Invalid native transaction identifier") from None


class NativeAccountSummary(StrictModel):
    account_id: str
    home_currency: str
    balance: Decimal
    unrealized_pl: Decimal
    nav: Decimal
    margin_used: Decimal = Field(ge=0)
    margin_available: Decimal
    margin_closeout_percent: Decimal
    margin_call_percent: Decimal | None = None
    last_transaction_id: str
    observed_at: datetime

    _currency = field_validator("home_currency")(currency_code)

    @field_validator(
        "balance",
        "unrealized_pl",
        "nav",
        "margin_used",
        "margin_available",
        "margin_closeout_percent",
        "margin_call_percent",
        mode="before",
    )
    @classmethod
    def exact_amount(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        return money(value)

    @model_validator(mode="after")
    def nav_identity(self) -> "NativeAccountSummary":
        with FINANCIAL.local():
            if self.balance + self.unrealized_pl != self.nav:
                raise ValueError("OANDA account NAV identity is inconsistent")
        return self


@dataclass(frozen=True, slots=True)
class AccountRead:
    summary: NativeAccountSummary
    document: ReadDocument
    unsupported_instruments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InstrumentRead:
    admitted: tuple[CurrencyInstrument, ...]
    excluded: tuple[str, ...]
    document_sha256: str


@dataclass(frozen=True, slots=True)
class HistoryRead:
    environment: Environment
    account_id: str
    from_transaction_id: str
    last_transaction_id: str
    transactions: tuple[dict[str, Any], ...]
    observed_at: datetime
    document_sha256: str

    @property
    def unfiltered(self) -> bool:
        return True


class V20ReadAdapter:
    def __init__(
        self,
        *,
        environment: Environment,
        credential: CredentialReference,
        vault: NativeVault,
        governor: ObservationGovernor,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        credential.authorize(purpose="V20_OBSERVATION", environment=environment)
        if governor.service != "OANDA_V20":
            raise SafetyFence("v20 requires its own canonical request quota")
        self.environment = environment
        self.credential = credential
        self.endpoints = V20Endpoints.for_environment(environment)
        self._http = BoundedHTTPReader(
            origins=frozenset({self.endpoints.rest, self.endpoints.stream}),
            credential=credential,
            vault=vault,
            governor=governor,
            transport=transport,
        )

    def _authorize(self, account_id: str | None = None) -> None:
        self.credential.authorize(
            purpose="V20_OBSERVATION", environment=self.environment, account_id=account_id
        )

    async def close(self) -> None:
        await self._http.close()

    async def accounts(self) -> tuple[str, ...]:
        """Confirm enrolled memberships; never silently enroll additional sub-accounts."""
        self._authorize()
        document = await self._http.get(self.endpoints.rest, "/v3/accounts")
        rows = document.body.get("accounts")
        if not isinstance(rows, list):
            raise Unavailable("Account discovery response has no account list")
        try:
            all_ids = tuple(account_segment(row["id"]) for row in rows if isinstance(row, dict))
        except (KeyError, TypeError, ValueError):
            raise Unavailable("Account discovery response is malformed") from None
        if len(all_ids) != len(rows) or len(all_ids) != len(set(all_ids)):
            raise Unavailable("Account discovery contains malformed or duplicate scopes")
        enrolled = set(self.credential.account_ids)
        if not enrolled.issubset(all_ids):
            raise SafetyFence("Credential no longer has access to every enrolled sub-account")
        return tuple(value for value in all_ids if value in enrolled)

    async def account(self, account_id: str) -> AccountRead:
        self._authorize(account_id)
        document = await self._http.get(self.endpoints.rest, f"/v3/accounts/{account_segment(account_id)}")
        row = document.body.get("account")
        if not isinstance(row, dict) or row.get("id") != account_id:
            raise SafetyFence("Account response does not match the requested account")
        try:
            financial_fields = ("balance", "unrealizedPL", "NAV", "marginUsed", "marginAvailable", "marginCloseoutPercent")
            if any(type(row.get(name)) is not str for name in financial_fields):
                raise ValueError("OANDA financial wire fields must be decimal strings")
            if row.get("marginCallPercent") is not None and type(row["marginCallPercent"]) is not str:
                raise ValueError("OANDA margin call percentage must be a decimal string")
            last_id = transaction_id(document.body["lastTransactionID"])
            if "lastTransactionID" in row and row["lastTransactionID"] != last_id:
                raise ValueError("Account cut differs from response cut")
            summary = NativeAccountSummary.from_wire(dict(
                account_id=account_id,
                home_currency=row["currency"],
                balance=row["balance"],
                unrealized_pl=row["unrealizedPL"],
                nav=row["NAV"],
                margin_used=row["marginUsed"],
                margin_available=row["marginAvailable"],
                margin_closeout_percent=row["marginCloseoutPercent"],
                margin_call_percent=row.get("marginCallPercent"),
                last_transaction_id=last_id,
                observed_at=utc_now(),
            ))
            unsupported: set[str] = set()
            for collection in ("trades", "positions", "orders"):
                children = row[collection]
                if not isinstance(children, list):
                    raise ValueError("Missing native account children")
                for child in children:
                    if not isinstance(child, dict):
                        raise ValueError("Malformed native account child")
                    name = child.get("instrument")
                    if (
                        name is None
                        and collection == "orders"
                        and child.get("type")
                        in {"STOP_LOSS", "TAKE_PROFIT", "TRAILING_STOP_LOSS", "GUARANTEED_STOP_LOSS"}
                    ):
                        continue  # native dependent orders bind by TradeID.
                    if not isinstance(name, str):
                        raise ValueError("Missing child instrument")
                    try:
                        currency_pair(name)
                    except ValueError:
                        unsupported.add(name[:128])
            return AccountRead(summary, document, tuple(sorted(unsupported)))
        except (KeyError, ValueError, TypeError, ValidationFailure):
            raise Unavailable("Native account snapshot is incomplete or inconsistent") from None

    async def instruments(self, account_id: str) -> InstrumentRead:
        self._authorize(account_id)
        document = await self._http.get(
            self.endpoints.rest, f"/v3/accounts/{account_segment(account_id)}/instruments"
        )
        rows = document.body.get("instruments")
        if not isinstance(rows, list):
            raise Unavailable("Native instrument response is incomplete")
        admitted: list[CurrencyInstrument] = []
        excluded: list[str] = []
        observed: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                raise Unavailable("Malformed native instrument metadata")
            name = row["name"]
            if name in observed:
                raise Unavailable("Duplicate native instrument metadata")
            observed.add(name)
            try:
                currency_pair(name)
            except ValueError:
                excluded.append(name[:128])
                continue
            if row.get("type") != "CURRENCY":
                excluded.append(name[:128])
                continue
            try:
                admitted.append(
                    CurrencyInstrument.from_wire(dict(
                        name=name,
                        instrument_type="CURRENCY",
                        trade_units_precision=row["tradeUnitsPrecision"],
                        display_precision=row["displayPrecision"],
                        pip_location=row["pipLocation"],
                        minimum_trade_size=row["minimumTradeSize"],
                        maximum_order_units=row["maximumOrderUnits"],
                        margin_rate=row["marginRate"],
                    ))
                )
            except (KeyError, ValueError, TypeError, ValidationFailure):
                raise Unavailable("Currency instrument precision or margin metadata is invalid") from None
        return InstrumentRead(tuple(admitted), tuple(excluded), document.sha256)

    async def since_id(self, account_id: str, cursor: str) -> HistoryRead:
        """REST catch-up is deliberately unfiltered. Stream heartbeats cannot replace it."""
        self._authorize(account_id)
        cursor = transaction_id(cursor)
        document = await self._http.get(
            self.endpoints.rest,
            f"/v3/accounts/{account_segment(account_id)}/transactions/sinceid",
            params={"id": cursor},
        )
        rows = document.body.get("transactions")
        if not isinstance(rows, list):
            raise Unavailable("Historical response has no transaction array")
        last_id = transaction_id(document.body.get("lastTransactionID"))
        if int(last_id) < int(cursor):
            raise SafetyFence("Native history cut regressed")
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("accountID") != account_id:
                raise SafetyFence("Historical transaction account mismatch")
            native_id = transaction_id(row.get("id"))
            if native_id in seen or not int(cursor) < int(native_id) <= int(last_id):
                raise SafetyFence("Historical transaction is duplicated or outside its declared cut")
            seen.add(native_id)
            result.append(row)
        # A successful but incomplete body cannot be called unfiltered coverage.
        if last_id != cursor and last_id not in seen:
            raise SafetyFence("Historical response omitted its declared native head")
        return HistoryRead(
            self.environment, account_id, cursor, last_id, tuple(result), utc_now(), document.sha256
        )

    async def transactions_stream(self, account_id: str) -> AsyncGenerator[dict[str, Any], None]:
        self._authorize(account_id)
        path = f"/v3/accounts/{account_segment(account_id)}/transactions/stream"
        # Explicit closure propagates consumer cancellation to the HTTP response
        # and releases its durable stream lease before returning to the caller.
        async with aclosing(self._http.stream(self.endpoints.stream, path, private=True)) as stream:
            async for event in stream:
                self._authorize(account_id)
                if event.get("type") == "HEARTBEAT":
                    transaction_id(event.get("lastTransactionID"))
                elif event.get("accountID") != account_id:
                    raise SafetyFence("Private stream transaction account mismatch")
                else:
                    transaction_id(event.get("id"))
                # This is observation only. It never advances a REST coverage cursor.
                yield event
