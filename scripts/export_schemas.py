#!/usr/bin/env python3
"""Export exact data contracts. This creates no approvals or broker authority."""
from __future__ import annotations
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from oandafly.domain.capabilities import AuthorizationGrant, RiskLimits
from oandafly.domain.policy import VerifiedAccountProfile
from oandafly.neural.artifacts import GraphManifest, KernelConfig, ModelManifest
from oandafly.neural.qualification import DatasetReview, DeploymentApproval, ModelValidation
from oandafly.security.attestations import SignedDocument, TrustPolicy
from oandafly.security.authorization import EligibilityEvidence
from oandafly.security.credentials import CredentialReference
from oandafly.training.registry import CandidateReference
from oandafly.training.trainer import CostAssumptions, TrainingSpec
from oandafly.worker.configuration import WorkerConfig

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    destination = ROOT / "examples" / "schemas"
    destination.mkdir(parents=True, exist_ok=True)
    models = (AuthorizationGrant, RiskLimits, VerifiedAccountProfile, GraphManifest,
              KernelConfig, ModelManifest, DatasetReview, DeploymentApproval,
              ModelValidation, SignedDocument, TrustPolicy, EligibilityEvidence,
              CredentialReference, CandidateReference, CostAssumptions, TrainingSpec,
              WorkerConfig)
    for cls in models:
        document = {"$schema": "https://json-schema.org/draft/2020-12/schema", **cls.model_json_schema()}
        (destination / (cls.__name__ + ".schema.json")).write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"schemas": len(models), "approvals_created": 0}))


if __name__ == "__main__":
    main()
