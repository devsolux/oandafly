"""Native v20 pricing, bid/ask history and account-update observations."""
from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from oandafly.adapters.http import ReadDocument
from oandafly.adapters.v20 import V20ReadAdapter, transaction_id
from oandafly.domain.errors import SafetyFence, Unavailable
from oandafly.domain.instruments import CurrencyInstrument, currency_pair
from oandafly.domain.primitives import money, require_utc, utc_now
from oandafly.execution.risk import HomeConversions
from oandafly.marketdata.contracts import Candle, Quote
from oandafly.security.credentials import account_segment

GRANULARITIES = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600}


def native_time(value: object) -> datetime:
    if type(value) is not str or len(value) > 40:
        raise Unavailable("Native timestamp is missing or malformed")
    try:
        # Python preserves microseconds; the original response hash binds all
        # native nanosecond text. Bar identity uses exact aligned boundaries.
        return require_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise Unavailable("Native timestamp is invalid") from exc


def wire_decimal(value: object) -> Decimal:
    if type(value) is not str:
        raise Unavailable("Native financial fields must be decimal strings")
    return money(value)


def quote_from_native(row: Mapping[str, Any], *, instrument: CurrencyInstrument, environment: str,
                      received_at: datetime | None = None) -> Quote:
    if row.get("instrument") != instrument.name or row.get("type", "PRICE") != "PRICE":
        raise SafetyFence("Native price belongs to another instrument or event type")
    bids, asks = row.get("bids"), row.get("asks")
    if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks or len(bids) > 100 or len(asks) > 100:
        raise Unavailable("Native pricing has no bounded executable bid/ask book")
    try:
        bid = max(wire_decimal(level["price"]) for level in bids if int(level["liquidity"]) > 0)
        ask = min(wire_decimal(level["price"]) for level in asks if int(level["liquidity"]) > 0)
        status = row.get("status")
        explicit = row.get("tradeable")
        if explicit is not None and type(explicit) is not bool:
            raise ValueError("Invalid tradeability indicator")
        tradeable = status == "tradeable" and explicit is not False
        return Quote(instrument=instrument.name, bid=bid, ask=ask,
                     quantum=Decimal(1).scaleb(-instrument.display_precision),
                     observed_at=native_time(row["time"]), received_at=received_at or utc_now(),
                     tradeable=tradeable, source="OANDA_" + environment)
    except (KeyError, ValueError, TypeError) as exc:
        raise Unavailable("Native executable price is incomplete") from exc


@dataclass(frozen=True)
class PricingRead:
    quotes: tuple[Quote, ...]
    conversions: tuple[HomeConversions, ...]
    observed_at: datetime
    document_sha256: str

    def quote(self, instrument: str) -> Quote:
        for value in self.quotes:
            if value.instrument == instrument:
                return value
        raise Unavailable("Requested price is missing")

    def conversion(self, currency: str, *, home_currency: str) -> HomeConversions:
        if currency == home_currency:
            return HomeConversions(currency=currency, position_value=Decimal(1),
                                   account_gain=Decimal(1), account_loss=Decimal(1))
        for value in self.conversions:
            if value.currency == currency:
                return value
        raise Unavailable("Native home conversion is missing; do not substitute a mid-rate")


