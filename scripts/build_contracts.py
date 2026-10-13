"""Native bundle identity, architecture, resource and source-parity gates."""
from __future__ import annotations

import hashlib
import json
import plistlib
import struct
import sys
import tomllib
from pathlib import Path
from oandafly.macos import profile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def identity(root: Path = ROOT) -> dict:
    value = json.loads((root / "packaging/application.json").read_text(encoding="utf-8"))
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    if (value.get("name"), value.get("version"), value.get("identifier"), value.get("entrypoint")) != (
        project["name"], project["version"], "com.devsolux.oandafly", "oandafly.desktop:main"):
        raise ValueError("Packaging identity/entrypoint drift")
    if value.get("default_armed") is not False or value.get("automatic_worker_start") is not False:
        raise ValueError("Desktop launch must not arm or start a worker automatically")
    if set(value.get("targets", [])) != {"macos-arm64", "windows-amd64", "linux-amd64"}:
        raise ValueError("Unexpected native target list")
    if value["minimum_os"]["macos-arm64"] != profile()["application_minimum"]:
        raise ValueError("macOS minimum differs from the central profile")
    return value


def binary_kind(path: Path) -> dict:
    with path.open("rb") as stream:
        header = stream.read(64)
        if header.startswith(b"\x7fELF"):
            if len(header) < 20 or header[4:6] != b"\x02\x01":
                raise ValueError("Expected a 64-bit little-endian ELF")
            return {"format": "ELF", "machine": struct.unpack_from("<H", header, 18)[0]}
        if header[:2] == b"MZ":
            if len(header) < 64:
                raise ValueError("Truncated PE header")
            offset = struct.unpack_from("<I", header, 60)[0]
            if offset > 4 * 1024**2:
                raise ValueError("Invalid PE header offset")
            stream.seek(offset)
            pe = stream.read(128)
            if pe[:4] != b"PE\0\0" or len(pe) < 94 or struct.unpack_from("<H", pe, 24)[0] != 0x20B:
                raise ValueError("Expected a PE32+ executable")
            return {"format": "PE", "machine": struct.unpack_from("<H", pe, 4)[0],
                    "subsystem": struct.unpack_from("<H", pe, 92)[0]}
        if header[:4] == b"\xcf\xfa\xed\xfe":
            return {"format": "MACHO64", "cpu_type": struct.unpack_from("<I", header, 4)[0]}
        raise ValueError("Unrecognized native executable format")


def bundle_paths(bundle: Path, target: str) -> tuple[Path, Path, Path]:
    if target == "macos-arm64":
        return bundle / "Contents/MacOS/oandafly", bundle / "Contents/MacOS/oandafly-cli", bundle / "Contents/Resources"
    return (bundle / ("oandafly.exe" if target == "windows-amd64" else "oandafly"),
            bundle / ("oandafly-cli.exe" if target == "windows-amd64" else "oandafly-cli"), bundle / "_internal")


def inspect_bundle(bundle: Path, target: str, root: Path = ROOT) -> dict:
    value = identity(root)
    gui, cli, resources = bundle_paths(bundle, target)
    for path in (gui, cli):
        if not path.is_file():
            raise ValueError("Missing native GUI/CLI executable: " + path.name)
    binaries = {"gui": binary_kind(gui), "cli": binary_kind(cli)}
    if target == "windows-amd64":
        if binaries != {"gui": {"format": "PE", "machine": 0x8664, "subsystem": 2},
                        "cli": {"format": "PE", "machine": 0x8664, "subsystem": 3}}:
            raise ValueError("Windows GUI/CLI must be AMD64 with correct native subsystems")
    elif target == "linux-amd64":
        if any(item != {"format": "ELF", "machine": 62} for item in binaries.values()):
            raise ValueError("Linux binaries must be AMD64 ELF64")
    elif target == "macos-arm64":
        if any(item != {"format": "MACHO64", "cpu_type": 0x0100000C} for item in binaries.values()):
            raise ValueError("macOS binaries must be thin ARM64 Mach-O")
        info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
        for key, expected in {"CFBundleIdentifier": value["identifier"], "CFBundleShortVersionString": "1.0.0",
                              "CFBundleVersion": "1.0.0", "NSHighResolutionCapable": True,
                              "LSBackgroundOnly": False, "LSUIElement": False, "LSMinimumSystemVersion": profile()["application_minimum"]}.items():
            if info.get(key) != expected:
                raise ValueError("Conflicting/missing macOS bundle metadata: " + key)
    else:
        raise ValueError("Unknown native target")
    source_files = sorted(path for path in (root / "src/oandafly").rglob("*")
                          if path.is_file() and "__pycache__" not in path.parts
                          and (path.suffix in {".py", ".json", ".png", ".ico", ".icns"} or path.name == "py.typed"))
    for original in source_files:
        relative = original.relative_to(root / "src")
        delivered = resources / relative
        if not delivered.is_file() or hashlib.sha256(delivered.read_bytes()).digest() != hashlib.sha256(original.read_bytes()).digest():
            raise ValueError("Native source/resource parity failure: " + relative.as_posix())
    if not (resources / "release/application.json").is_file():
        raise ValueError("Native release identity metadata missing")
    return {"target": target, "binaries": binaries, "source_resource_files_verified": len(source_files), "passed": True}
