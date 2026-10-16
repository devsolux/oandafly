"""Authority and policy faults using explicitly synthetic account evidence."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from oandafly.domain.context import (
    AccountStopEvidence,
    AuthorityCoordinator,
    ContextKey,
    EngineConfig,
    NativeStopProofVerifier,
    PositionUnits,
    ReadinessProof,
    StopProof,
    TradeUnits,
    VerifiedAccountProfile,
)
from oandafly.domain.errors import Conflict, SafetyFence, ValidationFailure
from oandafly.domain.instruments import CurrencyInstrument, currency_code, currency_pair, is_regulatory_major
from oandafly.domain.policy import LeverageRule, PaperProfileVerifier
from oandafly.domain.primitives import utc_now
from tests.support import supported_database
from oandafly.storage.ledger import Ledger


class FixtureBarrier:
    blocked = False

    def has_unresolved_in(self, connection: sqlite3.Connection, authority_id: str) -> bool:
        assert connection.in_transaction
        assert authority_id == "test-authority"
        return self.blocked


class FixtureStopVerifier:
    """No broker is present in lifecycle fixtures; native integration tests omit this."""

    def verify_stop_in(self, connection: sqlite3.Connection, source: ContextKey, proof: StopProof) -> None:
        assert connection.in_transaction
        assert proof.source_context_sha256 == source.fingerprint


def paper_profile(**changes: object) -> VerifiedAccountProfile:
    now = utc_now()
    data: dict[str, object] = {
        "profile_id": "paper-v1", "version": 1, "authority_id": "test-authority",
        "environment": "SANDBOX", "account_ids": ("SYNTHETIC:PAPER:one",),
        "home_currency": "EUR", "broker_entity": "SYNTHETIC PAPER",
        "legal_product": "SYNTHETIC_FX", "region": "DE", "negative_balance_protection": True,
        "api_binding": "PAPER", "instrument_rules": (
            LeverageRule(instrument=CurrencyInstrument(name="EUR_USD"), maximum_leverage=Decimal("20")),
        ),
        "verification_issuer": "oandafly.synthetic.paper.v1", "evidence_sha256": "a" * 64,
        "verified_at": now - timedelta(minutes=1), "expires_at": now + timedelta(days=1),
    }
    data.update(changes)
    return VerifiedAccountProfile.model_validate(data)


class FixtureDrain:
    def __init__(self, coordinator: AuthorityCoordinator) -> None:
        self.coordinator = coordinator
        self.fail_at: str | None = None
        self.proof_change: dict[str, object] = {}
        self.account_change: dict[str, object] = {}
        self.calls: list[str] = []

    def quiesce(self, source: ContextKey) -> None:
        self.calls.append("quiesce")
        assert self.coordinator.snapshot().state == "QUIESCING"
        if self.fail_at == "quiesce":
            raise SafetyFence("fixture producer did not stop")

    def drain(self, source: ContextKey) -> None:
        self.calls.append("drain")
        assert self.coordinator.snapshot().state == "DRAINING"
        if self.fail_at == "drain":
            raise SafetyFence("fixture close outcome is unknown")

    def reconcile(self, source: ContextKey) -> StopProof:
        self.calls.append("reconcile")
        assert self.coordinator.snapshot().state == "RECONCILING"
        if self.fail_at == "reconcile":
            raise SafetyFence("fixture REST coverage incomplete")
        accounts = []
        if source.product_track == "TRADING":
            profile = self.coordinator.profile(source)
            for account_id in profile.account_ids:
                account: dict[str, object] = {
                    "account_id": account_id, "environment": profile.environment,
                    "snapshot_transaction_id": "701", "covered_transaction_id": "709",
                    "observed_at": utc_now(), "financing_finalized": True,
                    "financing_evidence_sha256": "b" * 64, "source_snapshot_sha256": "c" * 64,
                }
                account.update(self.account_change)
                accounts.append(AccountStopEvidence.model_validate(account))
        data: dict[str, object] = {
            "source_context_sha256": source.fingerprint, "observed_at": utc_now(),
            "complete_account_universe": True, "accounts": tuple(accounts),
        }
        data.update(self.proof_change)
        return StopProof.model_validate(data)


class FixtureInitializer:
    def __init__(self, coordinator: AuthorityCoordinator) -> None:
        self.coordinator = coordinator
        self.calls: list[str] = []
        self.fail = False
        self.late_fence = False

    def reset_transients(self, source: ContextKey, target: ContextKey) -> None:
        self.calls.append("reset")
        state = self.coordinator.snapshot()
        assert state.state == "RESETTING"
        assert state.lease_status == "RETIRED"
        assert state.lease_epoch is None
        assert not state.armed

    def initialize(self, target: ContextKey, profile: VerifiedAccountProfile | None) -> None:
        self.calls.append("initialize")
        snapshot = self.coordinator.snapshot()
        assert snapshot.state == "INITIALIZING_NEW"
        assert snapshot.lease_status == "RETIRED"
        if self.late_fence:
            self.coordinator.add_fence("LATE_SOURCE_FACT", "fixture late source mutation", snapshot.context)
        if self.fail:
            raise SafetyFence("No trusted full MaleCNS graph is installed")


class FixtureReadiness:
    def verify(self, context: ContextKey, profile: VerifiedAccountProfile) -> ReadinessProof:
        return ReadinessProof(
            context_sha256=context.fingerprint, profile_version=profile.version,
            observed_at=utc_now(), evidence_sha256="d" * 64,
            structural_gate_verified=True, account_reconciled=True,
        )


@pytest.fixture
def core(tmp_path: Path):  # type: ignore[no-untyped-def]
    db = supported_database(tmp_path / "state.sqlite3")
    barrier = FixtureBarrier()
    coordinator = AuthorityCoordinator(db, "test-authority", tmp_path / "locks", barrier=barrier, stop_verifier=FixtureStopVerifier())
    coordinator.register_profile(paper_profile(), PaperProfileVerifier())
    try:
        yield db, coordinator, barrier
    finally:
        coordinator.close()
        db.close()


def enter_trading(coordinator: AuthorityCoordinator) -> ContextKey:
    state = coordinator.snapshot()
    target = ContextKey(authority_id="test-authority", product_track="TRADING", account_profile_id="paper-v1", epoch=state.context.epoch + 1)
    transition = coordinator.request_transition(target, state.revision, str(uuid4()))
    result = coordinator.run_transition(transition.transition_id, FixtureDrain(coordinator), FixtureInitializer(coordinator))
    assert result.state == "READY_UNARMED"
    return target


def arm(coordinator: AuthorityCoordinator) -> None:
    coordinator.arm(coordinator.snapshot().revision, str(uuid4()), FixtureReadiness(), utc_now() + timedelta(seconds=30))


@pytest.mark.parametrize("name", ["XAU_USD", "XAG_USD", "BTC_USD", "ETH_USD", "USDT_USD", "USD_USD", "EUR/USD", "eur_usd", "AAA_USD"])
def test_non_currency_and_malformed_pairs_rejected(name: str) -> None:
    with pytest.raises((ValueError, ValidationFailure)):
        CurrencyInstrument(name=name)


@pytest.mark.parametrize("asset_type", ["CFD", "METAL", "COMMODITY", "CRYPTO"])
def test_native_classification_fenced(asset_type: str) -> None:
    with pytest.raises(ValidationError):
        CurrencyInstrument.model_validate({"name": "EUR_USD", "instrument_type": asset_type})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0.05, Decimal("NaN"), Decimal("Infinity"), "Infinity"])
def test_financial_models_reject_approximate_or_nonfinite_values(value: object) -> None:
    with pytest.raises((ValueError, ValidationFailure)):
        CurrencyInstrument.model_validate({"name": "EUR_USD", "margin_rate": value})


def test_regulatory_major_definition_and_broker_minimum() -> None:
    assert is_regulatory_major("EUR_CHF")
    assert not is_regulatory_major("AUD_USD")
    assert not is_regulatory_major("NZD_USD")
    major = CurrencyInstrument(name="EUR_USD", margin_rate=Decimal("0.03"))
    LeverageRule(instrument=major, maximum_leverage=Decimal("30"))
    with pytest.raises(ValidationError):
        LeverageRule(instrument=major, maximum_leverage=Decimal("30.00000000001"))
    with pytest.raises(ValidationError):
        LeverageRule(instrument=CurrencyInstrument(name="AUD_USD", margin_rate=Decimal("0.01")), maximum_leverage=Decimal("20.00001"))
    with pytest.raises(ValidationError):
        LeverageRule(instrument=CurrencyInstrument(name="EUR_USD", margin_rate=Decimal("0.05")), maximum_leverage=Decimal("30"))
    with pytest.raises(ValidationError):
        LeverageRule(instrument=CurrencyInstrument(name="EUR_USD", margin_rate=Decimal("0.05000000000000000000000000000000001")), maximum_leverage=Decimal("20"))


@pytest.mark.parametrize("entity", ["OANDA TMS BROKERS S.A.", "OANDA Global Markets", "OANDA_GLOBAL_MARKETS"])
def test_incompatible_entities_rejected(entity: str) -> None:
    with pytest.raises(ValidationError):
        paper_profile(broker_entity=entity, api_binding="V20", legal_product="NON_CFD_FX", region="GLOBAL")


def test_currency_classification_cannot_establish_legal_eligibility() -> None:
    # A profile is data, not approval. Native eligibility is checked by the
    # independent verifier and cannot be inferred from the instrument type.
    native = paper_profile(broker_entity="OANDA EUROPE", api_binding="V20", legal_product="NON_CFD_FX")
    with pytest.raises(ValueError):
        PaperProfileVerifier().verify(native)
    with pytest.raises(ValidationError):
        paper_profile(legal_product="CFD")
    with pytest.raises(ValidationError):
        paper_profile(negative_balance_protection=False)
    with pytest.raises(ValidationError):
        paper_profile(account_ids=("SYNTHETIC:PAPER:one", "SYNTHETIC:PAPER:one"))
    for value in ("true", "false", 1, 0):
        with pytest.raises(ValidationError):
            paper_profile(negative_balance_protection=value)


def test_config_identity_defaults_and_closed_schema() -> None:
    config = EngineConfig()
    assert config.application_identifier == "com.devsolux.oandafly"
    assert config.product_track == "DATA" and config.runtime_mode == "PAPER"
    for changes in ({"application_version": "2.0.0"}, {"auto_arm": True}, {"live_execution_enabled": True}, {"environment": "LIVE"}, {"secret": "unexpected"}):
        with pytest.raises(ValidationError):
            EngineConfig.model_validate(changes)


def test_currency_helpers_and_native_precision() -> None:
    assert currency_code("EUR") == "EUR"
    assert currency_pair("USD_JPY") == "USD_JPY"
    instrument = CurrencyInstrument(name="USD_JPY", pip_location=-2)
    assert instrument.pip_size == Decimal("0.01")
    with pytest.raises(ValidationError):
        CurrencyInstrument(name="EUR_USD", minimum_trade_size=Decimal("0.1"), trade_units_precision=0)


def test_source_lease_retires_before_target_resources_and_never_autoarms(core):  # type: ignore[no-untyped-def]
    db, coordinator, _ = core
    context = enter_trading(coordinator)
    state = coordinator.snapshot()
    assert state.context == context and state.lease_status == "ACTIVE" and not state.armed
    with db.transaction() as connection:
        with pytest.raises(SafetyFence):
            coordinator.assert_mutation(connection, context, 1)
    arm(coordinator)
    with db.transaction() as connection:
        assert coordinator.assert_mutation(connection, context, 1) == coordinator._row(connection)["revision"]
    target = ContextKey(authority_id="test-authority", epoch=2)
    transition = coordinator.request_transition(target, coordinator.snapshot().revision, str(uuid4()))
    with db.transaction() as connection:
        with pytest.raises(SafetyFence):
            coordinator.assert_mutation(connection, context, 1, "ENTRY")
        coordinator.assert_mutation(connection, context, 1, "RECOVERY")
    result = coordinator.run_transition(transition.transition_id, FixtureDrain(coordinator), FixtureInitializer(coordinator))
    assert result.state == "READY_UNARMED"
    assert coordinator.snapshot().lease_status == "NONE"
    with db.transaction() as connection:
        with pytest.raises(SafetyFence):
            coordinator.assert_mutation(connection, context, 1, "RECOVERY")
        events = [row[0] for row in connection.execute("SELECT event FROM authority_audit WHERE json_extract(body,'$.transition_id')=? ORDER BY id", (transition.transition_id,))]
    assert events == ["REQUESTED", "QUIESCING", "DRAINING", "RECONCILING", "SOURCE_LEASE_RETIRED", "RESETTING", "INITIALIZING_NEW", "TARGET_READY_UNARMED"]


def _trading_to_trading_handover(db, coordinator: AuthorityCoordinator) -> Ledger:  # type: ignore[no-untyped-def]
    enter_trading(coordinator)
    ledger = Ledger(db)
    ledger.establish_baseline("SANDBOX", "SYNTHETIC:PAPER:one", "EUR", Decimal("10000"), "701", utc_now())
    coordinator.stop_verifier = NativeStopProofVerifier()
    target = ContextKey(authority_id="test-authority", product_track="TRADING", account_profile_id="paper-v1",
                        epoch=coordinator.snapshot().context.epoch + 1)
    transition = coordinator.request_transition(target, coordinator.snapshot().revision, str(uuid4()))
    drain = FixtureDrain(coordinator)
    drain.account_change = {"covered_transaction_id": "701"}
    assert coordinator.run_transition(transition.transition_id, drain, FixtureInitializer(coordinator)).state == "READY_UNARMED"
    return ledger


LATE_FINANCING = {"id": "704", "type": "DAILY_FINANCING", "financing": "-0.25", "accountBalance": "9999.75"}


def test_first_arm_after_trading_handover_still_rejects_late_source_facts(core):  # type: ignore[no-untyped-def]
    db, coordinator, _ = core
    ledger = _trading_to_trading_handover(db, coordinator)
    ledger.ingest("SANDBOX", "SYNTHETIC:PAPER:one", LATE_FINANCING)
    with pytest.raises(SafetyFence, match="later native fact"):
        arm(coordinator)
    assert not coordinator.snapshot().armed


def test_rearm_after_trading_handover_does_not_reverify_point_in_time_retirement(core):  # type: ignore[no-untyped-def]
    db, coordinator, _ = core
    ledger = _trading_to_trading_handover(db, coordinator)
    arm(coordinator)
    ledger.ingest("SANDBOX", "SYNTHETIC:PAPER:one", LATE_FINANCING)  # the target's own account activity
    coordinator.disarm()
    arm(coordinator)
    assert coordinator.snapshot().armed


@pytest.mark.parametrize("stage", ["quiesce", "drain", "reconcile"])
def test_each_drain_failure_prevents_target_initialization_and_retirement(core, stage: str):  # type: ignore[no-untyped-def]
    _, coordinator, _ = core
    enter_trading(coordinator)
    transition = coordinator.request_transition(ContextKey(authority_id="test-authority", epoch=2), coordinator.snapshot().revision, str(uuid4()))
    drain, initializer = FixtureDrain(coordinator), FixtureInitializer(coordinator)
    drain.fail_at = stage
    assert coordinator.run_transition(transition.transition_id, drain, initializer).state == "BLOCKED_DRAIN"
    assert initializer.calls == []
    assert coordinator.snapshot().lease_status == "ACTIVE"
    drain.fail_at = None
    assert coordinator.run_transition(transition.transition_id, drain, initializer).state == "READY_UNARMED"


@pytest.mark.parametrize("change", [
    {"trades": (TradeUnits(trade_id="7", current_units=Decimal("1")),)},
    {"positions": (PositionUnits(instrument="EUR_USD", long_units=Decimal("100"), short_units=Decimal("-100")),)},
    {"pending_order_ids": ("123",)}, {"unknown_intent_ids": ("unknown",)},
    {"financing_finalized": False}, {"covered_transaction_id": "700"},
    {"account_id": "wrong-account"}, {"environment": "LIVE"},
    {"observed_at": utc_now() - timedelta(days=1)},
])
def test_gross_flat_history_and_financing_proof_faults_block_handover(core, change: dict[str, object]):  # type: ignore[no-untyped-def]
    _, coordinator, _ = core
    enter_trading(coordinator)
    transition = coordinator.request_transition(ContextKey(authority_id="test-authority", epoch=2), coordinator.snapshot().revision, str(uuid4()))
    drain, initializer = FixtureDrain(coordinator), FixtureInitializer(coordinator)
    drain.account_change = change
    result = coordinator.run_transition(transition.transition_id, drain, initializer)
    assert result.state == "BLOCKED_DRAIN" and not initializer.calls
    assert coordinator.snapshot().lease_status == "ACTIVE"


@pytest.mark.parametrize("change", [
    {"source_context_sha256": "f" * 64}, {"complete_account_universe": False},
    {"accounts": ()}, {"observed_at": utc_now() - timedelta(days=1)},
    {"observed_at": utc_now() + timedelta(days=1)},
])
def test_scope_and_freshness_faults_block_handover(core, change: dict[str, object]):  # type: ignore[no-untyped-def]
    _, coordinator, _ = core
    enter_trading(coordinator)
    transition = coordinator.request_transition(ContextKey(authority_id="test-authority", epoch=2), coordinator.snapshot().revision, str(uuid4()))
    drain = FixtureDrain(coordinator)
    drain.proof_change = change
    assert coordinator.run_transition(transition.transition_id, drain, FixtureInitializer(coordinator)).state == "BLOCKED_DRAIN"


def test_unknown_outbox_barrier_blocks_then_reconciliation_allows(core):  # type: ignore[no-untyped-def]
    _, coordinator, barrier = core
    enter_trading(coordinator)
    barrier.blocked = True
    transition = coordinator.request_transition(ContextKey(authority_id="test-authority", epoch=2), coordinator.snapshot().revision, str(uuid4()))
    initializer = FixtureInitializer(coordinator)
    assert coordinator.run_transition(transition.transition_id, FixtureDrain(coordinator), initializer).state == "BLOCKED_DRAIN"
    assert initializer.calls == []
    barrier.blocked = False
    assert coordinator.run_transition(transition.transition_id, FixtureDrain(coordinator), initializer).state == "READY_UNARMED"


def test_failed_initialization_keeps_source_retired_and_can_revise_to_data(core):  # type: ignore[no-untyped-def]
    _, coordinator, _ = core
    target = ContextKey(authority_id="test-authority", product_track="TRADING", account_profile_id="paper-v1", epoch=1)
    transition = coordinator.request_transition(target, coordinator.snapshot().revision, str(uuid4()))
    initializer = FixtureInitializer(coordinator)
    initializer.fail = True
    result = coordinator.run_transition(transition.transition_id, FixtureDrain(coordinator), initializer)
    assert result.state == "FAILED_INITIALIZATION"
    assert coordinator.snapshot().lease_status == "RETIRED"
    assert coordinator.snapshot().context.product_track == "DATA"
    data_target = ContextKey(authority_id="test-authority", epoch=1)
    coordinator.revise_failed_target(transition.transition_id, data_target, coordinator.snapshot().revision)
    initializer.fail = False
    assert coordinator.run_transition(transition.transition_id, FixtureDrain(coordinator), initializer).state == "READY_UNARMED"
    assert coordinator.snapshot().context == data_target


def test_late_source_fence_prevents_target_activation(core):  # type: ignore[no-untyped-def]
    _, coordinator, _ = core
    target = ContextKey(authority_id="test-authority", product_track="TRADING", account_profile_id="paper-v1", epoch=1)
    transition = coordinator.request_transition(target, coordinator.snapshot().revision, str(uuid4()))
    initializer = FixtureInitializer(coordinator)
    initializer.late_fence = True
    assert coordinator.run_transition(transition.transition_id, FixtureDrain(coordinator), initializer).state == "FAILED_INITIALIZATION"
    assert coordinator.snapshot().lease_status == "RETIRED"


def test_context_request_is_idempotent_but_uuid_payload_reuse_is_rejected(core):  # type: ignore[no-untyped-def]
    _, coordinator, _ = core
    state = coordinator.snapshot()
    target = ContextKey(authority_id="test-authority", epoch=1)
    command = str(uuid4())
    first = coordinator.request_transition(target, state.revision, command)
    assert coordinator.request_transition(target, state.revision, command) == first
    with pytest.raises(Conflict):
        coordinator.request_transition(target, state.revision + 1, command)
    with pytest.raises(Conflict):
        coordinator.request_transition(target, state.revision, str(uuid4()))


def test_epoch_authority_and_profile_fencing(core):  # type: ignore[no-untyped-def]
    db, coordinator, _ = core
    for target in (ContextKey(authority_id="different", epoch=1), ContextKey(authority_id="test-authority", epoch=2)):
        with pytest.raises(ValidationFailure):
            coordinator.request_transition(target, coordinator.snapshot().revision, str(uuid4()))
    context = enter_trading(coordinator)
    arm(coordinator)
    with db.transaction() as connection:
        with pytest.raises(SafetyFence):
            coordinator.assert_mutation(connection, context, 2)
        with pytest.raises(SafetyFence):
            coordinator.assert_mutation(connection, context.model_copy(update={"epoch": 2}), 1)


def test_missing_outbox_barrier_fails_closed_for_trading_arm(core):  # type: ignore[no-untyped-def]
    _, coordinator, _ = core
    enter_trading(coordinator)
    coordinator.barrier = None
    with pytest.raises(SafetyFence, match="outbox barrier"):
        arm(coordinator)


def test_arm_deadline_and_revision_races_never_commit(core):  # type: ignore[no-untyped-def]
    _, coordinator, _ = core
    enter_trading(coordinator)
    with pytest.raises(SafetyFence):
        coordinator.arm(coordinator.snapshot().revision, str(uuid4()), FixtureReadiness(), utc_now() - timedelta(seconds=1))

    class ChangingReadiness(FixtureReadiness):
        def verify(self, context: ContextKey, profile: VerifiedAccountProfile) -> ReadinessProof:
            coordinator.disarm("concurrent command")
            return super().verify(context, profile)

    with pytest.raises(SafetyFence):
        coordinator.arm(coordinator.snapshot().revision, str(uuid4()), ChangingReadiness(), utc_now() + timedelta(seconds=30))
    assert not coordinator.snapshot().armed


def test_fence_resolves_only_exact_origin_and_never_rearms(core):  # type: ignore[no-untyped-def]
    _, coordinator, _ = core
    context = enter_trading(coordinator)
    arm(coordinator)
    fence = coordinator.add_fence("MANUAL_ACTIVITY", "fixture external order", context)
    assert not coordinator.snapshot().armed
    with pytest.raises(SafetyFence):
        arm(coordinator)
    with pytest.raises(SafetyFence):
        coordinator.resolve_fence(fence, "b" * 64, context=context.model_copy(update={"epoch": 99}))
    coordinator.resolve_fence(fence, "b" * 64, context=context)
    assert not coordinator.snapshot().armed
    arm(coordinator)
    assert coordinator.snapshot().armed


def test_profile_versions_are_immutable_and_active_cannot_change(core):  # type: ignore[no-untyped-def]
    _, coordinator, _ = core
    with pytest.raises(Conflict):
        coordinator.register_profile(paper_profile(evidence_sha256="e" * 64), PaperProfileVerifier())
    enter_trading(coordinator)
    with pytest.raises(Conflict):
        coordinator.register_profile(paper_profile(version=2), PaperProfileVerifier())


def test_restart_disarms_and_requires_a_new_proven_recovery_handover(tmp_path: Path) -> None:
    db = supported_database(tmp_path / "state.sqlite3")
    locks = tmp_path / "locks"
    first = AuthorityCoordinator(db, "test-authority", locks, barrier=FixtureBarrier(), stop_verifier=FixtureStopVerifier())
    first.register_profile(paper_profile(), PaperProfileVerifier())
    context = enter_trading(first)
    arm(first)
    first.close()
    second = AuthorityCoordinator(db, "test-authority", locks, barrier=FixtureBarrier(), stop_verifier=FixtureStopVerifier())
    try:
        assert second.snapshot().state == "RECOVERY_REQUIRED"
        assert second.snapshot().lease_status == "RECOVERY" and not second.snapshot().armed
        with db.transaction() as connection:
            with pytest.raises(SafetyFence):
                second.assert_mutation(connection, context, 1)
            second.assert_mutation(connection, context, 1, "RECOVERY")
        transition = second.request_transition(ContextKey(authority_id="test-authority", epoch=2), second.snapshot().revision, str(uuid4()))
        assert second.run_transition(transition.transition_id, FixtureDrain(second), FixtureInitializer(second)).state == "READY_UNARMED"
    finally:
        second.close()
        db.close()


def test_same_process_and_real_second_process_cannot_steal_authority(core, tmp_path: Path):  # type: ignore[no-untyped-def]
    db, coordinator, _ = core
    with pytest.raises(Conflict):
        AuthorityCoordinator(db, "test-authority", tmp_path / "locks")
    code = """
