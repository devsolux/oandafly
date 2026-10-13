"""Independent arithmetic and precision boundaries, without a broker transport."""

from __future__ import annotations

from decimal import ROUND_UP, Decimal, Inexact, localcontext

import pytest
from pydantic import ValidationError

from oandafly.domain.errors import ValidationFailure
from oandafly.execution.risk import (
    HomeConversions,
    InstrumentRisk,
    MarginState,
    MarginTier,
    incremental_margin,
    rounded_target_units,
    spendable_margin,
    tier_margin,
)

D = Decimal


def test_sizing_and_leverage_contracts_ignore_ambient_decimal_state() -> None:
    kwargs = dict(direction=1, equity=D("10000"), allocation=D("0.5"), free_margin=D("57.123"),
                  stop_distance=D("0.004"), maximum_loss=D("1000"))
    expected = rounded_target_units(instrument=instrument(), **kwargs)
    with localcontext() as context:
        context.prec, context.rounding = 3, ROUND_UP
        context.traps[Inexact] = True
        result = rounded_target_units(instrument=instrument(), **kwargs)
        assert result == expected
        assert incremental_margin(result, instrument()) <= D("57.123")


def test_requested_leverage_tightens_the_broker_margin_floor() -> None:
    # A safer profile must not inherit the broker's more permissive leverage.
    with pytest.raises(ValidationError, match="leverage floor"):
        instrument(requested_leverage=D("10"))
    assert instrument(requested_leverage=D("10"), margin_tiers=(MarginTier(rate=D("0.10")),))


def instrument(**changes: object) -> InstrumentRisk:
    data: dict[str, object] = {
        "name": "EUR_USD", "base_currency": "EUR", "quote_currency": "USD",
        "trade_units_precision": 2, "pip_location": -4, "minimum_trade_size": D("0.01"),
        "maximum_order_units": D("1000000"), "maximum_position_units": D("1000000"),
        "bid": D("1.1000"), "ask": D("1.1002"), "conversions": HomeConversions(
            currency="USD", position_value=D("0.90"), account_gain=D("0.89"), account_loss=D("0.92")),
        "requested_leverage": D("20"), "broker_max_leverage": D("20"),
        "margin_tiers": (MarginTier(upper_notional=D("1000"), rate=D("0.05")),
                         MarginTier(upper_notional=D("5000"), rate=D("0.10")), MarginTier(rate=D("0.20"))),
        "maximum_spread_pips": D("3"),
    }
    data.update(changes)
    return InstrumentRisk.model_validate(data)


@pytest.mark.parametrize("native,stress,required,holds,debits,expected", [
    ("100", "200", "10", "5", "2", "93"),
    ("200", "100", "10", "5", "2", "85"),
    ("-1", "100", "0", "0", "0", "0"),
    ("100", "-1", "0", "0", "0", "0"),
    ("20", "100", "0", "25", "0", "0"),
])
def test_joint_minimum_keeps_native_and_tier_stress_independent(native: str, stress: str, required: str, holds: str, debits: str, expected: str) -> None:
    result = spendable_margin(MarginState(
        native_margin_available=D(native), stressed_equity=D(stress), independent_margin_requirement=D(required),
        incremental_reservations=D(holds), native_unreflected_debits=D(debits), gates={"scope": True},
    ))
    assert result.spendable == D(expected)


@pytest.mark.parametrize("gates", [{}, {"scope": False}, {"scope": True, "financing": False}])
def test_missing_or_false_gate_zeroes_spendable(gates: dict[str, bool]) -> None:
    result = spendable_margin(MarginState(native_margin_available=D("1000"), stressed_equity=D("1000"),
                                         independent_margin_requirement=D("0"), gates=gates))
    assert result.spendable == 0 and result.blocked_gates


@pytest.mark.parametrize("notional,expected", [("0", "0"), ("1000", "50"), ("1500", "100"), ("5000", "450"), ("6000", "650")])
def test_tier_boundary_integral(notional: str, expected: str) -> None:
    assert tier_margin(D(notional), instrument().margin_tiers) == D(expected)


@pytest.mark.parametrize("direction", [-1, 1])
def test_target_rounds_toward_zero_on_decimal_unit_lattice(direction: int) -> None:
    metadata = instrument()
    quantity = rounded_target_units(direction=direction, equity=D("10000"), allocation=D("0.9"),  # type: ignore[arg-type]
                                    instrument=metadata, free_margin=D("57.123"), stop_distance=D("0.004"),
                                    maximum_loss=D("1000"), gap_buffer=D("0.001"))
    assert quantity * D(direction) > 0
    assert quantity % D("0.01") == 0
    assert incremental_margin(quantity, metadata) <= D("57.123")
    assert incremental_margin(abs(quantity) + D("0.01"), metadata) > D("57.123")


def test_adverse_loss_conversion_reduces_target_units() -> None:
    benign = instrument(conversions=HomeConversions(currency="USD", position_value=D("0.8"), account_gain=D("0.8"), account_loss=D("0.8")))
    adverse = instrument(conversions=HomeConversions(currency="USD", position_value=D("0.8"), account_gain=D("0.8"), account_loss=D("1.2")))
    args = dict(direction=1, equity=D("10000"), allocation=D("0.5"), free_margin=D("10000"), stop_distance=D("0.004"), maximum_loss=D("1000"))
    first = rounded_target_units(instrument=benign, **args)  # type: ignore[arg-type]
    second = rounded_target_units(instrument=adverse, **args)  # type: ignore[arg-type]
    assert second < first


def test_below_minimum_trade_size_returns_zero() -> None:
    metadata = instrument(minimum_trade_size=D("100"))
    assert rounded_target_units(direction=1, equity=D("1"), allocation=D("0.1"), instrument=metadata,
                                free_margin=D("1000"), stop_distance=D("0.001"), maximum_loss=D("1000")) == 0


@pytest.mark.parametrize("value", [1.1, float("nan"), float("inf"), True, "NaN", "Infinity"])
def test_financial_models_reject_inexact_or_nonfinite_values(value: object) -> None:
    with pytest.raises((ValidationError, ValidationFailure)):
        instrument(bid=value)


def test_spread_guard_blocks_target() -> None:
    assert rounded_target_units(direction=1, equity=D("10000"), allocation=D("1"),
                                instrument=instrument(ask=D("1.101")), free_margin=D("1000"),
                                stop_distance=D("0.005"), maximum_loss=D("100")) == 0


def test_risk_tiers_cannot_relax_effective_margin_floor() -> None:
    with pytest.raises(ValidationError):
        instrument(margin_tiers=(MarginTier(rate=D("0.01")),))


def test_gross_long_short_hedge_still_consumes_position_room() -> None:
    metadata = instrument(current_gross_units=D("1000"), maximum_position_units=D("1000"))
    assert rounded_target_units(direction=-1, equity=D("10000"), allocation=D("1"), instrument=metadata,
                                free_margin=D("10000"), stop_distance=D("0.001"), maximum_loss=D("10000")) == 0
