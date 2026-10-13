"""FX units are exposure memorandum values, not multi-currency cash deposits."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, localcontext
from typing import Annotated

from pydantic import BeforeValidator, Field, StrictBool, field_validator, model_validator

from oandafly.domain.instruments import currency_pair
from oandafly.domain.primitives import StrictModel, money, require_utc

Financial = Annotated[Decimal, BeforeValidator(money)]


class TradeExposure(StrictModel):
    trade_id: str = Field(min_length=1)
    instrument: str
    current_units: Financial

    @field_validator("instrument")
    @classmethod
    def _pair(cls, value: str) -> str:
        return currency_pair(value)


class PositionExposure(StrictModel):
    instrument: str
    long_units: Financial = Field(ge=0)
    short_units: Financial = Field(le=0)

    @field_validator("instrument")
    @classmethod
    def _pair(cls, value: str) -> str:
        return currency_pair(value)


class GrossExposureCut(StrictModel):
    account_id: str = Field(min_length=1)
    environment: str = Field(pattern=r"^(LIVE|SANDBOX)$")
    trades: tuple[TradeExposure, ...]
    positions: tuple[PositionExposure, ...]
    complete: StrictBool
    observed_at: datetime
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _valid(self) -> GrossExposureCut:
        require_utc(self.observed_at)
        if len({trade.trade_id for trade in self.trades}) != len(self.trades):
            raise ValueError("A native trade can occur only once in a complete cut")
        if len({position.instrument for position in self.positions}) != len(self.positions):
            raise ValueError("A native position can occur only once in a complete cut")
        if self.complete:
            by_instrument = {position.instrument: position for position in self.positions}
            for instrument in {trade.instrument for trade in self.trades} | set(by_instrument):
                with localcontext() as ctx:
                    ctx.prec = 512
                    long_units = sum((trade.current_units for trade in self.trades
                                      if trade.instrument == instrument and trade.current_units > 0), Decimal(0))
                    short_units = sum((trade.current_units for trade in self.trades
                                       if trade.instrument == instrument and trade.current_units < 0), Decimal(0))
                position = by_instrument.get(instrument)
                if (position is None and (long_units != 0 or short_units != 0)
                        or position is not None and (position.long_units != long_units or position.short_units != short_units)):
                    raise ValueError("Trade and position gross units disagree at the evidence cut")
        return self

    @property
    def proven_flat(self) -> bool:
        return (self.complete and all(trade.current_units == 0 for trade in self.trades)
                and all(position.long_units == 0 and position.short_units == 0 for position in self.positions))

    @property
    def gross_trade_units(self) -> Decimal:
        """A diagnostic gross count; unlike-currency units cannot form a risk budget."""
        with localcontext() as ctx:
            ctx.prec = 512
            return sum((abs(trade.current_units) for trade in self.trades), Decimal(0))
