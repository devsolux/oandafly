"""Headless single-account supervisor: neural proposals, independent safe execution.

Practice and live share this composition, never credentials or authority stores.
Missing qualification disables alpha entries, not observation/recovery services.
A GUI is an optional reader/control client. No synthetic strategy is substituted.
"""
from __future__ import annotations

import asyncio
import math
import signal
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from oandafly.adapters.governor import LocalRateGovernor
from oandafly.adapters.oanda.execution import NativeExecutionAdapter
from oandafly.adapters.oanda.observations import GRANULARITIES, NativeReadAdapter, native_time
from oandafly.domain.capabilities import RuntimeMode
from oandafly.domain.context import (AuthorityCoordinator, ContextKey, EngineConfig,
                                    ReadinessProof, StopProof)
from oandafly.domain.decisions import Action
from oandafly.domain.errors import SafetyFence
from oandafly.domain.policy import VerifiedAccountProfile
from oandafly.domain.primitives import money, sha256_json, utc_now
from oandafly.execution.outbox import OrderRequest, Outbox
from oandafly.execution.reconciler import NativeMutationGateway, OutboxReducer
from oandafly.execution.recovery import RecoveryRouter
from oandafly.execution.risk import RiskRegistry
from oandafly.marketdata.contracts import MarketCut
from oandafly.marketdata.history import CandleArchive
from oandafly.neural.artifacts import GraphManifest, ModelManifest, manifest_document
from oandafly.neural.isolation import InferenceProcess
from oandafly.neural.policy import decision
from oandafly.neural.qualification import DatasetReview, Qualification
from oandafly.neural.window import feature_window, infer_window
from oandafly.paths import secure_directory, state_directory
from oandafly.portfolio.equity import EquityLimits
from oandafly.portfolio.planner import OpenTrade, TargetStore, residual_plan
from oandafly.security.attestations import ApprovalVerifier, private_document, write_private_json
from oandafly.security.authorization import GrantAuthorizer, NativeProfileVerifier
from oandafly.security.vault import native_vault
from oandafly.storage.database import Database
from oandafly.storage.ledger import Ledger
from oandafly.worker.configuration import load_config
from oandafly.worker.control import ControlRequest, authority_directory
from oandafly.worker.risk_observations import assemble_risk, desired_units, entry_prices
from oandafly.worker.synchronization import AccountSynchronizer

CONTROL_SCHEMA = """
CREATE TABLE IF NOT EXISTS worker_control (
 authority_id TEXT PRIMARY KEY, halted INTEGER NOT NULL, flatten_requested INTEGER NOT NULL,
 last_request_id TEXT, last_operation TEXT);
CREATE TABLE IF NOT EXISTS worker_control_receipts (
 request_id TEXT PRIMARY KEY, operation TEXT NOT NULL, disposition TEXT NOT NULL, received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS worker_events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, detail TEXT NOT NULL, occurred_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_deployments (
 authority_id TEXT PRIMARY KEY, generation INTEGER NOT NULL, model_sha256 TEXT NOT NULL,
 model_path TEXT NOT NULL, validation_path TEXT NOT NULL, approval_path TEXT NOT NULL);
"""


class BootstrapSource:
    """Only the brand-new DATA source has no financial universe to retire."""
    @staticmethod
    def _require(source: ContextKey) -> None:
        if source.product_track != "DATA" or source.epoch != 0:
            raise SafetyFence("Bootstrap cannot drain or erase an existing trading source")

    def quiesce(self, source: ContextKey) -> None:
        self._require(source)

    def drain(self, source: ContextKey) -> None:
        self._require(source)

    def reconcile(self, source: ContextKey) -> StopProof:
        self._require(source)
        return StopProof(source_context_sha256=source.fingerprint, observed_at=utc_now(),
                         complete_account_universe=True)


