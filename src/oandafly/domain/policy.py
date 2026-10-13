"""Immutable account policy and independent legal-product admission boundary.

Conservative application caps are retained even where an account is not legally
a CFD account. They do not establish eligibility to open such an account.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, localcontext
from typing import Annotated, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from oandafly.domain.instruments import CurrencyInstrument, currency_code, is_regulatory_major
from oandafly.domain.primitives import StrictModel, money, require_utc, utc_now

Environment = Literal["LIVE", "SANDBOX"]
RuntimeMode = Literal["PAPER", "PRACTICE", "SHADOW", "REPLAY", "LIVE"]
Region = Literal["DE", "EU", "GLOBAL"]
LegalProduct = Literal["NON_CFD_FX", "CFD_FX", "SYNTHETIC_FX"]


class LeverageRule(StrictModel):
    instrument: CurrencyInstrument
    maximum_leverage: Decimal

    @field_validator("maximum_leverage", mode="before")
    @classmethod
    def exact_decimal(cls, value: object) -> Decimal:
        if not isinstance(value, (str, Decimal, int)) or isinstance(value, bool):
            raise ValueError("Leverage must be an exact decimal")
        return money(value)

    @model_validator(mode="after")
    def conservative_cap(self) -> LeverageRule:
        regulatory = Decimal("30") if is_regulatory_major(self.instrument.name) else Decimal("20")
        # Multiplication avoids a rounded reciprocal admitting excess leverage.
        if self.maximum_leverage < 1 or self.maximum_leverage > regulatory:
            raise ValueError("Leverage exceeds the application's ESMA 30:1 / 20:1 cap")
        with localcontext() as context:
            context.prec = 400
            if self.maximum_leverage * self.instrument.margin_rate > 1:
                raise ValueError("Leverage exceeds the stricter native broker margin rate")
        return self


class VerifiedAccountProfile(StrictModel):
    """A verified claim bundle; construction alone never enrolls its authority.

    AuthorityCoordinator.register_profile additionally invokes a trusted,
    deployment-supplied ProfileVerifier. API instrument type is insufficient.
    """

    profile_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
    version: Annotated[int, Field(strict=True, ge=1)]
    authority_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
    environment: Environment
    account_ids: Annotated[tuple[str, ...], Field(min_length=1, max_length=100)]
    home_currency: str
    broker_entity: Annotated[str, Field(min_length=1, max_length=100)]
    legal_product: LegalProduct
    region: Region = "DE"
    retail: Literal[True] = True
    negative_balance_protection: Annotated[bool, Field(strict=True)]
    api_binding: Literal["V20", "PAPER"]
    instrument_rules: Annotated[tuple[LeverageRule, ...], Field(min_length=1, max_length=300)]
    verification_issuer: Annotated[str, Field(min_length=1, max_length=200)]
    evidence_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    verified_at: datetime
    expires_at: datetime

    _home = field_validator("home_currency")(currency_code)
    _times = field_validator("verified_at", "expires_at")(require_utc)

    @field_validator("account_ids")
    @classmethod
    def exact_account_scope(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("Duplicate sub-accounts cannot form a disjoint scope")
        for account in value:
            if not account or len(account) > 128 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:-" for c in account):
                raise ValueError("Malformed account identifier")
        return value

    @model_validator(mode="after")
    def legal_and_native_compatibility(self) -> VerifiedAccountProfile:
        if self.expires_at <= self.verified_at:
            raise ValueError("Profile validity interval is empty")
        entity = " ".join(self.broker_entity.upper().replace("_", " ").split())
        if "TMS" in entity or "GLOBAL MARKETS" in entity:
            raise ValueError("OANDA TMS and OANDA Global Markets do not support v20")
        if len({item.instrument.name for item in self.instrument_rules}) != len(self.instrument_rules):
            raise ValueError("Duplicate instrument policy")
        if self.api_binding == "PAPER":
            if self.environment != "SANDBOX" or self.legal_product != "SYNTHETIC_FX":
                raise ValueError("PAPER must be a SANDBOX synthetic FX profile")
            if entity != "SYNTHETIC PAPER" or any(not x.startswith("SYNTHETIC:PAPER:") for x in self.account_ids):
                raise ValueError("Synthetic accounts must use the explicit PAPER namespace")
        else:
            if self.legal_product not in {"NON_CFD_FX", "CFD_FX"}:
                raise ValueError("v20 requires independent evidence of the actual FX legal product")
            if not self.broker_entity.upper().startswith("OANDA "):
                raise ValueError("Only independently verified OANDA entities are permitted")
        if self.region in {"DE", "EU"} and not self.negative_balance_protection:
            raise ValueError("German/EU retail policy requires negative balance protection")
        return self

    def require_current(self, now: datetime | None = None) -> None:
        current = require_utc(now or utc_now())
        if not self.verified_at <= current < self.expires_at:
            raise ValueError("Account verification is not currently valid")

    def instrument_rule(self, name: str) -> LeverageRule:
        for rule in self.instrument_rules:
            if rule.instrument.name == name:
                return rule
        raise ValueError("Instrument is outside the verified account policy")


class ProfileVerifier(Protocol):
    """Trusted process dependency, never deserialized from GUI/model input."""

    def verify(self, profile: VerifiedAccountProfile) -> None:
        """Raise unless signed evidence independently binds the exact profile."""


class PaperProfileVerifier:
    """Explicit synthetic qualification; cannot authorize any OANDA API call."""

    def verify(self, profile: VerifiedAccountProfile) -> None:
        profile.require_current()
        if profile.api_binding != "PAPER" or profile.verification_issuer != "oandafly.synthetic.paper.v1":
            raise ValueError("The PAPER verifier cannot qualify real broker accounts")


class EngineConfig(StrictModel):
    application_name: Literal["oandafly"] = "oandafly"
    application_version: Literal["1.0.0"] = "1.0.0"
    application_identifier: Literal["com.devsolux.oandafly"] = "com.devsolux.oandafly"
    environment: Environment = "SANDBOX"
    product_track: Literal["TRADING", "DATA"] = "DATA"
    runtime_mode: RuntimeMode = "PAPER"
    region: Region = "DE"
    proof_max_age_seconds: Annotated[int, Field(strict=True, ge=1, le=60)] = 15
    transition_timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=300)] = 120
    maximum_gross_leverage: Decimal = Decimal("20")

    @field_validator("maximum_gross_leverage", mode="before")
    @classmethod
    def exact_decimal(cls, value: object) -> Decimal:
        if not isinstance(value, (str, Decimal, int)) or isinstance(value, bool):
            raise ValueError("Leverage must be an exact decimal")
        return money(value)

    @model_validator(mode="after")
    def coherent_mode(self) -> EngineConfig:
        if not 1 <= self.maximum_gross_leverage <= 30:
            raise ValueError("Gross leverage must be in [1, 30]")
        if self.runtime_mode in {"PAPER", "PRACTICE", "REPLAY"} and self.environment != "SANDBOX":
            raise ValueError("PAPER/PRACTICE/REPLAY cannot bind the live environment")
        if self.runtime_mode == "LIVE" and self.environment != "LIVE":
            raise ValueError("LIVE runtime requires the live environment")
        return self
