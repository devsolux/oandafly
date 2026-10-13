"""Coherent native account cuts backed by unfiltered transaction history.

A full snapshot is intentionally used for the first correctness-oriented release.
The incremental /changes and streams are available adapter capabilities, not
substitutes for canonical history coverage. No opening balance is ever reset.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal

from oandafly.adapters.oanda.observations import NativeReadAdapter, PricingRead
from oandafly.adapters.v20 import AccountRead
from oandafly.domain.errors import SafetyFence
from oandafly.domain.financial import FINANCIAL
from oandafly.domain.instruments import CurrencyInstrument
from oandafly.domain.primitives import money, utc_now
from oandafly.execution.reconciler import OutboxReducer
from oandafly.storage.ledger import Ledger


@dataclass(frozen=True)
class AccountCut:
    account: AccountRead
    pricing: PricingRead
    instruments: tuple[CurrencyInstrument, ...]

    @property
    def cursor(self) -> str:
        return self.account.summary.last_transaction_id

    @property
    def raw(self) -> dict:
        return self.account.document.body["account"]


class AccountSynchronizer:
    def __init__(self, adapter: NativeReadAdapter, ledger: Ledger, reducer: OutboxReducer,
                 account_id: str, instruments: tuple[str, ...], *, timeout_seconds: int = 30) -> None:
        self.adapter, self.ledger, self.reducer = adapter, ledger, reducer
        self.db = ledger.db
        self.environment, self.account_id = adapter.environment, account_id
        self.universe, self.timeout = instruments, timeout_seconds
        self.latest: AccountCut | None = None
        self.latest_account: AccountRead | None = None
        self._lock = asyncio.Lock()
        self._metadata: tuple[CurrencyInstrument, ...] = ()
        self._metadata_at = 0.0

    async def refresh(self) -> AccountCut:
        async with self._lock:
            # A bounded caller deadline also covers an unstable moving snapshot.
            async with asyncio.timeout(self.timeout):
                return await self._refresh()

    async def _refresh(self) -> AccountCut:
        self.latest = None
        if not self._metadata or time.monotonic() - self._metadata_at > 300:
            observed = await self.adapter.instruments(self.account_id)
            by_name = {item.name: item for item in observed.admitted}
            if not set(self.universe) <= set(by_name):
                raise SafetyFence("Configured instruments are absent from the actual native account")
            self._metadata = tuple(by_name[name] for name in self.universe)
            self._metadata_at = time.monotonic()
        while True:
            account = await self.adapter.account(self.account_id)
            if account.unsupported_instruments:
                raise SafetyFence("Account contains unsupported assets; entries require a dedicated FX scope")
            raw, summary = account.document.body["account"], account.summary
            if any(t["instrument"] not in self.universe for t in raw["trades"]):
                raise SafetyFence("Native exposure lies outside the enrolled instrument universe")
            with self.db.transaction() as conn:
                old = conn.execute("SELECT baseline_cursor FROM ledger_accounts WHERE environment=? AND account_id=?",
                                   (self.environment, self.account_id)).fetchone()
                if old is None:
                    # One commit: a crash cannot leave cash enrolled but omit its
                    # nonzero execution baseline, or silently invent a new one.
                    self.ledger.establish_baseline_in(conn, self.environment, self.account_id,
                                                     summary.home_currency, summary.balance,
                                                     summary.last_transaction_id, summary.observed_at)
                    self.reducer.seed_snapshot_in(conn, environment=self.environment,
                                                  account_id=self.account_id, cursor=summary.last_transaction_id,
                                                  trades=raw["trades"], orders=raw["orders"])
            self.ledger.record_heartbeat(self.environment, self.account_id, summary.last_transaction_id)
            balance = self.ledger.balance(self.environment, self.account_id)
            history = await self.adapter.since_id(self.account_id, balance.covered_cursor)
            if not self.reducer.apply_history(environment=self.environment, account_id=self.account_id,
                                               requested_from_id=history.from_transaction_id,
                                               transactions=history.transactions,
                                               last_transaction_id=history.last_transaction_id):
                raise SafetyFence("Native history cannot produce a coherent execution projection")
            if history.last_transaction_id != summary.last_transaction_id:
                if int(history.last_transaction_id) < int(summary.last_transaction_id):
                    raise SafetyFence("History response precedes the actual native snapshot")
                continue  # New transaction crossed the observation cut; fetch it again.
            if not self.ledger.checkpoint(self.environment, self.account_id, summary.balance,
                                          summary.last_transaction_id, summary.observed_at):
                raise SafetyFence("Native cash does not match transaction-covered cash")
            self.reducer.require_caught_up(environment=self.environment, account_id=self.account_id)
            self._compare_execution(raw)
            self.latest_account = account
            pricing = await self.adapter.pricing(self.account_id, self._metadata)
            if (utc_now() - summary.observed_at).total_seconds() > 10:
                continue
            result = AccountCut(account, pricing, self._metadata)
            self.latest = result
            return result

    @FINANCIAL.calculation
    def _compare_execution(self, raw: dict) -> None:
        native = {t["id"]: (t["instrument"], money(t["currentUnits"])) for t in raw["trades"]}
        if len(native) != len(raw["trades"]):
            raise SafetyFence("Duplicate native trade IDs in account snapshot")
        with self.db.read() as conn:
            projected = {r["trade_id"]: (r["instrument"], money(r["current_units"])) for r in conn.execute(
                "SELECT trade_id,instrument,current_units FROM execution_trades WHERE environment=? AND account_id=?",
                (self.environment, self.account_id)) if money(r["current_units"]) != 0}
            projected_orders = {r[0] for r in conn.execute(
                "SELECT order_id FROM execution_orders WHERE environment=? AND account_id=? AND state='PENDING'",
                (self.environment, self.account_id))}
        if native != projected:
            raise SafetyFence("Native gross trades differ from canonical covered projection")
        native_orders = {o["id"] for o in raw["orders"] if o.get("state", "PENDING") == "PENDING"}
        if len(native_orders) != len(raw["orders"]) or projected_orders != native_orders:
            raise SafetyFence("Native pending orders differ from canonical covered projection")
        expected: dict[str, list[Decimal]] = {}
        for instrument, units in native.values():
            sides = expected.setdefault(instrument, [Decimal(0), Decimal(0)])
            sides[0 if units > 0 else 1] += units
        seen: set[str] = set()
        for position in raw["positions"]:
            name = position["instrument"]
            if name in seen:
                raise SafetyFence("Duplicate native position instrument")
            seen.add(name)
            observed = [money(position["long"]["units"]), money(position["short"]["units"])]
            if observed != expected.get(name, [Decimal(0), Decimal(0)]):
                raise SafetyFence("Native positions and gross trade sides disagree")
        if not set(expected) <= seen:
            raise SafetyFence("Native position collection omits open trades")
