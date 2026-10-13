#!/usr/bin/env python3
"""Shared native developer workflow behind Bash 3.2 and Windows PowerShell."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


def metadata() -> dict:
    result = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    if (result["name"], result["version"]) != ("oandafly", "1.0.0"):
        raise ValueError("Developer launcher and project identity do not match")
    return result


@contextlib.contextmanager
def operation(name: str):
    # Shared mutex for cleanup, setup and commands; never delete a stale lock blindly.
    if (ROOT / ".dev-build.lock").exists():
        raise ValueError("A native build lock exists; inspect its owner")
    lock = ROOT / ".dev-environment.lock"
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ValueError("Another developer operation may be active. Inspect .dev-environment.lock.") from exc
    try:
        if (ROOT / ".dev-build.lock").exists():
            raise ValueError("A native build started; developer operation refused")
        (lock / "owner.json").write_text(json.dumps({"pid": os.getpid(), "operation": name}), encoding="utf-8")
        yield
    finally:
        (lock / "owner.json").unlink(missing_ok=True)
        lock.rmdir()


def venv_python() -> Path:
    return ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def runtime_check(interpreter: Path, *, installed: bool = False, development: bool = False) -> None:
    # The check runs inside the selected Python, not the launcher's Python.
    code = r"""
import sys, platform, struct, tomllib, pathlib, importlib.metadata, runpy
root = pathlib.Path(sys.argv[1]); installed = sys.argv[2] == '1'; development = sys.argv[3] == '1'
assert sys.version_info[:2] == (3,13), 'Use Python 3.13, matching pyproject.toml'
assert struct.calcsize('P') == 8, 'Use 64-bit Python'
m = platform.machine().lower()
assert (sys.platform == 'darwin' and m == 'arm64') or (sys.platform in ('linux','win32') and m in ('amd64','x86_64')), 'Unsupported native target'
import tkinter
runpy.run_path(str(root/'src/oandafly/macos.py'))['require_runtime']()
p = tomllib.loads((root/'pyproject.toml').read_text())['project']
assert (p['name'],p['version']) == ('oandafly','1.0.0'), 'Project mismatch'
if installed:
    assert pathlib.Path(sys.prefix).resolve() == (root/'.venv').resolve(), 'Use this checkout .venv'
    import oandafly
    assert pathlib.Path(oandafly.__file__).resolve() == (root/'src/oandafly/__init__.py').resolve(), 'Different installation loaded'
    assert importlib.metadata.version('oandafly') == p['version'], 'Installed version mismatch'
    from packaging.requirements import Requirement
    requirements = p['dependencies'] + p['optional-dependencies']['native-vault'] + p['optional-dependencies']['feather']
    if development:
        requirements += p['optional-dependencies']['dev'] + p['optional-dependencies']['desktop-build']
    for value in requirements:
        r = Requirement(value)
        if r.marker is not None and not r.marker.evaluate(): continue
        version = importlib.metadata.version(r.name)
        assert r.specifier.contains(version), f'{r.name} {version} does not meet {r.specifier}; run setup'
