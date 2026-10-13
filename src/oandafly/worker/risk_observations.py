"""Translate fresh native observations into exact, independently bounded risk."""
from __future__ import annotations

from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from uuid import uuid4

from oandafly.domain.capabilities import AuthorizationGrant
from oandafly.domain.context import AuthoritySnapshot
from oandafly.domain.decisions import PolicyDecision
from oandafly.domain.errors import SafetyFence
from oandafly.domain.financial import FINANCIAL
from oandafly.domain.policy import VerifiedAccountProfile
from oandafly.domain.primitives import money, sha256_json, utc_now
from oandafly.execution.risk import (AccountExposureRisk, InstrumentRisk, MarginState, MarginTier,
                                    RiskEvidence, RiskObservation, rounded_target_units)
from oandafly.worker.synchronization import AccountCut

ZERO, ONE = Decimal(0), Decimal(1)


@FINANCIAL.calculation
def assemble_risk(*, cut: AccountCut, profile: VerifiedAccountProfile, grant: AuthorizationGrant,
                  authority: AuthoritySnapshot, instrument: str, gates: dict[str, bool],
                  unbooked_financing: Decimal = ZERO) -> RiskObservation:
    now, limits, summary = utc_now(), grant.limits, cut.account.summary
    if summary.account_id != grant.account_id or summary.home_currency != profile.home_currency:
        raise SafetyFence("Native account and independently enrolled profile disagree")
    age = (now - summary.observed_at).total_seconds()
    if age < 0 or age > limits.maximum_account_age_seconds:
        raise SafetyFence("Native account risk cut has expired")
    metadata = {row.name: row for row in cut.instruments}
    native = metadata[instrument]
    rule = profile.instrument_rule(instrument)
    # A changed native margin/precision contract needs a new independent profile.
    if native != rule.instrument:
        raise SafetyFence("Native instrument contract changed since account qualification")
    price = cut.pricing.quote(instrument)
    price.require_fresh(now, limits.maximum_quote_age_seconds)
    effective_leverage = min(limits.maximum_gross_leverage, rule.maximum_leverage)
    tier_rate = max(native.margin_rate,
                    (ONE / effective_leverage).quantize(Decimal("0.000000000000000001"), rounding=ROUND_CEILING))
    gross, currency = ZERO, {}
    units = ZERO
    for trade in cut.raw["trades"]:
        name, quantity = trade["instrument"], abs(money(trade["currentUnits"]))
        quote = cut.pricing.quote(name)
        quote.require_fresh(now, limits.maximum_quote_age_seconds)
        meta = metadata[name]
        conversion = cut.pricing.conversion(meta.quote_currency, home_currency=summary.home_currency)
        notional = quantity * quote.ask * conversion.adverse
        gross += notional
        for ccy in (meta.base_currency, meta.quote_currency):
            currency[ccy] = currency.get(ccy, ZERO) + notional
        if name == instrument:
            units += quantity
    if summary.nav <= 0:
        raise SafetyFence("Native equity is not positive")
    risk = InstrumentRisk(name=instrument, legal_product=profile.legal_product,
                          base_currency=native.base_currency, quote_currency=native.quote_currency,
                          trade_units_precision=native.trade_units_precision, pip_location=native.pip_location,
                          minimum_trade_size=native.minimum_trade_size, maximum_order_units=native.maximum_order_units,
                          maximum_position_units=limits.maximum_instrument_units,
                          bid=price.bid, ask=price.ask,
                          conversions=cut.pricing.conversion(native.quote_currency, home_currency=summary.home_currency),
                          requested_leverage=effective_leverage, broker_max_leverage=rule.maximum_leverage,
                          german_retail=profile.region in {"DE", "EU"},
                          margin_tiers=(MarginTier(rate=tier_rate),), current_gross_units=units,
                          maximum_spread_pips=limits.maximum_spread_pips)
    valid_until = min(summary.observed_at + timedelta(seconds=limits.maximum_account_age_seconds),
                      price.observed_at + timedelta(seconds=limits.maximum_quote_age_seconds),
                      grant.expires_at)
    if valid_until <= now:
        raise SafetyFence("Risk cut expired while collecting its inputs")
    evidence = RiskEvidence(revision=str(uuid4()), authority_revision=authority.revision,
                            profile_version=profile.version, account_revision=cut.account.document.sha256,
                            pricing_revision=cut.pricing.document_sha256, policy_revision=sha256_json(grant),
                            conversion_revision=cut.pricing.document_sha256, covered_transaction_id=cut.cursor,
                            valid_until=valid_until)
    # Gross floor can only tighten native margin; do not reconstruct or replace
    # broker marginAvailable. Explicit unbooked debits tighten both branches.
    margin = MarginState(native_margin_available=summary.margin_available,
                         stressed_equity=summary.nav,
                         independent_margin_requirement=max(summary.margin_used, gross / limits.maximum_gross_leverage),
                         native_unreflected_debits=max(ZERO, unbooked_financing),
                         stress_unreflected_debits=max(ZERO, unbooked_financing),
                         safety_buffer=summary.nav * limits.margin_buffer_fraction, gates=gates)
    return RiskObservation(authority_id=authority.context.authority_id, environment=profile.environment,
                           account_id=summary.account_id, evidence=evidence, instrument=risk, margin=margin,
                           maximum_entry_loss=summary.nav * limits.maximum_entry_loss_fraction,
                           stop_gap_buffer=limits.gap_buffer_pips * native.pip_size,
                           account_exposure=AccountExposureRisk(equity=summary.nav, current_gross_notional=gross,
                                                               currency_gross_notional=currency,
                                                               maximum_gross_leverage=limits.maximum_gross_leverage,
                                                               maximum_currency_fraction=limits.maximum_currency_fraction))


