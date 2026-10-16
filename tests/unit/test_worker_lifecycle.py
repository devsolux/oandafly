"""Worker lifecycle decisions on a real store with explicit synthetic collaborators.

Only the TradingWorker methods under test run; native, model and approval
dependencies are test doubles. This is not worker, model or broker qualification.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from oandafly.domain.errors import SafetyFence
from oandafly.domain.primitives import utc_now
from oandafly.worker import application
from oandafly.worker.application import CONTROL_SCHEMA, TradingWorker
from tests.support import supported_database

AUTHORITY = "lifecycle-test"


class Authority:
    def __init__(self, *, armed: bool) -> None:
        self.armed, self.arms = armed, 0

    def snapshot(self) -> SimpleNamespace:
        return SimpleNamespace(armed=self.armed, lease_status="ACTIVE", revision=1)

    def arm(self, revision: int, command_id: str, readiness: object, deadline: object) -> None:
        self.arms += 1
        self.armed = True

    def disarm(self, reason: str) -> None:
        self.armed = False


class Synchronizer:
    def __init__(self, *, trades: tuple[dict, ...] = ()) -> None:
        summary = SimpleNamespace(observed_at=utc_now(), nav=Decimal("1000"), last_transaction_id="1")
        self.latest_account = SimpleNamespace(summary=summary, document=SimpleNamespace(
            sha256="a" * 64, body={"account": {"trades": list(trades), "orders": []}}))
        self.latest = "cut"

    async def refresh(self) -> str:
        return "cut"


def grant(*, expires_in: timedelta = timedelta(minutes=5)) -> SimpleNamespace:
    now = utc_now()
    return SimpleNamespace(limits=None, allow_unattended_resume=False, allow_automatic_promotion=True,
                           issued_at=now - timedelta(hours=1), expires_at=now + expires_in,
                           grant_id="g1", instruments=("EUR_USD",))


@pytest.fixture
def worker(tmp_path: Path):  # type: ignore[no-untyped-def]
    db = supported_database(tmp_path / "state.sqlite3")
    db.initialize(CONTROL_SCHEMA, component=application.__name__)
    with db.transaction() as connection:
        connection.execute("INSERT INTO worker_control VALUES(?,0,0,NULL,NULL)", (AUTHORITY,))
    value = TradingWorker.__new__(TradingWorker)  # collaborators below are explicit test doubles
    value.db = db
    value.config = SimpleNamespace(
        authority_id=AUTHORITY, environment="SANDBOX", account_id="001-001-1234567-001", granularity="M5",
        maximum_holding_bars=12, instruments=("EUR_USD",), registry_directory=tmp_path / "registry",
        model_validation="validation.json", deployment_approval="deployment.json",
        model_startup_timeout_seconds=30, inference_memory_gib=1)
    value.path = lambda name: tmp_path / name
    value.pending = lambda: False
    value.authority = Authority(armed=False)
    value.authorizer = SimpleNamespace(entry_block_reason=None, qualification=None, grant=grant)
    value.equity = SimpleNamespace(observe=lambda **_: SimpleNamespace(hard_fence=None))
    value.synchronizer = Synchronizer()
    value.recovery, value.verifier = None, None
    value.model, value.native, value.manifest, value.qualification = object(), object(), None, None
    value._action_lock = asyncio.Lock()
    value._manual_resume = value._fresh_lease = False
    value._health_ready, value._last_health = False, 0.0
    value._model_attempt = value._promotion_attempt = 0.0
    value._selected_deployment, value._last_bar = None, {}
    value.safety_error = value.model_error = "NOT_STARTED"
    try:
        yield value
    finally:
        db.close()


def test_resume_while_armed_is_consumed_and_cannot_rearm_after_a_later_disarm(worker) -> None:  # type: ignore[no-untyped-def]
    worker.authority = Authority(armed=True)
    worker._manual_resume = True
    asyncio.run(worker.safety_cycle())
    assert worker._health_ready and not worker._manual_resume
    worker.block_entries("TRANSIENT_NATIVE_GAP")
    asyncio.run(worker.safety_cycle())
    assert not worker.authority.armed and worker.authority.arms == 0
    assert worker.authorizer.entry_block_reason == "SafetyFence"


def test_resume_after_disarm_arms_exactly_once(worker) -> None:  # type: ignore[no-untyped-def]
    worker._manual_resume = True
    asyncio.run(worker.safety_cycle())
    assert worker.authority.armed and worker.authority.arms == 1 and not worker._manual_resume


def test_one_failing_protective_close_does_not_starve_the_others(worker) -> None:  # type: ignore[no-untyped-def]
    unprotected = [{"id": identity, "openTime": utc_now().isoformat(), "currentUnits": "100"} for identity in ("11", "12")]
    worker.synchronizer = Synchronizer(trades=tuple(unprotected))
    worker.recovery = object()
    attempted: list[str] = []

    async def close(identity: str, units: Decimal, *, reason: str, action_id: object = None) -> None:
        attempted.append(identity)
        if identity == "11":
            raise SafetyFence("synthetic close failure")

    worker._close_trade = close
    with pytest.raises(SafetyFence, match="synthetic"):
        asyncio.run(worker.safety_cycle())
    assert attempted == ["11", "12"]


def test_expired_grant_never_authorizes_automatic_promotion(worker) -> None:  # type: ignore[no-untyped-def]
    observed: list[str] = []

    class Recorder:
        @property
        def latest(self) -> None:
            observed.append("latest")
            return None

    worker.synchronizer = Recorder()
    worker.authorizer.grant = lambda: grant(expires_in=-timedelta(minutes=1))
    asyncio.run(worker.maybe_promote())
    assert observed == []
    worker.authorizer.grant, worker._promotion_attempt = grant, 0.0
    asyncio.run(worker.maybe_promote())
    assert observed == ["latest"]  # a current grant proceeds to the flat-account check


@pytest.fixture
def model_doubles(worker, monkeypatch, tmp_path: Path):  # type: ignore[no-untyped-def]
    attempts: list[Path] = []
    closed: list[str] = []

    class Qualification:
        def __init__(self, **kwargs: object) -> None:
            attempts.append(kwargs["model_directory"])  # type: ignore[arg-type]
            if kwargs["model_directory"] == tmp_path / "persisted-model":
                raise SafetyFence("Model validation is not current")
            self.model, self.graph = SimpleNamespace(fingerprint="model"), SimpleNamespace(fingerprint="graph")
            self.generation = 1

    process = SimpleNamespace(model_sha256="model", graph_sha256="graph", close=lambda: closed.append("closed"))
    monkeypatch.setattr(application, "Qualification", Qualification)
    monkeypatch.setattr(application, "InferenceProcess", lambda **_: process)
    monkeypatch.setattr(application, "manifest_document", lambda path: path)
    monkeypatch.setattr(application, "ModelManifest", SimpleNamespace(
        model_validate_json=lambda _: SimpleNamespace(fingerprint="model")))
    with worker.db.transaction() as connection:
        connection.execute("INSERT INTO model_deployments VALUES(?,?,?,?,?,?)",
                           (AUTHORITY, 1, "model", str(tmp_path / "persisted-model"),
                            str(tmp_path / "persisted-validation.json"), str(tmp_path / "persisted-deployment.json")))
    worker.model = None
    return SimpleNamespace(attempts=attempts, closed=closed, process=process)


def test_load_model_falls_back_to_renewed_configured_approvals(worker, model_doubles, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    asyncio.run(worker.load_model())
    assert model_doubles.attempts == [tmp_path / "persisted-model", tmp_path / "model_directory"]
    assert worker.model is model_doubles.process and worker.model_error == "NONE"
    with worker.db.read() as connection:
        saved = connection.execute("SELECT model_path,validation_path FROM model_deployments").fetchone()
    assert tuple(saved) == (str(tmp_path / "model_directory"), str(tmp_path / "model_validation"))


def test_load_model_rejects_a_graph_differing_from_the_qualification(worker, model_doubles) -> None:  # type: ignore[no-untyped-def]
    model_doubles.process.graph_sha256 = "another-graph"
    asyncio.run(worker.load_model())
    assert worker.model is None and worker.model_error == "SafetyFence"
    assert model_doubles.closed == ["closed"]
