"""Serialized SQLite WAL transactions with durable commits and verified backups.

The database is a cooperating-process store, not a distributed lock. Authority
ownership additionally uses an OS lock in the canonical state directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from oandafly.domain.errors import Conflict, ValidationFailure
from oandafly.domain.primitives import canonical_json, utc_now
from oandafly.paths import secure_directory
from oandafly.storage.migrations import SCHEMA_VERSION, check_compatibility, migrate
from oandafly.storage.ownership import StoreOwnerLock
from oandafly.storage.readiness import InitializationFence

MIN_SQLITE = (3, 51, 3)


class Database:
    """A single serialized connection; transactions may not be nested.

    Callers may share the returned connection with reducers to atomically commit
    outbox facts and ledger entries. Never perform network I/O inside a transaction.
    """

    def __init__(self, path: Path, *, fence: InitializationFence | None = None,
                 before_migrate: Callable[[], None] | None = None) -> None:
        self._fence = fence
        self._require_access()
        if sqlite3.sqlite_version_info < MIN_SQLITE:
            raise ValidationFailure("SQLite 3.51.3 or newer is required by this release")
        path = Path(path).absolute()
        secure_directory(path.parent)
        self._check_path(path)
        self.path = path
        self._lock = threading.RLock()
        self._active = False
        self._closed = False
        self._owner = StoreOwnerLock(path)
        try:
            self._connection = sqlite3.connect(path, timeout=10, isolation_level=None,
                                               check_same_thread=False)
        except BaseException:
            self._owner.close()
            raise
        self._connection.row_factory = sqlite3.Row
        try:
            check_compatibility(self._connection)
            if os.name != "nt":
                os.chmod(path, 0o600)
            else:
                from oandafly.security.windows_acl import private_file
                private_file(path)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=10000")
            self._connection.execute("PRAGMA trusted_schema=OFF")
            self._verify_pragmas()
            if before_migrate is not None:
                before_migrate()
            migrate(self._connection)
        except BaseException:
            try:
                self._connection.close()
            finally:
                self._owner.close()
            raise

    def _verify_pragmas(self) -> None:
        for name, expected in (("journal_mode", "wal"), ("synchronous", 2),
                               ("foreign_keys", 1), ("trusted_schema", 0), ("busy_timeout", 10000)):
            row = self._connection.execute(f"PRAGMA {name}").fetchone()
            if row is None or row[0] != expected:
                raise ValidationFailure(f"Required SQLite {name} configuration is unavailable")

    @staticmethod
    def _check_path(path: Path) -> None:
        for item in (path, *path.parents):
            if item.is_symlink():
                raise ValidationFailure("State paths must not contain symbolic links")
        if path.exists():
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValidationFailure("Database path must be a regular file without hard links")
            if os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
                raise ValidationFailure("Existing database must be owned by this user with private permissions")
            if os.name == "nt":
                from oandafly.security.windows_acl import private_file
                private_file(path)
        for suffix in ("-wal", "-shm", "-journal"):
            companion = Path(str(path) + suffix)
            if companion.is_symlink():
                raise ValidationFailure("Database journal paths must not be symbolic links")
            if companion.exists():
                info = companion.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValidationFailure("Database journals must be unaliased regular files")
                if os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
                    raise ValidationFailure("Database journal ownership or permissions are unsafe")
                if os.name == "nt":
                    from oandafly.security.windows_acl import private_file
                    private_file(companion)
        if not path.exists() and any(Path(str(path) + suffix).exists()
                                     for suffix in ("-wal", "-shm", "-journal")):
            raise ValidationFailure("Orphaned journal files require operator recovery")

    def _require_access(self) -> None:
        if self._fence is not None:
            self._fence.require_access()

    def require_ready(self) -> None:
        """IPC admission cannot use the initializer's private database access."""
        if self._fence is not None:
            self._fence.require_ready()

    def _ensure_open(self) -> None:
        self._require_access()
        if self._closed:
            raise Conflict("Database is closed")
        self._owner.assert_held()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._require_access()
        with self._lock:
            self._ensure_open()
            if self._active:
                raise Conflict("Nested database transactions are forbidden")
            self._active = True
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                try:
                    yield self._connection
                    self._connection.commit()
                except BaseException:
                    self._connection.rollback()
                    raise
            finally:
                self._active = False

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        self._require_access()
        with self._lock:
            self._ensure_open()
            if self._active:
                raise Conflict("Use the current connection inside a transaction")
            self._active = True
            try:
                self._connection.execute("BEGIN")
                try:
                    yield self._connection
                finally:
                    self._connection.rollback()
            finally:
                self._active = False

    def initialize(self, script: str, *, component: str | None = None) -> None:
        """Execute complete DDL statements inside one rollback-capable transaction."""
        self._require_access()
        statements: list[str] = []
        pending = ""
        for char in script:
            pending += char
            if char == ";" and sqlite3.complete_statement(pending):
                statements.append(pending)
                pending = ""
        if pending.strip():
            if not sqlite3.complete_statement(pending + ";"):
                raise ValidationFailure("Incomplete schema statement")
            statements.append(pending)
        with self.transaction() as connection:
            if component is not None:
                checksum = hashlib.sha256(script.encode("utf-8")).hexdigest()
                existing = connection.execute("SELECT checksum FROM schema_components WHERE name=?", (component,)).fetchone()
                if existing is not None and existing[0] != checksum:
                    raise ValidationFailure("Component DDL changed without a numbered migration")
                connection.execute("INSERT OR IGNORE INTO schema_components VALUES(?,?)", (component, checksum))
            for statement in statements:
                connection.execute(statement)

    def audit(self, event: str, body: object) -> None:
        with self.transaction() as connection:
            connection.execute("INSERT INTO application_audit(time,event,body) VALUES(?,?,?)",
                               (utc_now().isoformat(), event, canonical_json(body)))

    def backup(self, destination: Path) -> dict[str, object]:
        """Create a new consistent backup; restoration is never automatic."""
        self._require_access()
        destination = destination.absolute()
        self._check_path(destination)
        secure_directory(destination.parent)
        parent_info = destination.parent.stat()
        if os.name != "nt" and (parent_info.st_uid != os.getuid() or parent_info.st_mode & 0o077):
            raise ValidationFailure("Backups require an owner-only destination directory")
        descriptor = os.open(destination, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        identity = os.fstat(descriptor)
        try:
            with self._lock:
                self._ensure_open()
                if self._active:
                    raise Conflict("Cannot backup inside an active transaction")
                target = sqlite3.connect(destination)
                try:
                    self._connection.backup(target)
                    check = target.execute("PRAGMA integrity_check").fetchone()[0]
                    if check != "ok":
                        raise ValidationFailure("Backup integrity check failed")
                finally:
                    target.close()
            published = destination.stat(follow_symlinks=False)
            if ((published.st_dev, published.st_ino) != (identity.st_dev, identity.st_ino)
                    or published.st_nlink != 1 or not stat.S_ISREG(published.st_mode)):
                raise ValidationFailure("Backup file identity changed during creation")
            with destination.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
                os.fsync(handle.fileno())
            manifest: dict[str, object] = {"format": 1, "file": destination.name,
                "sha256": digest, "size": destination.stat().st_size,
                "schema": SCHEMA_VERSION, "created_at": utc_now().isoformat(),
                "restore_policy": "REQUIRES_RETIRED_OWNER_AND_FRESH_RECONCILIATION"}
            manifest_path = destination.with_suffix(destination.suffix + ".manifest.json")
            with manifest_path.open("x", encoding="utf-8") as handle:
                json.dump(manifest, handle, sort_keys=True, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                os.chmod(manifest_path, 0o600)
                directory_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return manifest
        except BaseException:
            if destination.exists() and not destination.is_symlink():
                candidate = destination.stat()
                if (candidate.st_dev, candidate.st_ino) == (identity.st_dev, identity.st_ino):
                    destination.unlink(missing_ok=True)
            raise
        finally:
            os.close(descriptor)

    def close(self) -> None:
        self._require_access()
        with self._lock:
            if not self._closed:
                if self._active:
                    raise Conflict("Cannot close an active transaction")
                try:
                    self._connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
                finally:
                    try:
                        self._connection.close()
                    finally:
                        self._closed = True
                        self._owner.close()
