"""Fresh 1.0.0 worker configuration. Data-only paths and credential references."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from oandafly.domain.capabilities import RuntimeMode
from oandafly.domain.primitives import StrictModel
from oandafly.security.attestations import private_document
from oandafly.security.credentials import CredentialReference


class WorkerConfig(StrictModel):
    format: Literal["oandafly.worker.1"] = "oandafly.worker.1"
    application_version: Literal["1.0.0"] = "1.0.0"
    authority_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    environment: Literal["SANDBOX", "LIVE"]
    mode: Literal["PRACTICE", "SHADOW", "LIVE"]
    account_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    instruments: tuple[str, ...] = Field(min_length=1, max_length=30)
    granularity: Literal["M1", "M5", "M15", "M30", "H1"] = "M5"
    vault_backend: Literal["NATIVE", "SYSTEMD"] = "NATIVE"
    observation_credential: CredentialReference
    execution_credential: CredentialReference | None = None
    profile: str
    eligibility_approval: str
    trust_policy: str
    graph_directory: str
    model_directory: str
    dataset_review: str
    model_validation: str | None = None
    deployment_approval: str | None = None
    authorization_grant: str | None = None
    registry_directory: str | None = None
    observation_interval_seconds: int = Field(default=2, ge=1, le=5)
    reconciliation_timeout_seconds: int = Field(default=30, ge=5, le=120)
    model_startup_timeout_seconds: int = Field(default=120, ge=10, le=600)
    inference_memory_gib: int = Field(default=8, ge=2, le=128)
    maximum_holding_bars: int = Field(default=12, ge=1, le=288)

    @model_validator(mode="after")
    def capability_boundary(self) -> WorkerConfig:
        mode = RuntimeMode(self.mode)
        mode.require_environment(self.environment)
        from oandafly.domain.instruments import currency_pair
        if len(self.instruments) != len(set(self.instruments)):
            raise ValueError("Duplicate runtime instrument")
        for value in self.instruments:
            currency_pair(value)
        self.observation_credential.authorize(purpose="V20_OBSERVATION", environment=self.environment,
                                               account_id=self.account_id)
        if self.observation_credential.authority_id != self.authority_id:
            raise ValueError("Observation credential belongs to another authority")
        if mode == RuntimeMode.SHADOW:
            if any(value is not None for value in (self.execution_credential, self.authorization_grant, self.deployment_approval)):
                raise ValueError("SHADOW must not enroll a trading grant or execution credential")
        else:
            if any(value is None for value in (self.execution_credential, self.authorization_grant,
                                               self.model_validation, self.deployment_approval)):
                raise ValueError("Practice/live require all independent execution qualifications")
            self.execution_credential.authorize(purpose="V20_EXECUTION", environment=self.environment,
                                                 account_id=self.account_id)
            if self.execution_credential.authority_id != self.authority_id:
                raise ValueError("Execution credential belongs to another authority")
        return self

    def path(self, field: str, *, relative_to: Path) -> Path:
        value = getattr(self, field)
        if type(value) is not str or not value:
            raise ValueError(f"Required configuration path is absent: {field}")
        path = Path(value)
        if ".." in path.parts:
            raise ValueError("Configuration paths cannot contain parent traversal")
        path = path if path.is_absolute() else relative_to / path
        for parent in (path, *path.absolute().parents):
            if parent.is_symlink():
                raise ValueError("Configuration paths cannot traverse symlinks")
        return path.absolute()


def load_config(path: Path) -> WorkerConfig:
    return WorkerConfig.model_validate_json(private_document(path))
