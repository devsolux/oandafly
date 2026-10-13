"""Automatic full-topology reservoir/readout training, never self-authorization.

The entire declared graph remains active. Encoder and structural weights are
frozen; a readout over EVERY neuron learns long/short net-utility labels. Labels
use subsequent executable bid/ask bars and explicitly supplied cost assumptions.
They are not actual broker fills or account-home NAV. Native forward evaluation
and independent qualification are mandatory before this candidate may trade.
"""
from __future__ import annotations

import hashlib
import heapq
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import Field
from threadpoolctl import threadpool_limits

from oandafly.domain.financial import FINANCIAL
from oandafly.domain.numeric import NumericModel
from oandafly.domain.primitives import StrictModel, sha256_json, utc_now
from oandafly.marketdata.contracts import Candle
from oandafly.marketdata.features import FEATURE_NAMES
from oandafly.marketdata.history import CandleArchive
from oandafly.neural.artifacts import KernelConfig, load_graph
from oandafly.neural.publishing import publish_model
from oandafly.neural.runtime import FullCNSKernel
from oandafly.neural.window import feature_window
from oandafly.security.attestations import write_private_json


class CostAssumptions(StrictModel):
    format: Literal["oandafly.utility-costs.1"] = "oandafly.utility-costs.1"
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    commission_round_trip_fraction: Decimal = Field(ge=0)
    slippage_per_side_fraction: Decimal = Field(ge=0)
    financing_long_per_day_fraction: Decimal = Field(ge=0)
    financing_short_per_day_fraction: Decimal = Field(ge=0)
    convention: Literal["NEXT_BAR_OPEN_TO_HORIZON_CLOSE_WITH_EXPLICIT_COST_PROXY"] = "NEXT_BAR_OPEN_TO_HORIZON_CLOSE_WITH_EXPLICIT_COST_PROXY"


class TrainingSpec(NumericModel):
    format: Literal["oandafly.training.1"] = "oandafly.training.1"
    kernel: KernelConfig = KernelConfig()
    seed: int = Field(default=1, ge=0, le=2**32-1)
    horizon_bars: int = Field(default=3, ge=1, le=288)
    learning_rate: float = Field(default=0.02, gt=0, le=1)
    regularization: float = Field(default=0.0001, ge=0, le=0.1)
    maximum_seconds: int = Field(default=3600, ge=1, le=86400)
    maximum_bars: int = Field(default=20000, ge=256, le=100000)
    confidence_scale: float = Field(default=0.0003, gt=0)
    allocation_scale: float = Field(default=0.0005, gt=0)
    minimum_net_utility: float = Field(default=0.00005, ge=0)
    # Fixed chronological partitions; never re-fit on their labels in this run.
    train_fraction: float = Field(default=0.65, gt=0.3, lt=0.8)
    validation_fraction: float = Field(default=0.20, gt=0.05, lt=0.3)


def read_candles(directory: Path, *, instrument: str, maximum_bars: int) -> tuple[tuple[Candle, ...], str]:
    paths = sorted(directory.glob("*.jsonl"))
    if not paths or len(paths) > 100000:
        raise ValueError("Training needs a bounded immutable candle archive")
    identities: dict[datetime, Candle] = {}
    source = None
    digest = hashlib.sha256()
    oldest: list[datetime] = []
    for path in paths:
        for candle in CandleArchive.read(path):
            if candle.instrument != instrument:
                raise ValueError("Train each instrument from a separately scoped archive")
            if source is not None and candle.source != source:
                raise ValueError("Never mix practice/live/synthetic sources in one training run")
            source = candle.source
            if len(identities) >= maximum_bars and candle.start < oldest[0]:
                continue  # Explicit rolling selection; never load an unbounded T x N matrix.
            old = identities.get(candle.start)
            if old is not None and old.fingerprint != candle.fingerprint:
                raise ValueError("Historical bar changed; quarantine instead of selecting a revision")
            if old is None:
                heapq.heappush(oldest, candle.start)
            identities[candle.start] = candle
            if len(identities) > maximum_bars:
                del identities[heapq.heappop(oldest)]
    candles = tuple(identities[t] for t in sorted(identities))
    if any(a.end > b.start for a, b in zip(candles, candles[1:])):
        raise ValueError("Historical bars overlap")
    for candle in candles:
        digest.update(candle.fingerprint.encode("ascii"))
    return candles, digest.hexdigest()


@FINANCIAL.calculation
def net_utility_label(candles: tuple[Candle, ...], index: int, horizon: int,
                      costs: CostAssumptions) -> tuple[float, float] | None:
    """No label may include the signal bar's open or a fabricated fill."""
    if index + horizon >= len(candles):
        return None
    interval = candles[index:index+horizon+1]
    if any(a.end != b.start for a, b in zip(interval, interval[1:])):
        return None
    entry, exit = candles[index+1], candles[index+horizon]
    days = Decimal(str((exit.end - entry.start).total_seconds())) / Decimal(86400)
    friction = costs.commission_round_trip_fraction + 2 * costs.slippage_per_side_fraction
    long_value = (exit.bid_close - entry.ask_open) / entry.ask_open - friction - days * costs.financing_long_per_day_fraction
    short_value = (entry.bid_open - exit.ask_close) / entry.bid_open - friction - days * costs.financing_short_per_day_fraction
    return float(long_value), float(short_value)


