"""Conservative per-account FX valuation, coverage and non-poolable free margin.

Every native snapshot, conversion ladder and stress branch belongs to an explicit
evidence cut. A missing quote is missing coverage, never a zero-valued account.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Annotated, Protocol

from pydantic import BeforeValidator, Field, StrictBool, field_validator, model_validator

from oandafly.domain.errors import ValidationFailure
from oandafly.domain.instruments import currency_code
from oandafly.domain.primitives import StrictModel, money, require_utc

Financial = Annotated[Decimal, BeforeValidator(money)]
_ZERO = Decimal(0)


class CoverageStatus(StrEnum):
    COMPLETE_VERIFIED = "COMPLETE_VERIFIED"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    INCONSISTENT = "INCONSISTENT"
    UNKNOWN_UNIVERSE = "UNKNOWN_UNIVERSE"


class AccountIdentity(StrictModel):
    environment: str = Field(pattern=r"^(LIVE|SANDBOX)$")
    account_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9:._-]*$")

    @model_validator(mode="after")
    def synthetic_environment(self) -> AccountIdentity:
        # Synthetic money has no live-account interpretation.
        if self.account_id.startswith("SYNTHETIC:") and self.environment != "SANDBOX":
            raise ValueError("Synthetic accounts must remain in SANDBOX")
        return self


class ScopeManifest(StrictModel):
    """An enrolled disjoint account universe; account aliases are not extra cash."""

    authority_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    accounts: tuple[AccountIdentity, ...]
    universe_verified: StrictBool
    verified_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def _valid(self) -> ScopeManifest:
        require_utc(self.verified_at)
        require_utc(self.expires_at)
        if self.expires_at <= self.verified_at:
            raise ValueError("Scope expiry must follow verification")
        keys = [(account.environment, account.account_id) for account in self.accounts]
        if len(keys) != len(set(keys)):
            raise ValueError("Each native account may occur only once in a consolidated scope")
        # Read-only reporting must not inflate OANDA totals with synthetic cash.
        if len({account.account_id.startswith("SYNTHETIC:") for account in self.accounts}) > 1:
            raise ValueError("Synthetic and broker accounts require separate reporting scopes")
        return self


class MarginGates(StrictModel):
    context_active: StrictBool
    trading_track: StrictBool
    explicitly_armed: StrictBool
    lease_valid: StrictBool
    account_verified: StrictBool
    legal_policy_valid: StrictBool
    fx_only: StrictBool
    observations_fresh: StrictBool
    history_reconciled: StrictBool
    no_unknown_dispatch: StrictBool
    no_margin_call: StrictBool
    financing_attributed: StrictBool
    stress_qualified: StrictBool

    @property
    def enabled(self) -> bool:
        return all(getattr(self, name) is True for name in type(self).model_fields)


class SpendableInputs(StrictModel):
    """Independent native/stress bounds at one cut; all amounts are home currency.

    ``incremental_reservations`` contains only obligations absent from BOTH
    branches. If a hold is reflected by just one branch, subtract its residual
    from the other branch before constructing these inputs. Never subtract the
    full reservation book after the broker has already reflected its fills.
    """

    native_margin_available: Financial
    native_unreflected_debits: Financial = Field(default=Decimal(0), ge=0)
    stressed_equity: Financial
    stressed_margin_required: Financial = Field(ge=0)
    stress_safety_buffer: Financial = Field(default=Decimal(0), ge=0)
    stress_unreflected_debits: Financial = Field(default=Decimal(0), ge=0)
    incremental_reservations: Financial = Field(default=Decimal(0), ge=0)
    evidence_revision: str = Field(min_length=1)
    gates: MarginGates

    def spendable(self) -> Decimal:
        if not self.gates.enabled:
            return _ZERO
        with localcontext() as ctx:
            ctx.prec = 512
            native_bound = self.native_margin_available - self.native_unreflected_debits
            stress_bound = (self.stressed_equity - self.stressed_margin_required
                            - self.stress_safety_buffer - self.stress_unreflected_debits)
            return max(_ZERO, min(native_bound, stress_bound) - self.incremental_reservations)


class AccountSnapshot(StrictModel):
    identity: AccountIdentity
    currency: str
    balance: Financial
    unrealized_pl: Financial
    native_nav: Financial
    signed_unbooked_financing: Financial = Decimal(0)
    native_margin_available: Financial
    native_margin_closeout_percent: Financial = Field(ge=0)
    native_margin_call_percent: Financial | None = Field(default=None, ge=0)
    last_transaction_id: str = Field(pattern=r"^(0|[1-9][0-9]{0,39})$")
    covered_transaction_id: str = Field(pattern=r"^(0|[1-9][0-9]{0,39})$")
    snapshot_revision: str = Field(min_length=1)
    observed_at: datetime
    max_age_seconds: int = Field(default=15, ge=1, le=3600)
    native_currency_precision: int = Field(default=5, ge=0, le=12)
    verified: StrictBool
    projection_consistent: StrictBool
    provenance: str = Field(min_length=1)
    margin: SpendableInputs

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return currency_code(value)

    @model_validator(mode="after")
    def _valid(self) -> AccountSnapshot:
        require_utc(self.observed_at)
        if self.margin.native_margin_available != self.native_margin_available:
            raise ValueError("Native marginAvailable cannot be replaced by an independently computed value")
        if self.margin.evidence_revision != self.snapshot_revision:
            raise ValueError("The stress/reservation branch must bind the exact account evidence revision")
        return self

    @property
    def nav_consistent(self) -> bool:
        # Native fields can be separately rounded to account precision.
        with localcontext() as ctx:
            ctx.prec = 512
            tolerance = Decimal(1).scaleb(-self.native_currency_precision)
            return abs(self.native_nav - self.balance - self.unrealized_pl) <= tolerance


class DepthLevel(StrictModel):
    source_units: Financial = Field(gt=0)
    rate: Financial = Field(gt=0)


class ConversionQuote(StrictModel):
    """Executable-shaped source-to-reporting conversion ladders, not official fixings.

    Capacity is denominated in SOURCE currency. Positive assets consume bids;
    negative liabilities consume asks. Insufficient depth produces no valuation.
    The ladders are evidence, not a permission to initiate currency transfers.
    """

    source_currency: str
    target_currency: str
    bids: tuple[DepthLevel, ...]
    asks: tuple[DepthLevel, ...]
    observed_at: datetime
    evidence_revision: str = Field(min_length=1)
    max_age_seconds: int = Field(default=15, ge=1, le=3600)

    @field_validator("source_currency", "target_currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return currency_code(value)

    @model_validator(mode="after")
    def _valid(self) -> ConversionQuote:
        require_utc(self.observed_at)
        if self.source_currency == self.target_currency:
            if self.bids or self.asks:
                raise ValueError("An identity conversion has no synthetic spread or liquidity ladder")
            return self
        if not self.bids or not self.asks:
            raise ValueError("Both sides of a nonidentity conversion are required")
        if self.bids[0].rate > self.asks[0].rate:
            raise ValueError("Conversion quote is crossed")
        if any(a.rate < b.rate for a, b in zip(self.bids, self.bids[1:])):
            raise ValueError("Bid depth must be ordered from best to worst")
        if any(a.rate > b.rate for a, b in zip(self.asks, self.asks[1:])):
            raise ValueError("Ask depth must be ordered from best to worst")
        return self

    def convert(self, value: Decimal) -> Decimal:
        amount = money(value)
        if self.source_currency == self.target_currency or amount == 0:
            return amount
        levels = self.bids if amount > 0 else self.asks
        with localcontext() as ctx:
            ctx.prec = 512
            remaining = abs(amount)
            result = _ZERO
            for level in levels:
                take = min(remaining, level.source_units)
                result += take * level.rate
                remaining -= take
                if remaining == 0:
                    return result if amount > 0 else -result
        raise ValidationFailure("Conversion depth does not cover the account amount")


class AccountValuation(StrictModel):
    identity: AccountIdentity
    home_currency: str
    reporting_currency: str
    cash_native: Financial
    unrealized_pl_native: Financial
    unbooked_financing_native: Financial
    adjusted_equity_native: Financial
    total_value_reporting: Financial
    cash_reporting_memo: Financial | None
    pnl_reporting_memo: Financial | None
    financing_reporting_memo: Financial | None
    conversion_residual_memo: Financial | None
    spendable_home_currency: Financial
    snapshot_revision: str
    conversion_revision: str


class ConsolidatedBalance(StrictModel):
    authority_id: str
    scope_version: int
    reporting_currency: str
    coverage: CoverageStatus
    complete_total_value: Financial | None
    observed_total_value: Financial
    accounts: tuple[AccountValuation, ...]
    missing_accounts: tuple[AccountIdentity, ...]
    issues: tuple[str, ...]
    evaluated_at: datetime


class AccountReadAdapter(Protocol):
    async def read_snapshot(self, identity: AccountIdentity) -> AccountSnapshot:
        """Return a verified native scope/cursor cut or raise a typed read failure."""


class BalanceAggregator:
    """Absolute coverage and a reporting total; spendable funds remain per account."""

    def aggregate(self, scope: ScopeManifest, snapshots: Sequence[AccountSnapshot],
                  conversions: Sequence[ConversionQuote], reporting_currency: str,
                  *, now: datetime) -> ConsolidatedBalance:
        report = currency_code(reporting_currency)
        moment = require_utc(now)
        expected = {(item.environment, item.account_id): item for item in scope.accounts}
        by_account: dict[tuple[str, str], AccountSnapshot] = {}
        issues: list[str] = []
        inconsistent = False
        stale = moment >= scope.expires_at or scope.verified_at > moment
        partial = False
        for snapshot in snapshots:
            key = (snapshot.identity.environment, snapshot.identity.account_id)
            if key not in expected or key in by_account:
                inconsistent = True
                issues.append(f"DUPLICATE_OR_UNENROLLED_ACCOUNT:{key[0]}:{key[1]}")
                continue
            by_account[key] = snapshot
        quote_map: dict[str, ConversionQuote] = {}
        for supplied_quote in conversions:
            if supplied_quote.target_currency != report or supplied_quote.source_currency in quote_map:
                inconsistent = True
                issues.append("AMBIGUOUS_CONVERSION_SCOPE")
                continue
            quote_map[supplied_quote.source_currency] = supplied_quote
        missing = tuple(identity for key, identity in expected.items() if key not in by_account)
        if missing:
            partial = True
            issues.append("MISSING_ENROLLED_ACCOUNTS")
        values: list[AccountValuation] = []
        total = _ZERO
        for key, snapshot in by_account.items():
            account_stale = (snapshot.observed_at > moment or moment - snapshot.observed_at
                             > timedelta(seconds=snapshot.max_age_seconds))
            stale |= account_stale
            is_consistent = snapshot.projection_consistent and snapshot.nav_consistent
            inconsistent |= not is_consistent
            history_complete = int(snapshot.covered_transaction_id) >= int(snapshot.last_transaction_id)
            partial |= not snapshot.verified or not history_complete
            if not is_consistent:
                issues.append(f"NATIVE_PROJECTION_INCONSISTENT:{key[0]}:{key[1]}")
            if not history_complete:
                issues.append(f"HISTORY_NOT_CAUGHT_UP:{key[0]}:{key[1]}")
            quote = quote_map.get(snapshot.currency)
            if quote is None and snapshot.currency == report:
                quote = ConversionQuote(source_currency=report, target_currency=report, bids=(), asks=(),
                                        observed_at=moment, evidence_revision="IDENTITY")
            if quote is None:
                partial = True
                issues.append(f"MISSING_CONVERSION:{snapshot.currency}:{report}")
                continue
            quote_stale = quote.observed_at > moment or moment - quote.observed_at > timedelta(seconds=quote.max_age_seconds)
            stale |= quote_stale
            with localcontext() as ctx:
                ctx.prec = 512
                equity = snapshot.balance + snapshot.unrealized_pl + snapshot.signed_unbooked_financing
                try:
                    converted = quote.convert(equity)
                except ValidationFailure:
                    partial = True
                    issues.append(f"INSUFFICIENT_CONVERSION_DEPTH:{key[0]}:{key[1]}")
                    continue
                try:
                    cash_component = quote.convert(snapshot.balance)
                    pnl_component = quote.convert(snapshot.unrealized_pl)
                    financing_component = quote.convert(snapshot.signed_unbooked_financing)
                    cash_memo: Decimal | None = cash_component
                    pnl_memo: Decimal | None = pnl_component
                    financing_memo: Decimal | None = financing_component
                    residual: Decimal | None = converted - cash_component - pnl_component - financing_component
                except ValidationFailure:
                    # Gross memorandum components can exceed depth although the net account is covered.
                    cash_memo = pnl_memo = financing_memo = residual = None
                allowed = (snapshot.verified and is_consistent and history_complete and not account_stale and not quote_stale
                           and snapshot.native_margin_closeout_percent < 1
                           and (snapshot.native_margin_call_percent is None or snapshot.native_margin_call_percent < 1))
                spendable = snapshot.margin.spendable() if allowed else _ZERO
                values.append(AccountValuation(identity=snapshot.identity, home_currency=snapshot.currency, reporting_currency=report,
                    cash_native=snapshot.balance, unrealized_pl_native=snapshot.unrealized_pl,
                    unbooked_financing_native=snapshot.signed_unbooked_financing, adjusted_equity_native=equity,
                    total_value_reporting=converted, cash_reporting_memo=cash_memo, pnl_reporting_memo=pnl_memo,
                    financing_reporting_memo=financing_memo, conversion_residual_memo=residual,
                    spendable_home_currency=spendable, snapshot_revision=snapshot.snapshot_revision,
                    conversion_revision=quote.evidence_revision))
                total += converted
        if not scope.universe_verified:
            status = CoverageStatus.UNKNOWN_UNIVERSE
        elif inconsistent:
            status = CoverageStatus.INCONSISTENT
        elif stale:
            status = CoverageStatus.STALE
        elif partial:
            status = CoverageStatus.PARTIAL
        else:
            status = CoverageStatus.COMPLETE_VERIFIED
        if status != CoverageStatus.COMPLETE_VERIFIED:
            # A consolidated authority gate cannot silently arm the subset with available data.
            values = [item.model_copy(update={"spendable_home_currency": _ZERO}) for item in values]
        return ConsolidatedBalance(authority_id=scope.authority_id, scope_version=scope.version,
            reporting_currency=report, coverage=status,
            complete_total_value=total if status == CoverageStatus.COMPLETE_VERIFIED else None,
            observed_total_value=total, accounts=tuple(values), missing_accounts=missing,
            issues=tuple(issues), evaluated_at=moment)

    async def collect(self, adapter: AccountReadAdapter, scope: ScopeManifest,
                      conversions: Sequence[ConversionQuote], reporting_currency: str,
                      *, now: datetime, timeout_seconds: float = 10.0) -> ConsolidatedBalance:
        if not 0 < timeout_seconds <= 60:
            raise ValidationFailure("Read timeout must be in (0, 60] seconds")

        async def one(identity: AccountIdentity) -> AccountSnapshot:
            async with asyncio.timeout(timeout_seconds):
                result = await adapter.read_snapshot(identity)
                if result.identity != identity:
                    raise ValidationFailure("Read adapter returned a different native account")
                return result

        results = await asyncio.gather(*(one(identity) for identity in scope.accounts), return_exceptions=True)
        successful = [result for result in results if isinstance(result, AccountSnapshot)]
        return self.aggregate(scope, successful, conversions, reporting_currency, now=now)
