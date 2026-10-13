"""Final-payload identity, clean extraction and whole-directory publication."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

from scripts.release_policy import digest, document_digest


def payload_identity(bundle: Path) -> dict:
    root = bundle.resolve(strict=True)
    files = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            target = os.readlink(path)
            if Path(target).is_absolute() or not path.resolve(strict=True).is_relative_to(root):
                raise ValueError("Escaping or absolute payload link: " + key)
            files[key] = {"link": target, "mode": mode}
        elif path.is_file():
            files[key] = {"sha256": digest(path), "mode": mode}
        elif path.is_dir():
            files[key] = {"directory": True, "mode": mode}
        else:
            raise ValueError("Special file in payload: " + key)
    if not files:
        raise ValueError("Empty application payload")
    return {"sha256": document_digest(files), "files": files}


def extract(archive: Path, destination: Path, target: str, env: dict) -> Path:
    if destination.exists():
        raise ValueError("Final archive extraction requires a fresh destination")
    destination.mkdir(parents=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            names = z.namelist()
            if len(names) != len(set(names)) or z.testzip():
                raise ValueError("Corrupt or duplicate archive members")
            for item in z.infolist():
                path = PurePosixPath(item.filename)
                if path.is_absolute() or ".." in path.parts or "\\" in item.filename:
                    raise ValueError("Unsafe archive member")
                if stat.S_ISLNK(item.external_attr >> 16):
                    link = z.read(item).decode("utf-8")
                    candidate = destination / item.filename
                    resolved = (candidate.parent / link).resolve()
                    if Path(link).is_absolute() or not resolved.is_relative_to(destination.resolve()):
                        raise ValueError("Escaping archive link")
        if target == "macos-arm64":
            subprocess.run(["/usr/bin/ditto", "-x", "-k", str(archive), str(destination)],
                           env=env, check=True, timeout=300)
        else:
            with zipfile.ZipFile(archive) as z:
                z.extractall(destination)
    else:
        with tarfile.open(archive) as tar:
            tar.extractall(destination, filter="data")
    return destination / ("Oandafly.app" if target == "macos-arm64" else "oandafly")


def publish(prepared: Path, destination: Path) -> None:
    """Rename only a complete, fsynced receipt directory on the same filesystem."""
    if destination.exists() or destination.is_symlink():
        raise ValueError("Refusing to overwrite a previously published release")
    receipt = json.loads((prepared / "build.json").read_text())
    if receipt.get("passed") is not True or receipt.get("production_quality_ready") is not True:
        raise ValueError("Unqualified release cannot be published")
    expected = receipt["sha256"]
    archive = prepared / receipt["archive_name"]
    if digest(archive) != expected or (prepared / "SHA256SUMS").read_text() != expected + "  " + archive.name + "\n":
        raise ValueError("Incomplete archive/checksum sidecars")
    for required in ("NOTICE.md", "provenance.json", "native-inventory.json", "evidence"):
        if not (prepared / required).exists():
            raise ValueError("Missing release sidecar: " + required)
    for path in prepared.rglob("*"):
        if path.is_symlink():
            raise ValueError("Release sidecars must not be links")
        if path.is_file():
            with path.open("rb") as f:
                os.fsync(f.fileno())
    if os.name == "posix":
        for directory in [p for p in prepared.rglob("*") if p.is_dir()] + [prepared]:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    for path in (destination.parent, *destination.parents):
        if path.is_symlink():
            raise ValueError("Linked publication directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if prepared.stat().st_dev != destination.parent.stat().st_dev:
        raise ValueError("Atomic publication requires the same filesystem")
    os.rename(prepared, destination)