@FINANCIAL.calculation
def desired_units(decision: PolicyDecision, observation: RiskObservation, grant: AuthorizationGrant) -> Decimal | None:
    from oandafly.domain.decisions import Action
    if decision.action is Action.FLATTEN:
        return ZERO
    if decision.action is not Action.SET_TARGET or Decimal(str(decision.confidence)) < grant.limits.minimum_confidence:
        return None
    # Model floats cross into finance once, through canonical decimal strings.
    preference = Decimal(str(decision.preference))
    risk = observation.instrument.model_copy(update={"current_gross_units": ZERO})
    return rounded_target_units(direction=1 if preference > 0 else -1,
                                equity=observation.account_exposure.equity,
                                allocation=min(ONE, abs(preference)), instrument=risk,
                                free_margin=observation.account_exposure.equity,
                                stop_distance=grant.limits.stop_distance_pips * risk.pip,
                                maximum_loss=observation.maximum_entry_loss,
                                gap_buffer=(observation.stop_gap_buffer + grant.limits.slippage_bound_pips * risk.pip
                                            + risk.ask - risk.bid + 2 * risk.pip))


@FINANCIAL.calculation
def entry_prices(units: Decimal, risk: InstrumentRisk, grant: AuthorizationGrant,
                 display_precision: int) -> tuple[Decimal, Decimal]:
    quantum = ONE.scaleb(-display_precision)
    distance = grant.limits.stop_distance_pips * risk.pip
    slippage = grant.limits.slippage_bound_pips * risk.pip
    if units > 0:
        stop = (risk.bid - distance).quantize(quantum, rounding=ROUND_FLOOR)
        bound = (risk.ask + slippage).quantize(quantum, rounding=ROUND_CEILING)
    else:
        stop = (risk.ask + distance).quantize(quantum, rounding=ROUND_CEILING)
        bound = (risk.bid - slippage).quantize(quantum, rounding=ROUND_FLOOR)
    if min(stop, bound) <= 0:
        raise SafetyFence("Protective or bounded entry price is invalid")
    return stop, bound