import sys
from pathlib import Path
from oandafly.storage.database import Database
from tests.support import supported_database
from oandafly.domain.context import AuthorityCoordinator
from oandafly.domain.errors import Conflict
db = None
try:
    db = supported_database(Path(sys.argv[1]))
    AuthorityCoordinator(db, 'test-authority', Path(sys.argv[2]))
except Conflict:
    print('blocked')
else:
    raise RuntimeError('duplicate authority owner admitted')
finally:
    if db is not None:
        db.close()
"""
    completed = subprocess.run([sys.executable, "-c", code, str(db.path), str(tmp_path / "locks")],
                               env={"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
                               text=True, capture_output=True, timeout=15, check=True)
    assert completed.stdout.strip() == "blocked"
    assert coordinator.snapshot().context.product_track == "DATA"


def test_authority_store_binding_cannot_be_repointed_after_close(tmp_path: Path) -> None:
    first_db, second_db = supported_database(tmp_path / "one.sqlite3"), supported_database(tmp_path / "two.sqlite3")
    first = AuthorityCoordinator(first_db, "test-authority", tmp_path / "locks")
    first.close()
    try:
        with pytest.raises(SafetyFence, match="another canonical state store"):
            AuthorityCoordinator(second_db, "test-authority", tmp_path / "locks")
    finally:
        first_db.close()
        second_db.close()


def test_real_process_death_preserves_source_lease_and_reopens_recovery_only(tmp_path: Path) -> None:
    import sqlite3
    if sqlite3.sqlite_version_info < (3, 51, 3):
        pytest.skip("Production process-death recovery requires SQLite >=3.51.3")
    source_root = Path(__file__).resolve().parents[2]
    code = """
