from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from decimal import ROUND_UP, Decimal, Inexact, localcontext
from typing import Any, Literal

import pytest
from pydantic import ValidationError

from oandafly.domain.errors import Conflict, ValidationFailure
from oandafly.domain.financial import FINANCIAL
from oandafly.domain.primitives import StrictModel
from oandafly.execution.risk import MarginState, spendable_margin
from tests.support import supported_database
from oandafly.storage.migrations import MIGRATIONS, SCHEMA_VERSION, migrate
from oandafly.storage.ownership import StoreOwnerLock


class Example(StrictModel):
    name: str
    enabled: bool
    live: Literal[False] = False
    amount: Decimal
    time: datetime
    body: dict[str, Any]


def sample(**changes):
    values = dict(name="scope", enabled=True, amount=Decimal("1.0001"),
                  time=datetime(2026, 1, 1, tzinfo=timezone.utc), body={"nested": [{"gate": True}]})
    values.update(changes)
    return values


@pytest.mark.parametrize("change", [{"name": 1}, {"name": True}, {"enabled": 1}, {"live": 0},
                                     {"amount": "1.2"}, {"amount": 1}, {"amount": 1.2}])
def test_domain_constructor_rejects_scalar_coercion(change):
    with pytest.raises(ValidationError):
        Example(**sample(**change))


def test_wire_decoding_and_nested_detachment_are_separate():
    inputs = sample()
    model = Example(**inputs)
    inputs["body"]["nested"][0]["gate"] = False
    assert model.body["nested"][0]["gate"] is True
    with pytest.raises(TypeError):
        model.body["nested"][0]["gate"] = False
    with pytest.raises(TypeError):
        model.body.update({"extra": 1})
    wire = model.model_dump(mode="json")
    assert Example.from_wire(wire) == model
    wire["enabled"] = 1
    with pytest.raises(ValidationError):
        Example.from_wire(wire)
    with pytest.raises(ValidationError):
        Example.model_validate(model.model_copy(update={"enabled": 1}))


def test_financial_context_ignores_ambient_precision_rounding_exponent_and_traps():
    state = MarginState(native_margin_available=Decimal("100.009"), stressed_equity=Decimal("999"),
                        independent_margin_requirement=Decimal("0"), gates={"scope": True})
    with localcontext() as ambient:
        ambient.prec, ambient.rounding, ambient.Emax, ambient.Emin = 3, ROUND_UP, 2, -2
        ambient.traps[Inexact] = True
        result = spendable_margin(state)
        assert result.spendable == Decimal("100.009")
        with FINANCIAL.local() as first:
            first.prec = 1
        with FINANCIAL.local() as second:
            assert second.prec == 1024 and not second.flags[Inexact]
        assert ambient.prec == 3 and ambient.traps[Inexact]
    forged = state.model_copy(update={"gates": {"scope": 1}})
    with pytest.raises(ValidationError):
        spendable_margin(forged)


def test_store_lock_precedes_any_sqlite_connection(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    lock = StoreOwnerLock(path)
    try:
        monkeypatch.setattr(sqlite3, "connect", lambda *_a, **_k: pytest.fail("SQLite opened before ownership"))
        with pytest.raises(Conflict):
            supported_database(path)
        assert not path.exists()
    finally:
        lock.close()


def test_native_account_owner_cannot_fork_a_second_authority(tmp_path, monkeypatch):
    from oandafly.domain.context import AuthorityCoordinator
    locks = tmp_path / "account-locks"
    identity = "native-SANDBOX-001-001-1234567-001"
    owner = AuthorityCoordinator.acquire_ownership(locks, identity, tmp_path / "one.sqlite3")
    try:
        monkeypatch.setattr(sqlite3, "connect", lambda *_a, **_k: pytest.fail("SQLite must not open before ownership"))
        with pytest.raises(Conflict):
            AuthorityCoordinator.acquire_ownership(locks, identity, tmp_path / "two.sqlite3")
    finally:
        owner.close()


@pytest.mark.parametrize("tamper", ["UPDATE schema_migrations SET checksum='bad' WHERE version=1",
                                  "UPDATE schema_compatibility SET minimum_writer=999",
                                  "UPDATE schema_compatibility SET minimum_reader=999",
                                  "PRAGMA user_version=999"])
def test_migration_history_and_compatibility_fail_closed(tmp_path, tamper):
    path = tmp_path / "state.sqlite3"
    db = supported_database(path)
    with db.transaction() as connection:
        connection.execute(tamper)
    db.close()
    with pytest.raises(ValidationFailure):
        supported_database(path)


def test_migration_transaction_rolls_back_including_version(tmp_path, monkeypatch):
    from oandafly.storage import migrations
    from oandafly.storage.migrations import Migration

    connection = sqlite3.connect(tmp_path / "rollback.sqlite3", isolation_level=None)
    monkeypatch.setattr(migrations, "MIGRATIONS", (*MIGRATIONS, Migration(2, (
        "CREATE TABLE should_rollback (id INTEGER)", "INVALID SQL"))))
    try:
        with pytest.raises(sqlite3.OperationalError):
            migrate(connection)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        connection.close()


def test_component_changes_require_versioned_upgrade_and_pragmas_are_verified(tmp_path):
    db = supported_database(tmp_path / "state.sqlite3")
    try:
        db.initialize("CREATE TABLE example(id INTEGER);", component="example")
        with pytest.raises(ValidationFailure, match="numbered migration"):
            db.initialize("CREATE TABLE example(id TEXT);", component="example")
        with db.read() as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        db._connection.execute("PRAGMA synchronous=OFF")
        with pytest.raises(ValidationFailure, match="synchronous"):
            db._verify_pragmas()
    finally:
        db.close()


@pytest.mark.parametrize("version", [100, 101, 102, 103])
def test_pre_release_stores_are_rejected_without_mutation(tmp_path, version):
    path = tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE retained(value TEXT)")
        connection.execute("INSERT INTO retained VALUES('unchanged')")
        connection.execute(f"PRAGMA user_version={version}")
    path.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(ValidationFailure, match="Unsupported database schema"):
        supported_database(path)
    assert path.read_bytes() == before


def test_mapping_cannot_be_mutated_via_dict_base_and_dump_is_detached():
    model = Example(**sample())
    nested = model.body["nested"][0]
    with pytest.raises(TypeError):
        dict.__setitem__(nested, "gate", False)
    with pytest.raises(TypeError):
        dict.update(model.body, {"nested": []})
    with pytest.raises(TypeError):
        nested.__init__({"gate": False})
    with pytest.raises(TypeError):
        nested._values["gate"] = False
    dumped = model.model_dump(mode="json", exclude={"name"})
    assert "name" not in dumped
    dumped["body"]["nested"][0]["gate"] = False
    assert model.body["nested"][0]["gate"] is True
    assert Example.model_validate(model.model_copy(deep=True)) == model


def test_unavailable_wal_aborts_startup_and_releases_store_owner(tmp_path, monkeypatch):
    real_connect = sqlite3.connect

    class NoWAL(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "PRAGMA journal_mode=WAL":
                return super().execute("PRAGMA journal_mode=DELETE")
            return super().execute(sql, parameters)

    path = tmp_path / "no-wal.sqlite3"
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", lambda *a, **kw: real_connect(*a, **kw, factory=NoWAL))
        with pytest.raises(ValidationFailure, match="journal_mode"):
            supported_database(path)
    # Ownership from the failed boot was released and no migration ran.
    with closing(real_connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    repaired = supported_database(path)
    repaired.close()
