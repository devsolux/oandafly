"""Shared production gate inventory and isolated child environment."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

PROFILES = ("production_unsigned", "production_signed")
COMMON_GATES = ("preflight", "source_snapshot", "locked_environment", "quality",
                "complete_tests", "dependency_audit", "native_containment", "assembly",
                "native_inventory", "final_payload", "archive_extraction", "baseline_acceptance",
                "publication")
TRUST_GATES = ("signing_preflight", "developer_id", "notarization", "stapling", "gatekeeper")


def policy(profile: str, target: str) -> dict:
    if profile not in PROFILES:
        raise ValueError("Unknown production distribution profile")
    return {"profile": profile, "common_gates": list(COMMON_GATES),
            "trust_gates": {name: ("pending" if profile == "production_signed" and target == "macos-arm64"
                                   else "not_applicable_unsigned_profile" if profile == "production_unsigned"
                                   else "not_applicable_platform") for name in TRUST_GATES},
            "production_quality_ready": False, "apple_distribution_ready": False,
            "native_trading_qualified": False}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def document_digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def environment(temporary: Path, target: str) -> dict[str, str]:
    forbidden = [key for key in os.environ if key.startswith("PYTEST_") and os.environ[key]]
    if forbidden:
        raise ValueError("Release tests reject inherited pytest configuration: " + ", ".join(sorted(forbidden)))
    # Positive allowlist; signing/broker credentials never reach tests or the freezer.
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "DISPLAY", "WAYLAND_DISPLAY",
               "XAUTHORITY", "XDG_RUNTIME_DIR", "LANG", "LC_ALL"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    # The documented private Linux SQLite (scripts/build_sqlite.py, docs/BOOTSTRAP.md) is
    # selected with LD_LIBRARY_PATH; forward only absolute existing directories.
    library_path = os.environ.get("LD_LIBRARY_PATH")
    if target == "linux-amd64" and library_path:
        if not all(os.path.isabs(entry) and ".." not in Path(entry).parts and os.path.isdir(entry)
                   for entry in library_path.split(os.pathsep)):
            raise ValueError("LD_LIBRARY_PATH may only list absolute existing directories")
        env["LD_LIBRARY_PATH"] = library_path
    for key in ("HOME", "USERPROFILE", "TMPDIR", "TMP", "TEMP", "APPDATA", "LOCALAPPDATA",
                "XDG_CACHE_HOME", "UV_CACHE_DIR", "PIP_CACHE_DIR", "RUFF_CACHE_DIR", "PYINSTALLER_CONFIG_DIR"):
        path = temporary / key.lower()
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        env[key] = str(path)
    env.update(OANDAFLY_BUILD_TARGET=target, OANDAFLY_GUI_TEST_REQUIRED="1",
               PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONNOUSERSITE="1", UV_NO_CONFIG="1",
               UV_PYTHON_DOWNLOADS="never")
    return env


def validate_test_evidence(path: Path, binding: dict) -> dict:
    evidence = json.loads(path.read_text())
    if evidence.get("format") != "oandafly.release-test-evidence.1" or evidence.get("binding") != binding:
        raise ValueError("Stale or mismatched release test evidence")
    collected, selected, excluded = evidence["collected"], evidence["selected"], evidence["excluded"]
    if (not selected or len(collected) != len(set(collected)) or len(selected) != len(set(selected))
            or set(selected) & set(excluded) or set(collected) != set(selected) | set(excluded)
            or set(evidence["results"]) != set(selected) or evidence.get("passed") is not True
            or evidence.get("exitstatus") != 0 or evidence.get("issues")
            or any(result != {"setup": "passed", "call": "passed", "teardown": "passed"}
                   for result in evidence["results"].values())):
        raise ValueError("Incomplete release test collection/outcomes")
    if any(not reasons or any(reason not in {"linux_containment", "posix_only"} for reason in reasons)
           for reasons in excluded.values()):
        raise ValueError("Unreviewed platform exclusion")
    return evidence


def validate_audit(lock: Path, result: dict) -> dict:
    """Require exact coverage of every pin applicable to this audit interpreter."""
    import re
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    expected = set()
    for line in lock.read_text().replace("\\\n", " ").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        requirement = Requirement(re.sub(r"\s+--hash=sha256:[a-f0-9]{64}", "", line).strip())
        if requirement.marker is None or requirement.marker.evaluate():
            expected.add((canonicalize_name(requirement.name), str(next(iter(requirement.specifier))).removeprefix("==")))
    dependencies = result.get("dependencies", [])
    actual = [(canonicalize_name(d["name"]), d.get("version")) for d in dependencies]
    if (not expected or set(actual) != expected or len(set(actual)) != len(actual)
            or any(d.get("skip_reason") or d.get("vulns") for d in dependencies)):
        raise ValueError("Incomplete dependency audit coverage or unresolved advisories")
    return {"passed": True, "packages": len(expected), "lock_sha256": digest(lock)}
