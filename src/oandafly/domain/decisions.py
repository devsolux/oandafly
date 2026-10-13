"""Immutable model-to-portfolio contracts. Neural values are not money."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from oandafly.domain.numeric import NumericModel
from oandafly.domain.primitives import StrictModel, money, require_utc, sha256_json


class Action(StrEnum):
    ABSTAIN = "ABSTAIN"
    KEEP = "KEEP"
    SET_TARGET = "SET_TARGET"
    FLATTEN = "FLATTEN"


class PolicyDecision(NumericModel):
    decision_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    market_cut_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    instrument: str = Field(pattern=r"^[A-Z]{3}_[A-Z]{3}$")
    observed_at: datetime
    valid_until: datetime
    action: Action
    # An allocation preference only. The independent governor chooses actual units.
    preference: float = Field(ge=-1, le=1, allow_inf_nan=False)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=160)
    warmup_observed: int = Field(default=0, ge=0)
    warmup_required: int = Field(default=0, ge=0)

    _utc = field_validator("observed_at", "valid_until")(require_utc)

    @model_validator(mode="after")
    def semantics(self) -> PolicyDecision:
        if self.valid_until <= self.observed_at:
            raise ValueError("Decision validity must be positive")
        if self.action != Action.SET_TARGET and self.preference != 0:
            raise ValueError("Only SET_TARGET may carry an allocation preference")
        return self


class PortfolioTarget(StrictModel):
    environment: str = Field(pattern=r"^(SANDBOX|LIVE)$")
    account_id: str = Field(min_length=1, max_length=128)
    instrument: str = Field(pattern=r"^[A-Z]{3}_[A-Z]{3}$")
    generation: int = Field(ge=1)
    decision_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    units: Decimal
    created_at: datetime
    expires_at: datetime
    account_frontier: str = Field(pattern=r"^(0|[1-9][0-9]{0,39})$")

    _utc = field_validator("created_at", "expires_at")(require_utc)

    @field_validator("units", mode="before")
    @classmethod
    def exact_units(cls, value: object) -> Decimal:
        return money(value)  # type: ignore[arg-type]

    @model_validator(mode="after")
    def times(self) -> PortfolioTarget:
        if self.expires_at <= self.created_at:
            raise ValueError("Target expiry must follow creation")
        return self

    @property
    def fingerprint(self) -> str:
        return sha256_json(self)
