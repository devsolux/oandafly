"""Credential-free embedded-runtime exercises using newly created temporary data."""
from __future__ import annotations

import importlib
import json
import platform
import sqlite3
import sys
import tempfile
from pathlib import Path


def probe(*, require_frozen: bool = True) -> dict:
    from oandafly.macos import require_runtime
    require_runtime()
    if require_frozen and not getattr(sys, "frozen", False):
        raise ValueError("Embedded runtime acceptance requires the actual frozen console helper")
    if sys.version_info[:2] != (3, 13) or sqlite3.sqlite_version_info < (3, 51, 3):
        raise ValueError("Embedded Python/SQLite does not meet the declared runtime contract")
    import numpy as np
    import scipy.sparse as sparse
    import pyarrow as pa
    import certifi
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    array = np.array([1.0, 2.0], dtype=np.float64)
    if not np.array_equal(sparse.eye(2, format="csr") @ array, array):
        raise ValueError("Embedded NumPy/SciPy exercise failed")
    if pa.table({"value": [1, 2]}).to_pydict() != {"value": [1, 2]}:
        raise ValueError("Embedded PyArrow exercise failed")
    key = Ed25519PrivateKey.generate()  # ephemeral synthetic data; no signing Keychain
    key.public_key().verify(key.sign(b"oandafly-runtime-probe"), b"oandafly-runtime-probe")
    import ssl
    ssl.create_default_context(cafile=certifi.where())  # validate bundled CA resources, no network
    backend = {"darwin": "keyring.backends.macOS", "win32": "keyring.backends.Windows",
               "linux": "keyring.backends.SecretService"}[sys.platform]
    importlib.import_module(backend)  # import only; never obtain a keyring or request credentials
    with tempfile.TemporaryDirectory(prefix="oandafly-runtime-probe-") as directory:
        path = Path(directory) / "synthetic.sqlite3"
        one = sqlite3.connect(path)
        two = sqlite3.connect(path)
        try:
            if one.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise ValueError("Embedded SQLite WAL is unavailable")
            one.execute("CREATE TABLE probe(value INTEGER NOT NULL)"); one.commit()
            one.execute("INSERT INTO probe VALUES(1)"); one.commit()
            one.execute("BEGIN IMMEDIATE"); one.execute("INSERT INTO probe VALUES(2)")
            if two.execute("SELECT value FROM probe").fetchall() != [(1,)]:
                raise ValueError("SQLite concurrent-reader isolation failed")
            one.rollback()
        finally:
            one.close(); two.close()
        with sqlite3.connect(path) as reopened:
            if reopened.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Embedded SQLite integrity check failed")
            if reopened.execute("SELECT value FROM probe").fetchall() != [(1,)]:
                raise ValueError("SQLite rollback/reopen failed")
    return {"passed": True, "frozen": bool(getattr(sys, "frozen", False)),
            "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(), "machine": platform.machine(),
            "checks": {key: "passed" for key in ("sqlite_wal_concurrent_reader_rollback_reopen",
                       "numpy_scipy", "pyarrow", "cryptography", "certificates", "vault_backend_import")},
            "credentials_accessed": False, "native_containment_qualified": False,
            "native_trading_qualified": False}


def main() -> int:
    try:
        print(json.dumps(probe(), indent=2))
        return 0
    except (OSError, ValueError, ImportError, sqlite3.Error) as exc:
        print(json.dumps({"passed": False, "error_type": type(exc).__name__, "message": str(exc)}))
        return 2
