"""Decode learned full-CNS utilities into explicit, expiring portfolio preferences."""
from __future__ import annotations

import math
from datetime import timedelta

from oandafly.domain.decisions import Action, PolicyDecision
from oandafly.domain.primitives import sha256_json
from oandafly.marketdata.contracts import MarketCut
from oandafly.neural.artifacts import ModelManifest


def decision(
    cut: MarketCut, model: ModelManifest, *, utilities: tuple[float, float] | None,
    observations: int, warmup_required: int, reason: str | None = None,
    validity_seconds: int = 10,
) -> PolicyDecision:
    """Confidence is a model separation score, NOT a probability of profit.
    Its calibration must be independently evaluated before deployment.

    The model estimates net long and short utility; no technical-indicator rule
    supplies an alternative sign. Risk translates the preference to actual units.
    An expired/missing inference never implies a FLATTEN instruction.
    """
    if not 1 <= validity_seconds <= 60:
        raise ValueError("Decision validity exceeds the bounded execution window")
    action, preference, confidence = Action.ABSTAIN, 0.0, 0.0
    note = reason or "WARMUP"
    if utilities is not None and observations >= warmup_required and reason is None:
        if len(utilities) != 2 or not all(math.isfinite(v) for v in utilities):
            raise ValueError("Invalid learned utility output")
        long_value, short_value = utilities
        maximum = max(long_value, short_value)
        confidence = 1.0 - math.exp(-abs(long_value - short_value) / model.confidence_scale)
        if maximum <= model.minimum_net_utility:
            action, note = Action.FLATTEN, "NO_POSITIVE_NET_UTILITY"
        elif long_value == short_value:
            action, note = Action.ABSTAIN, "UNRESOLVED_MODEL_TIE"
        else:
            sign = 1.0 if long_value > short_value else -1.0
            preference = sign * math.tanh((maximum - model.minimum_net_utility) / model.allocation_scale)
            action, note = Action.SET_TARGET, "LEARNED_FULL_CNS_TARGET"
    identity = sha256_json({"model": model.fingerprint, "cut": cut.fingerprint,
                            "action": action.value, "preference": preference,
                            "confidence": confidence, "reason": note})
    return PolicyDecision(decision_id=identity, model_sha256=model.fingerprint,
                          market_cut_sha256=cut.fingerprint, instrument=cut.instrument,
                          observed_at=cut.as_of, valid_until=cut.as_of + timedelta(seconds=validity_seconds),
                          action=action, preference=preference, confidence=confidence, reason=note,
                          warmup_observed=observations, warmup_required=warmup_required)
