#!/usr/bin/env python3
"""One enforced production pipeline with separate unsigned and signed trust profiles."""
from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from oandafly.platforms import TARGETS, native_target
from scripts.build_contracts import bundle_paths, identity, inspect_bundle
from scripts.project_integrity import audit, create_manifest, source_files, RELEASE_FILES
from scripts.release_policy import PROFILES, COMMON_GATES, policy, environment, digest, document_digest, validate_test_evidence, validate_audit
from scripts.release_artifacts import payload_identity, extract, publish
from scripts.verify_build import verify

ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def build_lock(root: Path = ROOT):
    lock = root / ".dev-build.lock"
    def check_developer() -> None:
        other = root / ".dev-environment.lock"
        if other.exists():
            try:
                owner = json.loads((other / "owner.json").read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise ValueError("Cleanup/setup operation lock exists") from exc
            if owner.get("pid") != os.getppid() or owner.get("operation") not in {"build", "build-unsigned", "release"}:
                raise ValueError("Another developer operation is active")
    check_developer()
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ValueError("Build lock already exists; inspect its owner before removing a stale lock") from exc
    try:
        check_developer()  # Close the race with cleanup acquiring its mutex.
        (lock / "owner.json").write_text(json.dumps({"pid": os.getpid(), "started_at": time.time()}), encoding="utf-8")
        yield
    finally:
        (lock / "owner.json").unlink(missing_ok=True)
        lock.rmdir()


def plan(target: str, release: bool) -> dict:
    # Read-only recipe API for tests/documentation, never a successful build mode.
    return {**policy("production_signed" if release else "production_unsigned", target),
            "target": target, "spec": "packaging/oandafly.spec", "built": False}


def run_logged(command: list[str], log: Path, *, env: dict | None = None, timeout: int = 900, cwd: Path = ROOT) -> None:
    with log.open("w", encoding="utf-8") as handle:
        result = subprocess.run(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=handle, stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode:
        raise ValueError(f"Build step failed ({result.returncode}); see {log}")


def windows_sign(bundle: Path, logs: Path, env: dict) -> None:
    thumbprint, timestamp = env.get("WIN_SIGN_CERT_SHA1", ""), env.get("WIN_TIMESTAMP_URL", "")
    if not re.fullmatch(r"[0-9A-Fa-f]{40}", thumbprint) or not timestamp.startswith("https://"):
        raise ValueError("Windows release requires WIN_SIGN_CERT_SHA1 and an HTTPS WIN_TIMESTAMP_URL")
    tool = env.get("SIGNTOOL_EXE") or shutil.which("signtool")
    if not tool or not Path(tool).is_file():
        raise ValueError("Install Windows SDK SignTool or set its absolute SIGNTOOL_EXE path")
    for name in ("oandafly.exe", "oandafly-cli.exe"):
        run_logged([tool, "sign", "/sha1", thumbprint, "/fd", "SHA256", "/tr", timestamp, "/td", "SHA256", str(bundle/name)],
                   logs / (name+".sign.txt"), env=env)
        run_logged([tool, "verify", "/pa", "/all", "/tw", str(bundle/name)], logs/(name+".verify-signature.txt"), env=env)


def package_native(bundle: Path, destination: Path, target: str, env: dict | None = None) -> None:
    if destination.exists():
        raise ValueError("Refusing to overwrite a completed native archive")
    if target == "macos-arm64":
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(bundle), str(destination)], env=env, check=True, timeout=300)
    elif target == "linux-amd64":
        with tarfile.open(destination, "w:gz", dereference=False) as archive:
            archive.add(bundle, arcname="oandafly", recursive=True)
    elif target == "windows-amd64":
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(bundle.rglob("*")):
                if path.is_symlink():
                    raise ValueError("Windows portable bundles must not depend on symbolic links")
                if path.is_file():
                    archive.write(path, "oandafly/" + path.relative_to(bundle).as_posix())
    else:
        raise ValueError("Unknown package target")


