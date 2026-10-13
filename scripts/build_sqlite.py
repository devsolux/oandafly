#!/usr/bin/env python3
"""Build a private Linux SQLite 3.51.3 library; verify the official sqlite3.c digest.

This optional build helper needs network access, a C compiler and a dynamically
linked CPython sqlite3 module. It never installs into a system library directory
or changes Database admission. Run the resulting interpreter tests independently.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

URL = "https://www.sqlite.org/2026/sqlite-amalgamation-3510300.zip"
SOURCE_SHA3_256 = "32d5424f97e0a7fc5ed2f6335afbb58be4e0298bd7117a34e39d345ff13d859e"
MAX_DOWNLOAD = 8 * 1024**2
MAX_SOURCE = 16 * 1024**2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "linux":
        raise SystemExit("This optional private-library helper targets Linux only")
    prefix = args.prefix.absolute()
    if not args.prefix.is_absolute() or prefix == Path("/") or any(p in (Path("/usr"), Path("/lib"), Path("/lib64")) for p in (prefix, *prefix.parents)):
        raise SystemExit("Use a dedicated absolute private prefix, not a system library directory")
    if any(p.is_symlink() for p in (prefix, *prefix.parents)):
        raise SystemExit("The private build prefix cannot traverse symbolic links")
    compiler = shutil.which("cc")
    if compiler is None:
        raise SystemExit("A C compiler is required")
    target = prefix / "lib" / "libsqlite3.so.0"
    if target.exists():
        raise SystemExit("Private output already exists; verify it instead of overwriting")
    with urllib.request.urlopen(URL, timeout=60) as response:
        if not response.url.startswith("https://www.sqlite.org/"):
            raise SystemExit("Unexpected download origin")
        body = response.read(MAX_DOWNLOAD + 1)
    if len(body) > MAX_DOWNLOAD:
        raise SystemExit("Unexpectedly large SQLite source archive")
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        info = archive.getinfo("sqlite-amalgamation-3510300/sqlite3.c")
        if info.file_size > MAX_SOURCE:
            raise SystemExit("Unexpectedly large SQLite amalgamation")
        source = archive.read(info)
    if hashlib.sha3_256(source).hexdigest() != SOURCE_SHA3_256:
        raise SystemExit("Official sqlite3.c SHA3-256 verification failed")
    prefix.mkdir(parents=True, exist_ok=True, mode=0o700)
    (prefix / "lib").mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sqlite-build-", dir=prefix) as temporary:
        directory = Path(temporary)
        source_path = directory / "sqlite3.c"
        source_path.write_bytes(source)
        artifact = directory / "libsqlite3.so.0"
        command = [compiler, "-O2", "-fPIC", "-shared", "-DSQLITE_THREADSAFE=1",
                   "-DSQLITE_ENABLE_COLUMN_METADATA", "-DSQLITE_ENABLE_FTS5",
                   "-DSQLITE_ENABLE_RTREE", "-Wl,-soname,libsqlite3.so.0",
                   str(source_path), "-o", str(artifact), "-ldl", "-lpthread", "-lm"]
        subprocess.run(command, check=True, timeout=180)
        os.chmod(artifact, 0o700)
        os.replace(artifact, target)
    # Do not mutate this running interpreter; verify the actual linked library
    # in a fresh process using the explicitly selected private search directory.
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = str(target.parent) + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    code = "import sqlite3; assert sqlite3.sqlite_version_info >= (3,51,3), sqlite3.sqlite_version; print(sqlite3.sqlite_version)"
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise SystemExit("Library built, but CPython did not load a supported SQLite. Rebuild/review the interpreter; never spoof its version.")
    print(json.dumps({"sqlite": result.stdout.strip(), "library": str(target),
                      "sqlite3_c_sha3_256": SOURCE_SHA3_256,
                      "qualification": "PRIVATE_LIBRARY_BUILD_ONLY_NOT_APPLICATION_TESTS"}, indent=2))


if __name__ == "__main__":
    main()
