"""Explicit OS security capabilities; availability is never scientific approval.

Threat model: trusted application and numeric-only model artifacts under an
unprivileged OS account. The inference child must lose network, credential and
ledger access after loading. A same-user administrator is outside this boundary.
"""
from __future__ import annotations

import ctypes.util
import sys
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ContainmentCapability:
    backend: str | None
    implemented: bool
    prerequisite_available: bool
    reason: str


def capability(system: str | None = None) -> ContainmentCapability:
    system = sys.platform if system is None else system
    if system == "linux":
        available = ctypes.util.find_library("seccomp") is not None
        return ContainmentCapability("linux_seccomp", True, available,
                                     "Native denial tests required" if available else "Install libseccomp.so.2")
    if system == "darwin":
        return ContainmentCapability(None, False, False,
            "Software release blocker: macOS 26.0 App Sandbox inference helper and native denial tests are not implemented")
    return ContainmentCapability(None, False, False,
        "Software release blocker: Windows inference containment and native denial tests are not implemented")


def require_capability() -> dict:
    result = capability()
    if not result.implemented or not result.prerequisite_available:
        from oandafly.domain.errors import SafetyFence
        raise SafetyFence(result.reason)
    return asdict(result)
