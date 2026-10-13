"""Atomic, data-only model publication; publication conveys no trading authority."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import uuid4

import numpy as np

from oandafly.neural.artifacts import GraphManifest, KernelConfig, ModelManifest, describe_numeric, load_model
from oandafly.paths import secure_directory
from oandafly.security.attestations import write_private_json


def publish_model(destination: Path, *, graph: GraphManifest, encoder: np.ndarray,
                  readout: np.ndarray, bias: np.ndarray, kernel: KernelConfig,
                  training_receipt_sha256: str, seed: int, confidence_scale: float,
                  allocation_scale: float, minimum_net_utility: float = 0) -> ModelManifest:
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Model artifacts are immutable; choose a new artifact directory")
    secure_directory(destination.parent)
    stage = destination.parent / ("." + destination.name + "." + uuid4().hex + ".tmp")
    secure_directory(stage)
    try:
        for name, value in (("encoder.npy", encoder), ("readout.npy", readout), ("bias.npy", bias)):
            array = np.asarray(value, dtype="<f4", order="C")
            if not np.isfinite(array).all():
                raise ValueError("Cannot publish nonfinite model values")
            with (stage / name).open("xb") as stream:
                os.chmod(stage / name, 0o600)
                np.save(stream, array, allow_pickle=False)
                stream.flush()
                os.fsync(stream.fileno())
        manifest = ModelManifest(graph_sha256=graph.fingerprint, neurons=graph.neurons,
            kernel=kernel, confidence_scale=float(confidence_scale), allocation_scale=float(allocation_scale),
            minimum_net_utility=float(minimum_net_utility), training_receipt_sha256=training_receipt_sha256,
            seed=seed, files={name: describe_numeric(stage / name)
                              for name in ("encoder.npy", "readout.npy", "bias.npy")})
        write_private_json(stage / "manifest.json", manifest)
        load_model(stage, graph)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("Concurrent model publication")
        os.rename(stage, destination)
        if os.name != "nt":
            fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return manifest
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
