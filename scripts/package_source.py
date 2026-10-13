#!/usr/bin/env python3
"""Create a deterministic complete-source ZIP with an embedded per-file manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.project_integrity import MANIFEST_NAME, audit, source_files, write_manifest

ROOT = Path(__file__).resolve().parents[1]


def package(output: Path, *, root: Path = ROOT, require_locks: bool = False) -> dict:
    root, output = root.absolute(), output.absolute()
    if output.suffix != ".zip":
        raise ValueError("Source package output must end in .zip")
    if output.is_relative_to(root) and not output.is_relative_to(root / "dist"):
        raise ValueError("Write generated archives outside the source tree or inside dist/")
    result = audit(root, require_locks=require_locks)
    if not result["source_check_passed"]:
        raise ValueError("Source completeness checks failed: " + "; ".join(result["issues"]))
    write_manifest(root)
    files = sorted([*source_files(root), root / MANIFEST_NAME])
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(root).as_posix()
            info = zipfile.ZipInfo("oandafly-1.0.0/" + relative, date_time=(2026, 9, 22, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o100755 if path.suffix == ".sh" else 0o100644) << 16
            archive.writestr(info, path.read_bytes())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(digest + "  " + output.name + "\n", encoding="utf-8")
    return {
        "archive": str(output), "sha256": digest, "source_files": len(files),
        "bytes": output.stat().st_size, "embedded_manifest": MANIFEST_NAME,
        "missing_release_dependency_files": result["missing_release_dependency_files"],
        "native_trading_qualified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-locks", action="store_true")
    args = parser.parse_args()
    print(json.dumps(package(args.output, require_locks=args.require_locks), indent=2))


if __name__ == "__main__":
    main()
