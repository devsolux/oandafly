from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from oandafly.accounts.balance_aggregator import (
    AccountIdentity,
    AccountSnapshot,
    BalanceAggregator,
    ConversionQuote,
    CoverageStatus,
    DepthLevel,
    MarginGates,
    ScopeManifest,
    SpendableInputs,
)
from oandafly.accounts.exposure import GrossExposureCut, PositionExposure, TradeExposure
from oandafly.domain.errors import ValidationFailure

D = Decimal
NOW = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)


def test_synthetic_cash_cannot_enter_a_broker_consolidation_scope() -> None:
    # Explicit namespaces must remain separate even in read-only reporting.
    with pytest.raises(ValidationError, match="separate reporting scopes"):
        scope("A", "SYNTHETIC:PAPER:EUR")
    with pytest.raises(ValidationError, match="SANDBOX"):
        AccountIdentity(environment="LIVE", account_id="SYNTHETIC:PAPER:EUR")


def gates(**overrides: bool) -> MarginGates:
    data = dict.fromkeys(MarginGates.model_fields, True)
    data.update(overrides)
    return MarginGates.model_validate(data)


def margin(**overrides: object) -> SpendableInputs:
    return SpendableInputs.model_validate({"native_margin_available": D(100), "stressed_equity": D(1000),
        "stressed_margin_required": D(800), "evidence_revision": "cut-1", "gates": gates(), **overrides})


def account(name: str = "A", **overrides: object) -> AccountSnapshot:
    return AccountSnapshot.model_validate({"identity": AccountIdentity(environment="SANDBOX", account_id=name),
        "currency": "EUR", "balance": D(1000), "unrealized_pl": D(0), "native_nav": D(1000),
        "native_margin_available": D(100), "native_margin_closeout_percent": D("0.1"),
        "last_transaction_id": "100", "covered_transaction_id": "100", "snapshot_revision": "cut-1",
        "observed_at": NOW, "verified": True, "projection_consistent": True,
        "provenance": "SYNTHETIC_TEST_ONLY", "margin": margin(), **overrides})


def scope(*names: str, **overrides: object) -> ScopeManifest:
    return ScopeManifest.model_validate({"authority_id": "authority", "version": 1,
        "accounts": tuple(AccountIdentity(environment="SANDBOX", account_id=name) for name in names),
        "universe_verified": True, "verified_at": NOW - timedelta(seconds=1),
        "expires_at": NOW + timedelta(minutes=1), **overrides})


def quote(**overrides: object) -> ConversionQuote:
    return ConversionQuote.model_validate({"source_currency": "EUR", "target_currency": "USD",
        "bids": (DepthLevel(source_units=D(10000), rate=D("0.9")),),
        "asks": (DepthLevel(source_units=D(10000), rate=D("1.1")),),
        "observed_at": NOW, "evidence_revision": "fx-1", **overrides})


@pytest.mark.parametrize("native,stress,expected", [(100, 200, 100), (200, 100, 100), (-1, 100, 0), (100, -1, 0)])
def test_independent_margin_branches(native: int, stress: int, expected: int) -> None:
    inputs = margin(native_margin_available=D(native), stressed_equity=D(stress), stressed_margin_required=D(0))
    assert inputs.spendable() == expected


def test_native_debits_and_incremental_holds_do_not_replace_stress_math() -> None:
    inputs = margin(native_margin_available=D(200), native_unreflected_debits=D(30),
                    stressed_equity=D(1000), stressed_margin_required=D(800),
                    stress_safety_buffer=D(40), stress_unreflected_debits=D(10), incremental_reservations=D(20))
    assert inputs.spendable() == 130


@pytest.mark.parametrize("gate", list(MarginGates.model_fields))
def test_each_gate_independently_zeroes_spendable(gate: str) -> None:
    assert margin(gates=gates(**{gate: False})).spendable() == 0


def test_gates_reject_truthy_strings() -> None:
    with pytest.raises(ValidationError):
        MarginGates.model_validate({**dict.fromkeys(MarginGates.model_fields, True), "lease_valid": "true"})


def test_cash_and_pnl_net_within_account_before_asymmetric_conversion() -> None:
    snapshot = account(balance=D(100), unrealized_pl=D(-50), native_nav=D(50))
    result = BalanceAggregator().aggregate(scope("A"), [snapshot], [quote()], "USD", now=NOW)
    value = result.accounts[0]
    assert result.coverage == CoverageStatus.COMPLETE_VERIFIED and result.complete_total_value == 45
    assert value.cash_native == 100 and value.unrealized_pl_native == -50
    assert value.cash_reporting_memo == 90 and value.pnl_reporting_memo == -55
    assert value.conversion_residual_memo == 10


def test_separate_accounts_are_never_netted_before_conversion_or_margin_pooled() -> None:
    positive = account("A", balance=D(100), native_nav=D(100))
    liability = account("B", balance=D(-100), native_nav=D(-100),
                        margin=margin(stressed_equity=D(-100), stressed_margin_required=D(0)))
    result = BalanceAggregator().aggregate(scope("A", "B"), [positive, liability], [quote()], "USD", now=NOW)
    assert result.complete_total_value == -20
    assert result.accounts[0].spendable_home_currency == 100
    assert result.accounts[1].spendable_home_currency == 0
    assert "pooled_spendable" not in result.model_dump()


def test_nonlinear_ladders_are_evaluated_per_net_account() -> None:
    ladder = quote(bids=(DepthLevel(source_units=D(100), rate=D("0.9")), DepthLevel(source_units=D(100), rate=D("0.8"))))
    snapshot = account(balance=D(200), unrealized_pl=D(-50), native_nav=D(150))
    result = BalanceAggregator().aggregate(scope("A"), [snapshot], [ladder], "USD", now=NOW)
    assert result.complete_total_value == 130
    assert result.accounts[0].conversion_residual_memo == 15