def preflight(target: str, profile: str, stage: Path) -> tuple[dict, dict]:
    import sqlite3
    from oandafly.macos import require_runtime
    from oandafly.neural.containment import capability
    require_runtime()
    if sys.version_info[:2] != (3, 13) or sqlite3.sqlite_version_info < (3, 51, 3):
        raise ValueError("Production builds require Python 3.13 linked to SQLite >=3.51.3")
    env = environment(stage / "environment", target)
    report = {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
              "host": platform.platform(), "issues": []}
    if not shutil.which("uv"):
        report["issues"].append("Install uv; no automatic interpreter downloads are allowed")
    else:
        tool = subprocess.check_output([shutil.which("uv"), "--version"], text=True, timeout=10).split()
        pins = (ROOT / "requirements-build.in").read_text().splitlines()
        if len(tool) < 2 or "uv==" + tool[1] not in pins:
            report["issues"].append("The uv resolver must match requirements-build.in exactly")
        report["uv_version"] = tool[1] if len(tool) > 1 else None
    if shutil.disk_usage(stage).free < 4 * 1024**3:
        report["issues"].append("At least 4 GiB free staging space is required")
    if target == "macos-arm64":
        from scripts.macos_profile import discover, build_environment
        report["toolchain"] = discover()
        report["issues"].extend(report["toolchain"]["issues"])
        if report["toolchain"]["qualified"]:
            env.update(build_environment(report["toolchain"]))
        if not os.environ.get("OANDAFLY_BASELINE_CONFIG"):
            report["issues"].append("macOS 26.0 interactive baseline runner is not configured (OANDAFLY_BASELINE_CONFIG)")
    containment = capability()
    if not containment.implemented or not containment.prerequisite_available:
        report["issues"].append(containment.reason)
    # Diagnose signed credentials only when explicitly building that profile.
    # Do this even if a common prerequisite failed, retaining both sets of evidence.
    if profile == "production_signed" and target == "macos-arm64":
        from scripts.macos_signing import preflight as signing_preflight
        signing_env = {**env, **{key: os.environ[key] for key in ("MACOS_SIGN_IDENTITY", "MACOS_NOTARY_PROFILE") if key in os.environ}}
        try:
            report["signer"] = signing_preflight(signing_env, stage / "logs")
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            report["issues"].append(str(exc))
    (stage / "preflight.json").write_text(json.dumps(report, indent=2) + "\n")
    if report["issues"]:
        raise ValueError("Production preflight blocked: " + "; ".join(report["issues"]))
    return report, env


def snapshot(destination: Path) -> dict:
    result = audit(ROOT, require_locks=True, check_manifest=True)
    if not result["source_check_passed"]:
        raise ValueError("Source/manifest/lock checks failed: " + "; ".join(result["issues"]))
    expected = create_manifest(ROOT)
    for source in [*source_files(ROOT), ROOT / "PACKAGE_MANIFEST.json"]:
        path = destination / source.relative_to(ROOT)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, path)
    if create_manifest(destination) != expected or create_manifest(ROOT) != expected:
        raise ValueError("Source changed while capturing the tested snapshot")
    return expected


