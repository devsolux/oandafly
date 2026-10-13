"""Authenticated handoff to an operator-configured macOS 26.0 interactive runner.

The transport executable is trusted release infrastructure, not app configuration.
No infrastructure is created here and a missing runner is never a passing gate.
"""
from __future__ import annotations

import json
import secrets
import subprocess
import time
from pathlib import Path

from oandafly.macos import version
from scripts.release_policy import document_digest

REQUIRED_CHECKS = ("frozen_gui", "ordinary_startup", "readonly_installation", "sqlite_wal_recovery",
                   "native_dependencies", "contained_inference_denials", "worker_lifecycle")


def validate(request: dict, envelope: dict, public_key: str) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    result = envelope["result"]
    raw = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)).verify(bytes.fromhex(envelope["signature"]), raw)
    except (ValueError, InvalidSignature) as exc:
        raise ValueError("Baseline evidence signature does not match the configured runner") from exc
    if result.get("request_sha256") != document_digest(request):
        raise ValueError("Baseline evidence belongs to another source/profile/payload/request")
    if (time.time() - request["created_at"] > 7200 or result.get("completed_at", 0) < request["created_at"]
            or result["completed_at"] > time.time() + 30):
        raise ValueError("Stale or future baseline acceptance")
    if (version(result.get("runtime_os", "")) != (26, 0, 0) or result.get("architecture") != "arm64"
            or result.get("interactive_session") is not True or not result.get("host_build")
            or not result.get("hardware") or type(result.get("virtualized")) is not bool):
        raise ValueError("Baseline acceptance requires an observed macOS 26.0 ARM64 interactive session")
    if result.get("payload_sha256") != request["payload_sha256"]:
        raise ValueError("Baseline tested a different application payload")
    required = (*REQUIRED_CHECKS, *(('gatekeeper', 'stapling', 'quarantine_launch')
                                   if request["profile"] == "production_signed" else ()))
    if any(result.get("checks", {}).get(key) != "passed" for key in required):
        raise ValueError("Incomplete baseline runtime acceptance")
    return result


def handoff(config_path: Path | None, archive: Path, binding: dict, logs: Path, env: dict) -> dict:
    if config_path is None:
        raise ValueError("macOS 26.0 baseline runner missing; configure OANDAFLY_BASELINE_CONFIG before release")
    if not config_path.is_absolute() or config_path.is_symlink():
        raise ValueError("Baseline configuration must be an absolute regular reviewed file")
    config = json.loads(config_path.read_text())
    executable = Path(config["executable"])
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError("Baseline transport executable must be an existing absolute path")
    if len(bytes.fromhex(config["ed25519_public_key"])) != 32:
        raise ValueError("Invalid baseline runner public key")
    request = {**binding, "format": "oandafly.baseline-request.1", "nonce": secrets.token_hex(32),
               "created_at": time.time(), "archive": str(archive), "required_checks": list(REQUIRED_CHECKS)}
    request_path, output = logs / "baseline-request.json", logs / "baseline-result.json"
    request_path.write_text(json.dumps(request, indent=2) + "\n")
    subprocess.run([str(executable), str(request_path), str(output)], env=env, check=True, timeout=3600)
    if not output.is_file() or output.stat().st_size > 4 * 1024**2:
        raise ValueError("Missing or oversized baseline evidence")
    return validate(request, json.loads(output.read_text()), config["ed25519_public_key"])
