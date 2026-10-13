"""Independent account and operator grants, checked again at mutation boundaries."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import Field, field_validator

from oandafly.domain.capabilities import AuthorizationGrant, RuntimeMode
from oandafly.domain.context import ContextKey
from oandafly.domain.errors import SafetyFence
from oandafly.domain.policy import VerifiedAccountProfile
from oandafly.domain.primitives import StrictModel, require_utc, sha256_json, utc_now
from oandafly.neural.qualification import Qualification
from oandafly.security.attestations import ApprovalVerifier


class EligibilityEvidence(StrictModel):
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    underlying_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    eligible_v20_fx_account: bool
    reviewed_at: datetime
    expires_at: datetime
    _utc = field_validator("reviewed_at", "expires_at")(require_utc)


class NativeProfileVerifier:
    def __init__(self, verifier: ApprovalVerifier, document: Path) -> None:
        self.verifier, self.document = verifier, document

    def verify(self, profile: VerifiedAccountProfile) -> None:
        profile.require_current()
        document = self.verifier.verify(self.document, role="ACCOUNT_ELIGIBILITY")
        evidence = EligibilityEvidence.from_wire(document.payload)
        if (profile.api_binding != "V20" or not evidence.eligible_v20_fx_account
            or evidence.profile_sha256 != sha256_json(profile)
            or evidence.underlying_evidence_sha256 != profile.evidence_sha256
            or document.key_id != profile.verification_issuer
            or not evidence.reviewed_at <= utc_now() < evidence.expires_at):
            raise SafetyFence("Account eligibility has not been independently verified for this exact profile")


class GrantAuthorizer:
    def __init__(self, *, verifier: ApprovalVerifier, document: Path, mode: RuntimeMode,
                 account_id: str, qualification: Qualification | None = None) -> None:
        self.verifier, self.document, self.mode, self.account_id = verifier, document, mode, account_id
        self.qualification = qualification
        self.entry_block_reason: str | None = "STARTUP_NOT_RECONCILED"

    def grant(self) -> AuthorizationGrant:
        return AuthorizationGrant.from_wire(self.verifier.verify(self.document, role="TRADING_AUTHORIZATION").payload)

    def require(self, context: ContextKey, profile: VerifiedAccountProfile, action: str) -> None:
        grant = self.grant()
        if profile.account_ids != (self.account_id,):
            raise SafetyFence("This fresh worker owns exactly one separately qualified native account")
        grant.require(mode=self.mode, environment=context.environment, account_id=self.account_id,
                      authority_id=context.authority_id, profile_sha256=sha256_json(profile), action=action)
        if action == "ENTRY":
            if self.entry_block_reason is not None:
                raise SafetyFence(self.entry_block_reason)
            if self.qualification is None:
                raise SafetyFence("Full MaleCNS qualification is absent")
            self.qualification.require()
