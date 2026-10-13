"""Approved challenger discovery; paths grant no authority without signatures."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


from oandafly.domain.primitives import StrictModel
from oandafly.neural.qualification import Qualification
from oandafly.security.attestations import ApprovalVerifier, private_document


class CandidateReference(StrictModel):
    format: Literal["oandafly.candidate-reference.1"] = "oandafly.candidate-reference.1"
    model_directory: str
    validation_document: str
    deployment_document: str

    def path(self, field: str, root: Path) -> Path:
        candidate = Path(getattr(self, field))
        if ".." in candidate.parts:
            raise ValueError("Registry paths cannot contain traversal")
        result = candidate if candidate.is_absolute() else root / candidate
        if any(parent.is_symlink() for parent in (result, *result.absolute().parents)):
            raise ValueError("Registry paths cannot contain symbolic links")
        return result.absolute()


@dataclass(frozen=True)
class ApprovedCandidate:
    model: Path
    validation: Path
    deployment: Path
    qualification: Qualification


def select_approved_candidate(*, directory: Path, graph: Path, dataset_review: Path,
                              verifier: ApprovalVerifier, environment: str, account_id: str,
                              grant_id: str, instruments: tuple[str, ...], after_generation: int) -> ApprovedCandidate | None:
    paths = sorted(directory.glob("*.json"))
    if len(paths) > 1000:
        raise ValueError("Approved candidate registry exceeds its bound")
    candidates = []
    for path in paths:
        reference = CandidateReference.model_validate_json(private_document(path))
        model, validation, deployment = (reference.path(name, directory) for name in
                                         ("model_directory", "validation_document", "deployment_document"))
        # Invalid, expired or revoked documents are not silently ignored. The
        # registry itself is an operator-managed approved queue, not a raw inbox.
        qualification = Qualification(graph_directory=graph, model_directory=model, verifier=verifier,
            dataset_review=dataset_review, model_validation=validation, deployment_approval=deployment,
            environment=environment, account_id=account_id, grant_id=grant_id, instruments=instruments)
        if qualification.generation > after_generation:
            candidates.append(ApprovedCandidate(model, validation, deployment, qualification))
    if not candidates:
        return None
    generations = [candidate.qualification.generation for candidate in candidates]
    if len(generations) != len(set(generations)):
        raise ValueError("Deployment generation cannot select two competing artifacts")
    return max(candidates, key=lambda candidate: candidate.qualification.generation)
