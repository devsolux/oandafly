"""Read Mach-O load commands, including the ARM64 slice of universal libraries.

This checks declared linkage and deployment metadata; baseline execution remains
mandatory because load commands cannot prove availability of every imported API.
"""
from __future__ import annotations

import os
import plistlib
import stat
import struct
from pathlib import Path

from oandafly.macos import profile, version

MAGICS = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}
ARM64 = 0x0100000C


def packed_version(number: int) -> str:
    return f"{number >> 16}.{(number >> 8) & 255}.{number & 255}"


def image(path: Path) -> dict | None:
    with path.open("rb") as f:
        magic = f.read(4)
        if magic not in MAGICS:
            return None
        offset, size = 0, path.stat().st_size
        if magic in {b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}:
            count = struct.unpack(">I", f.read(4))[0]
            if not 1 <= count <= 32:
                raise ValueError(f"Invalid universal image: {path}")
            fmt = ">IIQQII" if magic[-1] == 0xBF else ">IIIII"
            entries = [struct.unpack(fmt, f.read(struct.calcsize(fmt))) for _ in range(count)]
            slices = [entry for entry in entries if entry[0] == ARM64]
            if len(slices) != 1:
                raise ValueError(f"Missing unique ARM64 slice: {path}")
            offset, size = slices[0][2:4]
            if offset < 8 + count * struct.calcsize(fmt) or offset + size > path.stat().st_size:
                raise ValueError(f"Invalid ARM64 slice bounds: {path}")
        f.seek(offset)
        header = f.read(32)
        if len(header) != 32 or header[:4] != b"\xcf\xfa\xed\xfe":
            raise ValueError(f"Expected ARM64 little-endian Mach-O: {path}")
        _, cpu, _, filetype, ncmds, command_bytes, _, _ = struct.unpack("<8I", header)
        if cpu != ARM64 or command_bytes > size - 32 or ncmds > command_bytes // 8:
            raise ValueError(f"Invalid ARM64 Mach-O header: {path}")
        raw = f.read(command_bytes)
    cursor, minima, sdks, dependencies, rpaths = 0, [], [], [], []
    for _ in range(ncmds):
        if cursor + 8 > len(raw):
            raise ValueError(f"Truncated load commands: {path}")
        command, length = struct.unpack_from("<II", raw, cursor)
        if length < 8 or cursor + length > len(raw):
            raise ValueError(f"Invalid load command size: {path}")
        body = raw[cursor:cursor + length]
        if command == 0x32:  # LC_BUILD_VERSION: platform, minos, sdk, ntools
            if length < 24 or struct.unpack_from("<I", body, 8)[0] != 1:
                raise ValueError(f"Expected macOS build platform: {path}")
            minimum, sdk = struct.unpack_from("<II", body, 12)
            minima.append(packed_version(minimum)); sdks.append(packed_version(sdk))
        elif command == 0x24:  # LC_VERSION_MIN_MACOSX
            if length < 16:
                raise ValueError(f"Invalid minimum OS command: {path}")
            minimum, sdk = struct.unpack_from("<II", body, 8)
            minima.append(packed_version(minimum)); sdks.append(packed_version(sdk))
        elif command in {0xC, 0x80000018, 0x8000001F, 0x80000023, 0x8000001C}:
            if length < 12:
                raise ValueError(f"Invalid linkage command: {path}")
            start = struct.unpack_from("<I", body, 8)[0]
            if not 12 <= start < length or b"\0" not in body[start:]:
                raise ValueError(f"Invalid linkage string: {path}")
            value = body[start:].split(b"\0", 1)[0].decode("utf-8", errors="strict")
            (rpaths if command == 0x8000001C else dependencies).append(value)
        cursor += length
    if cursor != len(raw) or not minima:
        raise ValueError(f"Missing or malformed deployment metadata: {path}")
    maximum = profile()["application_minimum"]
    if any(version(v) > version(maximum) for v in minima):
        raise ValueError(f"{path}: minimum runtime {minima} exceeds {maximum}; SDK stamps {sdks} are separate")
    return {"architecture": "arm64", "filetype": filetype, "minimum_os": minima, "sdk_stamps": sdks,
            "dependencies": dependencies, "rpaths": rpaths}


def audit(bundle: Path) -> dict:
    bundle = bundle.resolve(strict=True)
    inventory = {}
    for path in sorted(bundle.rglob("*")):
        relative = path.relative_to(bundle).as_posix()
        if path.is_symlink():
            if not path.resolve(strict=True).is_relative_to(bundle):
                raise ValueError("Escaping bundle symlink: " + relative)
            continue
        if not path.is_file():
            continue
        if path.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID):
            raise ValueError("Unsafe bundled file permissions: " + relative)
        record = image(path)
        if record:
            inventory[relative] = record
        if path.name == "Info.plist" and path.parent.name == "Contents":
            info = plistlib.loads(path.read_bytes())
            minimum = profile()["application_minimum"]
            if info.get("LSMinimumSystemVersion") != minimum:
                raise ValueError("App/helper LSMinimumSystemVersion mismatch: " + relative)
            overrides = info.get("LSMinimumSystemVersionByArchitecture", {})
            if any(version(value) != version(minimum) for value in overrides.values()):
                raise ValueError("Architecture-specific minimum OS mismatch: " + relative)
    if not inventory:
        raise ValueError("No ARM64 code in bundle")
    executables = [bundle / key for key, data in inventory.items() if data["filetype"] == 2]
    def system(value):
        return value.startswith(("/usr/lib/", "/System/Library/")) and ".." not in Path(value).parts
    def expand(value, loader, executable):
        value = value.replace("@loader_path", str(loader.parent)).replace("@executable_path", str(executable.parent))
        return Path(value).resolve()
    for key, data in inventory.items():
        loader = bundle / key
        for value in data["rpaths"]:
            if (value.startswith("/") and not system(value)) or not value.startswith(("/", "@loader_path", "@executable_path")):
                raise ValueError(f"Nonportable rpath in {key}: {value}")
            if not system(value) and any(not expand(value, loader, exe).is_relative_to(bundle) for exe in executables):
                raise ValueError(f"Escaping rpath in {key}: {value}")
        resolved = {}
        for dependency in data["dependencies"]:
            if system(dependency):
                resolved[dependency] = "apple_system_baseline_execution_required"
                continue
            if dependency.startswith("/"):
                raise ValueError(f"External absolute dependency in {key}: {dependency}")
            candidates = []
            for executable in executables:
                rpaths = data["rpaths"] + inventory[executable.relative_to(bundle).as_posix()]["rpaths"]
                if dependency.startswith("@rpath/"):
                    for rpath in rpaths:
                        candidates.append(expand(rpath, loader, executable) / dependency[7:])
                elif dependency.startswith(("@loader_path/", "@executable_path/")):
                    candidates.append(expand(dependency, loader, executable))
                else:
                    raise ValueError(f"Unqualified dependency in {key}: {dependency}")
            found = [p.resolve() for p in candidates if p.is_file() and p.resolve().is_relative_to(bundle)]
            if not found or any(p.relative_to(bundle).as_posix() not in inventory for p in found):
                raise ValueError(f"Unresolved/unqualified dependency in {key}: {dependency}")
            resolved[dependency] = sorted({p.relative_to(bundle).as_posix() for p in found})
        data["resolved_dependencies"] = resolved
    for executable in executables:
        if not os.access(executable, os.X_OK):
            raise ValueError("Missing executable permission: " + str(executable))
    return {"passed": True, "images": inventory, "application_minimum": profile()["application_minimum"],
            "baseline_execution_required": True}
