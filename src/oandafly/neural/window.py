"""The same bounded causal state policy for training, replay and native trading.

Every decision resets its instrument state and replays exactly the declared
number of feature frames. This makes restart behavior deterministic without
trusting an unaudited latent checkpoint. Budget the COMPLETE replay, not one
internal frame, when qualifying inference latency.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import numpy as np

from oandafly.domain.errors import Unavailable
from oandafly.marketdata.contracts import Candle
from oandafly.marketdata.features import WARMUP_BARS, encode
from oandafly.neural.isolation import InferenceProcess, InferenceResult


@dataclass(frozen=True)
class FeatureWindow:
    state: str
    observed: int
    required: int
    frames: tuple[np.ndarray, ...]


def feature_window(candles: tuple[Candle, ...], context_frames: int) -> FeatureWindow:
    if type(context_frames) is not int or not 1 <= context_frames <= 1000:
        raise ValueError("Invalid declared neural context window")
    needed = WARMUP_BARS + context_frames - 1
    if len(candles) < needed:
        return FeatureWindow("WARMUP", len(candles), needed, ())
    candles = candles[-needed:]
    frames = []
    for end in range(WARMUP_BARS, needed + 1):
        frame = encode(candles[end-WARMUP_BARS:end])
        if frame.values is None:
            return FeatureWindow(frame.state, frame.observed, needed, ())
        frames.append(frame.values)
    return FeatureWindow("READY", needed, needed, tuple(frames))


async def infer_window(process: InferenceProcess, instrument: str, window: FeatureWindow,
                       *, deadline_seconds: float) -> InferenceResult:
    if not window.frames or not 0 < deadline_seconds <= 30:
        raise ValueError("Inference requires a complete causal window and bounded deadline")
    deadline = time.monotonic() + deadline_seconds
    await asyncio.to_thread(process.reset, instrument)
    result = None
    for features in window.frames:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.close()
            raise Unavailable("Complete full-CNS decision window exceeded its qualified deadline")
        result = await process.infer_async(instrument, features, timeout=min(30, remaining))
    return result