class NativeReadAdapter(V20ReadAdapter):
    async def pricing(self, account_id: str, instruments: tuple[CurrencyInstrument, ...]) -> PricingRead:
        self._authorize(account_id)
        if not instruments or len(instruments) > 50 or len({i.name for i in instruments}) != len(instruments):
            raise ValueError("Pricing instrument universe is invalid")
        document = await self._http.get(self.endpoints.rest,
            f"/v3/accounts/{account_segment(account_id)}/pricing",
            params={"instruments": ",".join(i.name for i in instruments), "includeHomeConversions": "true"})
        rows, conversions = document.body.get("prices"), document.body.get("homeConversions")
        if not isinstance(rows, list) or not isinstance(conversions, list):
            raise Unavailable("Native prices and home conversions must come from the same response")
        metadata = {value.name: value for value in instruments}
        received = utc_now()
        observed = set()
        prices = []
        for row in rows:
            if not isinstance(row, dict) or row.get("instrument") not in metadata or row["instrument"] in observed:
                raise Unavailable("Pricing response contains unrequested or duplicate instruments")
            observed.add(row["instrument"])
            prices.append(quote_from_native(row, instrument=metadata[row["instrument"]],
                                            environment=self.environment, received_at=received))
        if observed != set(metadata):
            raise Unavailable("Pricing response omitted an instrument")
        native_conversions = []
        for row in conversions:
            if not isinstance(row, dict):
                raise Unavailable("Invalid home conversion document")
            try:
                native_conversions.append(HomeConversions(currency=row["currency"],
                    position_value=wire_decimal(row["positionValue"]),
                    account_gain=wire_decimal(row["accountGain"]), account_loss=wire_decimal(row["accountLoss"])))
            except (KeyError, ValueError, TypeError) as exc:
                raise Unavailable("Native home conversion is incomplete") from exc
        if len({value.currency for value in native_conversions}) != len(native_conversions):
            raise Unavailable("Duplicate home conversion currency")
        response_time = native_time(document.body.get("time"))
        if response_time > received + timedelta(seconds=1) or any(q.observed_at > response_time for q in prices):
            raise SafetyFence("Pricing response contains a future or inconsistent time cut")
        return PricingRead(tuple(prices), tuple(native_conversions), response_time, document.sha256)

    async def candles(self, account_id: str, instrument: CurrencyInstrument, *, granularity: str = "M5",
                      count: int = 100, from_time: datetime | None = None) -> tuple[Candle, ...]:
        self._authorize(account_id)
        if granularity not in GRANULARITIES or type(count) is not int or not 1 <= count <= 5000:
            raise ValueError("Unsupported bar interval or history request bound")
        params = {"price": "BA", "granularity": granularity, "count": str(count), "smooth": "false"}
        if from_time is not None:
            params.update({"from": require_utc(from_time).isoformat(), "includeFirst": "false"})
        document = await self._http.get(self.endpoints.rest,
            f"/v3/instruments/{currency_pair(instrument.name)}/candles", params=params)
        if document.body.get("instrument") != instrument.name or document.body.get("granularity") != granularity:
            raise SafetyFence("Candle response belongs to another instrument or interval")
        rows = document.body.get("candles")
        if not isinstance(rows, list) or len(rows) > count:
            raise Unavailable("Native candle response is unbounded or malformed")
        result = []
        now = utc_now()
        for row in rows:
            if not isinstance(row, dict) or type(row.get("complete")) is not bool:
                raise Unavailable("Candle completion flag is missing")
            if not row["complete"]:
                continue
            try:
                start = native_time(row["time"])
                fields = {f"{side}_{part}": wire_decimal(row[side][short])
                          for side in ("bid", "ask")
                          for part, short in (("open", "o"), ("high", "h"), ("low", "l"), ("close", "c"))}
                value = Candle(instrument=instrument.name, start=start,
                    end=start + timedelta(seconds=GRANULARITIES[granularity]), volume=row["volume"],
                    quantum=Decimal(1).scaleb(-instrument.display_precision), source="OANDA_" + self.environment,
                    **fields)
                if value.end > now or (result and result[-1].end > value.start):
                    raise ValueError("Future, duplicate or overlapping candle")
                result.append(value)
            except (KeyError, ValueError, TypeError) as exc:
                raise Unavailable("Native completed bid/ask candle violates its causal contract") from exc
        return tuple(result)

    async def account_changes(self, account_id: str, since: str) -> ReadDocument:
        self._authorize(account_id)
        document = await self._http.get(self.endpoints.rest,
            f"/v3/accounts/{account_segment(account_id)}/changes",
            params={"sinceTransactionID": transaction_id(since)})
        frontier = transaction_id(document.body.get("lastTransactionID"))
        if int(frontier) < int(since) or not isinstance(document.body.get("changes"), dict) or not isinstance(document.body.get("state"), dict):
            raise Unavailable("Native account update does not establish a complete current-state cut")
        return document

    async def pricing_stream(self, account_id: str, instruments: tuple[CurrencyInstrument, ...]) -> AsyncGenerator[Quote | dict[str, Any], None]:
        self._authorize(account_id)
        metadata = {value.name: value for value in instruments}
        if not metadata or len(metadata) != len(instruments) or len(metadata) > 50:
            raise ValueError("Invalid pricing stream universe")
        async for row in self._http.stream(self.endpoints.stream,
                f"/v3/accounts/{account_segment(account_id)}/pricing/stream", private=False,
                params={"instruments": ",".join(metadata), "snapshot": "true"}):
            self._authorize(account_id)
            if row.get("type") == "HEARTBEAT":
                native_time(row.get("time"))
                yield {"type": "HEARTBEAT", "time": row["time"]}
            elif row.get("instrument") in metadata:
                yield quote_from_native(row, instrument=metadata[row["instrument"]], environment=self.environment)
            else:
                raise Unavailable("Unexpected native pricing stream event")
