"""Runtime policy only: running the installed app never probes developer tools."""
from __future__ import annotations

import json
import platform
import re
import sys
from pathlib import Path


def profile() -> dict:
    return json.loads((Path(__file__).parent / "resources/macos-profile.json").read_text(encoding="utf-8"))


def version(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+){0,2}", value):
        raise ValueError(f"Invalid numeric version: {value!r}")
    parts = [int(part) for part in value.split(".")]
    parts += [0] * (3 - len(parts))
    return parts[0], parts[1], parts[2]


def require_runtime() -> None:
    if sys.platform == "darwin":
        requested = profile()
        if platform.machine().lower() != requested["architecture"]:
            raise ValueError("Oandafly requires native ARM64 execution; relaunch outside Rosetta.")
        actual = platform.mac_ver()[0]
        if version(actual) < version(requested["application_minimum"]):
            raise ValueError(f"Oandafly requires macOS {requested['application_minimum']} or newer; observed {actual}.")