def _state(kernel: FullCNSKernel, encoder: np.ndarray, candles: tuple[Candle, ...],
           index: int, deadline: float) -> np.ndarray | None:
    window = feature_window(candles[:index+1], kernel.config.neural_warmup_bars)
    if not window.frames:
        return None
    kernel.reset()
    for features in window.frames:
        kernel.advance(encoder @ features, deadline=deadline)
    return kernel.state


def train_candidate(*, graph_directory: Path, archive_directory: Path, instrument: str,
                    destination: Path, spec: TrainingSpec, costs: CostAssumptions,
                    allow_fixture: bool = False) -> dict:
    from oandafly.domain.instruments import currency_pair
    currency_pair(instrument)
    if spec.train_fraction + spec.validation_fraction >= 0.95:
        raise ValueError("A separate chronological forward segment is required")
    if destination.exists():
        raise FileExistsError("Candidate destinations are immutable")
    graph, arrays = load_graph(graph_directory)
    if graph.scope != "FULL_DECLARED_MALECNS" and not allow_fixture:
        raise ValueError("Real candidate training requires the complete declared graph")
    candles, archive_digest = read_candles(archive_directory, instrument=instrument, maximum_bars=spec.maximum_bars)
    if len(candles) < max(256, spec.kernel.neural_warmup_bars + 33 + 6 * spec.horizon_bars):
        raise ValueError("Training archive is too short for causal partitions and warmup")
    rng = np.random.default_rng(spec.seed)
    encoder = rng.normal(0, 0.25 / np.sqrt(len(FEATURE_NAMES)), size=(graph.neurons, len(FEATURE_NAMES))).astype(np.float32)
    readout = np.zeros((2, graph.neurons), dtype=np.float32)
    bias = np.zeros(2, dtype=np.float32)
    kernel = FullCNSKernel(graph, arrays, spec.kernel)
    n_train, n_validate = int(len(candles)*spec.train_fraction), int(len(candles)*(spec.train_fraction+spec.validation_fraction))
    deadline = time.monotonic() + spec.maximum_seconds
    metrics = {}
    segments = (("train", 0, n_train), ("validation", n_train, n_validate), ("forward_research", n_validate, len(candles)))
    with threadpool_limits(limits=1):
        for name, start, end in segments:
            squared_error, count, gross_utility = np.zeros(2, dtype=np.float64), 0, 0.0
            # Purge labels crossing the partition; sample at non-overlapping
            # holding intervals. Context uses only known bars, never future labels.
            for i in range(start, end-spec.horizon_bars, spec.horizon_bars):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Full-topology candidate exceeded its declared training budget")
                labels = net_utility_label(candles, i, spec.horizon_bars, costs)
                if labels is None:
                    continue
                state = _state(kernel, encoder, candles, i, deadline)
                if state is None:
                    continue
                prediction = readout @ state + bias
                error = np.asarray(labels, dtype=np.float32) - prediction
                squared_error += np.square(error.astype(np.float64))
                chosen = int(np.argmax(prediction))
                if prediction[chosen] > spec.minimum_net_utility:
                    gross_utility += labels[chosen]
                if name == "train":
                    denominator = float(np.dot(state, state)) + 1.0
                    step = spec.learning_rate / denominator
                    readout *= 1.0 - step * spec.regularization
                    readout += step * error[:, None] * state[None, :]
                    bias += step * error
                count += 1
            if count < 5:
                raise ValueError("Chronological segment has insufficient complete non-overlapping observations")
            metrics[name] = {"observations": count, "mse_long": float(squared_error[0]/count),
                             "mse_short": float(squared_error[1]/count),
                             "selected_fixed_horizon_utility_sum_not_pnl": gross_utility,
                             "start": candles[start].start.isoformat(), "end": candles[end-1].end.isoformat()}
    receipt = {"format": "oandafly.candidate-training.1", "status": "UNQUALIFIED_CANDIDATE",
               "instrument": instrument, "graph_sha256": graph.fingerprint, "archive_sha256": archive_digest,
               "source": candles[0].source, "spec": spec.model_dump(mode="json"),
               "cost_assumptions": costs.model_dump(mode="json"), "metrics": metrics,
               "completed_at": utc_now().isoformat(),
               "limitations": ["Fixed-horizon quote utility is not actual broker execution or account-home PnL.",
                               "Research forward metrics are not a repeatedly reusable promotion holdout.",
                               "No evaluation in this process can sign its own trading qualification."]}
    receipt_digest = sha256_json(receipt)
    model = publish_model(destination, graph=graph, encoder=encoder, readout=readout, bias=bias,
                          kernel=spec.kernel, training_receipt_sha256=receipt_digest, seed=spec.seed,
                          confidence_scale=spec.confidence_scale, allocation_scale=spec.allocation_scale,
                          minimum_net_utility=spec.minimum_net_utility)
    write_private_json(destination / "training-receipt.json", receipt)
    return {"model_sha256": model.fingerprint, "training_receipt_sha256": receipt_digest,
            "status": "UNQUALIFIED_CANDIDATE", "metrics": metrics}
