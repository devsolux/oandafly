"""Read-only eligibility; secret/keychain probes belong to an explicit signed build."""
from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import asdict

from oandafly.macos import require_runtime
from oandafly.neural.containment import capability


def diagnose() -> dict:
    issues = []
    try:
        require_runtime()
        if sys.version_info[:2] != (3, 13):
            raise ValueError("Python 3.13 required")
        import tkinter
        tk_version = tkinter.TkVersion
    except (ValueError, ImportError) as exc:
        issues.append(str(exc))
        tk_version = None
    common = list(issues)
    if sqlite3.sqlite_version_info < (3, 51, 3):
        common.append("Python-linked SQLite >=3.51.3 required")
    containment = capability()
    if not containment.implemented or not containment.prerequisite_available:
        common.append(containment.reason)
    toolchain = None
    if sys.platform == "darwin":
        from scripts.macos_profile import discover
        toolchain = discover()
        common.extend(toolchain["issues"])
        if not os.environ.get("OANDAFLY_BASELINE_CONFIG"):
            common.append("No macOS 26.0 baseline acceptance runner configured")
    return {"runtime": {"eligible": not issues, "issues": issues, "tk_version": tk_version,
                        "sqlite": sqlite3.sqlite_version, "window_tested": False},
            "production_unsigned": {"eligible": False if common else None, "issues": common,
                                    "remaining_checks": "full production pipeline", "signing_credentials_probed": False},
            "production_signed": {"eligible": False if common else None, "issues": common,
                                  "credential_resources": "not_probed; explicit build performs key/profile usability checks"},
            "toolchain": toolchain, "containment": asdict(containment), "native_trading_qualified": False}
