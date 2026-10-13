"""Evidence-backed readiness for an explicitly new full-MaleCNS model family.

No dependency on unavailable original Coinfly code; no constant 'ready' switch.
An independently signed report must bind the actual full graph, model, release
source, runtime, costs and qualification scenarios. Signatures authenticate the
reviewer, not the truth of a scientific or performance claim.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import platform
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from oandafly.domain.errors import SafetyFence
from oandafly.domain.primitives import StrictModel, require_utc, sha256_json, utc_now
from oandafly.neural.artifacts import GraphManifest, ModelManifest, manifest_document
from oandafly.security.attestations import ApprovalVerifier

REQUIRED_SCENARIOS = frozenset({
    "full_topology_integrity", "numerical_stability", "deterministic_restart_warmup",
    "causal_purged_validation", "cost_financing_accounting", "untouched_forward_evaluation",
    "inference_deadline_containment", "stale_market_data", "native_history_gap_recovery",
    "out_of_order_transactions", "duplicate_order_prevention", "unknown_dispatch_reconciliation",
    "protective_order_failure", "risk_and_currency_limits", "restart_with_open_positions",
    "environment_account_isolation", "kill_switch", "confidence_calibration",
})


def source_identity() -> str:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    sources = sorted(root.rglob("*.py"))
    if not sources or not (root / "__main__.py").is_file() or not (root / "neural/runtime.py").is_file():
        raise SafetyFence("Runtime source inventory is absent; frozen bundle cannot qualify")
    for path in sources:
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def runtime_identity() -> str:
    versions = {}
    for package in ("numpy", "scipy", "pydantic", "httpx", "cryptography", "threadpoolctl"):
        versions[package] = importlib.metadata.version(package)
    return sha256_json({"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                       "system": platform.system(), "machine": platform.machine(), "packages": versions})


class DatasetReview(StrictModel):
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    release: str
    source_sha256: dict[str, str]
    curated_neurons: int = Field(ge=1)
    internal_connections: int = Field(ge=1)
    internal_contacts: int = Field(ge=1)
    excluded_endpoint_rows: int = Field(ge=0)
    completeness: Literal["ALL_CURATED_NEURONS_ALL_INTERNAL_PUBLISHED_EDGES"]
    sign_assumptions_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_at: datetime
    expires_at: datetime
    _utc = field_validator("reviewed_at", "expires_at")(require_utc)

    def require(self, graph: GraphManifest) -> None:
        if graph.scope != "FULL_DECLARED_MALECNS":
            raise SafetyFence("A test fixture cannot qualify a broker trading policy")
        if (self.artifact_sha256, self.release, self.curated_neurons, self.internal_connections,
            self.internal_contacts, self.excluded_endpoint_rows, dict(self.source_sha256)) != (
            graph.fingerprint, graph.release, graph.neurons, graph.connections, graph.contacts,
            graph.excluded_endpoint_rows, dict(graph.source_sha256)):
            raise SafetyFence("Independent dataset review disagrees with the actual import inventory")
        if not self.reviewed_at <= utc_now() < self.expires_at:
            raise SafetyFence("Dataset review is not current")


class ModelValidation(StrictModel):
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    graph_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    training_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Each required passing scenario has an independently reviewable evidence file.
    passing_scenarios: dict[str, str]
    inference_deadline_ms: int = Field(ge=1, le=30000)
    measured_p99_ms: int = Field(ge=1, le=30000)
    validation_end: datetime
    forward_start: datetime
    forward_end: datetime
    reviewed_at: datetime
    expires_at: datetime
    _utc = field_validator("validation_end", "forward_start", "forward_end", "reviewed_at", "expires_at")(require_utc)

    @model_validator(mode="after")
    def evidence(self) -> ModelValidation:
        if not self.validation_end < self.forward_start < self.forward_end <= self.reviewed_at < self.expires_at:
            raise ValueError("Forward evaluation must be separate, completed and independently reviewed")
        if not REQUIRED_SCENARIOS <= set(self.passing_scenarios):
            raise ValueError("Model qualification scenario evidence is incomplete")
        if any(len(v) != 64 or any(c not in "0123456789abcdef" for c in v) for v in self.passing_scenarios.values()):
            raise ValueError("Scenario evidence must be content-addressed")
        if self.measured_p99_ms >= self.inference_deadline_ms:
            raise ValueError("Measured full-model latency does not meet the qualified deadline")
        return self


class DeploymentApproval(StrictModel):
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment: Literal["SANDBOX", "LIVE"]
    account_id: str
    grant_id: str
    instruments: tuple[str, ...] = Field(min_length=1, max_length=30)
    generation: int = Field(ge=1)
    valid_from: datetime
    valid_until: datetime
    live_qualification_report_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    _utc = field_validator("valid_from", "valid_until")(require_utc)

    @model_validator(mode="after")
    def scoped(self) -> DeploymentApproval:
        if self.valid_until <= self.valid_from or len(set(self.instruments)) != len(self.instruments):
            raise ValueError("Invalid deployment interval or instrument universe")
        if self.environment == "LIVE" and self.live_qualification_report_sha256 is None:
            raise ValueError("Live deployment needs separate account-specific qualification evidence")
        return self


class Qualification:
    def __init__(self, *, graph_directory: Path, model_directory: Path, verifier: ApprovalVerifier,
                 dataset_review: Path, model_validation: Path, deployment_approval: Path,
                 environment: str, account_id: str, grant_id: str, instruments: tuple[str, ...]) -> None:
        self.graph = GraphManifest.model_validate_json(manifest_document(graph_directory / "manifest.json"))
        self.model = ModelManifest.model_validate_json(manifest_document(model_directory / "manifest.json"))
        self.verifier = verifier
        self.dataset_review, self.model_validation, self.deployment_approval = dataset_review, model_validation, deployment_approval
        self.environment, self.account_id, self.grant_id, self.instruments = environment, account_id, grant_id, instruments
        self.source_sha256, self.runtime_sha256 = source_identity(), runtime_identity()
        self.deadline_seconds = 0.0
        self.generation = 0
        self.require()

    def require(self) -> str:
        dataset_doc = self.verifier.verify(self.dataset_review, role="DATASET_REVIEW")
        DatasetReview.from_wire(dataset_doc.payload).require(self.graph)
        validation_doc = self.verifier.verify(self.model_validation, role="MODEL_VALIDATION")
        validation = ModelValidation.from_wire(validation_doc.payload)
        if (validation.artifact_sha256, validation.graph_sha256, validation.source_sha256,
            validation.runtime_sha256, validation.training_receipt_sha256) != (
            self.model.fingerprint, self.graph.fingerprint, self.source_sha256, self.runtime_sha256,
            self.model.training_receipt_sha256):
            raise SafetyFence("Model validation belongs to another model, dataset, source or runtime")
        if self.model.graph_sha256 != self.graph.fingerprint or self.model.neurons != self.graph.neurons:
            raise SafetyFence("Model does not bind the full reviewed graph")
        if not validation.reviewed_at <= utc_now() < validation.expires_at:
            raise SafetyFence("Model validation is not current")
        deployment_doc = self.verifier.verify(self.deployment_approval, role="DEPLOYMENT")
        deployment = DeploymentApproval.from_wire(deployment_doc.payload)
        if (deployment.artifact_sha256, deployment.validation_document_sha256, deployment.environment,
            deployment.account_id, deployment.grant_id, set(deployment.instruments)) != (
            self.model.fingerprint, validation_doc.digest, self.environment,
            self.account_id, self.grant_id, set(self.instruments)):
            raise SafetyFence("Deployment approval belongs to another qualified model or account scope")
        if not deployment.valid_from <= utc_now() < deployment.valid_until:
            raise SafetyFence("Deployment approval is not current")
        if self.environment == "LIVE" and "practice_native_soak" not in validation.passing_scenarios:
            raise SafetyFence("Live requires completed native practice qualification")
        self.deadline_seconds = validation.inference_deadline_ms / 1000
        self.generation = deployment.generation
        return sha256_json({"dataset": dataset_doc.digest, "validation": validation_doc.digest,
                            "deployment": deployment_doc.digest})
