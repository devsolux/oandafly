"""Mode, account and authorization are independent; no enable-live boolean."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from oandafly.domain.errors import SafetyFence
from oandafly.domain.primitives import StrictModel, money, require_utc, utc_now


class RuntimeMode(StrEnum):
    REPLAY = "REPLAY"
    PAPER = "PAPER"
    PRACTICE = "PRACTICE"
    SHADOW = "SHADOW"
    LIVE = "LIVE"

    @property
    def broker_mutation(self) -> bool:
        return self in {self.PRACTICE, self.LIVE}

    def require_environment(self, environment: str) -> None:
        if environment not in {"LIVE", "SANDBOX"}:
            raise SafetyFence("Unknown broker environment")
        if self == self.LIVE and environment != "LIVE":
            raise SafetyFence("LIVE mode requires the live environment")
        if self in {self.REPLAY, self.PAPER, self.PRACTICE} and environment != "SANDBOX":
            raise SafetyFence("Replay, paper and practice cannot bind live")


class RiskLimits(StrictModel):
    """Operator-approved bounds, never parameters selected by the neural model."""

    maximum_gross_leverage: Decimal = Field(default=Decimal("2"), ge=1, le=30)
    maximum_instrument_units: Decimal = Field(default=Decimal("1000"), gt=0)
    maximum_entry_loss_fraction: Decimal = Field(default=Decimal("0.0025"), gt=0, le=Decimal("0.02"))
    maximum_daily_loss_fraction: Decimal = Field(default=Decimal("0.01"), gt=0, le=Decimal("0.1"))
    maximum_drawdown_fraction: Decimal = Field(default=Decimal("0.03"), gt=0, le=Decimal("0.2"))
    maximum_currency_fraction: Decimal = Field(default=Decimal("1"), gt=0, le=5)
    maximum_spread_pips: Decimal = Field(default=Decimal("3"), gt=0, le=100)
    stop_distance_pips: Decimal = Field(default=Decimal("30"), gt=0, le=1000)
    gap_buffer_pips: Decimal = Field(default=Decimal("10"), ge=0, le=1000)
    slippage_bound_pips: Decimal = Field(default=Decimal("2"), gt=0, le=100)
    margin_buffer_fraction: Decimal = Field(default=Decimal("0.25"), ge=0, lt=1)
    maximum_quote_age_seconds: int = Field(default=5, ge=1, le=30)
    maximum_account_age_seconds: int = Field(default=10, ge=1, le=30)
    minimum_confidence: Decimal = Field(default=Decimal("0.6"), ge=0, le=1)

    @field_validator(
        "maximum_gross_leverage", "maximum_instrument_units", "maximum_entry_loss_fraction",
        "maximum_daily_loss_fraction", "maximum_drawdown_fraction", "maximum_currency_fraction",
        "maximum_spread_pips", "stop_distance_pips", "gap_buffer_pips", "slippage_bound_pips",
        "margin_buffer_fraction", "minimum_confidence", mode="before",
    )
    @classmethod
    def exact_values(cls, value: object) -> Decimal:
        return money(value)  # type: ignore[arg-type]


class AuthorizationGrant(StrictModel):
    grant_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    authority_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    environment: Literal["SANDBOX", "LIVE"]
    mode: Literal["PRACTICE", "LIVE"]
    account_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_family: Literal["MALECNS_FULL_RATE_V1"] = "MALECNS_FULL_RATE_V1"
    instruments: tuple[str, ...] = Field(min_length=1, max_length=30)
    limits: RiskLimits
    issued_at: datetime
    expires_at: datetime
    # Recovery can remain authorized briefly beyond entry authorization.
    recovery_until: datetime
    allow_unattended_resume: bool = False
    allow_automatic_promotion: bool = False

    _utc = field_validator("issued_at", "expires_at", "recovery_until")(require_utc)

    @field_validator("instruments")
    @classmethod
    def unique_instruments(cls, names: tuple[str, ...]) -> tuple[str, ...]:
        from oandafly.domain.instruments import currency_pair
        if len(set(names)) != len(names):
            raise ValueError("Duplicate authorized instruments")
        for name in names:
            currency_pair(name)
        return names

    @model_validator(mode="after")
    def coherent(self) -> AuthorizationGrant:
        RuntimeMode(self.mode).require_environment(self.environment)
        if not self.issued_at < self.expires_at <= self.recovery_until:
            raise ValueError("Invalid authorization intervals")
        if (self.recovery_until - self.issued_at).days > 366:
            raise ValueError("Authorization must be renewed at least yearly")
        return self

    def require(
        self, *, mode: RuntimeMode, environment: str, account_id: str,
        authority_id: str, profile_sha256: str, action: str,
        instrument: str | None = None, now: datetime | None = None,
    ) -> None:
        mode.require_environment(environment)
        if not mode.broker_mutation or mode.value != self.mode:
            raise SafetyFence("This mode has no matching broker mutation capability")
        if (environment, account_id, authority_id, profile_sha256) != (
            self.environment, self.account_id, self.authority_id, self.profile_sha256,
        ):
            raise SafetyFence("Authorization belongs to another account, environment or profile")
        if action not in {"ENTRY", "RECOVERY"}:
            raise SafetyFence("Unknown authorization operation")
        current = require_utc(now or utc_now())
        end = self.expires_at if action == "ENTRY" else self.recovery_until
        if not self.issued_at <= current < end:
            raise SafetyFence("Authorization is not current")
        if instrument is not None and instrument not in self.instruments:
            raise SafetyFence("Instrument is outside the authorization envelope")