import os
import sys
from pathlib import Path
from oandafly.storage.database import Database
from tests.support import supported_database
from oandafly.domain.context import AuthorityCoordinator
from oandafly.domain.policy import PaperProfileVerifier
from test_context import FixtureBarrier, FixtureStopVerifier, arm, enter_trading, paper_profile
db = supported_database(Path(sys.argv[1]))
coordinator = AuthorityCoordinator(db, 'test-authority', Path(sys.argv[2]), barrier=FixtureBarrier(), stop_verifier=FixtureStopVerifier())
coordinator.register_profile(paper_profile(), PaperProfileVerifier())
enter_trading(coordinator)
arm(coordinator)
os._exit(23)
"""
    path, locks = tmp_path / "crashed.sqlite3", tmp_path / "locks"
    completed = subprocess.run(
        [sys.executable, "-c", code, str(path), str(locks)],
        env={"PYTHONPATH": str(source_root) + ":" + str(source_root / "src") + ":" + str(Path(__file__).parent)},
        text=True, capture_output=True, timeout=15, check=False,
    )
    assert completed.returncode == 23, completed.stderr
    db = supported_database(path)
    coordinator = AuthorityCoordinator(db, "test-authority", locks, barrier=FixtureBarrier(), stop_verifier=FixtureStopVerifier())
    try:
        state = coordinator.snapshot()
        assert state.context.product_track == "TRADING"
        assert state.context.epoch == 1 and state.lease_epoch == 1
        assert state.state == "RECOVERY_REQUIRED" and not state.armed
        with db.transaction() as connection:
            with pytest.raises(SafetyFence):
                coordinator.assert_mutation(connection, state.context, 1, "ENTRY")
            coordinator.assert_mutation(connection, state.context, 1, "RECOVERY")
        transition = coordinator.request_transition(ContextKey(authority_id="test-authority", epoch=2), state.revision, str(uuid4()))
        assert coordinator.run_transition(transition.transition_id, FixtureDrain(coordinator), FixtureInitializer(coordinator)).state == "READY_UNARMED"
    finally:
        coordinator.close()
        db.close()


def test_expired_command_waiting_for_sql_writer_never_requests_handover(core):  # type: ignore[no-untyped-def]
    db, coordinator, _ = core
    initial = coordinator.snapshot()
    started = threading.Event()

    def request() -> None:
        started.set()
        coordinator.request_transition(ContextKey(authority_id="test-authority", epoch=1), initial.revision,
                                       str(uuid4()), deadline=utc_now() + timedelta(milliseconds=20))

    with ThreadPoolExecutor(max_workers=1) as executor:
        with db.transaction():
            future = executor.submit(request)
            assert started.wait(timeout=1)
            time.sleep(0.04)
        with pytest.raises(SafetyFence, match="deadline"):
            future.result(timeout=2)
    assert coordinator.snapshot() == initial


def test_arm_deadline_expires_during_readiness_without_mutation(core):  # type: ignore[no-untyped-def]
    _, coordinator, _ = core
    enter_trading(coordinator)

    class SlowReadiness(FixtureReadiness):
        def verify(self, context: ContextKey, profile: VerifiedAccountProfile) -> ReadinessProof:
            time.sleep(0.04)
            return super().verify(context, profile)

    with pytest.raises(SafetyFence):
        coordinator.arm(coordinator.snapshot().revision, str(uuid4()), SlowReadiness(), utc_now() + timedelta(milliseconds=20))
    assert not coordinator.snapshot().armed


def test_native_fill_after_snapshot_invalidates_same_transaction_retirement(core):  # type: ignore[no-untyped-def]
    db, coordinator, _ = core
    enter_trading(coordinator)
    ledger = Ledger(db)
    ledger.establish_baseline("SANDBOX", "SYNTHETIC:PAPER:one", "EUR", Decimal("10000"), "701", utc_now())
    coordinator.stop_verifier = NativeStopProofVerifier()
    transition = coordinator.request_transition(ContextKey(authority_id="test-authority", epoch=2), coordinator.snapshot().revision, str(uuid4()))

    class LateFillDrain(FixtureDrain):
        def reconcile(self, source: ContextKey) -> StopProof:
            self.account_change = {"covered_transaction_id": "701"}
            proof = super().reconcile(source)
            ledger.ingest("SANDBOX", "SYNTHETIC:PAPER:one", {
                "id": "703", "type": "ORDER_FILL", "instrument": "EUR_USD",
                "pl": "0", "financing": "0", "units": "100", "price": "1.10020",
                "tradeOpened": {"tradeID": "703", "units": "100", "price": "1.10020"},
                "accountBalance": "10000",
            })
            return proof

    initializer = FixtureInitializer(coordinator)
    result = coordinator.run_transition(transition.transition_id, LateFillDrain(coordinator), initializer)
    assert result.state == "BLOCKED_DRAIN"
    assert not initializer.calls
    assert coordinator.snapshot().lease_status == "ACTIVE"
    assert ledger.balance("SANDBOX", "SYNTHETIC:PAPER:one").observed_head == "703"


def test_financing_after_retirement_prevents_target_lease_activation(core):  # type: ignore[no-untyped-def]
    db, coordinator, _ = core
    enter_trading(coordinator)
    ledger = Ledger(db)
    ledger.establish_baseline("SANDBOX", "SYNTHETIC:PAPER:one", "EUR", Decimal("10000"), "701", utc_now())
    coordinator.stop_verifier = NativeStopProofVerifier()
    transition = coordinator.request_transition(ContextKey(authority_id="test-authority", epoch=2), coordinator.snapshot().revision, str(uuid4()))
    drain = FixtureDrain(coordinator)
    drain.account_change = {"covered_transaction_id": "701"}

    class LateFinancingInitializer(FixtureInitializer):
        def initialize(self, target: ContextKey, profile: VerifiedAccountProfile | None) -> None:
            super().initialize(target, profile)
            ledger.ingest("SANDBOX", "SYNTHETIC:PAPER:one", {
                "id": "704", "type": "DAILY_FINANCING", "financing": "-0.25", "accountBalance": "9999.75",
            })

    result = coordinator.run_transition(transition.transition_id, drain, LateFinancingInitializer(coordinator))
    assert result.state == "FAILED_INITIALIZATION"
    assert coordinator.snapshot().lease_status == "RETIRED"
    assert coordinator.snapshot().context.product_track == "TRADING"
    assert ledger.balance("SANDBOX", "SYNTHETIC:PAPER:one").balance == Decimal("9999.75")


def test_native_stop_verifier_cannot_be_replaced_by_a_fresh_handwritten_flat_proof(core):  # type: ignore[no-untyped-def]
    _, coordinator, _ = core
    enter_trading(coordinator)
    coordinator.stop_verifier = NativeStopProofVerifier()
    transition = coordinator.request_transition(ContextKey(authority_id="test-authority", epoch=2), coordinator.snapshot().revision, str(uuid4()))
    result = coordinator.run_transition(transition.transition_id, FixtureDrain(coordinator), FixtureInitializer(coordinator))
    assert result.state == "BLOCKED_DRAIN"
    assert coordinator.snapshot().lease_status == "ACTIVE"
