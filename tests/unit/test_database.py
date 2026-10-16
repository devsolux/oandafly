from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from oandafly.domain.errors import Conflict, ValidationFailure
from tests.support import supported_database


def test_transaction_rolls_back_all_domain_changes(tmp_path: Path) -> None:
    db = supported_database(tmp_path / "store.db")
    db.initialize("CREATE TABLE facts (id TEXT PRIMARY KEY, amount TEXT NOT NULL);")
    with pytest.raises(RuntimeError):
        with db.transaction() as connection:
            connection.execute("INSERT INTO facts VALUES('a','0.100000000000000001')")
            raise RuntimeError("injected power loss before commit")
    with db.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
    db.close()


def test_nested_transaction_cannot_commit_outer_work(tmp_path: Path) -> None:
    db = supported_database(tmp_path / "store.db")
    with pytest.raises(Conflict):
        with db.transaction() as connection:
            connection.execute("INSERT INTO application_meta VALUES('uncommitted','1')")
            with db.transaction():
                pytest.fail("nested transaction must be rejected")
    with db.read() as connection:
        assert connection.execute("SELECT * FROM application_meta WHERE key='uncommitted'").fetchall() == []
    db.close()


def test_schema_failure_rolls_back_prior_ddl(tmp_path: Path) -> None:
    db = supported_database(tmp_path / "store.db")
    with pytest.raises(sqlite3.OperationalError):
        db.initialize("CREATE TABLE should_not_exist (id INTEGER); CREATE BROKEN SYNTAX;")
    with db.read() as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='should_not_exist'").fetchone() is None
    db.close()


def test_verified_backup_includes_committed_wal_without_resetting_state(tmp_path: Path) -> None:
    db = supported_database(tmp_path / "store.db")
    with db.transaction() as connection:
        connection.execute("INSERT INTO application_meta VALUES('unknown_dispatch','intent-123')")
    destination = tmp_path / "backups" / "verified.sqlite3"
    manifest = db.backup(destination)
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == manifest["sha256"]
    assert json.loads(destination.with_suffix(".sqlite3.manifest.json").read_text()) == manifest
    with closing(sqlite3.connect(destination)) as backup:
        assert backup.execute("SELECT value FROM application_meta WHERE key='unknown_dispatch'").fetchone()[0] == "intent-123"
    with pytest.raises(FileExistsError):
        db.backup(destination)
    db.close()


@pytest.mark.posix_only
@pytest.mark.skipif(os.name == "nt", reason="POSIX filesystem identity test")
def test_hard_linked_database_is_rejected_before_permission_changes(tmp_path: Path) -> None:
    original = tmp_path / "original.db"
    db = supported_database(original)
    db.close()
    alias = tmp_path / "aliased.db"
    os.link(original, alias)
    with pytest.raises(ValidationFailure, match="hard links"):
        supported_database(alias)


@pytest.mark.posix_only
@pytest.mark.skipif(os.name == "nt", reason="POSIX journal ownership test")
def test_orphan_journal_cannot_create_empty_successful_store(tmp_path: Path) -> None:
    path = tmp_path / "lost.db"
    orphan = Path(str(path) + "-wal")
    orphan.write_bytes(b"unreconciled")
    orphan.chmod(0o600)
    with pytest.raises(ValidationFailure, match="Orphaned"):
        supported_database(path)
    assert not path.exists()


def test_sqlite_future_schema_is_not_silently_downgraded(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    db = supported_database(path)
    with db.transaction() as connection:
        connection.execute("PRAGMA user_version=999")
    db.close()
    with pytest.raises(ValidationFailure, match="schema"):
        supported_database(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 999
