#!/usr/bin/env python3
"""Launch the actual desktop and require native visibility, heartbeat and clean exit.

No worker/configuration/vault is invoked. A source smoke is explicitly labelled and
cannot create a frozen-build qualification. This is not an OS malware sandbox.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.native_window import visible_window

ROOT = Path(__file__).resolve().parents[1]


def executable_path(path: Path) -> Path:
    path = path.absolute()
    if path.suffix == ".app":
        return path / "Contents/MacOS/oandafly"
    if path.is_dir():
        return path / ("oandafly.exe" if os.name == "nt" else "oandafly")
    return path


def clean_environment(temporary: Path) -> dict[str, str]:
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "DISPLAY", "WAYLAND_DISPLAY",
               "XAUTHORITY", "XDG_RUNTIME_DIR", "LANG", "LC_ALL", "DBUS_SESSION_BUS_ADDRESS"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    for key in ("TMPDIR", "TMP", "TEMP", "HOME", "USERPROFILE", "XDG_CACHE_HOME", "APPDATA", "LOCALAPPDATA"):
        directory = temporary / key.lower()
        directory.mkdir(mode=0o700)
        env[key] = str(directory)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def verify(path: Path | None, *, logs: Path, source: bool = False, startup_seconds: float = 10) -> dict:
    if not 1 <= startup_seconds <= 30:
        raise ValueError("Startup budget must be in [1,30] seconds")
    if source and path is not None:
        raise ValueError("Choose either --source or a compiled artifact")
    logs = logs.absolute()
    logs.mkdir(parents=True, exist_ok=True)
    report = {"format": "oandafly.desktop-verification.1", "passed": False,
              "frozen_required": not source, "source_smoke": source, "native_trading_qualified": False}
    process = None
    heartbeat = {}
    # Resolve paths first; launching from a fresh cwd detects accidental source imports.
    if source:
        command = [sys.executable, "-I", "-B", str(ROOT / "packaging/entrypoint.py")]
        # Explicit source-only entrypoint switch uses this checkout. A compiled
        # app never relies on it, and the ordinary verification path rejects it.
        command += ["--source-checkout", str(ROOT)]
    else:
        if path is None:
            raise ValueError("Supply a compiled .app / executable / onedir directory")
        executable = executable_path(path)
        if not executable.is_file():
            raise ValueError("Compiled executable is absent")
        command = [str(executable)]
    with tempfile.TemporaryDirectory(prefix="oandafly-gui-check-") as name:
        temp = Path(name)
        nonce = secrets.token_hex(32)
        request = temp / "request.json"
        request.write_text(json.dumps({"format": "oandafly.gui-probe.1", "nonce": nonce, "require_frozen": not source}), encoding="utf-8")
        os.chmod(request, 0o600)
        command += ["--verify-build", "--request", str(request)]
        try:
            with (logs / "stdout.txt").open("w", encoding="utf-8") as stdout, (logs / "stderr.txt").open("w", encoding="utf-8") as stderr:
                started = time.monotonic()
                process = subprocess.Popen(command, cwd=temp, env=clean_environment(temp), stdin=subprocess.DEVNULL,
                                           stdout=stdout, stderr=stderr, close_fds=True)
                observed = None
                ticks_at_ack = None
                while time.monotonic()-started < startup_seconds+8:
                    output = temp / "heartbeat.json"
                    if output.is_file():
                        try:
                            heartbeat = json.loads(output.read_text(encoding="utf-8"))
                        except (OSError, ValueError):
                            continue
                        if (heartbeat.get("nonce") != nonce or heartbeat.get("pid") != process.pid
                                or heartbeat.get("main_thread") is not True or (not source and heartbeat.get("frozen") is not True)):
                            raise ValueError("GUI heartbeat identity/runtime mismatch")
                        if heartbeat.get("error"):
                            raise ValueError("GUI callback/watchdog failure: " + str(heartbeat["error"]))
                        if observed is None and heartbeat.get("mapped"):
                            observation = visible_window(process.pid, int(heartbeat["window_id"]))
                            if observation["visible"]:
                                observed = observation
                                ticks_at_ack = int(heartbeat["ticks"])
                                report["startup_seconds"] = time.monotonic()-started
                                if report["startup_seconds"] > startup_seconds:
                                    raise ValueError("Native window missed its startup deadline")
                                (temp / "ack.json").write_text(json.dumps({"nonce": nonce, "pid": process.pid, "window_observed": True}), encoding="utf-8")
                    if process.poll() is not None:
                        break
                    if observed is None and time.monotonic()-started > startup_seconds:
                        raise ValueError("No native visible desktop window within the startup budget")
                    time.sleep(.1)
                if process.poll() is None:
                    raise ValueError("Desktop did not finish its bounded shutdown")
                # The process may publish its final record between our last
                # heartbeat read and poll(). Read it again after confirmed exit.
                output = temp / "heartbeat.json"
                if output.is_file():
                    heartbeat = json.loads(output.read_text(encoding="utf-8"))
                if (heartbeat.get("nonce") != nonce or heartbeat.get("pid") != process.pid
                        or heartbeat.get("main_thread") is not True
                        or (not source and heartbeat.get("frozen") is not True)):
                    raise ValueError("Final GUI heartbeat identity/runtime mismatch")
                report.update(exit_code=process.returncode, window=observed, heartbeat=heartbeat)
                if (process.returncode != 0 or observed is None or heartbeat.get("completed") is not True
                        or heartbeat.get("ticks", 0) < (ticks_at_ack or 0)+10
                        or heartbeat.get("held_after_ack_seconds", 0) < 3):
                    raise ValueError("Desktop exited without completing the responsive visible-window hold")
                report["passed"] = True
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            report["heartbeat"] = heartbeat
            (logs / "verification.json").write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path, nargs="?")
    parser.add_argument("--source", action="store_true", help="Source smoke only; never native-build qualification")
    parser.add_argument("--logs", type=Path, default=ROOT / "build/gui-check")
    parser.add_argument("--startup-seconds", type=float, default=10)
    args = parser.parse_args()
    try:
        report = (verify(args.artifact, logs=args.logs, source=True, startup_seconds=args.startup_seconds)
                  if args.source else verify_release(args.artifact, logs=args.logs))
        print(json.dumps(report, indent=2))
        return 0 if report["passed"] else 1
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"passed": False, "error": str(exc)}), file=sys.stderr)
        return 1


def verify_release(path: Path | None, *, logs: Path) -> dict:
    """Verify published output against its profile, not just an arbitrary GUI."""
    from scripts.release_artifacts import extract, payload_identity
    from scripts.release_policy import COMMON_GATES, PROFILES, digest
    from scripts.build_contracts import bundle_paths
    if path is None:
        raise ValueError("Supply a profile release directory or its native archive")
    directory = path if path.is_dir() else path.parent
    receipt = json.loads((directory / "build.json").read_text())
    selected = receipt.get("profile")
    if (selected not in PROFILES or receipt.get("passed") is not True or not receipt.get("production_quality_ready")
            or any(receipt.get("gates", {}).get(key) != "passed" for key in COMMON_GATES)):
        raise ValueError("Incomplete production profile/gate receipt")
    filename = receipt["archive_name"]
    if Path(filename).name != filename:
        raise ValueError("Invalid archive name in receipt")
    archive = directory / filename
    if not path.is_dir() and archive.absolute() != path.absolute():
        raise ValueError("Supplied archive differs from receipt")
    if digest(archive) != receipt["sha256"]:
        raise ValueError("Published archive SHA-256 mismatch")
    provenance = json.loads((directory / "provenance.json").read_text())
    logs.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="oandafly-release-verify-") as name:
        temporary = Path(name)
        env = clean_environment(temporary)
        app = extract(archive, temporary / "extracted", receipt["target"], env)
        if payload_identity(app) != provenance["payload"]:
            raise ValueError("Extracted app differs from the accepted payload")
        if receipt["target"] == "macos-arm64":
            from scripts.macho_audit import audit
            from scripts.macos_signing import verify_signature, distribution_trust
            audit(app)
            signed = selected == "production_signed"
            verify_signature(app, signed, env, logs, team=receipt["preflight"].get("signer", {}).get("team"))
            if signed:
                distribution_trust(app, env, logs)
            elif any(v != "not_applicable_unsigned_profile" for v in receipt["trust_gates"].values()):
                raise ValueError("Unsigned receipt falsely claims distribution trust")
        _, cli, _ = bundle_paths(app, receipt["target"])
        subprocess.run([str(cli), "doctor", "--desktop"], cwd=temporary, env=env, check=True, timeout=30, capture_output=True)
        embedded = subprocess.run([str(cli), "--verify-runtime"], cwd=temporary, env=env,
                                  check=True, timeout=60, capture_output=True, text=True)
        runtime = json.loads(embedded.stdout)
        if runtime.get("passed") is not True or runtime.get("frozen") is not True:
            raise ValueError("Frozen runtime verification failed")
        result = verify(app, logs=logs / "gui")
        if payload_identity(app) != provenance["payload"]:
            raise ValueError("Runtime verification modified the sealed app")
        return {**result, "profile": selected, "archive_sha256": receipt["sha256"]}


if __name__ == "__main__":
    raise SystemExit(main())
