#!/usr/bin/env python3
"""Audit the hashed runtime lock in an isolated, versioned pip-audit environment.

The report records actual tool output and its input hashes. Advisory matches are
not a security certificate, exploitability analysis or verification of wheel bytes.
No project dependency is installed, upgraded, fixed or ignored by this script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

AUDITOR_VERSION = "2.10.1"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    # Keep this source-checkout tool usable before an editable project install.
    # The shared path resolver has no third-party imports on POSIX.
    sys.path.insert(0, str(root / "src"))
    try:
        from oandafly.paths import runtime_environment, secure_directory, state_directory
    finally:
        sys.path.pop(0)
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required to run pip-audit outside the project environment")
    requirement_path = root / "requirements.lock"
    if not requirement_path.is_file() or not (root / "uv.lock").is_file():
        raise SystemExit("Generate fresh uv.lock and hashed requirements.lock from pyproject.toml before auditing")
    requirements = requirement_path.read_bytes()
    requested = sorted(
        set(re.findall(r"^([A-Za-z0-9_.-]+)==([^ ;\\\r\n]+)", requirements.decode("utf-8"), re.MULTILINE))
    )
    if not requested:
        raise SystemExit("The hashed runtime lock contains no exact package pins")
    storage = state_directory()
    raw_report_path = secure_directory(storage / "logs" / "dependency-audit") / f"{uuid4()}.json"
    command = [
        uv,
        "tool",
        "run",
        "--isolated",
        "--default-index",
        "https://pypi.org/simple",
        "--no-env-file",
        "--no-build",
        "--from",
        f"pip-audit=={AUDITOR_VERSION}",
        "pip-audit",
        "--require-hashes",
        "--disable-pip",
        "--strict",
        "--progress-spinner",
        "off",
        "--vulnerability-service",
        "pypi",
        "--cache-dir",
        str(secure_directory(storage / "cache" / "pip-audit")),
        "--format",
        "json",
        "--output",
        str(raw_report_path),
        "--requirement",
        str(requirement_path),
    ]
    environment = os.environ.copy()
    environment.update(runtime_environment(storage))
    environment.update({"UV_CACHE_DIR": str(secure_directory(storage / "cache" / "uv")),
                        "UV_TOOL_DIR": str(secure_directory(storage / "cache" / "audit-tools"))})
    started = datetime.now(UTC)
    completed = subprocess.run(
        command, cwd=root, env=environment, capture_output=True, text=True, timeout=180, check=False
    )
    finished = datetime.now(UTC)
    result: dict[str, Any] = json.loads(raw_report_path.read_text()) if raw_report_path.is_file() else {}
    dependencies: list[dict[str, Any]] = result.get("dependencies", [])
    skipped = [dependency for dependency in dependencies if "skip_reason" in dependency]
    checked = [dependency for dependency in dependencies if "skip_reason" not in dependency]
    findings = [vulnerability for dependency in checked for vulnerability in dependency.get("vulns", [])]
    unchanged = requirement_path.read_bytes() == requirements
    known_result = completed.returncode in {0, 1} and len(dependencies) > 0 and unchanged
    report = {
        "schema_version": 1,
        "application": {"name": "oandafly", "version": "1.0.0", "identifier": "com.devsolux.oandafly"},
        "started_at_utc": started.isoformat(),
        "finished_at_utc": finished.isoformat(),
        "tool": {
            "name": "pip-audit",
            "version": AUDITOR_VERSION,
            "isolation": "uv tool run --isolated",
            "service": "pypi",
        },
        "input": {
            "filename": requirement_path.name,
            "sha256": _sha256(requirements),
            "uv_lock_sha256": _sha256((root / "uv.lock").read_bytes()),
            "unchanged_during_audit": unchanged,
            "exact_pins": [{"name": name, "version": version} for name, version in requested],
        },
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "machine": platform.machine(),
        },
        "scope": f"{requirement_path.name} on the current host; marker evaluation follows the isolated auditor interpreter",
        "status": "KNOWN_ADVISORY_QUERY_COMPLETED" if known_result else "AUDIT_INCOMPLETE",
        "exit_code": completed.returncode,
        "counts": {
            "requested_pins_before_marker_evaluation": len(requested),
            "audited_packages": len(checked),
            "skipped_packages": len(skipped),
            "vulnerability_entries": len(findings),
            "affected_packages": sum(bool(dependency.get("vulns")) for dependency in checked),
        },
        "advisory_ids": sorted({str(finding["id"]) for finding in findings}),
        "auditor_stdout": completed.stdout.strip(),
        "auditor_stderr": completed.stderr.strip(),
        "auditor_result": result,
        "reproduction": "python scripts/audit_dependencies.py",
        "limitations": [
            "This reports known PyPI advisory records available to the auditor at the recorded time; the task-local HTTP cache follows the service cache policy. Later or undisclosed vulnerabilities are outside coverage.",
            "No project dependency was fixed or upgraded; the auditor and its dependencies run in a separate uv tool environment.",
            "--disable-pip parses the already hashed lock without installing target packages; this is not a fresh download or artifact-byte verification.",
            "Development-only dependencies, isolated build tools, operating system, CPython, SQLite, Tcl/Tk and native libraries are outside this runtime-lock advisory query.",
            "The audit does not establish exploitability, malicious-package absence, software licenses, trading safety, supported platform qualification or a security certification.",
        ],
    }
    destination = raw_report_path.with_name(raw_report_path.stem + "-summary.json")
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "counts": report["counts"],
                "exit_code": completed.returncode,
                "output": str(destination),
            }
        )
    )
    if not known_result:
        raise SystemExit(2)
    if findings:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
