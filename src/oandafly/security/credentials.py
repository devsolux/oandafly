"""Metadata-only, purpose-bound credential references and redacted secrets."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator

from oandafly.domain.errors import SafetyFence, ValidationFailure
from oandafly.domain.primitives import StrictModel, require_utc, utc_now

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def account_segment(value: str) -> str:
    if not _ACCOUNT.fullmatch(value):
        raise ValidationFailure("Invalid account identifier")
    return value


class CredentialReference(StrictModel):
    credential_id: UUID
    authority_id: str = Field(min_length=1, max_length=128)
    environment: Literal["LIVE", "SANDBOX"]
    purpose: Literal["V20_OBSERVATION", "V20_EXECUTION", "RATES_OBSERVATION"]
    account_ids: tuple[str, ...] = ()
    expires_at: datetime
    entitlement_evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("authority_id")
    @classmethod
    def validate_authority(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("Invalid authority identifier")
        return value

    @field_validator("account_ids")
    @classmethod
    def validate_accounts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or len(value) > 256:
            raise ValueError("Invalid or duplicate account scope")
        return tuple(account_segment(item) for item in value)

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def validate_scope(self) -> "CredentialReference":
        if self.purpose == "RATES_OBSERVATION":
            if self.account_ids or self.entitlement_evidence_sha256 is None:
                raise ValueError("Rates keys require separate entitlement evidence and no trading accounts")
        elif not self.account_ids:
            raise ValueError("v20 credentials require an explicitly enrolled account scope")
        return self

    @property
    def vault_key(self) -> str:
        # Secrets cannot move between purposes, accounts, environments or authorities.
        digest = hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()
        return f"com.devsolux.oandafly/{self.credential_id}/{digest}"

    def authorize(self, *, purpose: str, environment: str, account_id: str | None = None) -> None:
        if self.expires_at <= utc_now():
            raise SafetyFence("Credential enrollment has expired")
        if self.purpose != purpose or self.environment != environment:
            raise SafetyFence("Credential capability does not match requested service")
        if account_id is not None and account_segment(account_id) not in self.account_ids:
            raise SafetyFence("Account is outside enrolled credential scope")


class NativeVault(Protocol):
    """Secret material crosses this boundary only inside the worker."""

    def store(self, reference: CredentialReference, secret: SecretStr) -> None: ...
    def load(self, reference: CredentialReference) -> SecretStr: ...
    def delete(self, reference: CredentialReference) -> None: ...


def token_bytes(secret: SecretStr) -> bytes:
    token = secret.get_secret_value()
    if not 1 <= len(token) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValidationFailure("Bearer credential must be a bounded opaque printable token")
    return token.encode("ascii")
