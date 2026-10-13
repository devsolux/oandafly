"""Observe and qualify the requested toolchain without changing global selection."""
from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

from oandafly.macos import profile, version


def validate(observed: dict, inherited: dict[str, str]) -> list[str]:
    expected = profile()
    errors = []
    for key, wanted in (("architecture", "arm64"), ("translated", "0"),
                        ("clang_product", expected["clang_product"]), ("clang_build", expected["clang_build"]),
                        ("sdk_build", expected["sdk_build"])):
        if observed.get(key) != wanted:
            errors.append(f"{key}: expected {wanted!r}; observed {observed.get(key)!r}")
    for key in ("xcode_version", "clang_version", "sdk_version"):
        try:
            matches = version(observed.get(key, "")) == version(expected[key])
        except ValueError:
            matches = False
        if not matches:
            errors.append(f"{key}: expected {expected[key]!r}; observed {observed.get(key)!r}")
    for key in ("xcode_build", "host_build", "hardware", "system_developer_dir"):
        if not observed.get(key):
            errors.append(f"{key}: expected an independently observed identity; observed missing")
    for key in ("xcode_minimum_host", "application_minimum", "installed_xcode_minimum_host"):
        minimum = observed.get(key) if key == "installed_xcode_minimum_host" else expected[key]
        try:
            compatible = version(observed.get("host_version", "")) >= version(minimum or "")
        except ValueError:
            compatible = False
        if not compatible:
            errors.append(f"host_version: expected >= {minimum!r} ({key}); observed {observed.get('host_version')!r}")
    # Parsed macOS paths retain POSIX semantics in portable policy tests too.
    developer = PurePosixPath(observed.get("developer_dir", "/missing"))
    if not developer.is_absolute() or developer.parts[-2:] != ("Contents", "Developer"):
        errors.append("developer_dir: expected an absolute full Xcode Contents/Developer directory, not CommandLineTools")
    for key in ("clang_path", "sdk_path"):
        path = PurePosixPath(observed.get(key, "/missing"))
        if not path.is_absolute() or not path.is_relative_to(developer):
            errors.append(f"{key}: expected a path inside {developer}; observed {str(path)!r}")
    def selected(value: str) -> PurePosixPath:
        return PurePosixPath(Path(value).resolve().as_posix()) if sys.platform == "darwin" else PurePosixPath(value)
    if inherited.get("DEVELOPER_DIR") and (not PurePosixPath(inherited["DEVELOPER_DIR"]).is_absolute()
            or selected(inherited["DEVELOPER_DIR"]) != developer):
        errors.append("DEVELOPER_DIR conflicts with the effective absolute Xcode developer directory")
    if inherited.get("TOOLCHAINS") not in (None, "", "com.apple.dt.toolchain.XcodeDefault"):
        errors.append("TOOLCHAINS: expected the default Apple toolchain; observed a conflicting override")
    if inherited.get("SDKROOT") and selected(inherited["SDKROOT"]) != PurePosixPath(observed.get("sdk_path", "/missing")):
        errors.append("SDKROOT conflicts with the SDK independently resolved by xcrun")
    if inherited.get("MACOSX_DEPLOYMENT_TARGET"):
        try:
            matches = version(inherited["MACOSX_DEPLOYMENT_TARGET"]) == version(expected["deployment_target"])
        except ValueError:
            matches = False
        if not matches:
            errors.append(f"MACOSX_DEPLOYMENT_TARGET: expected {expected['deployment_target']}; observed {inherited['MACOSX_DEPLOYMENT_TARGET']!r}")
    for key, result in observed.get("commands", {}).items():
        if result["returncode"] != 0:
            errors.append(f"{key}: discovery failed ({result['returncode']}): {result['stderr'].strip()}")
    return errors


def discover(environment: dict[str, str] | None = None) -> dict:
    inherited = dict(os.environ if environment is None else environment)
    # Only developer selection and basic executable lookup belong in discovery children.
    env = {k: v for k, v in inherited.items() if k in {"PATH", "DEVELOPER_DIR", "TOOLCHAINS", "SDKROOT"}}
    commands = {}
    def query(name: str, *command: str, system_selection: bool = False) -> str:
        selection = {k: v for k, v in env.items() if k != "DEVELOPER_DIR"} if system_selection else env
        try:
            result = subprocess.run(list(command), env=selection, text=True, capture_output=True, timeout=30)
            commands[name] = {"argv": list(command), "stdout": result.stdout, "stderr": result.stderr,
                              "returncode": result.returncode}
            return result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError) as exc:
            commands[name] = {"argv": list(command), "stdout": "", "stderr": str(exc), "returncode": -1}
            return ""
    actual = {"host_version": query("host_version", "/usr/bin/sw_vers", "-productVersion"),
              "host_build": query("host_build", "/usr/bin/sw_vers", "-buildVersion"),
              "architecture": query("architecture", "/usr/bin/uname", "-m"),
              "hardware": query("hardware", "/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"),
              "translated": query("translated", "/usr/sbin/sysctl", "-in", "sysctl.proc_translated"),
              "system_developer_dir": query("system_developer_dir", "/usr/bin/xcode-select", "-p", system_selection=True),
              "developer_dir": query("developer_dir", "/usr/bin/xcode-select", "-p")}
    # Apple ARM hardware may omit this optional sysctl on an untranslated process.
    if not actual["translated"] and commands["translated"]["returncode"] == 0:
        actual["translated"] = "0"
    xcode = query("xcode", "/usr/bin/xcodebuild", "-version")
    match = re.fullmatch(r"Xcode ([\d.]+)\nBuild version (\S+)", xcode)
    if match:
        actual.update(xcode_version=match[1], xcode_build=match[2])
    actual["clang_path"] = query("clang_path", "/usr/bin/xcrun", "--sdk", "macosx", "--find", "clang")
    clang = query("clang", "/usr/bin/xcrun", "--sdk", "macosx", "clang", "--version")
    match = re.search(r"^(Apple clang) version ([\d.]+) \((clang-[^)]+)\)$", clang, re.MULTILINE)
    if match:
        actual.update(clang_product=match[1], clang_version=match[2], clang_build=match[3])
    for key, option in (("sdk_path", "--show-sdk-path"), ("sdk_version", "--show-sdk-version"),
                        ("sdk_build", "--show-sdk-build-version")):
        actual[key] = query(key, "/usr/bin/xcrun", "--sdk", "macosx", option)
    for key in ("developer_dir", "clang_path", "sdk_path"):
        if actual[key]:
            actual[key] = str(Path(actual[key]).resolve())
    try:
        info = plistlib.loads((Path(actual["developer_dir"]).parent / "Info.plist").read_bytes())
        actual["installed_xcode_minimum_host"] = info.get("LSMinimumSystemVersion")
    except (OSError, ValueError):
        actual["installed_xcode_minimum_host"] = None
    actual["commands"] = commands
    errors = validate(actual, inherited)
    return {"expected": profile(), "observed": actual, "issues": errors, "qualified": not errors}


def build_environment(report: dict) -> dict[str, str]:
    if not report["qualified"]:
        raise ValueError("macOS reference profile blocked: " + "; ".join(report["issues"]))
    return {"DEVELOPER_DIR": report["observed"]["developer_dir"],
            "SDKROOT": report["observed"]["sdk_path"],
            "MACOSX_DEPLOYMENT_TARGET": profile()["deployment_target"],
            "TOOLCHAINS": "com.apple.dt.toolchain.XcodeDefault"}
