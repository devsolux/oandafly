"""Explicit Developer ID branch. Never imported/executed as an unsigned fallback."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


def run(command: list[str], log: Path, env: dict, timeout: int = 60) -> str:
    try:
        result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                                text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        log.write_text(json.dumps({"timeout": True, "stdout": str(exc.stdout or ""),
                                   "stderr": str(exc.stderr or "")}, indent=2))
        raise ValueError("Apple operation timed out; retain diagnostics and any known submission ID: " + str(log)) from exc
    log.write_text(json.dumps({"returncode": result.returncode, "stdout": result.stdout,
                              "stderr": result.stderr}, indent=2) + "\n")
    if result.returncode:
        raise ValueError("Apple operation failed; see " + str(log))
    return result.stdout


def configuration(env: dict) -> tuple[str, str]:
    # No defaults, stripping, certificate auto-selection, or shell interpretation.
    signer, profile = env.get("MACOS_SIGN_IDENTITY", ""), env.get("MACOS_NOTARY_PROFILE", "")
    if (not signer or signer == "-" or signer.startswith("-") or not profile or profile.startswith("-")
            or any(ord(c) < 32 for c in signer + profile) or not signer.strip() or not profile.strip()):
        raise ValueError("Signed preflight: invalid exported MACOS_SIGN_IDENTITY / MACOS_NOTARY_PROFILE")
    return signer, profile


def preflight(env: dict, logs: Path) -> dict:
    signer, profile = configuration(env)
    listing = run(["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"], logs / "identity-probe.json", env)
    matches = [(fingerprint, label, team) for fingerprint, label, team in
               re.findall(r'\b([0-9A-Fa-f]{40}) "(Developer ID Application: [^\n"]+ \(([A-Z0-9]+)\))"', listing)
               if fingerprint.lower() == signer.lower() or label == signer]
    if len(matches) != 1:
        raise ValueError("Signed preflight: configured Developer ID identity and private key are unavailable or ambiguous")
    # A real signature proves private-key access, not only certificate presence.
    probe = logs / "signing-key-probe"
    shutil.copy2("/usr/bin/true", probe)
    run(["/usr/bin/codesign", "--force", "--options", "runtime", "--timestamp", "--sign", signer, str(probe)],
        logs / "key-access.json", env)
    run(["/usr/bin/xcrun", "notarytool", "history", "--keychain-profile", profile, "--output-format", "json"],
        logs / "notary-profile-probe.json", env)
    probe.unlink()
    return {"fingerprint": matches[0][0], "team": matches[0][2], "private_key_usable": True, "profile_usable": True}


def verify_signature(bundle: Path, signed: bool, env: dict, logs: Path, *, team: str | None = None) -> dict:
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", str(bundle)],
        logs / "signature-verification.json", env)
    # codesign emits display metadata on stderr even when successful.
    run(["/usr/bin/codesign", "-d", "--verbose=4", str(bundle)], logs / "signature-state.json", env)
    state = json.loads((logs / "signature-state.json").read_text())["stderr"]
    if signed:
        if ("Authority=Developer ID Application:" not in state or not team
                or f"TeamIdentifier={team}" not in state or "runtime" not in state):
            raise ValueError("Final payload lacks the expected Developer ID team/hardened runtime")
    elif "Signature=adhoc" not in state or "Authority=Developer ID" in state:
        raise ValueError("Unsigned ARM64 payload must have only local ad-hoc sealing")
    return {"state": "developer_id" if signed else "ad_hoc", "verified": True}


def sign_and_notarize(bundle: Path, inventory: dict, env: dict, logs: Path, team: str) -> dict:
    signer, profile = configuration(env)
    images = [bundle / path for path in inventory["images"]]
    containers = [p for p in bundle.rglob("*") if p.suffix in {".app", ".framework", ".xpc"} and p.is_dir() and not p.is_symlink()]
    # No blanket entitlements. Each future sandbox helper needs its own reviewed policy.
    for i, path in enumerate(sorted(set(images + containers), key=lambda p: len(p.parts), reverse=True) + [bundle]):
        run(["/usr/bin/codesign", "--force", "--options", "runtime", "--timestamp", "--sign", signer, str(path)],
            logs / f"sign-{i:04d}.json", env, timeout=180)
    verify_signature(bundle, True, env, logs, team=team)
    upload = logs / "notary-submission.zip"
    run(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(bundle), str(upload)], logs / "submission-archive.json", env, 300)
    # Submit once without waiting so the identifier is persisted before the long wait.
    submission = json.loads(run(["/usr/bin/xcrun", "notarytool", "submit", str(upload),
                                 "--keychain-profile", profile, "--output-format", "json"],
                                logs / "notary-submit.json", env, 300))
    submission_id = submission.get("id")
    if not isinstance(submission_id, str) or not re.fullmatch(r"[A-Fa-f0-9-]{36}", submission_id):
        raise ValueError("Notarization did not return a submission identifier; inspect before retrying")
    (logs / "notary-submission-id.txt").write_text(submission_id + "\n")
    status = json.loads(run(["/usr/bin/xcrun", "notarytool", "wait", submission_id, "--keychain-profile", profile,
                             "--timeout", "20m", "--output-format", "json"], logs / "notary-wait.json", env, 1250))
    run(["/usr/bin/xcrun", "notarytool", "log", submission_id, "--keychain-profile", profile,
         str(logs / "notary-diagnostic.json")], logs / "notary-log.json", env)
    if status.get("status") != "Accepted":
        raise ValueError("Notarization not Accepted; submission " + submission_id)
    run(["/usr/bin/xcrun", "stapler", "staple", str(bundle)], logs / "staple.json", env, 180)
    distribution_trust(bundle, env, logs)
    return {"submission_id": submission_id, "status": "Accepted"}


def distribution_trust(bundle: Path, env: dict, logs: Path) -> None:
    run(["/usr/bin/xcrun", "stapler", "validate", str(bundle)], logs / "staple-validation.json", env)
    run(["/usr/sbin/spctl", "--assess", "--type", "execute", "--verbose=2", str(bundle)],
        logs / "gatekeeper.json", env)
