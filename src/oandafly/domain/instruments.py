"""Strict currency instruments; API classification does not establish legal form.

The allowlist deliberately excludes precious metals, accounting units, private
currencies and cryptocurrencies. New ISO currencies require a reviewed release.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from oandafly.domain.primitives import StrictModel, money

# Reviewed application registry, 2026-09-19. Some entries remain for historical
# accounting. Native account instrument enrollment separately determines whether
# a pair is currently available; registry membership is never trading permission.
NATIONAL_CURRENCIES = frozenset(
    "AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND "
    "BOB BRL BSD BTN BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF "
    "DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD "
    "HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW "
    "KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU "
    "MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP "
    "PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS "
    "SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX "
    "USD UYU UZS VES VND VUV WST XAF XCD XCG XOF XPF YER ZAR ZMW ZWG".split()
)

# ESMA's regulatory definition is not the colloquial set of seven major pairs.
REGULATORY_MAJOR_CURRENCIES = frozenset({"USD", "EUR", "JPY", "GBP", "CAD", "CHF"})


def currency_code(value: str) -> str:
    if value not in NATIONAL_CURRENCIES:
        raise ValueError(f"Unsupported national currency: {value!r}")
    return value


def currency_pair(value: str) -> str:
    if len(value) != 7 or value[3] != "_":
        raise ValueError("Currency pair must use exact BASE_QUOTE notation")
    base, quote = value.split("_")
    currency_code(base)
    currency_code(quote)
    if base == quote:
        raise ValueError("Base and quote currencies must differ")
    return value


def is_regulatory_major(instrument: str) -> bool:
    currency_pair(instrument)
    base, quote = instrument.split("_")
    return base in REGULATORY_MAJOR_CURRENCIES and quote in REGULATORY_MAJOR_CURRENCIES


class CurrencyInstrument(StrictModel):
    """An admitted FX instrument with broker precision, not a legal attestation."""

    name: str
    instrument_type: Literal["CURRENCY"] = "CURRENCY"
    trade_units_precision: Annotated[int, Field(strict=True, ge=0, le=8)] = 0
    display_precision: Annotated[int, Field(strict=True, ge=0, le=10)] = 5
    pip_location: Annotated[int, Field(strict=True, ge=-10, le=0)] = -4
    minimum_trade_size: Decimal = Decimal("1")
    maximum_order_units: Decimal = Decimal("100000000")
    margin_rate: Decimal = Decimal("0.05")

    _pair = field_validator("name")(currency_pair)

    @field_validator("minimum_trade_size", "maximum_order_units", "margin_rate", mode="before")
    @classmethod
    def exact_decimal(cls, value: object) -> Decimal:
        if not isinstance(value, (str, Decimal, int)) or isinstance(value, bool):
            raise ValueError("Financial values must be Decimal, decimal strings or integers")
        return money(value)

    @model_validator(mode="after")
    def valid_sizes(self) -> CurrencyInstrument:
        if self.minimum_trade_size <= 0 or self.maximum_order_units < self.minimum_trade_size:
            raise ValueError("Invalid native trade size range")
        if not Decimal("0") < self.margin_rate <= Decimal("1"):
            raise ValueError("Margin rate must be in (0, 1]")
        quantum = Decimal(1).scaleb(-self.trade_units_precision)
        with localcontext() as context:
            context.prec = 400
            if self.minimum_trade_size % quantum or self.maximum_order_units % quantum:
                raise ValueError("Trade sizes exceed the broker's tradeUnitsPrecision")
        return self

    @property
    def base_currency(self) -> str:
        return self.name[:3]

    @property
    def quote_currency(self) -> str:
        return self.name[4:]

    @property
    def pip_size(self) -> Decimal:
        return Decimal(1).scaleb(self.pip_location)

    @property
    def units_quantum(self) -> Decimal:
        return Decimal(1).scaleb(-self.trade_units_precision)
