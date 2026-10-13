#!/usr/bin/env python3
"""Resolve/export fresh hashed locks in staging; never invent a lock on failure.

This needs uv and a reachable index or a sufficient verified local uv cache.
It does not install packages, approve a dependency audit or contact OANDA.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ("uv.lock", "requirements.lock", "requirements-dev.lock", "requirements-build.lock")


def validate_export(content: str) -> None:
    logical = content.replace("\\\n", " ")
    requirements = [line.strip() for line in logical.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
    if not requirements:
        raise ValueError("An empty export is not a dependency lock")
    for line in requirements:
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*==[^\s;]+(?:\s|;|$)", line):
            raise ValueError("Requirements must contain exact third-party pins, not editable/project paths")
        if not re.search(r"--hash=sha256:[0-9a-f]{64}(?:\s|$)", line):
            raise ValueError("Every pinned requirement needs an artifact SHA-256")


def resolve(root: Path = ROOT, *, check: bool = False, offline: bool = False, timeout: int = 180) -> dict:
    uv = shutil.which("uv")
    if uv is None:
        raise ValueError("Install uv before resolving the project dependency graph")
    root = root.absolute()
    if sys.version_info[:2] != (3, 13):
        raise ValueError("Generate this project's locks with Python 3.13")
    # Resolve in an isolated project so failed network requests do not replace
    # committed locks. No environment, credentials or application code is executed.
    with tempfile.TemporaryDirectory(prefix=".oandafly-lock-", dir=root) as name:
        stage = Path(name)
        for source in ("pyproject.toml", "README.md", "requirements-build.in"):
            shutil.copy2(root / source, stage / source)
        # Preserve previously resolved build pins as well as the project lock.
        # uv pip compile prefers pins from its existing output unless upgraded.
        for filename in FILES:
            if (root / filename).is_file():
                shutil.copy2(root / filename, stage / filename)
        env = dict(os.environ)
        # Do not inherit a caller's index override or credentials into this task.
        for key in list(env):
            if key.startswith(("PIP_", "UV_INDEX", "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX", "UV_FIND_LINKS")):
                env.pop(key, None)
        env["UV_HTTP_RETRIES"] = "0"
        env["UV_HTTP_TIMEOUT"] = str(min(timeout, 30))
        global_options = ["--no-config", "--no-python-downloads"]
        if offline:
            global_options.append("--offline")
        def run(arguments: list[str]) -> None:
            subprocess.run([uv, *global_options, *arguments], cwd=stage, env=env,
                           check=True, timeout=timeout)
        run(["lock", *( ["--check"] if check else [] ), "--python", sys.executable,
             "--default-index", "https://pypi.org/simple"])
        export = ["export", "--locked", "--format", "requirements.txt", "--no-emit-project", "--no-dev",
                  "--no-header", "--no-annotate", "--extra", "native-vault", "--extra", "feather"]
        run([*export, "--output-file", "requirements.lock"])
        run([*export, "--extra", "dev", "--extra", "desktop-build", "--output-file", "requirements-dev.lock"])
        run(["pip", "compile", "requirements-build.in", "--universal", "--python", sys.executable,
             "--generate-hashes", "--no-header", "--no-annotate", "--no-build",
             "--default-index", "https://pypi.org/simple", "--output-file", "requirements-build.lock"])
        for filename in FILES[1:]:
            validate_export((stage / filename).read_text(encoding="utf-8"))
        changed = [filename for filename in FILES if not (root / filename).is_file()
                   or (root / filename).read_bytes() != (stage / filename).read_bytes()]
        if check and changed:
            raise ValueError("Missing/stale dependency exports: " + ", ".join(changed))
        if not check:
            # All four artifacts have been generated successfully before publication.
            # A crash during replacement is caught by --check; no release is approved here.
            for filename in FILES:
                os.replace(stage / filename, root / filename)
        return {"status": "LOCKS_CHECKED" if check else "LOCKS_GENERATED_REVIEW_REQUIRED",
                "files": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in FILES},
                "dependencies_installed": False, "security_audit_completed": False,
                "native_trading_qualified": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    try:
        if not 1 <= args.timeout <= 600:
            raise ValueError("Timeout must be between 1 and 600 seconds")
        print(json.dumps(resolve(check=args.check, offline=args.offline, timeout=args.timeout), indent=2))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "LOCKS_UNRESOLVED", "error_type": type(exc).__name__,
                          "message": str(exc), "no_placeholder_locks_created": True}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
