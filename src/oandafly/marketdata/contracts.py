"""Exact quotes/candles and causal market cuts shared by all execution modes."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from typing import Literal

from pydantic import Field, field_validator, model_validator

from oandafly.domain.errors import SafetyFence
from oandafly.domain.primitives import StrictModel, money, require_utc, sha256_json


class Quote(StrictModel):
    instrument: str = Field(pattern=r"^[A-Z]{3}_[A-Z]{3}$")
    bid: Decimal = Field(gt=0)
    ask: Decimal = Field(gt=0)
    quantum: Decimal = Field(gt=0)
    observed_at: datetime
    received_at: datetime
    tradeable: bool
    source: Literal["OANDA_SANDBOX", "OANDA_LIVE", "REPLAY", "PAPER"]

    _utc = field_validator("observed_at", "received_at")(require_utc)

    @field_validator("bid", "ask", "quantum", mode="before")
    @classmethod
    def exact(cls, value: object) -> Decimal:
        return money(value)  # type: ignore[arg-type]

    @model_validator(mode="after")
    def quote_lattice(self) -> Quote:
        with localcontext() as ctx:
            ctx.prec = 128
            if self.ask < self.bid or self.bid % self.quantum or self.ask % self.quantum:
                raise ValueError("Executable quotes must be ordered and on their native quantum")
        if self.observed_at > self.received_at + timedelta(seconds=1):
            raise ValueError("Broker quote is materially future-dated")
        return self

    @property
    def midpoint(self) -> Decimal:
        # A derived half-tick is exact; it need not be an executable order price.
        with localcontext() as ctx:
            ctx.prec = 128
            return (self.bid + self.ask) / Decimal(2)

    @property
    def midpoint_quantum(self) -> Decimal:
        return self.quantum / Decimal(2)

    @property
    def fingerprint(self) -> str:
        return sha256_json(self)

    def require_fresh(self, now: datetime, maximum_age: float) -> None:
        current = require_utc(now)
        age = (current - self.observed_at).total_seconds()
        if not self.tradeable or not 0 <= age <= maximum_age:
            raise SafetyFence("Instrument price is stale, future-dated or not tradeable")


class Candle(StrictModel):
    instrument: str = Field(pattern=r"^[A-Z]{3}_[A-Z]{3}$")
    start: datetime
    end: datetime
    bid_open: Decimal = Field(gt=0)
    bid_high: Decimal = Field(gt=0)
    bid_low: Decimal = Field(gt=0)
    bid_close: Decimal = Field(gt=0)
    ask_open: Decimal = Field(gt=0)
    ask_high: Decimal = Field(gt=0)
    ask_low: Decimal = Field(gt=0)
    ask_close: Decimal = Field(gt=0)
    volume: int = Field(ge=0)
    quantum: Decimal = Field(gt=0)
    complete: Literal[True] = True
    source: Literal["OANDA_SANDBOX", "OANDA_LIVE", "REPLAY", "PAPER"]

    _utc = field_validator("start", "end")(require_utc)

    @field_validator("bid_open", "bid_high", "bid_low", "bid_close", "ask_open", "ask_high", "ask_low", "ask_close", "quantum", mode="before")
    @classmethod
    def exact(cls, value: object) -> Decimal:
        return money(value)  # type: ignore[arg-type]

    @model_validator(mode="after")
    def structure(self) -> Candle:
        if self.end <= self.start:
            raise ValueError("Candle must have a positive duration")
        with localcontext() as ctx:
            ctx.prec = 128
            for side in ("bid", "ask"):
                values = [getattr(self, side + "_" + part) for part in ("open", "high", "low", "close")]
                opening, high, low, closing = values
                if any(value % self.quantum for value in values) or not low <= min(opening, closing) <= max(opening, closing) <= high:
                    raise ValueError("Invalid native OHLC lattice or bounds")
            if any(getattr(self, "bid_" + part) > getattr(self, "ask_" + part) for part in ("open", "high", "low", "close")):
                raise ValueError("Crossed bid/ask candle")
        return self

    @property
    def mid_close(self) -> Decimal:
        with localcontext() as ctx:
            ctx.prec = 128
            return (self.bid_close + self.ask_close) / Decimal(2)

    @property
    def fingerprint(self) -> str:
        return sha256_json(self)


class MarketCut(StrictModel):
    instrument: str
    as_of: datetime
    quote: Quote
    candles: tuple[Candle, ...] = Field(min_length=1, max_length=5000)
    account_frontier: str = Field(pattern=r"^(0|[1-9][0-9]{0,39})$")
    account_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    _utc = field_validator("as_of")(require_utc)

    @model_validator(mode="after")
    def causal(self) -> MarketCut:
        if self.quote.instrument != self.instrument or any(c.instrument != self.instrument for c in self.candles):
            raise ValueError("Market cut mixes instruments")
        if any(c.source != self.quote.source or c.quantum != self.quote.quantum for c in self.candles):
            raise ValueError("Market cut mixes environments, sources or price precision")
        if self.quote.observed_at > self.as_of or any(c.end > self.as_of for c in self.candles):
            raise ValueError("Market cut contains future observations")
        if any(a.end > b.start for a, b in zip(self.candles, self.candles[1:])):
            raise ValueError("Candle history is duplicated, overlapping or out of order")
        return self

    @property
    def fingerprint(self) -> str:
        return sha256_json(self)
