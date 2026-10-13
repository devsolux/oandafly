#!/usr/bin/env python3
"""Verify wheel/runtime files and sdist/source parity against this checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tarfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.project_integrity import MANIFEST_NAME, source_files

ROOT = Path(__file__).resolve().parents[1]


def check_distribution(path: Path, root: Path = ROOT) -> dict:
    root = root.absolute()
    files = source_files(root)
    if path.name.endswith(".whl"):
        expected = {p.relative_to(root / "src").as_posix(): p.read_bytes()
                    for p in files if p.is_relative_to(root / "src")}
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise ValueError("Wheel CRC failed")
            actual = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
        unexpected_runtime = sorted({name for name in actual if name.startswith("oandafly/")} - set(expected))
        kind = "wheel"
    else:
        expected = {p.relative_to(root).as_posix(): p.read_bytes() for p in files}
        if (root / MANIFEST_NAME).is_file():
            expected[MANIFEST_NAME] = (root / MANIFEST_NAME).read_bytes()
        actual = {}
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                if member.issym() or member.islnk():
                    raise ValueError("Source distribution contains links")
                if not member.isfile():
                    continue
                name = member.name.split("/", 1)[-1]
                handle = archive.extractfile(member)
                if handle is None or name in actual:
                    raise ValueError("Invalid/duplicate source member")
                actual[name] = handle.read()
        unexpected_runtime = []
        kind = "sdist"
    missing = sorted(set(expected) - set(actual))
    changed = sorted(name for name in set(expected) & set(actual)
                     if hashlib.sha256(expected[name]).digest() != hashlib.sha256(actual[name]).digest())
    return {"kind": kind, "archive": path.name, "expected_files": len(expected),
            "missing": missing, "changed": changed, "unexpected_runtime_files": unexpected_runtime,
            "passed": not (missing or changed or unexpected_runtime)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", type=Path, nargs="+")
    args = parser.parse_args()
    result = [check_distribution(path) for path in args.archives]
    print(json.dumps(result, indent=2))
    return 0 if all(item["passed"] for item in result) else 2


if __name__ == "__main__":
    raise SystemExit(main())