def build(target: str, *, profile: str = "production_unsigned") -> dict:
    if native_target().key != target:
        raise ValueError("Cross-compilation is unsupported; run this build on its native target")
    receipt = policy(profile, target)
    identity()
    with build_lock():
        run_id = uuid4().hex
        stage = ROOT / "build/desktop" / target / run_id
        for parent in stage.parents:
            if parent == ROOT:
                break
            if parent.is_symlink():
                raise ValueError("Build staging must not traverse directory links")
        logs = stage / "logs"
        logs.mkdir(parents=True)
        receipt.update(format="oandafly.production-build.1", version="1.0.0", run_id=run_id,
                       target=target, passed=False, gates={key: "pending" for key in COMMON_GATES})
        active = "preflight"
        def gate(name: str) -> None:
            nonlocal active
            receipt["gates"][active] = "passed"
            active = name
        try:
            observations, env = preflight(target, profile, stage)
            receipt["preflight"] = observations
            gate("source_snapshot")
            source = stage / "source"
            manifest = snapshot(source)
            binding = {"run_id": run_id, "profile": profile, "target": target,
                       "source_sha256": document_digest(manifest),
                       "lock_hashes": {name: digest(source / name) for name in RELEASE_FILES},
                       "contract_sha256": digest(source / "packaging/release-tests.json"),
                       "host": platform.platform(), "python": platform.python_version()}
            receipt["binding"] = binding
            gate("locked_environment")
            uv = shutil.which("uv")
            run_logged([sys.executable, "-I", str(source / "scripts/lock_dependencies.py"), "--check"], logs / "locks.txt", env=env)
            isolated = stage / "venv"
            run_logged([uv, "venv", "--python", sys.executable, "--no-python-downloads", str(isolated)], logs / "venv.txt", env=env)
            python = isolated / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            run_logged([uv, "pip", "install", "--python", str(python), "--require-hashes", "--only-binary", ":all:",
                        "-r", str(source / "requirements-dev.lock"), "-r", str(source / "requirements-build.lock")],
                       logs / "dependencies.txt", env=env)
            run_logged([uv, "pip", "install", "--python", str(python), "--no-deps", "--no-build-isolation", str(source)],
                       logs / "install.txt", env=env)
            binding["interpreter_sha256"] = digest(python.resolve())
            binding["interpreter_path"] = str(python)
            (logs / "binding.json").write_text(json.dumps(binding, indent=2) + "\n")
            def execute(command, name, timeout=900):
                run_logged([str(python), "-I", *command], logs / name, env=env, timeout=timeout, cwd=source)
            gate("quality")
            run_logged([uv, "pip", "check", "--python", str(python)], logs / "dependency-consistency.txt", env=env)
            if os.name != "nt":
                run_logged(["/bin/bash", "-n", str(source / "dev.sh")], logs / "bash.txt", env=env)
            execute(["-m", "ruff", "check", "src", "tests", "scripts"], "ruff.txt")
            execute(["-m", "mypy", "--follow-imports=silent", "src/oandafly/macos.py",
                     "src/oandafly/neural/containment.py", "scripts/release_policy.py"], "types.txt")
            execute([str(source / "scripts/project_integrity.py"), "--verify-manifest", "--require-locks"], "integrity.txt")
            gate("complete_tests")
            evidence_path = logs / "complete-tests.json"
            env.update(OANDAFLY_TEST_CONTRACT=str(source / "packaging/release-tests.json"),
                       OANDAFLY_TEST_BINDING=str(logs / "binding.json"), OANDAFLY_TEST_EVIDENCE=str(evidence_path))
            execute([str(source / "scripts/run_release_tests.py")], "tests.txt")
            receipt["test_evidence"] = validate_test_evidence(evidence_path, binding)
            for key in ("OANDAFLY_TEST_CONTRACT", "OANDAFLY_TEST_BINDING", "OANDAFLY_TEST_EVIDENCE"):
                env.pop(key)
            gate("dependency_audit")
            for lock in ("requirements-dev.lock", "requirements-build.lock"):
                execute(["-m", "pip_audit", "--require-hashes", "--disable-pip", "--strict", "--progress-spinner", "off",
                         "-r", str(source / lock), "--format", "json", "--output", str(logs / (lock + ".audit.json"))],
                        lock + ".audit.txt")
                result = json.loads((logs / (lock + ".audit.json")).read_text())
                validate_audit(source / lock, result)
            gate("native_containment")
            from oandafly.neural.containment import require_capability
            receipt["containment"] = require_capability()
            gate("assembly")
            execute(["-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(stage / "bundle"),
                     "--workpath", str(stage / "work"), str(source / "packaging/oandafly.spec")], "assembly.txt")
            bundle = stage / "bundle" / ("Oandafly.app" if target == "macos-arm64" else "oandafly")
            if target == "linux-amd64":
                shutil.copy2(source / "packaging/linux/install.sh", bundle / "install.sh")
                (bundle / "install.sh").chmod(0o755)
            elif target == "windows-amd64":
                shutil.copy2(source / "packaging/windows/install-shortcut.ps1", bundle / "install-shortcut.ps1")
            gate("native_inventory")
            receipt["bundle"] = inspect_bundle(bundle, target, source)
            inventory = receipt["bundle"]
            if target == "macos-arm64":
                from scripts.macho_audit import audit as audit_macho
                inventory = audit_macho(bundle)
                from scripts.macos_signing import sign_and_notarize, verify_signature
                if profile == "production_signed":
                    signing_env = {**env, **{key: os.environ[key] for key in ("MACOS_SIGN_IDENTITY", "MACOS_NOTARY_PROFILE")}}
                    receipt["notarization"] = sign_and_notarize(bundle, inventory, signing_env, logs, observations["signer"]["team"])
                    receipt["trust_gates"] = {key: "passed" for key in receipt["trust_gates"]}
                receipt["signature"] = verify_signature(bundle, profile == "production_signed", env, logs,
                                                        team=observations.get("signer", {}).get("team"))
            elif profile == "production_signed" and target == "windows-amd64":
                windows_sign(bundle, logs, {**env, **{k: os.environ[k] for k in
                    ("WIN_SIGN_CERT_SHA1", "WIN_TIMESTAMP_URL", "SIGNTOOL_EXE") if k in os.environ}})
            gate("final_payload")
            receipt["gui"] = verify(bundle, logs=logs / "final-gui")
            if not receipt["gui"]["passed"]:
                raise ValueError("Final frozen GUI acceptance failed")
            _, cli, _ = bundle_paths(bundle, target)
            run_logged([str(cli), "doctor", "--desktop"], logs / "frozen-doctor.json", env=env, cwd=logs)
            doctor = json.loads((logs / "frozen-doctor.json").read_text())
            if not doctor.get("desktop", {}).get("prerequisites_satisfied"):
                raise ValueError("Frozen runtime prerequisites failed")
            run_logged([str(cli), "--verify-runtime"], logs / "embedded-runtime.json", env=env, cwd=logs)
            embedded = json.loads((logs / "embedded-runtime.json").read_text())
            if embedded.get("passed") is not True or embedded.get("frozen") is not True:
                raise ValueError("Embedded-runtime verification cannot be replaced with source evidence")
            receipt["embedded_runtime"] = embedded
            payload = payload_identity(bundle)
            binding["payload_sha256"] = payload["sha256"]
            gate("archive_extraction")
            prepared = stage / "publication"
            prepared.mkdir()
            filename = f"oandafly-1.0.0-{target}-{profile.replace('_', '-')}-{run_id}{TARGETS[target].archive_suffix}"
            archive = prepared / filename
            package_native(bundle, archive, target, env)
            extracted = extract(archive, stage / "extracted", target, env)
            if payload_identity(extracted) != payload:
                raise ValueError("Final archive extraction changed application bytes, links or permissions")
            inspect_bundle(extracted, target, source)
            _, extracted_cli, _ = bundle_paths(extracted, target)
            run_logged([str(extracted_cli), "--verify-runtime"], logs / "extracted-runtime.json", env=env, cwd=logs)
            extracted_runtime = json.loads((logs / "extracted-runtime.json").read_text())
            if extracted_runtime.get("passed") is not True or extracted_runtime.get("frozen") is not True:
                raise ValueError("Extracted embedded-runtime verification failed")
            if target == "macos-arm64":
                audit_macho(extracted)
                verify_signature(extracted, profile == "production_signed", env, logs, team=observations.get("signer", {}).get("team"))
                if profile == "production_signed":
                    from scripts.macos_signing import distribution_trust
                    distribution_trust(extracted, env, logs)
            receipt["extracted_gui"] = verify(extracted, logs=logs / "extracted-gui")
            if not receipt["extracted_gui"]["passed"] or payload_identity(extracted) != payload:
                raise ValueError("Extracted final application acceptance failed or modified the sealed bundle")
            gate("baseline_acceptance")
            binding["archive_sha256"] = digest(archive)
            if target == "macos-arm64":
                from scripts.baseline_acceptance import handoff
                configured = os.environ.get("OANDAFLY_BASELINE_CONFIG")
                receipt["baseline"] = handoff(Path(configured) if configured else None, archive, binding, logs, env)
            else:
                receipt["baseline"] = {"status": "not_applicable_platform", "native_host_tested": platform.platform()}
            if create_manifest(ROOT) != manifest or create_manifest(source) != manifest:
                raise ValueError("Reviewed or tested source changed during qualification")
            gate("publication")
            final = ROOT / "dist" / target / profile.replace("_", "-") / run_id
            receipt.update(passed=True, production_quality_ready=True,
                           apple_distribution_ready=profile == "production_signed" and target == "macos-arm64",
                           archive_name=filename, archive=str(final / filename), sha256=digest(archive))
            receipt["gates"]["publication"] = "passed"
            (prepared / "build.json").write_text(json.dumps(receipt, indent=2) + "\n")
            (prepared / "SHA256SUMS").write_text(receipt["sha256"] + "  " + filename + "\n")
            (prepared / "provenance.json").write_text(json.dumps({"binding": binding, "payload": payload, "environment": observations}, indent=2) + "\n")
            (prepared / "native-inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
            shutil.copy2(source / "NOTICE.md", prepared / "NOTICE.md")
            shutil.copytree(logs, prepared / "evidence")
            publish(prepared, final)
            return receipt
        except BaseException as exc:
            receipt.update(passed=False, production_quality_ready=False, apple_distribution_ready=False,
                           error_type=type(exc).__name__, message=str(exc), failed_stage=active)
            receipt["gates"][active] = "failed"
            (stage / "failed.json").write_text(json.dumps(receipt, indent=2) + "\n")
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=PROFILES)
    args = parser.parse_args()
    try:
        result = build(native_target().key, profile=args.profile)
        print(json.dumps(result, indent=2))
        return 0
    except KeyboardInterrupt:
        print("Production build cancelled; prior releases preserved", file=sys.stderr)
        return 130
    except (OSError, ValueError, subprocess.SubprocessError, importlib.metadata.PackageNotFoundError) as exc:
        print(json.dumps({"passed": False, "error_type": type(exc).__name__, "message": str(exc),
                          "prior_releases_preserved": True}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
