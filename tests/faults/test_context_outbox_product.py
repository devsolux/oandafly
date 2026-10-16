"""Real SQLite, authority, ledger and PAPER transport fault injections.

Readiness below is an explicit test capability for synthetic execution. It is not
installed in the application and is not evidence of MaleCNS or broker qualification.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from oandafly.domain.context import (
    AccountStopEvidence,
    AuthorityCoordinator,
    ContextKey,
    ReadinessProof,
    StopProof,
)
from oandafly.domain.errors import Conflict, SafetyFence, TransportAmbiguous, ValidationFailure
from oandafly.domain.instruments import CurrencyInstrument
from oandafly.domain.policy import LeverageRule, PaperProfileVerifier, VerifiedAccountProfile
from oandafly.domain.primitives import utc_now
from oandafly.execution.outbox import DispatchState, OrderRequest, Outbox, OutboxStatus
from oandafly.execution.reconciler import NativeMutationGateway, OutboxReducer
from oandafly.execution.recovery import RecoveryRouter
from oandafly.execution.risk import (
    HomeConversions,
    InstrumentRisk,
    MarginState,
    MarginTier,
    RiskEvidence,
    RiskObservation,
    RiskRegistry,
)
from tests.support import supported_database
from oandafly.storage.ledger import Ledger
from oandafly.simulation.broker import ACCOUNT_ID, PaperBroker

D = Decimal


def test_invalid_fill_cannot_ack_or_reenter_dependency_replay(harness: Harness) -> None:
    # Quarantining an invalid TradeID must roll back all execution projections.
    record = harness.outbox.prepare(harness.request())
    with harness.db.transaction() as connection:
        connection.execute("UPDATE outbox SET dispatch_state='DISPATCH_STARTED' WHERE intent_id=?", (record.intent_id,))
    invalid = fill_fact(record)
    invalid["tradeOpened"] = {"tradeID": "invalid", "units": "100"}
    result = harness.reducer.ingest(invalid, environment="SANDBOX", account_id=ACCOUNT_ID)
    assert result.quarantined
    assert harness.outbox.get(record.intent_id).status == OutboxStatus.PENDING_SUBMISSION
    unrelated = {"id": "13", "type": "MARGIN_CALL_EXIT", "accountID": ACCOUNT_ID}
    assert not harness.reducer.ingest(unrelated, environment="SANDBOX", account_id=ACCOUNT_ID).quarantined
    assert harness.outbox.open_trades(environment="SANDBOX", account_id=ACCOUNT_ID) == ()


def test_filled_native_order_does_not_regress_on_late_cancel(harness: Harness) -> None:
    # Reordered terminal observations must preserve proven filled exposure.
    filled = {"id": "12", "type": "ORDER_FILL", "accountID": ACCOUNT_ID,
              "orderID": "11", "instrument": "EUR_USD", "units": "100", "pl": "0", "financing": "0",
              "tradeOpened": {"tradeID": "12", "units": "100"}}
    harness.reducer.ingest(filled, environment="SANDBOX", account_id=ACCOUNT_ID)
    harness.reducer.ingest({"id": "13", "type": "ORDER_CANCEL", "orderID": "11"},
                           environment="SANDBOX", account_id=ACCOUNT_ID)
    assert harness.cover_fixture_history()
    with harness.db.read() as connection:
        assert connection.execute("SELECT state FROM execution_orders WHERE order_id='11'").fetchone()[0] == "FILLED"


def test_account_cannot_spend_another_risk_observations_margin(harness: Harness) -> None:
    # Same authority/environment still cannot pool independent account budgets.
    poor = harness.observation.model_copy(update={"margin": harness.observation.margin.model_copy(
        update={"native_margin_available": D(0)})})
    rich = harness.observation.model_copy(update={"account_id": "SYNTHETIC:PAPER:OTHER"})
    harness.risk.publish(rich)
    harness.risk.publish(poor.model_copy(update={"evidence": poor.evidence.model_copy(update={"revision": "poor"})}))
    with pytest.raises(SafetyFence, match="per-account"):
        harness.outbox.prepare(harness.request(evidence=poor.evidence.model_copy(update={"revision": "poor"})))


def test_cash_projection_discrepancy_blocks_entry_even_with_fresh_risk(harness: Harness) -> None:
    # REST can discover a cash mismatch after execution reduction succeeded.
    fact = {"id": "11", "type": "DAILY_FINANCING", "accountID": ACCOUNT_ID,
            "financing": "-1", "accountBalance": "9990"}
    harness.reducer.ingest(fact, environment="SANDBOX", account_id=ACCOUNT_ID)
    harness.ledger.apply_history("SANDBOX", ACCOUNT_ID, "1", [fact], "11", unfiltered=True)
    harness.publish_risk("new-cut", covered="11")
    with pytest.raises(SafetyFence, match="ledger"):
        harness.outbox.prepare(harness.request())


def test_default_authority_barrier_cannot_ignore_execution_fences(harness: Harness) -> None:
    # Removing an optional injected barrier must never relax STOP requirements.
    harness.coordinator.barrier = None
    with harness.db.transaction() as connection:
        connection.execute("INSERT INTO execution_fences VALUES(?,?,?,?,?,?,NULL,?)",
            ("test-fence", "fault-test", "SANDBOX", ACCOUNT_ID, "TEST", "{}", utc_now().isoformat()))
    state = harness.coordinator.snapshot()
    target = ContextKey(authority_id="fault-test", epoch=state.context.epoch + 1)
    transition = harness.coordinator.request_transition(target, state.revision, str(uuid4()))
    initializer = FixtureInitializer()
    assert harness.coordinator.run_transition(transition.transition_id, FixtureDrain(harness.ledger), initializer).state == "BLOCKED_DRAIN"
    assert not initializer.calls


def test_duplicate_trade_reductions_are_quarantined_atomically(harness: Harness) -> None:
    opening = {"id":"12", "type":"ORDER_FILL", "orderID":"11", "instrument":"EUR_USD",
        "units":"100", "pl":"0", "financing":"0", "tradeOpened":{"tradeID":"12","units":"100"}}
    harness.reducer.ingest(opening, environment="SANDBOX", account_id=ACCOUNT_ID)
    assert harness.cover_fixture_history()
    closing = {"id":"14", "type":"ORDER_FILL", "orderID":"13", "instrument":"EUR_USD",
        "units":"-200", "pl":"0", "financing":"0", "tradesClosed":[{"tradeID":"12","units":"100"}]*2}
    harness.reducer.ingest(closing, environment="SANDBOX", account_id=ACCOUNT_ID)
    assert not harness.cover_fixture_history()
    assert harness.outbox.open_trades(environment="SANDBOX",account_id=ACCOUNT_ID)[0]["current_units"]=="100"


def test_uncovered_reductions_do_not_discard_native_cash(harness: Harness) -> None:
    for identity in range(100, 1125):
        fact={"id":str(identity),"type":"ORDER_FILL","orderID":str(identity-1),
            "instrument":"EUR_USD","units":"-1","pl":"0","financing":"0",
            "tradeReduced":{"tradeID":"99","units":"1"}}
        harness.reducer.ingest(fact,environment="SANDBOX",account_id=ACCOUNT_ID)
    cash={"id":"1125","type":"DAILY_FINANCING","financing":"-1"}
    harness.reducer.ingest(cash,environment="SANDBOX",account_id=ACCOUNT_ID)
    assert not harness.cover_fixture_history()
    with harness.db.read() as connection:
        assert connection.execute("SELECT amount FROM ledger_cash_journal WHERE transaction_id='1125' AND side='CASH'").fetchone()[0]=="-1"
        assert connection.execute("SELECT state FROM execution_projection_state").fetchone()[0]=="FENCED"


def test_republishing_risk_cannot_hide_clock_discontinuity(harness: Harness) -> None:
    # An identical revision is not new timing evidence and cannot reset a stale
    # or discontinuous monotonic grant before the dispatch guard checks it.
    previous = harness.risk._monotonic()
    harness.risk._monotonic = lambda: previous + 120
    with pytest.raises(SafetyFence, match="clock"):
        harness.risk.publish(harness.observation)
        harness.outbox.prepare(harness.request())


class FixtureGovernor:
    calls = 0

    async def acquire(self, *, kind: Literal["request", "connection"] = "request", deadline: float) -> None:
        self.calls += 1
        assert kind == "request" and deadline > 0
        await asyncio.sleep(0)


class FixtureInitializer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def reset_transients(self, source: ContextKey, target: ContextKey) -> None:
        self.calls.append("reset")

    def initialize(self, target: ContextKey, profile: VerifiedAccountProfile | None) -> None:
        self.calls.append("initialize")


class FixtureReadiness:
    def verify(self, context: ContextKey, profile: VerifiedAccountProfile) -> ReadinessProof:
        return ReadinessProof(
            context_sha256=context.fingerprint, profile_version=profile.version,
            observed_at=utc_now(), evidence_sha256="d" * 64,
            structural_gate_verified=True, account_reconciled=True,
        )


class FixtureDrain:
    """Flat snapshot evidence; the separate durable outbox barrier must veto it."""

    def __init__(self, ledger: Ledger | None = None) -> None:
        self.ledger = ledger

    def quiesce(self, source: ContextKey) -> None:
        assert source.authority_id == "fault-test"

    def drain(self, source: ContextKey) -> None:
        assert source.authority_id == "fault-test"

    def reconcile(self, source: ContextKey) -> StopProof:
        balance = None if self.ledger is None else self.ledger.balance("SANDBOX", ACCOUNT_ID)
        accounts = () if source.product_track == "DATA" else (
            AccountStopEvidence(
                account_id=ACCOUNT_ID, environment="SANDBOX", snapshot_transaction_id="0" if balance is None else balance.observed_head,
                covered_transaction_id="0" if balance is None else balance.covered_cursor, observed_at=utc_now(), financing_finalized=True,
                financing_evidence_sha256="b" * 64, source_snapshot_sha256="c" * 64,
            ),
        )
        return StopProof(source_context_sha256=source.fingerprint, observed_at=utc_now(),
                         complete_account_universe=True, accounts=accounts)


class Harness:
    def __init__(self, directory: Path) -> None:
        self.db = supported_database(directory / "state.sqlite3")
        self.coordinator = AuthorityCoordinator(self.db, "fault-test", directory / "locks")
        self.risk = RiskRegistry(self.db)
        self.outbox = Outbox(self.db, self.coordinator, self.risk)
        self.coordinator.barrier = self.outbox
        self.ledger = Ledger(self.db)
        self.reducer = OutboxReducer(self.outbox, ledger=self.ledger)
        self.paper = PaperBroker(self.db, self.consume)
        self.governor = FixtureGovernor()
        now = utc_now()
        profile = VerifiedAccountProfile(
            profile_id="synthetic", version=1, authority_id="fault-test", environment="SANDBOX",
            account_ids=(ACCOUNT_ID,), home_currency="EUR", broker_entity="SYNTHETIC PAPER",
            legal_product="SYNTHETIC_FX", region="DE", negative_balance_protection=True,
            api_binding="PAPER", instrument_rules=(LeverageRule(
                instrument=CurrencyInstrument(name="EUR_USD"), maximum_leverage=D("20")),),
            verification_issuer="oandafly.synthetic.paper.v1", evidence_sha256="a" * 64,
            verified_at=now - timedelta(minutes=1), expires_at=now + timedelta(days=1),
        )
        self.coordinator.register_profile(profile, PaperProfileVerifier())
        snapshot = self.coordinator.snapshot()
        self.context = ContextKey(authority_id="fault-test", environment="SANDBOX",
                                  product_track="TRADING", account_profile_id="synthetic", epoch=1)
        transition = self.coordinator.request_transition(self.context, snapshot.revision, str(uuid4()))
        assert self.coordinator.run_transition(transition.transition_id, FixtureDrain(), FixtureInitializer()).state == "READY_UNARMED"
        self.coordinator.arm(self.coordinator.snapshot().revision, str(uuid4()), FixtureReadiness(), utc_now() + timedelta(seconds=30))
        funding = self.paper.seed(D("10000"), uuid4())
        self.ledger.establish_baseline("SANDBOX", ACCOUNT_ID, "EUR", D("10000"), "1", now)
        self.reducer.seed_snapshot(environment="SANDBOX", account_id=ACCOUNT_ID, cursor="1")
        assert funding["duplicate"] is False
        self.publish_risk()

    def cover_fixture_history(self) -> bool:
        """The test author supplies the complete synthetic journal, not stream proof."""
        cursor = self.ledger.balance("SANDBOX", ACCOUNT_ID).covered_cursor
        with self.db.read() as connection:
            facts = [json.loads(row[0]) for row in connection.execute(
                "SELECT payload_json FROM execution_native_facts WHERE environment=? AND account_id=? AND (length(transaction_id),transaction_id)>(?,?) ORDER BY length(transaction_id),transaction_id",
                ("SANDBOX", ACCOUNT_ID, len(cursor), cursor))]
        final = cursor if not facts else str(facts[-1]["id"])
        return self.reducer.apply_history(environment="SANDBOX", account_id=ACCOUNT_ID,
            requested_from_id=cursor, transactions=facts, last_transaction_id=final)

    def consume(self, transaction: dict[str, object]) -> None:
        result = self.reducer.ingest(transaction, environment="SANDBOX", account_id=ACCOUNT_ID)
        assert not result.quarantined

    def publish_risk(self, revision: str = "cut-1", *, covered: str = "1") -> None:
        self.evidence = RiskEvidence(
            revision=revision, authority_revision=self.coordinator.snapshot().revision,
            profile_version=1, account_revision=revision, pricing_revision=revision,
            policy_revision="synthetic-policy-1", conversion_revision=revision,
            covered_transaction_id=covered, valid_until=utc_now() + timedelta(seconds=50),
        )
        self.observation = RiskObservation(
            authority_id="fault-test", environment="SANDBOX", account_id=ACCOUNT_ID,
            evidence=self.evidence,
            instrument=InstrumentRisk(
                name="EUR_USD", base_currency="EUR", quote_currency="USD", trade_units_precision=0,
                legal_product="SYNTHETIC_FX",
                pip_location=-4, minimum_trade_size=D("1"), maximum_order_units=D("1000000"),
                maximum_position_units=D("1000000"), bid=D("1.10000"), ask=D("1.10020"),
                conversions=HomeConversions(currency="USD", position_value=D("1") / D("1.10010"),
                                            account_gain=D("1") / D("1.10020"), account_loss=D("1") / D("1.10000")),
                requested_leverage=D("20"), broker_max_leverage=D("20"),
                current_gross_units=sum((abs(D(str(item["current_units"]))) for item in self.outbox.open_trades(environment="SANDBOX", account_id=ACCOUNT_ID)), D("0")),
                margin_tiers=(MarginTier(rate=D("0.05")),), maximum_spread_pips=D("5"),
            ),
            margin=MarginState(native_margin_available=D("10000"), stressed_equity=D("9900"),
                               independent_margin_requirement=D("0"), gates={"scope": True, "quote": True, "financing": True}),
            maximum_entry_loss=D("100"), stop_gap_buffer=D("0.001"),
        )
        self.risk.publish(self.observation)

    def request(self, **changes: object) -> OrderRequest:
        values: dict[str, object] = {
            "context": self.context, "account_id": ACCOUNT_ID, "profile_version": 1,
            "instrument": "EUR_USD", "units": D("100"), "stop_price": D("1.09500"),
            "price_bound": D("1.10100"), "evidence": self.evidence,
        }
        values.update(changes)
        return OrderRequest.from_wire(values)

    def close(self) -> None:
        self.coordinator.close()
        self.db.close()


@pytest.fixture
def harness(tmp_path: Path):  # type: ignore[no-untyped-def]
    value = Harness(tmp_path)
    try:
        yield value
    finally:
        value.close()


def create_fact(record: object, native_id: str = "11") -> dict[str, object]:
    from oandafly.execution.outbox import OutboxRecord
    assert isinstance(record, OutboxRecord)
    return {
        "id": native_id, "accountID": ACCOUNT_ID, "time": utc_now().isoformat(),
        "type": "MARKET_ORDER", "instrument": "EUR_USD", "units": "100",
        "positionFill": "OPEN_ONLY", "clientExtensions": {"id": record.client_id},
    }


def fill_fact(record: object, native_id: str = "12", order_id: str = "11") -> dict[str, object]:
    from oandafly.execution.outbox import OutboxRecord
    assert isinstance(record, OutboxRecord)
    return {
        "id": native_id, "accountID": ACCOUNT_ID, "time": utc_now().isoformat(),
        "type": "ORDER_FILL", "orderID": order_id, "clientOrderID": record.client_id,
        "instrument": "EUR_USD", "units": "100", "price": "1.1002", "pl": "0", "financing": "0",
        "commission": "0", "accountBalance": "10000",
        "tradeOpened": {"tradeID": native_id, "units": "100", "price": "1.1002"},
    }


def test_stream_fill_precedes_rest_response_without_state_regression(harness: Harness) -> None:
    async def scenario() -> None:
        await harness.paper.quote(D("1.10000"), D("1.10020"))
        prepared = harness.outbox.prepare(harness.request())
        stream_done = asyncio.Event()
        release_response = asyncio.Event()

        class DelayedHTTP:
            async def prepare_order(self, account_id: str) -> None:
                pass

            async def send_order_bytes(self, account_id: str, payload: bytes) -> Mapping[str, object]:
                response = await harness.paper.send_order_bytes(account_id, payload)
                stream_done.set()
                await release_response.wait()
                return response

        reconciler = NativeMutationGateway(harness.outbox, transport=DelayedHTTP(), governor=harness.governor, reducer=harness.reducer)
        task = asyncio.create_task(reconciler.dispatch(prepared.intent_id))
        await asyncio.wait_for(stream_done.wait(), 2)
        early = harness.outbox.get(prepared.intent_id)
        assert early.status == OutboxStatus.ACK_BY_FILL
        assert early.reservation_active  # Snapshot margin has not reflected this fill yet.
        harness.cover_fixture_history()
        assert len(harness.outbox.open_trades(environment="SANDBOX", account_id=ACCOUNT_ID)) == 1
        release_response.set()
        final = await task
        assert final.status == OutboxStatus.ACK_BY_FILL
        assert final.native_fill_id == early.native_fill_id
        assert final.dispatch_state == DispatchState.RESPONDED
        pending = harness.outbox.pending_orders(environment="SANDBOX", account_id=ACCOUNT_ID)
        assert all(item["trade_id"] is not None for item in pending)
        assert early.native_order_id not in {item["order_id"] for item in pending}
    asyncio.run(scenario())


def test_timeout_fences_handover_until_authoritative_cancel(harness: Harness) -> None:
    class AmbiguousHTTP:
        async def prepare_order(self, account_id: str) -> None:
            pass

        async def send_order_bytes(self, account_id: str, payload: bytes) -> Mapping[str, object]:
            raise TimeoutError("injected timeout after native invocation marker")

    prepared = harness.outbox.prepare(harness.request())
    reconciler = NativeMutationGateway(harness.outbox, transport=AmbiguousHTTP(), governor=harness.governor, reducer=harness.reducer)
    with pytest.raises(TransportAmbiguous):
        asyncio.run(reconciler.dispatch(prepared.intent_id))
    unknown = harness.outbox.get(prepared.intent_id)
    assert unknown.status == OutboxStatus.PENDING_SUBMISSION
    assert unknown.dispatch_state == DispatchState.UNKNOWN
    assert unknown.reservation_active
    with pytest.raises(SafetyFence):
        asyncio.run(reconciler.dispatch(prepared.intent_id))
    state = harness.coordinator.snapshot()
    target = ContextKey(authority_id="fault-test", environment="SANDBOX", product_track="DATA",
                        account_profile_id="unenrolled", epoch=state.context.epoch + 1)
    transition = harness.coordinator.request_transition(target, state.revision, str(uuid4()))
    initializer = FixtureInitializer()
    assert harness.coordinator.run_transition(transition.transition_id, FixtureDrain(harness.ledger), initializer).state == "BLOCKED_DRAIN"
    assert initializer.calls == []
    assert harness.coordinator.snapshot().lease_status in {"RECOVERY", "ACTIVE"}
    assert not harness.coordinator.snapshot().armed
    created = create_fact(prepared)
    harness.reducer.ingest(created, environment="SANDBOX", account_id=ACCOUNT_ID, source="REST")
    cancelled = {"id": "13", "type": "ORDER_CANCEL", "orderID": "11", "time": utc_now().isoformat(), "accountID": ACCOUNT_ID}
    harness.reducer.ingest(cancelled, environment="SANDBOX", account_id=ACCOUNT_ID, source="REST")
    harness.reducer.apply_history(environment="SANDBOX", account_id=ACCOUNT_ID, requested_from_id="1", transactions=(created,cancelled), last_transaction_id="13")
    assert harness.outbox.get(prepared.intent_id).status == OutboxStatus.CANCELLED
    assert harness.coordinator.run_transition(transition.transition_id, FixtureDrain(harness.ledger), initializer).state == "READY_UNARMED"
    assert harness.coordinator.snapshot().context == target


@pytest.mark.parametrize("name", ["XAU_USD", "XAG_USD", "BTC_USD", "ETH_USD", "USDT_USD", "AAA_USD"])
def test_model_rejects_unauthorized_currency_assets(harness: Harness, name: str) -> None:
    with pytest.raises((ValidationError, ValidationFailure, ValueError)):
        harness.request(instrument=name)


@pytest.mark.parametrize("pair,cap", [("EUR_USD", "30"), ("EUR_GBP", "30"), ("AUD_USD", "20"), ("USD_TRY", "20")])
def test_german_policy_rejects_excess_leverage(pair: str, cap: str) -> None:
    with pytest.raises(ValidationError):
        LeverageRule(instrument=CurrencyInstrument(name=pair, margin_rate=D("0.01")), maximum_leverage=D(cap) + 1)


def test_data_product_never_constructs_execution_request(harness: Harness) -> None:
    with pytest.raises(ValidationError):
        harness.request(context=ContextKey(authority_id="fault-test"))


def test_phase_a_is_idempotent_only_for_exact_permanent_local_uuid(harness: Harness) -> None:
    identity = uuid4()
    first = harness.outbox.prepare(harness.request(), intent_id=identity)
    second = harness.outbox.prepare(harness.request(), intent_id=identity)
    assert first == second
    with pytest.raises(Conflict):
        harness.outbox.prepare(harness.request(units=D("99")), intent_id=identity)
    harness.outbox.cancel_prepared(first.intent_id)
    with pytest.raises(SafetyFence):
        asyncio.run(NativeMutationGateway(harness.outbox, transport=harness.paper, governor=harness.governor, reducer=harness.reducer).dispatch(first.intent_id))


def test_stale_risk_cut_cannot_dispatch_prepared_payload(harness: Harness) -> None:
    prepared = harness.outbox.prepare(harness.request())
    harness.publish_risk("new-pricing-cut")
    with pytest.raises(SafetyFence):
        asyncio.run(NativeMutationGateway(harness.outbox, transport=harness.paper, governor=harness.governor, reducer=harness.reducer).dispatch(prepared.intent_id))
    assert harness.outbox.get(prepared.intent_id).dispatch_state == DispatchState.NOT_DISPATCHED


def test_final_boundary_rechecks_context_after_claim(harness: Harness) -> None:
    class DisarmingTransport:
        async def prepare_order(self, account_id: str) -> None:
            harness.coordinator.disarm("FAULT_INJECTION")

        async def send_order_bytes(self, account_id: str, payload: bytes) -> Mapping[str, object]:
            raise AssertionError("a disarmed authority reached native mutation")

    prepared = harness.outbox.prepare(harness.request())
    with pytest.raises(SafetyFence):
        asyncio.run(NativeMutationGateway(harness.outbox, transport=DisarmingTransport(), governor=harness.governor, reducer=harness.reducer).dispatch(prepared.intent_id))
    result = harness.outbox.get(prepared.intent_id)
    assert result.dispatch_state == DispatchState.NOT_SENT
    assert result.invocation_marker is None
    assert not result.reservation_active


def test_fill_before_create_then_late_network_error_keeps_fill(harness: Harness) -> None:
    prepared = harness.outbox.prepare(harness.request())
    created, filled = create_fact(prepared), fill_fact(prepared)

    class FillThenTimeout:
        async def prepare_order(self, account_id: str) -> None:
            pass

        async def send_order_bytes(self, account_id: str, payload: bytes) -> Mapping[str, object]:
            harness.reducer.ingest(filled, environment="SANDBOX", account_id=ACCOUNT_ID)
            harness.reducer.ingest(created, environment="SANDBOX", account_id=ACCOUNT_ID)
            raise TimeoutError("REST connection died after private fill")

    result = asyncio.run(NativeMutationGateway(harness.outbox, transport=FillThenTimeout(), governor=harness.governor, reducer=harness.reducer).dispatch(prepared.intent_id))
    assert result.status == OutboxStatus.ACK_BY_FILL
    assert result.native_fill_id == "12"
    assert result.reservation_active
    duplicate = harness.reducer.ingest(filled, environment="SANDBOX", account_id=ACCOUNT_ID, source="REST")
    assert duplicate.duplicate and not duplicate.quarantined
    assert harness.cover_fixture_history()
    assert harness.outbox.open_trades(environment="SANDBOX", account_id=ACCOUNT_ID)[0]["current_units"] == "100"
    harness.publish_risk("included-fill-cut", covered="12")
    assert not harness.outbox.get(prepared.intent_id).reservation_active


def test_exact_native_recovery_closes_trade_without_reentry(harness: Harness) -> None:
    async def scenario() -> None:
        await harness.paper.quote(D("1.10000"), D("1.10020"))
        prepared = harness.outbox.prepare(harness.request())
        await NativeMutationGateway(harness.outbox, transport=harness.paper, governor=harness.governor, reducer=harness.reducer).dispatch(prepared.intent_id)
        assert harness.cover_fixture_history()
        trade = harness.outbox.open_trades(environment="SANDBOX", account_id=ACCOUNT_ID)[0]
        recovery = RecoveryRouter(harness.outbox, reducer=harness.reducer, transport=harness.paper, governor=harness.governor)
        action = recovery.prepare(context=harness.context, account_id=ACCOUNT_ID, profile_version=1,
                                  operation="CLOSE_TRADE", target_id=str(trade["trade_id"]), units=D("100"))
        result = await recovery.dispatch(action.action_id)
        assert result.state == "RESOLVED"
        assert harness.cover_fixture_history()
        assert harness.outbox.open_trades(environment="SANDBOX", account_id=ACCOUNT_ID) == ()
        assert harness.outbox.get(prepared.intent_id).status == OutboxStatus.ACK_BY_FILL
        with pytest.raises(SafetyFence):
            await recovery.dispatch(action.action_id)
    asyncio.run(scenario())


def test_entry_failing_before_final_guard_is_proven_unsent(harness: Harness) -> None:
    class VaultFailure:
        async def prepare_order(self, account_id: str) -> None:
            raise OSError("synthetic credential read failure before any native bytes")

        async def send_order_bytes(self, account_id: str, payload: bytes) -> Mapping[str, object]:
            raise AssertionError("an unprepared entry reached native mutation")

    prepared = harness.outbox.prepare(harness.request())
    gateway = NativeMutationGateway(harness.outbox, transport=VaultFailure(), governor=harness.governor, reducer=harness.reducer)
    with pytest.raises(SafetyFence, match="proven unsent"):
        asyncio.run(gateway.dispatch(prepared.intent_id))
    result = harness.outbox.get(prepared.intent_id)
    assert (result.status, result.dispatch_state) == (OutboxStatus.CANCELLED, DispatchState.NOT_SENT)
    assert result.invocation_marker is None and not result.reservation_active
    # A transient pre-send failure is not an ambiguous mutation fencing later entries.
    harness.outbox.prepare(harness.request())


def _open_paper_trade(harness: Harness) -> str:
    async def scenario() -> None:
        await harness.paper.quote(D("1.10000"), D("1.10020"))
        prepared = harness.outbox.prepare(harness.request())
        await NativeMutationGateway(harness.outbox, transport=harness.paper, governor=harness.governor, reducer=harness.reducer).dispatch(prepared.intent_id)
    asyncio.run(scenario())
    assert harness.cover_fixture_history()
    return str(harness.outbox.open_trades(environment="SANDBOX", account_id=ACCOUNT_ID)[0]["trade_id"])


def test_recovery_failing_before_final_guard_is_proven_unsent_and_retryable(harness: Harness) -> None:
    trade = _open_paper_trade(harness)

    class CredentialFailure:
        async def close_trade(self, account_id: str, trade_id: str, units: Decimal, *, before_send) -> Mapping[str, object]:
            raise OSError("synthetic credential failure before the final guard")

    failing = RecoveryRouter(harness.outbox, reducer=harness.reducer, transport=CredentialFailure(), governor=harness.governor)
    first = failing.prepare(context=harness.context, account_id=ACCOUNT_ID, profile_version=1,
                            operation="CLOSE_TRADE", target_id=trade, units=D("100"))
    with pytest.raises(SafetyFence, match="proven unsent"):
        asyncio.run(failing.dispatch(first.action_id))
    assert failing.get(first.action_id).state == "NOT_SENT"
    assert failing.pending("fault-test") == ()
    recovery = RecoveryRouter(harness.outbox, reducer=harness.reducer, transport=harness.paper, governor=harness.governor)
    retry = recovery.prepare(context=harness.context, account_id=ACCOUNT_ID, profile_version=1,
                             operation="CLOSE_TRADE", target_id=trade, units=D("100"))
    assert asyncio.run(recovery.dispatch(retry.action_id)).state == "RESOLVED"


def test_worker_retries_unsent_protective_close_under_new_identity(harness: Harness) -> None:
    from types import SimpleNamespace
    from uuid import NAMESPACE_URL, uuid5
    from oandafly.worker.application import TradingWorker
    trade = _open_paper_trade(harness)
    worker = TradingWorker.__new__(TradingWorker)  # only the recovery-identity helper is exercised
    worker.db, worker.config = harness.db, SimpleNamespace(authority_id="fault-test")
    first = uuid5(NAMESPACE_URL, "oandafly:protection:fault-test:"+trade+":MISSING_PROTECTION")

    class CredentialFailure:
        async def close_trade(self, account_id: str, trade_id: str, units: Decimal, *, before_send) -> Mapping[str, object]:
            raise OSError("synthetic credential failure before the final guard")

    failing = RecoveryRouter(harness.outbox, reducer=harness.reducer, transport=CredentialFailure(), governor=harness.governor)
    assert worker._next_recovery_id(first, trade) == first
    failing.prepare(context=harness.context, account_id=ACCOUNT_ID, profile_version=1,
                    operation="CLOSE_TRADE", target_id=trade, units=D("100"), action_id=first)
    with pytest.raises(SafetyFence):
        asyncio.run(failing.dispatch(str(first)))
    assert worker._next_recovery_id(first, trade) is None  # retries of a pre-send failure back off
    with harness.db.transaction() as connection:
        connection.execute("UPDATE execution_recovery SET updated_at=? WHERE action_id=?",
                           ((utc_now() - timedelta(minutes=1)).isoformat(), str(first)))
    retry = worker._next_recovery_id(first, trade)
    assert retry is not None and retry != first
    recovery = RecoveryRouter(harness.outbox, reducer=harness.reducer, transport=harness.paper, governor=harness.governor)
    recovery.prepare(context=harness.context, account_id=ACCOUNT_ID, profile_version=1,
                     operation="CLOSE_TRADE", target_id=trade, units=D("100"), action_id=retry)
    assert asyncio.run(recovery.dispatch(str(retry))).state == "RESOLVED"
    assert worker._next_recovery_id(first, trade) is None  # a sent close is never multiplied


def test_recovery_transport_skipping_its_guard_remains_ambiguous(harness: Harness) -> None:
    trade = _open_paper_trade(harness)

    class GuardSkipping:
        async def close_trade(self, account_id: str, trade_id: str, units: Decimal, *, before_send) -> Mapping[str, object]:
            return {}

    recovery = RecoveryRouter(harness.outbox, reducer=harness.reducer, transport=GuardSkipping(), governor=harness.governor)
    action = recovery.prepare(context=harness.context, account_id=ACCOUNT_ID, profile_version=1,
                              operation="CLOSE_TRADE", target_id=trade, units=D("100"))
    with pytest.raises(TransportAmbiguous):
        asyncio.run(recovery.dispatch(action.action_id))
    assert recovery.get(action.action_id).state == "UNKNOWN"


def test_profile_cannot_route_to_foreign_sub_account(harness: Harness) -> None:
    with pytest.raises(SafetyFence):
        harness.outbox.prepare(harness.request(account_id="SYNTHETIC:PAPER:FOREIGN"))


def test_prepared_second_order_is_fenced_when_first_becomes_unknown(harness: Harness) -> None:
    first = harness.outbox.prepare(harness.request())
    second = harness.outbox.prepare(harness.request())
    calls: list[str] = []

    class TimeoutHTTP:
        async def prepare_order(self, account_id: str) -> None:
            pass

        async def send_order_bytes(self, account_id: str, payload: bytes) -> Mapping[str, object]:
            calls.append(account_id)
            raise TimeoutError("possible first mutation")

    reconciler = NativeMutationGateway(harness.outbox, transport=TimeoutHTTP(), governor=harness.governor, reducer=harness.reducer)
    with pytest.raises(TransportAmbiguous):
        asyncio.run(reconciler.dispatch(first.intent_id))
    with pytest.raises(SafetyFence):
        asyncio.run(reconciler.dispatch(second.intent_id))
    assert calls == [ACCOUNT_ID]
    assert harness.outbox.get(second.intent_id).dispatch_state == DispatchState.NOT_DISPATCHED


def test_new_private_native_fact_invalidates_old_risk_cut(harness: Harness) -> None:
    prepared = harness.outbox.prepare(harness.request())
    harness.reducer.ingest({"id": "10", "type": "DAILY_FINANCING", "accountID": ACCOUNT_ID,
                            "time": utc_now().isoformat(), "financing": "-0.50", "accountBalance": "9999.50"},
                           environment="SANDBOX", account_id=ACCOUNT_ID)
    with pytest.raises(SafetyFence):
        asyncio.run(NativeMutationGateway(harness.outbox, transport=harness.paper, governor=harness.governor, reducer=harness.reducer).dispatch(prepared.intent_id))
    assert harness.outbox.get(prepared.intent_id).invocation_marker is None


def test_two_dispatch_threads_cannot_send_one_intent_twice(harness: Harness) -> None:
    async def scenario() -> None:
        await harness.paper.quote(D("1.10000"), D("1.10020"))
        prepared = harness.outbox.prepare(harness.request())
        reconciler = NativeMutationGateway(harness.outbox, transport=harness.paper, governor=harness.governor, reducer=harness.reducer)
        outcomes = await asyncio.gather(reconciler.dispatch(prepared.intent_id), reconciler.dispatch(prepared.intent_id), return_exceptions=True)
        assert sum(not isinstance(value, BaseException) for value in outcomes) == 1
        assert harness.outbox.get(prepared.intent_id).status == OutboxStatus.ACK_BY_FILL
        harness.cover_fixture_history()
        assert len(harness.outbox.open_trades(environment="SANDBOX", account_id=ACCOUNT_ID)) == 1
        with harness.db.read() as connection:
            assert connection.execute("SELECT count(*) FROM outbox_events WHERE event='NATIVE_INVOCATION_STARTED'").fetchone()[0] == 1
    asyncio.run(scenario())


def test_old_epoch_client_extension_reuse_cannot_create_second_order(harness: Harness) -> None:
    async def scenario() -> None:
        await harness.paper.quote(D("1.10000"), D("1.10020"))
        prepared = harness.outbox.prepare(harness.request())
        await NativeMutationGateway(harness.outbox, transport=harness.paper, governor=harness.governor, reducer=harness.reducer).dispatch(prepared.intent_id)
        original = harness.outbox.get(prepared.intent_id)
        conflict = harness.reducer.ingest(create_fact(original, "50"), environment="SANDBOX", account_id=ACCOUNT_ID)
        assert conflict.quarantined
        final = harness.outbox.get(prepared.intent_id)
        assert final.native_order_id == original.native_order_id
        assert final.status == OutboxStatus.ACK_BY_FILL
        with harness.db.read() as connection:
            assert harness.outbox.has_unresolved_in(connection, harness.context.authority_id)
    asyncio.run(scenario())



def test_transport_receives_only_the_exact_immutable_prepared_bytes(harness: Harness) -> None:
    class BytesTransport:
        async def prepare_order(self, account_id: str) -> None:
            pass

        async def send_order_bytes(self, account_id: str, payload: bytes) -> Mapping[str, object]:
            assert type(payload) is bytes
            with pytest.raises(TypeError):
                payload[0] = 0
            assert json.loads(payload)["order"]["units"] == "100"
            assert hashlib.sha256(payload).hexdigest() == prepared.payload_hash
            raise TimeoutError("native outcome unknown")

    prepared = harness.outbox.prepare(harness.request())
    with pytest.raises(TransportAmbiguous):
        asyncio.run(NativeMutationGateway(harness.outbox, transport=BytesTransport(), governor=harness.governor,
                               reducer=harness.reducer).dispatch(prepared.intent_id))
    final = harness.outbox.get(prepared.intent_id)
    assert final.invocation_marker is not None
    assert final.dispatch_state == DispatchState.UNKNOWN


def test_gateway_rejects_durable_payload_changes_during_transport_preparation(harness: Harness) -> None:
    prepared = harness.outbox.prepare(harness.request())

    class TamperingPreparation:
        async def prepare_order(self, account_id: str) -> None:
            changed = prepared.model_dump(mode="json")["payload"]
            changed["order"]["units"] = "9999"
            with harness.db.transaction() as connection:
                connection.execute("UPDATE outbox SET payload_json=? WHERE intent_id=?",
                                   (json.dumps(changed), prepared.intent_id))

        async def send_order_bytes(self, account_id: str, payload: bytes) -> Mapping[str, object]:
            pytest.fail("The native byte transmission boundary must not be reached")

    with pytest.raises(SafetyFence):
        asyncio.run(NativeMutationGateway(harness.outbox, transport=TamperingPreparation(), governor=harness.governor,
                               reducer=harness.reducer).dispatch(prepared.intent_id))
    final = harness.outbox.get(prepared.intent_id)
    assert final.invocation_marker is None and final.dispatch_state == DispatchState.NOT_SENT


def test_monotonic_clock_discontinuity_invalidates_all_risk_grants(harness: Harness) -> None:
    prepared = harness.outbox.prepare(harness.request())
    previous = harness.risk._monotonic()
    harness.risk._monotonic = lambda: previous + 120
    with pytest.raises(SafetyFence):
        asyncio.run(NativeMutationGateway(harness.outbox, transport=harness.paper, governor=harness.governor, reducer=harness.reducer).dispatch(prepared.intent_id))
    assert harness.outbox.get(prepared.intent_id).dispatch_state == DispatchState.NOT_DISPATCHED


def test_restarted_risk_registry_cannot_reuse_old_process_grant(harness: Harness) -> None:
    prepared = harness.outbox.prepare(harness.request())
    restarted = RiskRegistry(harness.db)
    harness.outbox.risk = restarted
    with pytest.raises(SafetyFence):
        asyncio.run(NativeMutationGateway(harness.outbox, transport=harness.paper, governor=harness.governor, reducer=harness.reducer).dispatch(prepared.intent_id))
    assert harness.outbox.get(prepared.intent_id).invocation_marker is None


def test_trade_subtree_cannot_hide_more_units_than_native_fill(harness: Harness) -> None:
    prepared = harness.outbox.prepare(harness.request())

    class FalseSubtree:
        async def prepare_order(self, account_id: str) -> None:
            pass

        async def send_order_bytes(self, account_id: str, payload: bytes) -> Mapping[str, object]:
            filled = fill_fact(prepared)
            filled["tradeOpened"] = {"tradeID": "12", "units": "10000", "price": "1.1002"}
            reduction = harness.reducer.ingest(filled, environment="SANDBOX", account_id=ACCOUNT_ID)
            assert reduction.quarantined
            raise TimeoutError("native subtree conflict")

    with pytest.raises(TransportAmbiguous):
        asyncio.run(NativeMutationGateway(harness.outbox, transport=FalseSubtree(), governor=harness.governor, reducer=harness.reducer).dispatch(prepared.intent_id))
    assert harness.outbox.get(prepared.intent_id).dispatch_state == DispatchState.UNKNOWN
    assert harness.outbox.open_trades(environment="SANDBOX", account_id=ACCOUNT_ID) == ()


def test_wire_whitespace_tampering_after_claim_cannot_rewrite_durable_bytes(harness: Harness) -> None:
    prepared = harness.outbox.prepare(harness.request())

    class ChangedWire:
        async def prepare_order(self, account_id: str) -> None:
            # Semantically identical JSON still violates the committed byte contract.
            with harness.db.transaction() as connection:
                connection.execute("UPDATE outbox SET payload_json=payload_json || ' ' WHERE intent_id=?",
                                   (prepared.intent_id,))

        async def send_order_bytes(self, account_id: str, payload: bytes) -> Mapping[str, object]:
            pytest.fail("Changed durable wire must never be sent")

    with pytest.raises(SafetyFence):
        asyncio.run(NativeMutationGateway(harness.outbox, transport=ChangedWire(), governor=harness.governor,
                               reducer=harness.reducer).dispatch(prepared.intent_id))
    final = harness.outbox.get(prepared.intent_id)
    assert final.dispatch_state == DispatchState.NOT_SENT and final.invocation_marker is None


@pytest.mark.parametrize("other_pair", ["EUR_USD", "GBP_USD"])
@pytest.mark.parametrize("negative_branch", ["native_margin_available", "stressed_equity"])
def test_rich_foreign_account_and_pair_cannot_clear_own_drawdown(harness: Harness, other_pair, negative_branch):
    original = harness.observation
    rich = original.model_copy(update={"account_id": "SYNTHETIC:PAPER:OTHER",
        "instrument": original.instrument.model_copy(update={"name": other_pair, "base_currency": other_pair[:3]}),
        "margin": original.margin.model_copy(update={"native_margin_available": D("1000000000"),
                                                      "stressed_equity": D("1000000000")})})
    harness.risk.publish(rich)
    poor = original.model_copy(update={"margin": original.margin.model_copy(update={negative_branch: D("-1")}),
                                      "evidence": original.evidence.model_copy(update={"revision": "drawdown"})})
    harness.risk.publish(poor)
    with pytest.raises(SafetyFence, match="per-account"):
        harness.outbox.prepare(harness.request(evidence=poor.evidence))
    with harness.db.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
