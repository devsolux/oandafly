"""Causal neural stimulation. These features do not choose trade direction."""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from oandafly.domain.errors import SafetyFence
from oandafly.marketdata.contracts import Candle

FEATURE_NAMES = (
    "return_1", "return_2", "return_4", "return_8", "return_16", "volatility_16",
    "range_fraction", "spread_fraction", "volume_relative", "hour_sin", "hour_cos", "bias",
)
FEATURE_SCHEMA = "oandafly.market-encoder.1"
WARMUP_BARS = 33


@dataclass(frozen=True, slots=True)
class FeatureFrame:
    state: str
    observed: int
    required: int
    values: NDArray[np.float32] | None


def encode(candles: tuple[Candle, ...]) -> FeatureFrame:
    if len(candles) < WARMUP_BARS:
        return FeatureFrame("WARMUP", len(candles), WARMUP_BARS, None)
    candles = candles[-WARMUP_BARS:]
    # A gap (including an ordinary weekend) starts a new warmup. It is never
    # silently forward-filled or treated as an evenly spaced return sequence.
    run_start = 0
    for i, (before, after) in enumerate(zip(candles, candles[1:]), start=1):
        if before.end != after.start:
            run_start = i
    if run_start:
        return FeatureFrame("WARMUP_AFTER_GAP", len(candles) - run_start, WARMUP_BARS, None)
    duration = candles[0].end - candles[0].start
    if any(c.end - c.start != duration for c in candles):
        raise SafetyFence("Feature history changes bar duration")
    close = np.array([float(c.mid_close) for c in candles], dtype=np.float64)
    returns = np.diff(np.log(close))
    last = candles[-1]
    levels = [(math.log(close[-1]) - math.log(close[-1 - n])) * 1000 for n in (1, 2, 4, 8, 16)]
    volatility = float(np.std(returns[-16:])) * 1000
    range_fraction = float((last.ask_high - last.bid_low) / last.mid_close) * 1000
    spread = float((last.ask_close - last.bid_close) / last.mid_close) * 1000
    mean_volume = sum(c.volume for c in candles[-17:-1]) / 16
    relative_volume = last.volume / max(mean_volume, 1) - 1
    hour = last.end.hour + last.end.minute / 60
    vector = np.asarray(levels + [volatility, range_fraction, spread, relative_volume,
                                  math.sin(hour / 24 * math.tau), math.cos(hour / 24 * math.tau), 1], dtype=np.float32)
    # Versioned bounded stimulation is numerical conditioning, not a signal gate.
    vector = np.clip(vector, -8, 8).astype(np.float32)
    if not np.isfinite(vector).all():
        raise SafetyFence("Nonfinite encoder output")
    vector.setflags(write=False)
    return FeatureFrame("READY", len(candles), WARMUP_BARS, vector)