def test_gross_memo_depth_can_be_missing_while_net_valuation_is_valid() -> None:
    ladder = quote(bids=(DepthLevel(source_units=D(50), rate=D("0.9")),))
    snapshot = account(balance=D(100), unrealized_pl=D(-50), native_nav=D(50))
    result = BalanceAggregator().aggregate(scope("A"), [snapshot], [ladder], "USD", now=NOW)
    assert result.complete_total_value == 45 and result.accounts[0].cash_reporting_memo is None


@pytest.mark.parametrize("status,scope_overrides,account_overrides", [
    (CoverageStatus.COMPLETE_VERIFIED, {}, {}),
    (CoverageStatus.UNKNOWN_UNIVERSE, {"universe_verified": False}, {}),
    (CoverageStatus.PARTIAL, {}, {"verified": False}),
    (CoverageStatus.PARTIAL, {}, {"covered_transaction_id": "99"}),
    (CoverageStatus.STALE, {}, {"observed_at": NOW - timedelta(seconds=16)}),
    (CoverageStatus.INCONSISTENT, {}, {"projection_consistent": False}),
    (CoverageStatus.INCONSISTENT, {}, {"native_nav": D(900)}),
])
def test_absolute_coverage(status: CoverageStatus, scope_overrides: dict[str, object], account_overrides: dict[str, object]) -> None:
    result = BalanceAggregator().aggregate(scope("A", **scope_overrides), [account(**account_overrides)], [], "EUR", now=NOW)
    assert result.coverage == status
    if status != CoverageStatus.COMPLETE_VERIFIED:
        assert result.complete_total_value is None and result.accounts[0].spendable_home_currency == 0


def test_missing_account_is_not_zero_and_zeroes_consolidated_entry_gate() -> None:
    result = BalanceAggregator().aggregate(scope("A", "B"), [account()], [], "EUR", now=NOW)
    assert result.coverage == CoverageStatus.PARTIAL and result.observed_total_value == 1000
    assert result.complete_total_value is None and result.missing_accounts[0].account_id == "B"
    assert result.accounts[0].spendable_home_currency == 0


def test_missing_conversion_has_no_fabricated_zero_value() -> None:
    result = BalanceAggregator().aggregate(scope("A"), [account()], [], "USD", now=NOW)
    assert result.coverage == CoverageStatus.PARTIAL and not result.accounts and result.complete_total_value is None


def test_duplicate_observation_never_doubles_cash() -> None:
    result = BalanceAggregator().aggregate(scope("A"), [account(), account()], [], "EUR", now=NOW)
    assert result.coverage == CoverageStatus.INCONSISTENT and result.observed_total_value == 1000


@pytest.mark.parametrize("percent", [D(1), D("1.5")])
def test_native_closeout_threshold_forces_zero_spendable(percent: Decimal) -> None:
    result = BalanceAggregator().aggregate(scope("A"), [account(native_margin_closeout_percent=percent)], [], "EUR", now=NOW)
    assert result.accounts[0].spendable_home_currency == 0


def test_unreflected_financing_adjusts_nav_once() -> None:
    result = BalanceAggregator().aggregate(scope("A"), [account(signed_unbooked_financing=D(-5))], [], "EUR", now=NOW)
    assert result.complete_total_value == 995 and result.accounts[0].cash_native == 1000


@pytest.mark.parametrize("bad", [float("nan"), 1.5, True, "Infinity"])
def test_approximate_or_nonfinite_money_is_rejected(bad: object) -> None:
    with pytest.raises((ValidationError, ValidationFailure)):
        account(balance=bad)


def test_scope_aliases_cannot_enroll_same_native_account_twice() -> None:
    with pytest.raises(ValidationError):
        scope("A", "A")


def test_unknown_non_currency_codes_rejected() -> None:
    with pytest.raises(ValidationError):
        account(currency="XAU")
    with pytest.raises(ValidationError):
        quote(source_currency="BTC")


def test_hedge_net_zero_is_not_gross_flat() -> None:
    cut = GrossExposureCut(account_id="A", environment="SANDBOX", complete=True, observed_at=NOW, evidence_digest="a" * 64,
        trades=(TradeExposure(trade_id="1", instrument="EUR_USD", current_units=D(100)),
                TradeExposure(trade_id="2", instrument="EUR_USD", current_units=D(-100))),
        positions=(PositionExposure(instrument="EUR_USD", long_units=D(100), short_units=D(-100)),))
    assert cut.gross_trade_units == 200 and not cut.proven_flat


def test_missing_or_inconsistent_exposure_cut_cannot_prove_flat() -> None:
    incomplete = GrossExposureCut(account_id="A", environment="SANDBOX", complete=False, observed_at=NOW,
                                  evidence_digest="a" * 64, trades=(), positions=())
    assert not incomplete.proven_flat
    with pytest.raises(ValidationError):
        GrossExposureCut(account_id="A", environment="SANDBOX", complete=True, observed_at=NOW, evidence_digest="a" * 64,
                         trades=(), positions=(PositionExposure(instrument="EUR_USD", long_units=D(100), short_units=D(-100)),))


def test_read_adapter_failure_returns_partial_scope() -> None:
    class Reader:
        async def read_snapshot(self, identity: AccountIdentity) -> AccountSnapshot:
            if identity.account_id == "B":
                raise TimeoutError("fixture transport failure")
            return account(identity.account_id)

    result = asyncio.run(BalanceAggregator().collect(Reader(), scope("A", "B"), [], "EUR", now=NOW))
    assert result.coverage == CoverageStatus.PARTIAL and result.complete_total_value is None