print(f'Python {platform.python_version()} / {m} / Tk {tkinter.TkVersion}')
"""
    subprocess.run([str(interpreter), "-I", "-B", "-c", code, str(ROOT), str(int(installed)),
                    str(int(development))], check=True, timeout=30)


def cache_environment() -> dict[str, str]:
    # Source resolver is stdlib-only on POSIX; Windows ACL support is installed by setup.
    from oandafly.paths import runtime_environment, state_directory, secure_directory
    storage = state_directory()
    env = {**os.environ, **runtime_environment(storage)}
    for variable, suffix in (("UV_CACHE_DIR", "uv"), ("PIP_CACHE_DIR", "pip"),
                             ("RUFF_CACHE_DIR", "ruff"), ("PYINSTALLER_CONFIG_DIR", "pyinstaller")):
        env[variable] = str(secure_directory(storage / "cache" / suffix))
    return env


def setup(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="dev setup")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--resolve", action="store_true")
    args = parser.parse_args(arguments)
    if not args.python.is_absolute() or not args.python.is_file():
        raise ValueError("--python must be an absolute native interpreter executable")
    runtime_check(args.python)
    uv = shutil.which("uv")
    if not uv:
        raise ValueError("Install uv before setup")
    venv = ROOT / ".venv"
    if venv.is_symlink() or (hasattr(venv, "is_junction") and venv.is_junction()):
        raise ValueError("Refusing a symlink/junction .venv")
    if venv.exists():
        if not venv_python().is_file():
            raise ValueError("Existing .venv is incomplete; preserve/move it or use clean --all")
        base_code = "import pathlib,sys;print(pathlib.Path(sys._base_executable).resolve())"
        old = subprocess.check_output([str(venv_python()), "-I", "-c", base_code], text=True).strip()
        new = subprocess.check_output([str(args.python), "-I", "-c", base_code], text=True).strip()
        if old != new:
            raise ValueError("Existing .venv belongs to a different Python. Preserve/move it before changing interpreters.")
    if args.resolve:
        subprocess.run([str(args.python), "-I", str(ROOT / "scripts/lock_dependencies.py"),
                        *(["--offline"] if args.offline else [])], check=True)
    if not (ROOT / "uv.lock").is_file():
        raise ValueError("uv.lock is absent. Use setup --resolve or lock on a network-enabled host first.")
    options = [uv, "sync", "--project", str(ROOT), "--locked", "--python", str(args.python),
               "--no-python-downloads", "--no-default-groups", "--extra", "dev", "--extra", "native-vault",
               "--extra", "feather", "--extra", "desktop-build"]
    if args.offline:
        options.append("--offline")
    # Windows bootstrap may not yet have pywin32; let uv use its normal user cache
    # until native-vault dependencies exist. Never downgrade authority path ACLs.
    env = dict(os.environ) if os.name == "nt" else cache_environment()
    env["UV_PROJECT_ENVIRONMENT"] = str(venv)
    subprocess.run(options, cwd=ROOT, env=env, check=True)
    runtime_check(venv_python(), installed=True, development=True)
    print("Setup complete. Open the desktop with dev.sh or dev.ps1. No account has been started.")


def clean(all_files: bool) -> None:
    # Windows counterpart to standalone Bash cleanup. Never follow directory links.
    venv = ROOT / ".venv"
    if all_files and venv.exists():
        if venv.is_symlink() or (hasattr(venv, "is_junction") and venv.is_junction()):
            raise ValueError("Refusing a linked .venv")
        if Path(sys.prefix).resolve() == venv.resolve():
            raise ValueError("Run cleanup with an external Python, not the .venv being deleted")
        # Native Windows denies removal of running executable/DLL files; preflight
        # process paths as well. No bypass or forced process termination is performed.
        script = ("Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and "
                  "$_.ExecutablePath.StartsWith($env:OANDAFLY_CLEAN_VENV, [System.StringComparison]::OrdinalIgnoreCase) } | Select-Object -ExpandProperty ProcessId")
        if os.name == "nt":
            result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                env={**os.environ, "OANDAFLY_CLEAN_VENV": str(venv) + os.sep}, capture_output=True, text=True, check=True)
            if result.stdout.strip():
                raise ValueError("The development environment has active processes")
        else:
            lsof = shutil.which("lsof")
            if not lsof:
                raise ValueError("lsof is required before removing .venv")
            if subprocess.run([lsof, "-t", "+D", str(venv)], stdout=subprocess.DEVNULL).returncode == 0:
                raise ValueError("The development environment is in use")
    prune = {".git", ".hg", ".svn", ".venv", ".agents", ".codex", "dist", ".dev-environment.lock"}
    generated = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".hypothesis", ".tox", ".nox", ".eggs", "__pypackages__"}
    for directory, dirs, files in os.walk(ROOT, followlinks=False):
        for name in list(dirs):
            path = Path(directory) / name
            if name in prune or path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                dirs.remove(name)
            elif name in generated or name.endswith(".egg-info"):
                shutil.rmtree(path)
                dirs.remove(name)
        for name in files:
            if name.endswith((".pyc", ".pyo")) or name == ".DS_Store":
                (Path(directory) / name).unlink()
    for name in ("build", "htmlcov", ".audit_cache", ".audit_work", ".audit_tools", ".uv_cache"):
        path = ROOT / name
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
    for name in (".coverage", "coverage.xml", "coverage.json"):
        (ROOT / name).unlink(missing_ok=True)
    if all_files and venv.exists():
        shutil.rmtree(venv)
    print("Checkout cleanup complete; source, lockfiles and application storage preserved.")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command, extra = (args[0], args[1:]) if args else ("run", [])
    try:
        metadata()
        if command not in {"setup", "lock", "run", "build", "build-unsigned", "release", "verify", "test", "check", "tk", "doctor",
                           "doctor-worker", "worker", "package", "clean"}:
            raise ValueError("Unknown command; see dev.sh help or dev.ps1 help")
        if command in {"build", "build-unsigned", "release"}:
            if extra in (["--help"], ["-h"]):
                print(f"{command}: no arguments. Full production pipeline; build/release use exported signing settings.")
                return 0
            if extra:
                raise ValueError(f"{command} takes no arguments; signing is configured only by exported environment values")
        if command in {"check", "tk", "doctor", "doctor-worker"} and extra:
            raise ValueError(f"{command} takes no arguments")
        from oandafly.platforms import native_target
        native_target()
        if command == "doctor":
            from scripts.release_doctor import diagnose
            result = diagnose()
            print(json.dumps(result, indent=2))
            return 0 if result["runtime"]["eligible"] else 2
        with operation(command):
            if command == "clean":
                if extra not in ([], ["--all"]):
                    raise ValueError("clean accepts only --all")
                clean(bool(extra))
                return 0
            if command == "setup":
                setup(extra)
                return 0
            if command == "lock":
                return subprocess.run([sys.executable, "-I", str(ROOT / "scripts/lock_dependencies.py"), *extra], cwd=ROOT).returncode
            python = venv_python()
            if not python.is_file():
                raise ValueError("No usable .venv. Run setup --resolve first; launch never silently resolves dependencies.")
            if command in {"build", "build-unsigned", "release"}:
                # Isolated build environments are created by the backend; do not
                # touch canonical application storage or pass signing data to tests.
                profile = "production_unsigned" if command == "build-unsigned" else "production_signed"
                return subprocess.run([str(python), "-I", "-B", str(ROOT / "scripts/build_desktop.py"), profile], cwd=ROOT).returncode
            development = command in {"test", "check"}
            runtime_check(python, installed=True, development=development)
            env = cache_environment()
            base = [str(python), "-I", "-X", "pycache_prefix=" + env["PYTHONPYCACHEPREFIX"]]
            actions = {
                "run": ["-m", "oandafly", "desktop", *extra],
                "worker": ["-m", "oandafly", "run", *extra],
                "doctor": ["-m", "oandafly", "doctor", "--desktop"],
                "doctor-worker": ["-m", "oandafly", "doctor"],
                "tk": ["-m", "tkinter"],
                "test": ["-m", "pytest", "-q", *extra],
                "package": [str(ROOT / "scripts/package_source.py"), "--output", str(ROOT / "dist/oandafly-1.0.0.zip"), *extra],
                "verify": [str(ROOT / "scripts/verify_build.py"), *extra],
            }
            if command == "check":
                return subprocess.run([str(python), "-I", str(ROOT / "scripts/check_quality.py")], cwd=ROOT).returncode
            return subprocess.run([*base, *actions[command]], cwd=ROOT, env=env).returncode
    except KeyboardInterrupt:
        print("oandafly dev: cancelled; prior releases preserved", file=sys.stderr)
        return 130
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print("oandafly dev: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
