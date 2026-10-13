"""Content-addressed, data-only MaleCNS graph and learned model artifacts."""
from __future__ import annotations

import hashlib
import math
import mmap
import os
import stat
from pathlib import Path
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from pydantic import Field, model_validator

from oandafly.domain.errors import SafetyFence
from oandafly.domain.numeric import NumericModel
from oandafly.domain.primitives import sha256_json
from oandafly.marketdata.features import FEATURE_NAMES, FEATURE_SCHEMA

MAX_ARRAY_BYTES = 4 * 1024**3
GRAPH_FILES = frozenset({"neuron_ids.npy", "indptr.npy", "indices.npy", "contacts.npy", "signs.npy"})
MODEL_FILES = frozenset({"encoder.npy", "readout.npy", "bias.npy"})


class NumericFile(NumericModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=1, le=MAX_ARRAY_BYTES)
    dtype: str
    shape: tuple[int, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def bounded(self) -> NumericFile:
        if any(type(v) is not int or v < 0 for v in self.shape):
            raise ValueError("Invalid array shape")
        dtype = np.dtype(self.dtype)
        if dtype.hasobject or dtype.fields is not None or dtype.kind not in "iuf" or dtype.itemsize > 8:
            raise ValueError("Only primitive bounded numeric arrays are admitted")
        if math.prod(self.shape) * dtype.itemsize > MAX_ARRAY_BYTES:
            raise ValueError("Array allocation exceeds its resource bound")
        return self


class GraphManifest(NumericModel):
    format: Literal["oandafly.graph.1"] = "oandafly.graph.1"
    release: str = Field(min_length=1, max_length=100)
    scope: Literal["FULL_DECLARED_MALECNS", "TEST_FIXTURE"]
    topology: Literal["ALL_CURATED_NEURONS_ALL_INTERNAL_PUBLISHED_EDGES"] = "ALL_CURATED_NEURONS_ALL_INTERNAL_PUBLISHED_EDGES"
    neurons: int = Field(ge=1, le=1000000)
    connections: int = Field(ge=0, le=100000000)
    contacts: int = Field(ge=0)
    source_rows: int = Field(ge=0)
    excluded_endpoint_rows: int = Field(ge=0)
    excluded_endpoint_contacts: int = Field(ge=0)
    source_sha256: dict[str, str]
    import_spec: dict[str, str]
    files: dict[str, NumericFile]

    @model_validator(mode="after")
    def layout(self) -> GraphManifest:
        if set(self.files) != GRAPH_FILES:
            raise ValueError("Graph must contain exactly the declared full topology arrays")
        expected = {
            "neuron_ids.npy": ("<u8", (self.neurons,)), "indptr.npy": ("<u8", (self.neurons + 1,)),
            "indices.npy": ("<u4", (self.connections,)), "contacts.npy": ("<u4", (self.connections,)),
            "signs.npy": ("|i1", (self.neurons,)),
        }
        for name, (dtype, shape) in expected.items():
            item = self.files[name]
            if np.dtype(item.dtype) != np.dtype(dtype) or item.shape != shape:
                raise ValueError("Graph array shape/dtype disagrees with its declared topology")
        if self.source_rows != self.connections + self.excluded_endpoint_rows or self.contacts < self.connections:
            raise ValueError("Import accounting omits or invents published connection rows")
        if self.scope == "FULL_DECLARED_MALECNS" and not self.release.startswith("male-cns:v"):
            raise ValueError("Full graph must identify a MaleCNS release")
        if set(self.source_sha256) != {"neurons", "connections", "signs"}:
            raise ValueError("Import must bind all three original input files")
        if any(len(v) != 64 or any(c not in "0123456789abcdef" for c in v) for v in self.source_sha256.values()):
            raise ValueError("Input identity is not SHA-256")
        return self

    @property
    def fingerprint(self) -> str:
        return sha256_json(self)


class KernelConfig(NumericModel):
    specification: Literal["MALECNS_FULL_RATE_V1"] = "MALECNS_FULL_RATE_V1"
    dt_ms: float = Field(default=1, gt=0, le=100, allow_inf_nan=False)
    tau_ms: float = Field(default=20, gt=0, le=1000, allow_inf_nan=False)
    recurrent_gain: float = Field(default=0.95, gt=0, lt=1, allow_inf_nan=False)
    steps_per_bar: int = Field(default=20, ge=1, le=1000)
    neural_warmup_bars: int = Field(default=10, ge=1, le=1000)

    @model_validator(mode="after")
    def stable_integrator(self) -> KernelConfig:
        if self.dt_ms > self.tau_ms:
            raise ValueError("Euler leak coefficient must lie in (0,1]")
        return self


class ModelManifest(NumericModel):
    format: Literal["oandafly.model.1"] = "oandafly.model.1"
    family: Literal["MALECNS_FULL_RATE_V1"] = "MALECNS_FULL_RATE_V1"
    graph_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    neurons: int = Field(ge=1, le=1000000)
    feature_schema: Literal["oandafly.market-encoder.1"] = FEATURE_SCHEMA
    feature_names: tuple[str, ...] = FEATURE_NAMES
    kernel: KernelConfig
    state_policy: Literal["BOUNDED_CAUSAL_WINDOW_V1"] = "BOUNDED_CAUSAL_WINDOW_V1"
    # Readout estimates separate net long and short utilities. No momentum override.
    output_names: tuple[Literal["LONG_NET_UTILITY", "SHORT_NET_UTILITY"], ...] = ("LONG_NET_UTILITY", "SHORT_NET_UTILITY")
    confidence_scale: float = Field(gt=0, allow_inf_nan=False)
    allocation_scale: float = Field(gt=0, allow_inf_nan=False)
    minimum_net_utility: float = Field(default=0, ge=0, allow_inf_nan=False)
    training_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed: int = Field(ge=0, le=2**32-1)
    files: dict[str, NumericFile]

    @model_validator(mode="after")
    def shape_contract(self) -> ModelManifest:
        if self.feature_names != FEATURE_NAMES or self.output_names != ("LONG_NET_UTILITY", "SHORT_NET_UTILITY"):
            raise ValueError("Unrecognized encoder or action decoder semantics")
        if set(self.files) != MODEL_FILES:
            raise ValueError("Model arrays are incomplete or contain undeclared inputs")
        shapes = {"encoder.npy": (self.neurons, len(FEATURE_NAMES)), "readout.npy": (2, self.neurons), "bias.npy": (2,)}
        for name, shape in shapes.items():
            if self.files[name].shape != shape or np.dtype(self.files[name].dtype) != np.dtype("<f4"):
                raise ValueError("Model numeric layout is incompatible")
        return self

    @property
    def fingerprint(self) -> str:
        return sha256_json(self)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_document(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
        raise SafetyFence("Artifact manifest is not a bounded regular file")
    for parent in path.absolute().parents:
        if parent.is_symlink():
            raise SafetyFence("Artifact directories must not contain symlinks")
    return path.read_bytes()


def load_numeric(path: Path, specification: NumericFile) -> NDArray[np.generic]:
    """Hash, parse and mmap the SAME opened inode; never pickle or decompress."""
    for parent in (path.absolute(), *path.absolute().parents):
        if parent.is_symlink():
            raise SafetyFence("Numeric artifact path contains a symlink")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != specification.size:
            raise SafetyFence("Numeric artifact file identity/size changed")
        digest = hashlib.sha256()
        with os.fdopen(os.dup(fd), "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            if digest.hexdigest() != specification.sha256:
                raise SafetyFence("Numeric artifact checksum failed")
            stream.seek(0)
            version = np.lib.format.read_magic(stream)
            if version == (1, 0):
                shape, fortran, dtype = np.lib.format.read_array_header_1_0(stream, max_header_size=4096)
            elif version == (2, 0):
                shape, fortran, dtype = np.lib.format.read_array_header_2_0(stream, max_header_size=4096)
            else:
                raise SafetyFence("Unsupported numeric file version")
            offset = stream.tell()
        if (dtype.hasobject or dtype.fields is not None or dtype != np.dtype(specification.dtype)
                or shape != specification.shape or fortran
                or offset + math.prod(shape) * dtype.itemsize != specification.size):
            raise SafetyFence("Numeric header violates its signed shape/dtype boundary")
        mapped = mmap.mmap(fd, 0, access=mmap.ACCESS_READ)
        array = np.ndarray(shape=shape, dtype=dtype, buffer=mapped, offset=offset)
        if dtype.kind == "f" and not np.isfinite(array).all():
            mapped.close()
            raise SafetyFence("Nonfinite model artifact")
        array.setflags(write=False)
        after = os.fstat(fd)
        if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise SafetyFence("Artifact changed during validation")
        return array
    finally:
        os.close(fd)


def describe_numeric(path: Path) -> NumericFile:
    value = np.load(path, allow_pickle=False, mmap_mode="r")
    return NumericFile(sha256=file_sha256(path), size=path.stat().st_size,
                       dtype=value.dtype.str, shape=value.shape)


def load_graph(directory: Path) -> tuple[GraphManifest, dict[str, NDArray[np.generic]]]:
    manifest = GraphManifest.model_validate_json(manifest_document(directory / "manifest.json"))
    arrays = {name: load_numeric(directory / name, spec) for name, spec in manifest.files.items()}
    ids, pointer = arrays["neuron_ids.npy"], arrays["indptr.npy"]
    indices, contacts, signs = arrays["indices.npy"], arrays["contacts.npy"], arrays["signs.npy"]
    if (np.any(ids[1:] <= ids[:-1]) or np.any(ids == 0) or pointer[0] != 0
            or pointer[-1] != manifest.connections or np.any(pointer[1:] < pointer[:-1])
            or np.any(indices >= manifest.neurons) or np.any(contacts == 0)
            or int(contacts.sum(dtype=np.uint64)) != manifest.contacts
            or not np.isin(signs, [-1, 1]).all()):
        raise SafetyFence("Full topology numeric invariants failed")
    for start, stop in zip(pointer[:-1], pointer[1:]):
        row = indices[int(start):int(stop)]
        if np.any(row[1:] <= row[:-1]):
            raise SafetyFence("Duplicate or noncanonical directed edges")
    return manifest, arrays


def load_model(directory: Path, graph: GraphManifest) -> tuple[ModelManifest, dict[str, NDArray[np.generic]]]:
    manifest = ModelManifest.model_validate_json(manifest_document(directory / "manifest.json"))
    if manifest.graph_sha256 != graph.fingerprint or manifest.neurons != graph.neurons:
        raise SafetyFence("Learned model does not bind this exact full topology")
    arrays = {name: load_numeric(directory / name, spec) for name, spec in manifest.files.items()}
    return manifest, arrays