class TradingWorker:
    def __init__(self, configuration: Path) -> None:
        self.configuration = configuration.absolute()
        self.config = load_config(self.configuration)
        self.base = self.configuration.parent
        self.mode = RuntimeMode(self.config.mode)
        self.root = authority_directory(self.config.authority_id)
        self.stop = asyncio.Event()
        self.model: InferenceProcess | None = None
        self.manifest: ModelManifest | None = None
        self.qualification: Qualification | None = None
        self.native: NativeExecutionAdapter | None = None
        self.gateway: NativeMutationGateway | None = None
        self.recovery: RecoveryRouter | None = None
        self.model_error = "NOT_STARTED"
        self.safety_error = "NOT_RECONCILED"
        self.last_decisions: dict[str, dict] = {}
        self._manual_resume = False
        self._fresh_lease = False
        self._model_attempt = 0.0
        self._promotion_attempt = 0.0
        self._selected_deployment: tuple[Path, Path, Path] | None = None
        self._action_lock = asyncio.Lock()
        self._policy_lock = asyncio.Lock()
        self._last_bar: dict[str, object] = {}
        self._last_archive: dict[str, object] = {}
        self.bars: dict[str, tuple] = {}
        self._health_ready = False
        self._last_health = 0.0
        self.verifier = ApprovalVerifier(self.path("trust_policy"))
        self.profile = VerifiedAccountProfile.model_validate_json(private_document(self.path("profile")))
        if (self.profile.authority_id != self.config.authority_id or self.profile.environment != self.config.environment
                or self.profile.account_ids != (self.config.account_id,)):
            raise SafetyFence("Worker configuration does not match its exact independently scoped profile")
        self.profile_verifier = NativeProfileVerifier(self.verifier, self.path("eligibility_approval"))
        self.authorizer = None if self.mode == RuntimeMode.SHADOW else GrantAuthorizer(
            verifier=self.verifier, document=self.path("authorization_grant"), mode=self.mode,
            account_id=self.config.account_id)
        self._account_ownership = AuthorityCoordinator.acquire_ownership(state_directory() / "native-account-locks",
            "native-" + self.config.environment + "-" + self.config.account_id, self.root / "state.sqlite3")
        ownership = AuthorityCoordinator.acquire_ownership(state_directory() / "locks",
                                                          self.config.authority_id, self.root / "state.sqlite3")
        try:
            self.db = Database(self.root / "state.sqlite3")
            self.authority = AuthorityCoordinator(self.db, self.config.authority_id, state_directory() / "locks",
                initial_context=ContextKey(authority_id=self.config.authority_id, environment=self.config.environment),
                config=EngineConfig(environment=self.config.environment, runtime_mode=self.config.mode),
                ownership=ownership, mutation_authorizer=self.authorizer)
        except BaseException:
            ownership.close()
            self._account_ownership.close()
            raise
        self.db.initialize(CONTROL_SCHEMA, component=__name__)
        with self.db.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO worker_control VALUES(?,0,0,NULL,NULL)", (self.config.authority_id,))
        self.ledger = Ledger(self.db)
        self.risk = RiskRegistry(self.db)
        self.outbox = Outbox(self.db, self.authority, self.risk)
        self.authority.barrier = self.outbox
        self.reducer = OutboxReducer(self.outbox, ledger=self.ledger)
        self.targets = TargetStore(self.db)
        self.equity = EquityLimits(self.db)
        boot_identity = Path("/proc/sys/kernel/random/boot_id").read_text().strip() if Path("/proc/sys/kernel/random/boot_id").exists() else None
        self.governor = LocalRateGovernor(self.db, requests_per_second=5, boot_identity=boot_identity)
        if self.config.vault_backend == "SYSTEMD":
            from oandafly.security.linux_vault import SystemdCredentialVault
            self.vault = SystemdCredentialVault()
        else:
            self.vault = native_vault()
        self.reader = NativeReadAdapter(environment=self.config.environment,
            credential=self.config.observation_credential, vault=self.vault, governor=self.governor)
        self.synchronizer = AccountSynchronizer(self.reader, self.ledger, self.reducer, self.config.account_id,
            self.config.instruments, timeout_seconds=self.config.reconciliation_timeout_seconds)
        self.targets.suspend_all(self.config.environment, self.config.account_id, reason="OWNER_RESTART_REQUIRES_FRESH_DECISION")
        # Provably unclaimed entries may be cancelled locally on restart. An
        # invocation marker or UNKNOWN outcome is never reset or retransmitted.
        with self.db.read() as conn:
            unclaimed = [r[0] for r in conn.execute("SELECT intent_id FROM outbox WHERE authority_id=? AND dispatch_state='NOT_DISPATCHED' AND status='PENDING_SUBMISSION'",
                                                 (self.config.authority_id,))]
        for identity in unclaimed:
            self.outbox.cancel_prepared(identity)
        self._establish_context()
        if self.authorizer is not None:
            try:
                self.native = NativeExecutionAdapter(environment=self.config.environment, mode=self.mode,
                    credential=self.config.execution_credential, vault=self.vault, governor=self.governor,
                    authorize=self._authorize_transport)
                self.gateway = NativeMutationGateway(self.outbox, transport=self.native,
                                                       governor=self.governor, reducer=self.reducer)
                self.recovery = RecoveryRouter(self.outbox, reducer=self.reducer,
                                                         transport=self.native, governor=self.governor)
            except Exception as exc:
                self.event("EXECUTION_UNAVAILABLE", type(exc).__name__)
                self.model_error = "EXECUTION_UNAVAILABLE"

    def path(self, name: str) -> Path:
        return self.config.path(name, relative_to=self.base)

    def event(self, event: str, detail: str) -> None:
        # Never serialize arbitrary native exceptions, account documents or tokens.
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO worker_events(event,detail,occurred_at) VALUES(?,?,?)",
                         (event, detail[:160], utc_now().isoformat()))

    def _establish_context(self) -> None:
        snapshot = self.authority.snapshot()
        if self.mode == RuntimeMode.SHADOW:
            if snapshot.context.product_track != "DATA":
                raise SafetyFence("SHADOW cannot attach to a mutation-bearing authority store")
            self.profile_verifier.verify(self.profile)
            return
        if snapshot.context.product_track == "TRADING":
            retained = self.authority.profile(snapshot.context, snapshot.lease_profile_version)
            if retained != self.profile:
                raise SafetyFence("An active account/profile cannot be replaced by editing configuration")
            return
        self.authority.register_profile(self.profile, self.profile_verifier)
        target = ContextKey(authority_id=self.config.authority_id, environment=self.config.environment,
                            product_track="TRADING", account_profile_id=self.profile.profile_id, epoch=1)
        transition = self.authority.request_transition(target, snapshot.revision, str(uuid4()))
        final = self.authority.run_transition(transition.transition_id, BootstrapSource(), self)
        if final.state != "READY_UNARMED":
            raise SafetyFence("Fresh trading authority could not complete its audited source retirement")
        self._fresh_lease = True

    def reset_transients(self, source: ContextKey, target: ContextKey) -> None:
        BootstrapSource._require(source)
        self.targets.suspend_all(self.config.environment, self.config.account_id, reason="FRESH_TRADING_CONTEXT")

    def initialize(self, target: ContextKey, profile: VerifiedAccountProfile | None) -> None:
        if profile != self.profile or target.environment != self.config.environment:
            raise SafetyFence("Initializer cannot enroll a different account or environment")
        # Recovery ownership can be established without an alpha model. Entry
        # authorization is checked separately after native reconciliation.
        self.authorizer.require(target, self.profile, "RECOVERY")

    def _authorize_transport(self, account_id: str, action: str) -> None:
        if account_id != self.config.account_id:
            raise SafetyFence("Native transport requested a different account")
        self._account_ownership.assert_held()
        with self.db.read() as conn:
            snapshot = self.authority.snapshot_in(conn)
            self.authority.assert_mutation(conn, snapshot.context, self.profile.version, action=action)

    def pending(self) -> bool:
        with self.db.read() as conn:
            return self.outbox.has_unresolved_in(conn, self.config.authority_id)

    def control(self) -> tuple[bool, bool]:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM worker_control WHERE authority_id=?", (self.config.authority_id,)).fetchone()
            return bool(row["halted"]), bool(row["flatten_requested"])

    def block_entries(self, reason: str) -> None:
        if self.authorizer is not None:
            self.authorizer.entry_block_reason = reason
        if self.authority.snapshot().armed:
            self.authority.disarm(reason)
        self._health_ready = False

    async def commands(self) -> None:
        directory = secure_directory(self.root / "commands")
        paths = sorted(directory.glob("*.json"))
        if len(paths) > 100:
            self.block_entries("CONTROL_MAILBOX_OVERFLOW")
            return
        # Entries recheck this mailbox at their final authority boundary through
        # safety control state; the independent watcher runs even during inference.
        for path in paths:
            try:
                request = ControlRequest.model_validate_json(private_document(path))
                now = utc_now()
                valid = (request.authority_id == self.config.authority_id and path.stem == str(request.request_id)
                         and request.issued_at <= now < request.expires_at
                         and (request.expires_at-request.issued_at).total_seconds() <= 60)
                with self.db.transaction() as conn:
                    old = conn.execute("SELECT 1 FROM worker_control_receipts WHERE request_id=?", (str(request.request_id),)).fetchone()
                    if old is not None:
                        path.unlink()
                        continue
                    disposition = "ACCEPTED" if valid else "EXPIRED_OR_WRONG_SCOPE"
                    conn.execute("INSERT INTO worker_control_receipts VALUES(?,?,?,?)",
                                 (str(request.request_id), request.operation, disposition, now.isoformat()))
                    if valid:
                        halted = int(request.operation != "RESUME")
                        flatten = int(request.operation == "FLATTEN")
                        conn.execute("UPDATE worker_control SET halted=?,flatten_requested=?,last_request_id=?,last_operation=? WHERE authority_id=?",
                                     (halted, flatten, str(request.request_id), request.operation, self.config.authority_id))
                if valid:
                    self.targets.suspend_all(self.config.environment, self.config.account_id, reason="CONTROL_"+request.operation)
                    if request.operation == "RESUME":
                        self._manual_resume = True
                    else:
                        self.block_entries("OPERATOR_"+request.operation)
                    if request.operation == "STOP":
                        self.stop.set()
                path.unlink()
            except Exception as exc:
                self.block_entries("CONTROL_DOCUMENT_INVALID")
                self.event("CONTROL_DOCUMENT_INVALID", type(exc).__name__)
                # Do not repeatedly parse an invalid document or follow its links.
                if not path.is_symlink():
                    path.rename(path.with_suffix(".rejected"))

    def verify(self, context: ContextKey, profile: VerifiedAccountProfile) -> ReadinessProof:
        if self.authorizer is None or self.model is None or self.qualification is None:
            raise SafetyFence("No qualified full-CNS execution policy is installed")
        self.profile_verifier.verify(profile)
        self.authorizer.require(context, profile, "ENTRY")
        self.reducer.require_caught_up(environment=self.config.environment, account_id=self.config.account_id)
        cut = self.synchronizer.latest
        if cut is None or time.monotonic()-self._last_health > self.config.observation_interval_seconds+2:
            raise SafetyFence("Fresh independent native safety evidence is absent")
        if self.pending() or self.control()[0] or not self._health_ready:
            raise SafetyFence("Unresolved execution, protection or operator state blocks readiness")
        return ReadinessProof(context_sha256=context.fingerprint, profile_version=profile.version,
                              observed_at=utc_now(), evidence_sha256=sha256_json({
                                  "account": cut.account.document.sha256, "pricing": cut.pricing.document_sha256,
                                  "model": self.manifest.fingerprint, "qualification": self.qualification.require()}),
                              structural_gate_verified=True, account_reconciled=True)

    async def load_model(self) -> None:
        if self.model is not None or time.monotonic()-self._model_attempt < 30:
            return
        self._model_attempt = time.monotonic()
        try:
            graph_path, model_path = self.path("graph_directory"), self.path("model_directory")
            validation_path = self.path("model_validation") if self.config.model_validation else None
            deployment_path = self.path("deployment_approval") if self.config.deployment_approval else None
            if self.authorizer is not None:
                if self._selected_deployment is not None:
                    model_path, validation_path, deployment_path = self._selected_deployment
                else:
                    with self.db.read() as conn:
                        saved = conn.execute("SELECT model_path,validation_path,approval_path FROM model_deployments WHERE authority_id=?",
                                             (self.config.authority_id,)).fetchone()
                    if saved is not None:
                        model_path, validation_path, deployment_path = (Path(value) for value in saved)
                grant = self.authorizer.grant()
                if grant.instruments != self.config.instruments:
                    raise SafetyFence("Runtime and authorized instrument universes differ")
                qualification = Qualification(graph_directory=graph_path, model_directory=model_path,
                    verifier=self.verifier, dataset_review=self.path("dataset_review"),
                    model_validation=validation_path, deployment_approval=deployment_path,
                    environment=self.config.environment, account_id=self.config.account_id,
                    grant_id=grant.grant_id, instruments=self.config.instruments)
            else:
                graph = GraphManifest.model_validate_json(manifest_document(graph_path / "manifest.json"))
                DatasetReview.from_wire(self.verifier.verify(self.path("dataset_review"), role="DATASET_REVIEW").payload).require(graph)
                qualification = None
            manifest = ModelManifest.model_validate_json(manifest_document(model_path / "manifest.json"))
            process = await asyncio.to_thread(InferenceProcess, graph=graph_path, model=model_path,
                instruments=self.config.instruments, require_containment=True,
                startup_timeout=self.config.model_startup_timeout_seconds,
                memory_bytes=self.config.inference_memory_gib * 1024**3)
            if process.model_sha256 != manifest.fingerprint:
                process.close()
                raise SafetyFence("Loaded model differs from the reviewed artifact")
            if qualification is not None:
                with self.db.transaction() as conn:
                    old = conn.execute("SELECT generation,model_sha256 FROM model_deployments WHERE authority_id=?",
                                       (self.config.authority_id,)).fetchone()
                    if old is not None and (old["generation"] > qualification.generation or
                        old["generation"] == qualification.generation and old["model_sha256"] != manifest.fingerprint):
                        process.close()
                        raise SafetyFence("Deployment generation cannot regress or be rebound")
                    conn.execute("INSERT INTO model_deployments VALUES(?,?,?,?,?,?) ON CONFLICT(authority_id) DO UPDATE SET generation=excluded.generation,model_sha256=excluded.model_sha256,model_path=excluded.model_path,validation_path=excluded.validation_path,approval_path=excluded.approval_path",
                        (self.config.authority_id, qualification.generation, manifest.fingerprint, str(model_path),
                         str(validation_path), str(deployment_path)))
            self.model, self.manifest, self.qualification = process, manifest, qualification
            if self.authorizer is not None:
                self.authorizer.qualification = qualification
            self.model_error = "NONE"
            self._selected_deployment = None
            self._last_bar.clear()
            self.event("FULL_MODEL_LOADED", manifest.fingerprint)
        except Exception as exc:
            self.model_error = type(exc).__name__
            self.block_entries("FULL_MODEL_UNAVAILABLE")
            self.event("FULL_MODEL_UNAVAILABLE", type(exc).__name__)

    async def maybe_promote(self) -> None:
        if (self.authorizer is None or self.config.registry_directory is None
                or time.monotonic()-self._promotion_attempt < 60):
            return
        self._promotion_attempt = time.monotonic()
        grant = self.authorizer.grant()
        if not grant.allow_automatic_promotion or self.control()[0]:
            return
        cut = self.synchronizer.latest
        if cut is None or cut.raw["trades"] or cut.raw["orders"] or self.pending():
            return
        from oandafly.training.registry import select_approved_candidate
        with self.db.read() as conn:
            old = conn.execute("SELECT generation FROM model_deployments WHERE authority_id=?", (self.config.authority_id,)).fetchone()
        candidate = select_approved_candidate(directory=self.path("registry_directory"),
            graph=self.path("graph_directory"), dataset_review=self.path("dataset_review"),
            verifier=self.verifier, environment=self.config.environment, account_id=self.config.account_id,
            grant_id=grant.grant_id, instruments=self.config.instruments,
            after_generation=0 if old is None else old[0])
        if candidate is None:
            return
        async with self._action_lock:
            current = self.synchronizer.latest
            if current is None or current.raw["trades"] or current.raw["orders"] or self.pending():
                return
            self.block_entries("QUALIFIED_MODEL_PROMOTION")
            self.targets.suspend_all(self.config.environment, self.config.account_id, reason="QUALIFIED_MODEL_PROMOTION")
            if self.model is not None:
                await asyncio.to_thread(self.model.close)
            self.model = None
            self.authorizer.qualification = None
            self.qualification = None
            self._selected_deployment = (candidate.model, candidate.validation, candidate.deployment)
            self._model_attempt = 0.0
            await self.load_model()
            if self.model is None:
                # Keep entries fenced. A subsequent load may only restore the
                # previously persisted champion after re-validating its approvals.
                self._selected_deployment = None
            self.event("MODEL_PROMOTION_ATTEMPT", str(candidate.qualification.generation))

    async def _close_trade(self, identity: str, units: Decimal, *, reason: str, action_id: UUID | None = None) -> None:
        if self.recovery is None:
            raise SafetyFence("Native recovery capability is unavailable")
        with self.db.read() as conn:
            pending = conn.execute("SELECT action_id,state FROM execution_recovery WHERE authority_id=? AND target_id=? AND state IN ('PREPARED','DISPATCH_STARTED','UNKNOWN')",
                                   (self.config.authority_id, identity)).fetchone()
        if pending is not None:
            if pending["state"] == "PREPARED":
                await self.recovery.dispatch(pending["action_id"])
            return  # Never multiply an unresolved exact-trade close.
        snapshot = self.authority.snapshot()
        action_id = action_id or uuid5(NAMESPACE_URL, "oandafly:protection:"+self.config.authority_id+":"+identity+":"+reason)
        with self.db.read() as conn:
            old = conn.execute("SELECT state FROM execution_recovery WHERE action_id=?", (str(action_id),)).fetchone()
        if old is not None:
            return
        record = self.recovery.prepare(context=snapshot.context, account_id=self.config.account_id,
            profile_version=self.profile.version, operation="CLOSE_TRADE", target_id=identity,
            units=abs(units), action_id=action_id)
        await self.recovery.dispatch(record.action_id)

    async def safety_cycle(self) -> None:
        error = None
        try:
            cut = await self.synchronizer.refresh()
        except Exception as exc:
            cut = None
            error = type(exc).__name__
        account = self.synchronizer.latest_account
        if account is None or (utc_now()-account.summary.observed_at).total_seconds() > 10:
            self.block_entries("NATIVE_ACCOUNT_UNAVAILABLE")
            self.safety_error = error or "NATIVE_ACCOUNT_UNAVAILABLE"
            return
        halted, flatten = self.control()
        protection_ok, loss_ok = True, True
        if self.authorizer is not None:
            grant = self.authorizer.grant()
            equity = self.equity.observe(environment=self.config.environment, account_id=self.config.account_id,
                nav=account.summary.nav, frontier=account.summary.last_transaction_id,
                observed_at=account.summary.observed_at, source_sha256=account.document.sha256, limits=grant.limits)
            loss_ok = equity.hard_fence is None
            raw = account.document.body["account"]
            async with self._action_lock:
                # Only unconditional entry orders are cancelled. Attached stop
                # orders remain in place until the associated trade is closed.
                for order in raw["orders"]:
                    if order.get("tradeID") is None and order["type"] in {"LIMIT", "STOP", "MARKET_IF_TOUCHED"}:
                        protection_ok = False
                        if self.recovery is not None:
                            action_id = uuid5(NAMESPACE_URL, "oandafly:cancel:"+self.config.authority_id+":"+order["id"])
                            with self.db.read() as conn:
                                exists = conn.execute("SELECT 1 FROM execution_recovery WHERE action_id=?", (str(action_id),)).fetchone()
                            if exists is None:
                                record = self.recovery.prepare(context=self.authority.snapshot().context,
                                    account_id=self.config.account_id, profile_version=self.profile.version,
                                    operation="CANCEL_ORDER", target_id=order["id"], action_id=action_id)
                                await self.recovery.dispatch(record.action_id)
                for trade in raw["trades"]:
                    stop_order = trade.get("guaranteedStopLossOrder") or trade.get("stopLossOrder")
                    stop_ok = (isinstance(stop_order, dict) and stop_order.get("state") == "PENDING"
                               and money(stop_order.get("price", "0")) > 0)
                    age = (utc_now()-native_time(trade["openTime"])).total_seconds()
                    too_old = age > GRANULARITIES[self.config.granularity] * self.config.maximum_holding_bars
                    if not stop_ok or not loss_ok or flatten or too_old:
                        protection_ok = False
                        reason = "LOSS_FENCE" if not loss_ok else "OPERATOR_FLATTEN" if flatten else "MISSING_PROTECTION" if not stop_ok else "MAXIMUM_HOLDING_TIME"
                        await self._close_trade(trade["id"], money(trade["currentUnits"]), reason=reason)
            if flatten and not raw["trades"] and not raw["orders"] and not self.pending():
                with self.db.transaction() as conn:
                    conn.execute("UPDATE worker_control SET flatten_requested=0 WHERE authority_id=?", (self.config.authority_id,))
        ready = cut is not None and not error and protection_ok and loss_ok and not halted and not self.pending()
        self._last_health = time.monotonic()
        self._health_ready = ready
        self.safety_error = "NONE" if ready else error or "PROTECTION_LOSS_CONTROL_OR_PENDING"
        if not ready:
            self.block_entries(self.safety_error)
        elif self.authorizer is not None and self.model is not None and self.native is not None:
            try:
                self.authorizer.entry_block_reason = None
                snapshot = self.authority.snapshot()
                grant = self.authorizer.grant()
                may_resume = self._fresh_lease or self._manual_resume or grant.allow_unattended_resume
                if snapshot.lease_status == "RECOVERY":
                    if not may_resume:
                        raise SafetyFence("Restart requires explicit resume or an unattended-resume grant")
                    snapshot = self.authority.resume_verified(snapshot.revision, self)
                if not snapshot.armed:
                    if not may_resume:
                        raise SafetyFence("Disarmed authority requires authorized resume")
                    self.authority.arm(snapshot.revision, str(uuid4()), self, utc_now()+timedelta(seconds=15))
                    self._manual_resume = False
                    self._fresh_lease = False
                self._health_ready = True
            except Exception as exc:
                self.block_entries(type(exc).__name__)

    async def policy_cycle(self) -> None:
        await self.load_model()
        await self.maybe_promote()
        cut = self.synchronizer.latest
        if self.model is None or self.manifest is None or cut is None:
            return
        if self.authorizer is not None and (not self._health_ready or not self.authority.snapshot().armed):
            return
        for metadata in cut.instruments:
            bars = self.bars.get(metadata.name, ())
            if not bars:
                continue
            bars = bars[-(self.manifest.kernel.neural_warmup_bars+32):]
            if self._last_bar.get(metadata.name) == bars[-1].end:
                continue
            self._last_bar[metadata.name] = bars[-1].end
            if (utc_now()-bars[-1].end).total_seconds() > GRANULARITIES[self.config.granularity]+5:
                continue
            observed = self.synchronizer.latest
            if observed is None:
                return
            market_cut = MarketCut(instrument=metadata.name, as_of=utc_now(), quote=observed.pricing.quote(metadata.name),
                candles=bars, account_frontier=observed.cursor, account_sha256=observed.account.document.sha256)
            window = feature_window(bars, self.manifest.kernel.neural_warmup_bars)
            budget = self.qualification.deadline_seconds if self.qualification else 30
            result = await infer_window(self.model, metadata.name, window, deadline_seconds=budget) if window.frames else None
            selected = decision(market_cut, self.manifest, utilities=None if result is None else result.utilities,
                observations=0 if result is None else result.observations,
                warmup_required=self.manifest.kernel.neural_warmup_bars,
                reason=None if result is not None else window.state, validity_seconds=min(60, max(10, math.ceil(budget)+5)))
            self.last_decisions[metadata.name] = selected.model_dump(mode="json")
            if self.authorizer is None:
                self.event("SHADOW_DECISION", selected.decision_id)
                continue
            if self.targets.seen(environment=self.config.environment, account_id=self.config.account_id,
                                 instrument=metadata.name, model_sha256=self.manifest.fingerprint, bar_end=bars[-1].end):
                continue
            current = self.synchronizer.latest
            if current is None or not self._health_ready:
                continue
            grant = self.authorizer.grant()
            risk = assemble_risk(cut=current, profile=self.profile, grant=grant,
                authority=self.authority.snapshot(), instrument=metadata.name,
                gates={"independent_health": self._health_ready},
                unbooked_financing=self.ledger.balance(self.config.environment, self.config.account_id).unbooked_financing)
            units = desired_units(selected, risk, grant)
            if units == 0 and selected.action == Action.SET_TARGET:
                units = None  # Insufficient risk-sized quantity does not mean FLATTEN.
            self.targets.accept(selected, environment=self.config.environment, account_id=self.config.account_id,
                bar_end=bars[-1].end, units=units, frontier=current.cursor,
                target_expires_at=min(grant.expires_at, bars[-1].end+timedelta(seconds=GRANULARITIES[self.config.granularity])))

    async def execute_targets(self) -> None:
        if self.authorizer is None or self.gateway is None or not self._health_ready or not self.authority.snapshot().armed:
            return
        async with self._action_lock:
            cut = self.synchronizer.latest
            if cut is None or self.pending():
                return
            grant = self.authorizer.grant()
            for metadata in cut.instruments:
                target = self.targets.current(self.config.environment, self.config.account_id, metadata.name)
                if target is None or self.manifest is None or target.model_sha256 != self.manifest.fingerprint:
                    continue
                trades = tuple(OpenTrade(t["id"], t["instrument"], money(t["currentUnits"]))
                               for t in cut.raw["trades"] if t["instrument"] == metadata.name)
                step = residual_plan(target.units, trades)
                action_id = TargetStore.action_id(target, step)
                if step.operation == "CLOSE_TRADE":
                    await self._close_trade(step.trade_id, step.units, reason=step.reason, action_id=action_id)
                    return  # Reconcile the native close before considering any reversal entry.
                if step.operation != "ENTRY":
                    continue
                with self.db.read() as conn:
                    existing = conn.execute("SELECT 1 FROM outbox WHERE intent_id=?", (str(action_id),)).fetchone()
                if existing is not None:
                    continue
                risk = assemble_risk(cut=cut, profile=self.profile, grant=grant, authority=self.authority.snapshot(),
                    instrument=metadata.name, gates={"independent_health": self._health_ready},
                    unbooked_financing=self.ledger.balance(self.config.environment, self.config.account_id).unbooked_financing)
                self.risk.publish(risk)
                stop, bound = entry_prices(step.units, risk.instrument, grant, metadata.display_precision)
                request = OrderRequest(context=self.authority.snapshot().context, account_id=self.config.account_id,
                    profile_version=self.profile.version, instrument=metadata.name, units=step.units,
                    portfolio_target_sha256=target.fingerprint,
                    stop_price=stop, price_bound=bound, evidence=risk.evidence)
                await self.gateway.submit_order(request, intent_id=action_id)
                return  # At most one write before another independently reconciled account cut.

    def status(self) -> None:
        snapshot = self.authority.snapshot()
        cut = self.synchronizer.latest
        write_private_json(self.root / "status.json", {
            "application": "oandafly", "version": "1.0.0", "updated_at": utc_now().isoformat(),
            "authority_id": self.config.authority_id, "environment": self.config.environment, "mode": self.config.mode,
            "account_id": self.config.account_id, "authority": snapshot.model_dump(mode="json"),
            "safety": self.safety_error, "model_error": self.model_error,
            "model_sha256": None if self.manifest is None else self.manifest.fingerprint,
            "covered_frontier": None if cut is None else cut.cursor,
            "control": {"halted": self.control()[0], "flatten_requested": self.control()[1]},
            "last_decisions": self.last_decisions, "pending_mutations": self.pending(),
        }, replace=True)

    async def _safety_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await self.safety_cycle()
                await self.execute_targets()
            except Exception as exc:
                self.safety_error = type(exc).__name__
                self.block_entries(self.safety_error)
                self.event("SAFETY_CYCLE_BLOCKED", self.safety_error)
            self.status()
            await asyncio.sleep(self.config.observation_interval_seconds)

    async def _policy_loop(self) -> None:
        while not self.stop.is_set():
            try:
                async with self._policy_lock:
                    await self.policy_cycle()
            except Exception as exc:
                self.model_error = type(exc).__name__
                self.block_entries("MODEL_OR_POLICY_CYCLE_FAILED")
                if self.model is not None:
                    await asyncio.to_thread(self.model.close)
                    self.model = None
                self.event("MODEL_OR_POLICY_CYCLE_FAILED", self.model_error)
            await asyncio.sleep(2)

    async def _data_loop(self) -> None:
        """Native history capture is independent of an already-trained model."""
        next_boundary: dict[str, int] = {}
        seconds = GRANULARITIES[self.config.granularity]
        while not self.stop.is_set():
            cut = self.synchronizer.latest
            if cut is not None:
                for metadata in cut.instruments:
                    boundary = int(utc_now().timestamp()) // seconds
                    if next_boundary.get(metadata.name) == boundary:
                        continue
                    try:
                        previous = self.bars.get(metadata.name, ())
                        new = await self.reader.candles(self.config.account_id, metadata,
                            granularity=self.config.granularity, count=5000 if not previous else 512,
                            from_time=None if not previous else previous[-1].start)
                        if new:
                            if previous and new[0].start < previous[-1].end:
                                raise SafetyFence("Incremental history overlaps an immutable completed bar")
                            CandleArchive(secure_directory(self.root / "candles" / metadata.name)).write(new)
                            self.bars[metadata.name] = (previous+new)[-5000:]
                        # Do not suppress a delayed just-completed bar until the
                        # next interval; retry within the normal observation cadence.
                        latest = self.bars.get(metadata.name, ())
                        if latest and int(latest[-1].end.timestamp()) >= boundary*seconds:
                            next_boundary[metadata.name] = boundary
                    except Exception as exc:
                        self.event("CANDLE_CAPTURE_BLOCKED", type(exc).__name__)
            await asyncio.sleep(2)

    async def _control_loop(self) -> None:
        while not self.stop.is_set():
            await self.commands()
            await asyncio.sleep(0.25)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop.set)
            except NotImplementedError:
                pass
        tasks = [asyncio.create_task(self._safety_loop()), asyncio.create_task(self._policy_loop()),
                 asyncio.create_task(self._control_loop()), asyncio.create_task(self._data_loop())]
        stop_waiter = asyncio.create_task(self.stop.wait())
        try:
            done, _ = await asyncio.wait([*tasks, stop_waiter], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is not stop_waiter and not task.cancelled() and task.exception() is not None:
                    raise task.exception()
        finally:
            self.stop.set()
            self.block_entries("WORKER_STOPPING")
            self.targets.suspend_all(self.config.environment, self.config.account_id, reason="WORKER_STOPPING")
            for task in [*tasks, stop_waiter]:
                task.cancel()
            await asyncio.gather(*tasks, stop_waiter, return_exceptions=True)
            if self.model is not None:
                await asyncio.to_thread(self.model.close)
            await self.reader.close()
            if self.native is not None:
                await self.native.close()
            self.safety_error = "WORKER_STOPPED_NO_ENTRY_AUTHORITY"
            self.status()
            self.authority.close()
            self.db.close()
            self._account_ownership.close()


def run_worker(configuration: Path) -> None:
    asyncio.run(TradingWorker(configuration).run())
