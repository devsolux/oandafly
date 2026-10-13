"""Executable full-CNS sparse leaky-rate model, not an original-brain replica.

For every node at every substep:
    r_next = (1-a) r + a tanh(g W r + E x)
W retains every declared edge, with presynaptic signs and incoming absolute-row
normalization. 0<a<=1 and 0<g<1 give a bounded contractive state transition.
The market encoder is an explicitly artificial stimulation mechanism.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from oandafly.domain.errors import SafetyFence
from oandafly.neural.artifacts import GraphManifest, KernelConfig, load_graph, load_model


class FullCNSKernel:
    def __init__(self, graph: GraphManifest, arrays: dict[str, NDArray[np.generic]], config: KernelConfig) -> None:
        from scipy.sparse import csr_matrix
        self.graph, self.config = graph, config
        pointer = arrays["indptr.npy"].astype(np.int64)
        indices = arrays["indices.npy"].astype(np.int32)
        weight = arrays["contacts.npy"].astype(np.float32)
        unsigned = csr_matrix((weight, indices, pointer), shape=(graph.neurons, graph.neurons), copy=False)
        row_sum = np.asarray(unsigned.sum(axis=1)).ravel()
        # Every existing edge remains nonzero; isolated curated nodes are retained.
        divisors = np.repeat(np.maximum(row_sum, 1), np.diff(pointer))
        weight *= arrays["signs.npy"][indices]
        weight /= divisors
        self.matrix = unsigned
        self.state = np.zeros(graph.neurons, dtype=np.float32)
        self.steps = 0
        self.alpha = np.float32(config.dt_ms / config.tau_ms)
        if self.matrix.nnz != graph.connections or np.any(self.matrix.data == 0):
            raise SafetyFence("Runtime normalization changed the declared edge inventory")

    def advance(self, drive: NDArray[np.float32], *, deadline: float | None = None) -> NDArray[np.float32]:
        if drive.shape != (self.graph.neurons,) or not np.isfinite(drive).all():
            raise SafetyFence("Invalid full-CNS stimulation vector")
        for _ in range(self.config.steps_per_bar):
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Full-CNS inference deadline expired")
            recurrent = self.matrix @ self.state
            candidate = ((1 - self.alpha) * self.state
                         + self.alpha * np.tanh(self.config.recurrent_gain * recurrent + drive))
            if not np.isfinite(candidate).all():
                raise SafetyFence("Nonfinite full-CNS state; no alternative policy is permitted")
            self.state = candidate.astype(np.float32, copy=False)
            self.steps += 1
        return self.state.copy()

    def reset(self) -> None:
        self.state = np.zeros(self.graph.neurons, dtype=np.float32)
        self.steps = 0


class FullCNSModel:
    def __init__(self, graph_directory: Path, model_directory: Path, *, allow_fixture: bool = False) -> None:
        self.graph, arrays = load_graph(graph_directory)
        if self.graph.scope != "FULL_DECLARED_MALECNS" and not allow_fixture:
            raise SafetyFence("Test fixtures cannot be loaded into a broker policy runtime")
        self.manifest, loaded = load_model(model_directory, self.graph)
        # Detach champion weights from mutable files before accepting any features.
        self.weights = {name: np.array(value, copy=True, order="C") for name, value in loaded.items()}
        for value in self.weights.values():
            value.setflags(write=False)
        self.kernel = FullCNSKernel(self.graph, arrays, self.manifest.kernel)
        self.observations = 0

    def infer(self, features: NDArray[np.float32], *, deadline: float | None = None) -> tuple[float, float]:
        if features.shape != (self.weights["encoder.npy"].shape[1],) or not np.isfinite(features).all():
            raise SafetyFence("Feature vector violates its versioned encoder")
        drive = (self.weights["encoder.npy"] @ features).astype(np.float32)
        state = self.kernel.advance(drive, deadline=deadline)
        # Read ALL curated neuron states; no sampled/hash-projected substitute.
        utility = self.weights["readout.npy"] @ state + self.weights["bias.npy"]
        if not np.isfinite(utility).all():
            raise SafetyFence("Nonfinite neural decoder")
        self.observations += 1
        return float(utility[0]), float(utility[1])
