"""Atomic initialization of the fresh 1.0.0 schema; no pre-release migrations.

Checksums and reader/writer checks protect this schema, not legacy compatibility.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

from oandafly.domain.errors import ValidationFailure
from oandafly.domain.primitives import utc_now

SCHEMA_VERSION = 1
READER_VERSION = 1
WRITER_VERSION = 1


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        body = str(self.version) + "\n" + "\n".join(self.statements)
        return hashlib.sha256(body.encode()).hexdigest()


# The initial 1.0.0 schema. Pre-release stores are deliberately unsupported.
MIGRATIONS = (
    Migration(1, (
        "CREATE TABLE application_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "CREATE TABLE application_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, event TEXT NOT NULL, body TEXT NOT NULL)",
        "CREATE TABLE schema_compatibility (singleton INTEGER PRIMARY KEY CHECK(singleton=1), minimum_reader INTEGER NOT NULL, minimum_writer INTEGER NOT NULL)",
        "INSERT INTO schema_compatibility VALUES(1,1,1)",
        "INSERT INTO application_meta VALUES('product_schema','oandafly.full-malecns.1')",
        "CREATE TABLE schema_components (name TEXT PRIMARY KEY, checksum TEXT NOT NULL)",
    )),
)

def check_compatibility(connection: sqlite3.Connection) -> tuple[int, bool]:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version not in (0, *(migration.version for migration in MIGRATIONS)):
        raise ValidationFailure(f"Unsupported database schema version {version}")
    known = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone()
    if version == 0 and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone():
        raise ValidationFailure("Unversioned nonempty database requires explicit recovery")
    if version > 0 and not known:
        raise ValidationFailure("Database schema migration history is missing")
    if known:
        rows = connection.execute("SELECT version,checksum FROM schema_migrations ORDER BY version").fetchall()
        expected = [(m.version, m.checksum) for m in MIGRATIONS if m.version <= version]
        if [tuple(row) for row in rows] != expected:
            raise ValidationFailure("Database schema migration checksum/history mismatch")
        compatibility = connection.execute("SELECT minimum_reader,minimum_writer FROM schema_compatibility WHERE singleton=1").fetchone()
        if (compatibility is None or compatibility[0] > READER_VERSION
                or compatibility[1] > WRITER_VERSION):
            raise ValidationFailure("Database requires a newer reader or writer")
    return version, bool(known)


def migrate(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        version, known = check_compatibility(connection)
        if not known:
            connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
        for migration in MIGRATIONS:
            if migration.version <= version:
                continue
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute("INSERT INTO schema_migrations VALUES(?,?,?)",
                               (migration.version, migration.checksum, utc_now().isoformat()))
            connection.execute(f"PRAGMA user_version={migration.version}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
