"""Independent Decimal risk calculations; native margin is never pooled.

Native ``marginAvailable`` is a broker observation, not a quantity reconstructed
from the local tier model.  The independent stress branch can only tighten it.
The models in this module describe verified observations supplied by the worker;
they are not an API through which a UI or model can assert account eligibility.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Literal

from pydantic import Field, field_validator, model_validator

from oandafly.domain.errors import SafetyFence, ValidationFailure
from oandafly.domain.financial import FINANCIAL
from oandafly.domain.primitives import StrictModel, decimal_text, money, require_utc

ZERO = Decimal("0")
ONE = Decimal("1")
REGULATORY_MAJOR_CURRENCIES = frozenset({"USD", "EUR", "JPY", "GBP", "CAD", "CHF"})


class HomeConversions(StrictModel):
    """Account-home units for one quote-currency unit, from one pricing cut."""

    currency: str = Field(pattern=r"^[A-Z]{3}$")
    position_value: Decimal = Field(gt=0)
    account_gain: Decimal = Field(gt=0)
    account_loss: Decimal = Field(gt=0)

    @field_validator("position_value", "account_gain", "account_loss", mode="before")
    @classmethod
    def exact_numbers(cls, value: object) -> Decimal:
        return money(value)  # type: ignore[arg-type]

    @property
    def adverse(self) -> Decimal:
        return max(self.position_value, self.account_gain, self.account_loss)


class MarginTier(StrictModel):
    """Inclusive cumulative upper notional bound in account-home currency."""

    upper_notional: Decimal | None = None
    rate: Decimal = Field(gt=0, le=1)

    @field_validator("upper_notional", "rate", mode="before")
    @classmethod
    def exact_numbers(cls, value: object) -> Decimal | None:
        return None if value is None else money(value)  # type: ignore[arg-type]


class InstrumentRisk(StrictModel):
    name: str = Field(pattern=r"^[A-Z]{3}_[A-Z]{3}$")
    asset_type: Literal["CURRENCY"] = "CURRENCY"
    # Synthetic qualification must never be relabelled as real legal eligibility.
    legal_product: Literal["NON_CFD_FX", "CFD_FX", "SYNTHETIC_FX"] = "NON_CFD_FX"
    base_currency: str = Field(pattern=r"^[A-Z]{3}$")
    quote_currency: str = Field(pattern=r"^[A-Z]{3}$")
    trade_units_precision: int = Field(ge=0, le=8)
    pip_location: int = Field(ge=-8, le=0)
    minimum_trade_size: Decimal = Field(gt=0)
    maximum_order_units: Decimal = Field(gt=0)
    maximum_position_units: Decimal = Field(gt=0)
    bid: Decimal = Field(gt=0)
    ask: Decimal = Field(gt=0)
    conversions: HomeConversions
    requested_leverage: Decimal = Field(gt=0)
    broker_max_leverage: Decimal = Field(gt=0)
    german_retail: bool = True
    margin_tiers: tuple[MarginTier, ...] = Field(min_length=1)
    current_gross_units: Decimal = Field(default=ZERO, ge=0)
    maximum_spread_pips: Decimal = Field(gt=0)

    @field_validator(
        "minimum_trade_size", "maximum_order_units", "maximum_position_units",
        "bid", "ask", "requested_leverage", "broker_max_leverage",
        "current_gross_units", "maximum_spread_pips", mode="before",
    )
    @classmethod
    def exact_numbers(cls, value: object) -> Decimal:
        return money(value)  # type: ignore[arg-type]

    @model_validator(mode="after")
    @FINANCIAL.calculation
    def check_contract(self) -> InstrumentRisk:
        from oandafly.domain.instruments import CurrencyInstrument
        CurrencyInstrument(name=self.name)
        if self.name != f"{self.base_currency}_{self.quote_currency}":
            raise ValueError("instrument identity disagrees with base/quote currencies")
        if self.base_currency == self.quote_currency:
            raise ValueError("a currency pair must contain two distinct currencies")
        # The legal-product attestation is independently verified by the account
        # profile.  These fields also prevent any downstream accidental widening.
        prohibited = {"XAU", "XAG", "XPT", "XPD", "BTC", "ETH", "USDT", "USDC"}
        if {self.base_currency, self.quote_currency} & prohibited:
            raise ValueError("non-currency asset is outside the FX universe")
        if self.conversions.currency != self.quote_currency:
            raise ValueError("HomeConversions must convert this pair's quote currency")
        if self.ask < self.bid:
            raise ValueError("crossed bid/ask observation")
        regulatory_cap = Decimal("30") if {
            self.base_currency, self.quote_currency
        } <= REGULATORY_MAJOR_CURRENCIES else Decimal("20")
        cap = min(self.broker_max_leverage, regulatory_cap) if self.german_retail else self.broker_max_leverage
        if self.requested_leverage > cap:
            raise ValueError("leverage exceeds the stricter verified broker/retail cap")
        previous_upper = ZERO
        previous_rate = ZERO
        # An explicitly lower requested leverage must also tighten margin tiers.
        effective_leverage = min(cap, self.requested_leverage)
        for index, tier in enumerate(self.margin_tiers):
            if tier.rate * effective_leverage < ONE:
                raise ValueError("margin tier is below the effective leverage floor")
            if tier.rate < previous_rate:
                raise ValueError("margin rates must not decrease with position size")
            if tier.upper_notional is None:
                if index != len(self.margin_tiers) - 1:
                    raise ValueError("only the final tier may be unbounded")
            elif tier.upper_notional <= previous_upper:
                raise ValueError("tier limits must increase strictly")
            else:
                previous_upper = tier.upper_notional
            previous_rate = tier.rate
        if self.margin_tiers[-1].upper_notional is not None:
            raise ValueError("the final risk tier must cover the remaining exposure")
        return self

    @property
    @FINANCIAL.calculation
    def pip(self) -> Decimal:
        return ONE.scaleb(self.pip_location)

    @property
    @FINANCIAL.calculation
    def adverse_unit_notional(self) -> Decimal:
        return self.ask * self.conversions.adverse

    @property
    @FINANCIAL.calculation
    def spread_pips(self) -> Decimal:
        return (self.ask - self.bid) / self.pip


class MarginState(StrictModel):
    """The debit terms exclude amounts already reflected in their own branch."""

    native_margin_available: Decimal
    stressed_equity: Decimal
    independent_margin_requirement: Decimal = Field(ge=0)
    native_unreflected_debits: Decimal = Field(default=ZERO, ge=0)
    stress_unreflected_debits: Decimal = Field(default=ZERO, ge=0)
    incremental_reservations: Decimal = Field(default=ZERO, ge=0)
    safety_buffer: Decimal = Field(default=ZERO, ge=0)
    gates: Mapping[str, bool]

    @field_validator(
        "native_margin_available", "stressed_equity", "independent_margin_requirement",
        "native_unreflected_debits", "stress_unreflected_debits",
        "incremental_reservations", "safety_buffer", mode="before",
    )
    @classmethod
    def exact_numbers(cls, value: object) -> Decimal:
        return money(value)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class MarginDecision:
    native_bound: Decimal
    stress_bound: Decimal
    spendable: Decimal
    blocked_gates: tuple[str, ...]


@FINANCIAL.calculation
def spendable_margin(state: MarginState) -> MarginDecision:
    """Joint minimum bound for exactly one enrolled account, without pooling."""
    state = MarginState.model_validate(state)
    blocked = tuple(sorted(key for key, ready in state.gates.items() if not ready))
    if not state.gates:
        blocked = ("missing_gating_evidence",)
    native = max(ZERO, state.native_margin_available - state.native_unreflected_debits)
    stress = max(
        ZERO, state.stressed_equity - state.independent_margin_requirement
        - state.stress_unreflected_debits - state.safety_buffer,
    )
    available = max(ZERO, min(native, stress) - state.incremental_reservations)
    return MarginDecision(native, stress, ZERO if blocked else available, blocked)


@FINANCIAL.calculation
def tier_margin(notional: Decimal, tiers: tuple[MarginTier, ...]) -> Decimal:
    if notional < ZERO or not notional.is_finite():
        raise ValidationFailure("notional must be a finite nonnegative Decimal")
    remaining = notional
    lower = ZERO
    result = ZERO
    for tier in tiers:
        portion = remaining if tier.upper_notional is None else min(remaining, tier.upper_notional - lower)
        result += portion * tier.rate
        remaining -= portion
        if remaining == ZERO:
            return result
        if tier.upper_notional is not None:
            lower = tier.upper_notional
    raise SafetyFence("tier policy does not cover the proposed gross notional")


@FINANCIAL.calculation
def incremental_margin(units: Decimal, instrument: InstrumentRisk) -> Decimal:
    money(units)
    instrument = InstrumentRisk.model_validate(instrument)
    current = instrument.current_gross_units * instrument.adverse_unit_notional
    proposed = (instrument.current_gross_units + abs(units)) * instrument.adverse_unit_notional
    return tier_margin(proposed, instrument.margin_tiers) - tier_margin(current, instrument.margin_tiers)


@FINANCIAL.calculation
def rounded_target_units(
    *, direction: Literal[-1, 1], equity: Decimal, allocation: Decimal,
    instrument: InstrumentRisk, free_margin: Decimal,
    stop_distance: Decimal, maximum_loss: Decimal, gap_buffer: Decimal = ZERO,
) -> Decimal:
    """Toward-zero lattice sizing bounded by equity, tier margin and stop loss.

    Bisection uses integer unit ticks, so tier boundaries and final rounding can
    never make the result exceed a budget.  It also accommodates fractional
    ``tradeUnitsPrecision`` without binary floating point.
    """
    for value in (equity, allocation, free_margin, stop_distance, maximum_loss, gap_buffer):
        money(value)
    instrument = InstrumentRisk.model_validate(instrument)
    if type(direction) is not int or direction not in (-1, 1):
        raise ValidationFailure("direction must be -1 or +1")
    if not ZERO <= allocation <= ONE or min(equity, free_margin, maximum_loss) < ZERO:
        raise ValidationFailure("invalid equity, allocation or risk budget")
    if stop_distance <= ZERO or gap_buffer < ZERO:
        raise ValidationFailure("a positive protective stop distance is required")
    if instrument.spread_pips > instrument.maximum_spread_pips:
        return ZERO
    tick = ONE.scaleb(-instrument.trade_units_precision)
    with FINANCIAL.local():
        notional_units = equity * allocation / instrument.adverse_unit_notional
        loss_per_unit = (stop_distance + gap_buffer + instrument.ask - instrument.bid) * instrument.conversions.adverse
        loss_units = maximum_loss / loss_per_unit
        position_room = max(ZERO, instrument.maximum_position_units - instrument.current_gross_units)
        cap = min(notional_units, loss_units, instrument.maximum_order_units, position_room)
        low, high = 0, int((cap / tick).to_integral_value(rounding=ROUND_DOWN))
        while low < high:
            midpoint = (low + high + 1) // 2
            if incremental_margin(Decimal(midpoint) * tick, instrument) <= free_margin:
                low = midpoint
            else:
                high = midpoint - 1
        units = Decimal(low) * tick
        return ZERO if units < instrument.minimum_trade_size else Decimal(direction) * units


class RiskEvidence(StrictModel):
    """A source-version cut, not an assertion of approval by an untrusted caller."""

    revision: str = Field(min_length=1, max_length=128)
    authority_revision: int = Field(ge=0)
    profile_version: int = Field(ge=1)
    account_revision: str = Field(min_length=1, max_length=128)
    pricing_revision: str = Field(min_length=1, max_length=128)
    policy_revision: str = Field(min_length=1, max_length=128)
    conversion_revision: str = Field(min_length=1, max_length=128)
    covered_transaction_id: str = Field(default="0", pattern=r"^[0-9]+$")
    valid_until: datetime

    @field_validator("valid_until")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        return require_utc(value)


class AccountExposureRisk(StrictModel):
    """Conservative gross (not netted) native exposure in account-home units."""
    equity: Decimal = Field(gt=0)
    current_gross_notional: Decimal = Field(ge=0)
    currency_gross_notional: Mapping[str, Decimal]
    maximum_gross_leverage: Decimal = Field(gt=0, le=30)
    maximum_currency_fraction: Decimal = Field(gt=0, le=5)

    @field_validator("currency_gross_notional")
    @classmethod
    def valid_currency_values(cls, value: Mapping[str, Decimal]) -> Mapping[str, Decimal]:
        from oandafly.domain.instruments import currency_code
        for currency, amount in value.items():
            currency_code(currency)
            if amount < 0:
                raise ValueError("Currency gross exposure cannot be negative")
        return value


@FINANCIAL.calculation
def require_account_capacity(exposure: AccountExposureRisk, *, base: str, quote: str,
                             new_notional: Decimal, other_reserved_margin: Decimal) -> None:
    # Every admitted margin tier is at least 1/max_gross_leverage. Therefore
    # reserved_margin * max_gross_leverage is a conservative upper bound on
    # pending gross notional. Assigning it to both proposed currencies is
    # deliberately stricter than assuming the pending orders are uncorrelated.
    if new_notional <= 0 or other_reserved_margin < 0:
        raise SafetyFence("Invalid proposed account exposure")
    pending_bound = other_reserved_margin * exposure.maximum_gross_leverage
    if exposure.current_gross_notional + pending_bound + new_notional > exposure.equity * exposure.maximum_gross_leverage:
        raise SafetyFence("Whole-account gross leverage limit is exhausted")
    for currency in (base, quote):
        if exposure.currency_gross_notional.get(currency, ZERO) + pending_bound + new_notional > exposure.equity * exposure.maximum_currency_fraction:
            raise SafetyFence("Whole-account currency concentration limit is exhausted")


class RiskObservation(StrictModel):
    """Worker-only immutable observation installed from verified read adapters."""

    authority_id: str
    environment: str
    account_id: str
    evidence: RiskEvidence
    instrument: InstrumentRisk
    margin: MarginState
    maximum_entry_loss: Decimal = Field(gt=0)
    stop_gap_buffer: Decimal = Field(default=ZERO, ge=0)
    account_exposure: AccountExposureRisk | None = None

    @field_validator("maximum_entry_loss", "stop_gap_buffer", mode="before")
    @classmethod
    def exact_numbers(cls, value: object) -> Decimal:
        return money(value)  # type: ignore[arg-type]


class RiskRegistry:
    """Durable risk cuts consumed transactionally with outbox reservations."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS execution_risk_observations (
        authority_id TEXT NOT NULL,
        environment TEXT NOT NULL,
        account_id TEXT NOT NULL,
        instrument TEXT NOT NULL,
        revision TEXT NOT NULL,
        observation_json TEXT NOT NULL,
        PRIMARY KEY(authority_id, environment, account_id, instrument)
    );
    """

    def __init__(self, database: object, *, monotonic_clock: Callable[[], float] = time.monotonic) -> None:
        from oandafly.storage.database import Database
        if not isinstance(database, Database):
            raise TypeError("RiskRegistry requires the shared Database")
        self.db = database
        self._monotonic = monotonic_clock
        self._anchors: dict[tuple[str, str, str, str], tuple[str, datetime, float, float]] = {}
        self._clock_faulted = False
        self.db.initialize(self.SCHEMA, component=__name__)

    @FINANCIAL.calculation
    def publish(self, observation: RiskObservation) -> None:
        from oandafly.domain.primitives import utc_now
        # Revalidate/detach mutable children and unchecked model_copy updates
        # before they become durable authority for a financial reservation.
        observation = RiskObservation.model_validate_json(observation.model_dump_json())
        published_at = utc_now()
        duration = (observation.evidence.valid_until - published_at).total_seconds()
        if not 0 < duration <= 60:
            raise SafetyFence("risk observations require a fresh validity interval of at most 60 seconds")
        measured = self._monotonic()
        with self.db.transaction() as connection:
            key = (observation.authority_id, observation.environment, observation.account_id,
                   observation.instrument.name)
            anchor = self._anchors.get(key)
            # Re-publication cannot erase a clock discontinuity before the
            # dispatch guard sees it, even when the source facts are unchanged.
            if anchor is not None:
                drift = (published_at - anchor[1]).total_seconds() - (measured - anchor[2])
                if measured < anchor[2] or abs(drift) > 2:
                    self._clock_faulted = True
            if self._clock_faulted:
                raise SafetyFence("risk clock continuity is invalid; restart and refresh verified observations")
            old = connection.execute(
                "SELECT revision,observation_json FROM execution_risk_observations WHERE authority_id=? AND environment=? AND account_id=? AND instrument=?",
                (observation.authority_id, observation.environment, observation.account_id, observation.instrument.name),
            ).fetchone()
            serialized = observation.model_dump_json()
            if old is not None and old["revision"] == observation.evidence.revision and old["observation_json"] != serialized:
                raise SafetyFence("a risk observation revision cannot be reused for changed source facts")
            if old is not None:
                previous = RiskObservation.model_validate_json(old["observation_json"])
                if int(observation.evidence.covered_transaction_id) < int(previous.evidence.covered_transaction_id):
                    raise SafetyFence("a verified native account cut cannot move backwards")
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_trades'").fetchone():
                trades = connection.execute(
                    "SELECT current_units,last_transaction_id FROM execution_trades WHERE environment=? AND account_id=? AND instrument=?",
                    (observation.environment, observation.account_id, observation.instrument.name),
                ).fetchall()
                known_units = sum((abs(money(trade["current_units"])) for trade in trades
                                   if int(trade["last_transaction_id"]) <= int(observation.evidence.covered_transaction_id)), ZERO)
                if observation.instrument.current_gross_units < known_units:
                    raise SafetyFence("native risk cut understates known gross trade units")
            connection.execute(
                "INSERT INTO execution_risk_observations VALUES(?,?,?,?,?,?) ON CONFLICT(authority_id,environment,account_id,instrument) DO UPDATE SET revision=excluded.revision,observation_json=excluded.observation_json",
                (observation.authority_id, observation.environment, observation.account_id,
                 observation.instrument.name, observation.evidence.revision, serialized),
            )
            # The fill reservation is incremental until a verified account cut
            # actually includes that fill. An early private stream event alone
            # cannot make a stale marginAvailable observation spendable twice.
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outbox'").fetchone():
                held = connection.execute(
                    "SELECT intent_id,native_fill_id FROM outbox WHERE authority_id=? AND environment=? AND account_id=? AND reservation_active=1 AND status='ACK_BY_FILL'",
                    (observation.authority_id, observation.environment, observation.account_id),
                ).fetchall()
                covered = int(observation.evidence.covered_transaction_id)
                for held_row in held:
                    fill_id = held_row["native_fill_id"]
                    if fill_id is not None and str(fill_id).isdigit() and int(fill_id) <= covered:
                        connection.execute("UPDATE outbox SET reservation_active=0,version=version+1 WHERE intent_id=?", (held_row["intent_id"],))
            # Publish the in-memory anchor under the same database lock. Two
            # producer threads must not reverse the order of committed cuts.
            # Idempotent publication retains the original monotonic deadline.
            if anchor is None or anchor[0] != observation.evidence.revision:
                self._anchors[key] = (observation.evidence.revision, published_at, measured, measured + duration)

    @FINANCIAL.calculation
    def validate_in(
        self, connection: sqlite3.Connection, *, authority_id: str, environment: str,
        account_id: str, instrument: str, units: Decimal, stop_price: Decimal,
        expected: RiskEvidence, now: datetime, own_intent_id: str | None = None,
        entry_bound: Decimal | None = None,
    ) -> Decimal:
        row = connection.execute(
            "SELECT observation_json FROM execution_risk_observations WHERE authority_id=? AND environment=? AND account_id=? AND instrument=?",
            (authority_id, environment, account_id, instrument),
        ).fetchone()
        if row is None:
            raise SafetyFence("verified, account-scoped risk observations are missing")
        observation = RiskObservation.model_validate_json(row["observation_json"])
        if observation.evidence != expected or require_utc(now) >= expected.valid_until:
            raise SafetyFence("risk evidence is stale, expired or belongs to a different source cut")
        # Native cash/history fences also block entry, including discrepancies
        # discovered by REST coverage after an otherwise valid execution fact.
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_accounts'").fetchone():
            ledger = connection.execute("SELECT covered_cursor,observed_head FROM ledger_accounts WHERE environment=? AND account_id=?",
                                        (environment, account_id)).fetchone()
            if (ledger is None or int(ledger["covered_cursor"]) < int(expected.covered_transaction_id)
                    or int(ledger["observed_head"]) > int(expected.covered_transaction_id)
                    or connection.execute("SELECT 1 FROM ledger_fences WHERE environment=? AND account_id=? AND resolved_at IS NULL LIMIT 1",
                                          (environment, account_id)).fetchone()):
                raise SafetyFence("Account ledger is missing, unreconciled or fenced")
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_projection_state'").fetchone():
            projection = connection.execute("SELECT applied_cursor,state FROM execution_projection_state WHERE environment=? AND account_id=?",
                                            (environment, account_id)).fetchone()
            if (projection is None or projection["state"] != "READY"
                    or int(projection["applied_cursor"]) < int(expected.covered_transaction_id)):
                raise SafetyFence("Risk requires the canonical execution projection at its account cut")
        anchor = self._anchors.get((authority_id, environment, account_id, instrument))
        if anchor is None or anchor[0] != expected.revision:
            raise SafetyFence("risk cut has no current worker monotonic validity anchor; refresh source observations")
        monotonic_now = self._monotonic()
        clock_drift = (require_utc(now) - anchor[1]).total_seconds() - (monotonic_now - anchor[2])
        if monotonic_now < anchor[2] or abs(clock_drift) > 2:
            self._clock_faulted = True
        if self._clock_faulted or monotonic_now >= anchor[3]:
            raise SafetyFence("risk clock continuity or monotonic deadline is invalid")
        head = connection.execute(
            "SELECT transaction_id FROM execution_native_facts WHERE environment=? AND account_id=? ORDER BY length(transaction_id) DESC,transaction_id DESC LIMIT 1",
            (environment, account_id),
        ).fetchone()
        if head is not None and int(head["transaction_id"]) > int(expected.covered_transaction_id):
            raise SafetyFence("risk account snapshot has not caught up with newly observed native facts")
        metadata = observation.instrument
        tick = ONE.scaleb(-metadata.trade_units_precision)
        if units == ZERO or units != units.quantize(tick, rounding=ROUND_DOWN):
            raise ValidationFailure("units do not satisfy tradeUnitsPrecision")
        if not metadata.minimum_trade_size <= abs(units) <= metadata.maximum_order_units:
            raise SafetyFence("order units exceed the verified instrument bounds")
        if metadata.current_gross_units + abs(units) > metadata.maximum_position_units:
            raise SafetyFence("gross exposure would exceed the position limit")
        if metadata.spread_pips > metadata.maximum_spread_pips:
            raise SafetyFence("spread exceeds the entry risk policy")
        entry = (metadata.ask if units > ZERO else metadata.bid) if entry_bound is None else entry_bound
        if entry <= ZERO:
            raise SafetyFence("entry bound must be positive")
        stop_distance = entry - stop_price if units > ZERO else stop_price - entry
        if stop_distance <= ZERO:
            raise SafetyFence("protective stop is on the wrong side of the entry")
        loss = abs(units) * (
            stop_distance + metadata.ask - metadata.bid + observation.stop_gap_buffer
        ) * metadata.conversions.adverse
        if loss > observation.maximum_entry_loss:
            raise SafetyFence("stop and gap loss exceeds the independent account budget")
        rows = connection.execute(
            "SELECT intent_id,reservation,request_json FROM outbox WHERE authority_id=? AND environment=? AND account_id=? AND reservation_active=1",
            (authority_id, environment, account_id),
        ).fetchall()
        reservations = sum((money(row["reservation"]) for row in rows if row["intent_id"] != own_intent_id), ZERO)
        import json
        other_pair_reservations = ZERO
        other_pair_units = ZERO
        sibling_bound = ZERO
        for pending in rows:
            if pending["intent_id"] == own_intent_id:
                continue
            saved = json.loads(pending["request_json"])
            if saved["instrument"] == instrument:
                other_pair_reservations += money(pending["reservation"])
                other_pair_units += abs(money(saved["units"]))
                bound = saved.get("price") if saved.get("order_type", "MARKET") == "LIMIT" else saved.get("price_bound")
                if bound is not None:
                    sibling_bound = max(sibling_bound, money(bound))
        if metadata.current_gross_units + other_pair_units + abs(units) > metadata.maximum_position_units:
            raise SafetyFence("pending and unreflected fills exhaust the gross position limit")
        margin = observation.margin.model_copy(update={
            "incremental_reservations": observation.margin.incremental_reservations + reservations,
        })
        adverse_metadata = metadata.model_copy(update={"ask": max(metadata.ask, entry)})
        if observation.account_exposure is None:
            if metadata.legal_product != "SYNTHETIC_FX":
                raise SafetyFence("Native trading requires a complete whole-account exposure cut")
        else:
            exposure = observation.account_exposure
            if metadata.requested_leverage > exposure.maximum_gross_leverage:
                raise SafetyFence("Instrument margin floor does not support the account reservation bound")
            require_account_capacity(exposure, base=metadata.base_currency, quote=metadata.quote_currency,
                                     new_notional=abs(units) * adverse_metadata.adverse_unit_notional,
                                     other_reserved_margin=reservations)
        # Reserve the complete tier cost of the pending bundle, allocating any
        # higher-tier increment to this new intent. Two concurrent entries cannot
        # both pretend to occupy the cheapest remaining tier.
        # Price the bundle at its most adverse member bound so each reservation is
        # reproducible whatever order the same-pair siblings were prepared in.
        bundle_metadata = (adverse_metadata if sibling_bound <= adverse_metadata.ask
                           else metadata.model_copy(update={"ask": sibling_bound}))
        bundle_margin = incremental_margin(abs(units) + other_pair_units, bundle_metadata)
        required = max(incremental_margin(units, adverse_metadata), bundle_margin - other_pair_reservations)
        if required > spendable_margin(margin).spendable:
            raise SafetyFence("per-account native/stress margin bounds do not fund this order")
        return money(decimal_text(required))
