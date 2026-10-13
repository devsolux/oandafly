"""Source-package contracts; no broker access or execution authority."""
from __future__ import annotations

import json
import shutil
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts.check_distribution import check_distribution
from scripts.lock_dependencies import validate_export
from scripts.package_source import package
from scripts.project_integrity import (
    MANIFEST_NAME, RELEASE_FILES, ROOT, audit, source_files, verify_manifest, write_manifest,
)


def put(root: Path, name: str, content: str = "example\n") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_checked_out_source_is_complete() -> None:
    result = audit(ROOT)
    assert result["source_check_passed"], result["issues"]
    assert result["python_modules"] >= 83
    assert result["native_trading_qualified"] is False


@pytest.mark.parametrize("name", [*RELEASE_FILES, ".python-version", "requirements-build.in",
                                  "deploy/systemd/oandafly.service.template"])
def test_packager_retains_supported_release_files(tmp_path: Path, name: str) -> None:
    assert put(tmp_path, name) in source_files(tmp_path)


@pytest.mark.parametrize("name", ["private/token.json", "models/encoder.npy", "credentials/ref.json",
                                  "src/oandafly/__pycache__/x.pyc", ".venv/private.json",
                                  "dist/old.zip", "state.sqlite3", "signer.pem", ".env"])
def test_runtime_data_and_build_caches_are_excluded(tmp_path: Path, name: str) -> None:
    put(tmp_path, name)
    assert source_files(tmp_path) == []


def test_source_link_is_rejected(tmp_path: Path) -> None:
    target = put(tmp_path, "README.md")
    link = tmp_path / "src/oandafly/link.py"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    with pytest.raises(ValueError, match="Symbolic link"):
        source_files(tmp_path)


def test_manifest_is_verifiable_and_does_not_hash_itself(tmp_path: Path) -> None:
    put(tmp_path, "README.md")
    manifest = write_manifest(tmp_path)
    assert MANIFEST_NAME not in manifest["files"]
    assert manifest["file_count_excluding_manifest"] == 1
    assert verify_manifest(tmp_path) == []


@pytest.mark.parametrize("action", ["modify", "delete", "add"])
def test_manifest_detects_changes(tmp_path: Path, action: str) -> None:
    target = put(tmp_path, "README.md")
    write_manifest(tmp_path)
    if action == "modify":
        target.write_text("edited", encoding="utf-8")
    elif action == "delete":
        target.unlink()
    else:
        put(tmp_path, "scripts/new.py", "pass\n")
    assert verify_manifest(tmp_path)


def test_missing_manifest_is_not_silently_recreated(tmp_path: Path) -> None:
    assert verify_manifest(tmp_path)
    assert not (tmp_path / MANIFEST_NAME).exists()


def test_missing_required_files_fail_source_check(tmp_path: Path) -> None:
    result = audit(tmp_path)
    assert not result["source_check_passed"]
    assert "src/oandafly/neural/runtime.py" in result["missing_required_source_files"]


def test_release_mode_requires_locks(tmp_path: Path) -> None:
    result = audit(tmp_path, require_locks=True)
    assert result["missing_release_dependency_files"] == list(RELEASE_FILES)
    assert any("Unresolved release dependency" in issue for issue in result["issues"])


def test_hashed_export_is_accepted() -> None:
    validate_export("# dependencies only\nexample==1.2.3 \\\n    --hash=sha256:" + "1" * 64 + "\n")


@pytest.mark.parametrize("content", ["", "# only comments\n", "-e .\n", "example>=1\n",
                                      "example==1.0\n", "../project\n"])
def test_unresolved_or_project_requirements_are_not_locks(content: str) -> None:
    with pytest.raises(ValueError):
        validate_export(content)


def test_build_inputs_match_declared_backend_pins() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    pins = {line for line in (ROOT / "requirements-build.in").read_text().splitlines()
            if line and not line.startswith("#")}
    assert set(metadata["build-system"]["requires"]).issubset(pins)
    assert next(x for x in metadata["project"]["optional-dependencies"]["dev"]
                if x.startswith("build==")) in pins


def test_worker_examples_have_separate_authority_and_no_shadow_mutations() -> None:
    examples = {mode: json.loads((ROOT / f"examples/worker.{mode}.json.template").read_text())
                for mode in ("shadow", "practice", "live")}
    for name, document in examples.items():
        assert document["mode"] == name.upper()
        assert document["environment"] == ("LIVE" if name == "live" else "SANDBOX")
        assert document["observation_credential"]["authority_id"] == document["authority_id"]
        assert "<" in document["account_id"]
    assert all(examples["shadow"][name] is None for name in
               ("execution_credential", "authorization_grant", "deployment_approval"))


def test_templates_match_declared_top_level_schema_fields() -> None:
    mapping = {"authorization-grant": "AuthorizationGrant", "candidate-reference": "CandidateReference",
               "cost-assumptions": "CostAssumptions", "credential-reference": "CredentialReference",
               "dataset-review": "DatasetReview", "deployment-approval": "DeploymentApproval",
               "eligibility-evidence": "EligibilityEvidence", "model-validation": "ModelValidation",
               "risk-limits": "RiskLimits", "training-spec": "TrainingSpec", "trust-policy": "TrustPolicy",
               "account-profile": "VerifiedAccountProfile"}
    for filename, model in mapping.items():
        schema = json.loads((ROOT / f"examples/schemas/{model}.schema.json").read_text())
        template = json.loads((ROOT / f"examples/{filename}.json.template").read_text())
        assert set(template) == set(schema["properties"])
        assert set(schema.get("required", [])).issubset(template)


def test_training_defaults_parse_but_cost_evidence_is_not_fabricated() -> None:
    from oandafly.training.trainer import CostAssumptions, TrainingSpec
    from oandafly.domain.errors import ValidationFailure
    TrainingSpec.model_validate_json((ROOT / "examples/training-spec.json.template").read_text())
    with pytest.raises((ValueError, ValidationFailure)):
        CostAssumptions.model_validate_json((ROOT / "examples/cost-assumptions.json.template").read_text())


def test_distribution_checker_detects_missing_runtime_file(tmp_path: Path) -> None:
    root = tmp_path / "source"
    put(root, "src/oandafly/__init__.py", '__version__ = "1.0.0"\n')
    wheel = tmp_path / "empty.whl"
    with zipfile.ZipFile(wheel, "w"):
        pass
    report = check_distribution(wheel, root)
    assert report["missing"] == ["oandafly/__init__.py"]
    assert not report["passed"]


def test_sdist_checker_requires_workflow_files(tmp_path: Path) -> None:
    root = tmp_path / "source"
    readme = put(root, "README.md")
    put(root, ".github/workflows/ci.yml", "name: Source checks\n")
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(readme, arcname="oandafly-1.0.0/README.md")
    assert check_distribution(archive, root)["missing"] == [".github/workflows/ci.yml"]


def test_source_zip_is_deterministic_and_verifies_after_extraction(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for path in source_files(ROOT):
        target = source / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    first = package(tmp_path / "one.zip", root=source)
    second = package(tmp_path / "two.zip", root=source)
    assert first["sha256"] == second["sha256"]
    with zipfile.ZipFile(tmp_path / "one.zip") as archive:
        assert archive.testzip() is None
        assert "oandafly-1.0.0/PACKAGE_MANIFEST.json" in archive.namelist()
        archive.extractall(tmp_path / "unpacked")  # Controlled archive created above.
    assert verify_manifest(tmp_path / "unpacked/oandafly-1.0.0") == []


def test_packaging_does_not_overwrite_checkout(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside the source tree"):
        package(tmp_path / "release.zip", root=tmp_path)
