"""Explicit native desktop targets; desktop readiness is not trading authority."""
from __future__ import annotations

import platform
import struct
import sys
from dataclasses import asdict, dataclass
from oandafly.macos import profile, require_runtime


@dataclass(frozen=True)
class DesktopTarget:
    key: str
    system: str
    machine: str
    minimum_os: str
    archive_suffix: str


TARGETS = {
    "macos-arm64": DesktopTarget("macos-arm64", "darwin", "arm64", profile()["application_minimum"], ".zip"),
    "windows-amd64": DesktopTarget("windows-amd64", "win32", "amd64", "10.0.22000", ".zip"),
    "linux-amd64": DesktopTarget("linux-amd64", "linux", "amd64", "glibc 2.35", ".tar.gz"),
}


def native_target(system: str | None = None, machine: str | None = None, *, bits: int | None = None) -> DesktopTarget:
    system = sys.platform if system is None else system
    machine = platform.machine().lower() if machine is None else machine.lower()
    bits = struct.calcsize("P") * 8 if bits is None else bits
    machine = {"x86_64": "amd64", "x64": "amd64", "aarch64": "arm64"}.get(machine, machine)
    for target in TARGETS.values():
        if (system, machine, bits) == (target.system, target.machine, 64):
            return target
    raise ValueError("Supported desktop targets: macOS ARM64, Windows AMD64 and Linux AMD64 (64-bit Python).")


def host_report() -> dict:
    from oandafly.neural.containment import capability
    try:
        require_runtime()
        target = asdict(native_target())
    except ValueError:
        target = None
    return {"target": target, "system": sys.platform, "machine": platform.machine(),
            "python": platform.python_version(), "native_desktop_target": target is not None,
            "production_inference_backend": "LINUX_SECCOMP" if sys.platform == "linux" else None,
            "containment": asdict(capability()),
            "native_trading_qualified": False}
