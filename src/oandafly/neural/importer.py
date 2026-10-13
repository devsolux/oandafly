"""Two-pass full-topology compiler for pinned CSV or Feather source tables.

No brain-region filter, synapse pruning or invented neurons is applied. The
curated neuron input defines the universe; every source edge with both endpoints
in that universe is retained. Excluded endpoints are committed in the receipt.
The sign table is a separately reviewed modelling assumption, not inferred here.
"""
from __future__ import annotations

import csv
import hashlib
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, Literal
from uuid import uuid4

import numpy as np
from numpy.typing import NDArray

from oandafly.domain.errors import SafetyFence
from oandafly.neural.artifacts import GraphManifest, describe_numeric, file_sha256, load_graph
from oandafly.paths import secure_directory
from oandafly.security.attestations import write_private_json

BATCH_ROWS = 100000


@dataclass(frozen=True, slots=True)
class ImportColumns:
    neuron_id: str = "bodyId"
    pre_id: str = "bodyId_pre"
    post_id: str = "bodyId_post"
    contacts: str = "weight"
    sign_id: str = "bodyId"
    sign: str = "sign"


def _integer(value: object, *, signed: bool = False) -> int:
    if isinstance(value, (int, np.integer)) and not isinstance(value, (bool, np.bool_)):
        result = int(value)
    elif isinstance(value, str) and value and (value.isascii() and value.isdigit() or signed and value in {"-1", "+1"}):
        result = int(value)
    else:
        raise ValueError("Source IDs/counts must be exact integers, never floating-point")
    if not (-1 if signed else 0) <= result <= 2**64 - 1:
        raise ValueError("Source integer exceeds the supported range")
    return result


def batches(path: Path, columns: tuple[str, ...]) -> Iterator[dict[str, NDArray[np.generic]]]:
    if path.is_symlink() or not path.is_file():
        raise SafetyFence("Scientific inputs must be explicit regular files")
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames is None or len(reader.fieldnames) != len(set(reader.fieldnames)) or not set(columns) <= set(reader.fieldnames):
                raise ValueError("CSV does not contain the explicitly mapped unique columns")
            buffer: dict[str, list[str]] = {column: [] for column in columns}
            for row in reader:
                for column in columns:
                    value = row.get(column)
                    if value is None:
                        raise ValueError("Source row is incomplete")
                    buffer[column].append(value)
                if len(buffer[columns[0]]) == BATCH_ROWS:
                    yield {column: np.asarray(values, dtype=object) for column, values in buffer.items()}
                    buffer = {column: [] for column in columns}
            if buffer[columns[0]]:
                yield {column: np.asarray(values, dtype=object) for column, values in buffer.items()}
    elif path.suffix.lower() in {".feather", ".arrow"}:
        try:
            import pyarrow as pa
            import pyarrow.ipc as ipc
        except ImportError as exc:
            raise RuntimeError("Feather import requires the data extra (pyarrow)") from exc
        with pa.memory_map(str(path), "r") as source:
            reader = ipc.open_file(source)
            if not set(columns) <= set(reader.schema.names):
                raise ValueError("Feather columns do not match the explicitly declared source mapping")
            for number in range(reader.num_record_batches):
                batch = reader.get_batch(number)
                for offset in range(0, batch.num_rows, BATCH_ROWS):
                    part = batch.slice(offset, BATCH_ROWS)
                    yield {column: part.column(column).to_numpy(zero_copy_only=False) for column in columns}
    else:
        raise ValueError("Only CSV and Feather/Arrow IPC source tables are supported")


def _unsigned(values: NDArray[np.generic]) -> NDArray[np.uint64]:
    if values.dtype.kind == "u":
        return values.astype(np.uint64, copy=False)
    if values.dtype.kind == "i":
        if np.any(values < 0):
            raise ValueError("Negative native body ID or contact count")
        return values.astype(np.uint64)
    return np.fromiter((_integer(value) for value in values), dtype=np.uint64, count=len(values))


def compile_graph(
    *, neurons_path: Path, connections_path: Path, signs_path: Path, destination: Path,
    release: str, scope: Literal["FULL_DECLARED_MALECNS", "TEST_FIXTURE"],
    columns: ImportColumns = ImportColumns(),
) -> GraphManifest:
    """Compile a fresh immutable directory. Never overwrite or silently crop."""
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("A compiled graph directory is immutable")
    source_paths = {"neurons": neurons_path, "connections": connections_path, "signs": signs_path}
    input_hashes = {name: file_sha256(path) for name, path in source_paths.items()}
    chunks = [_unsigned(batch[columns.neuron_id]) for batch in batches(neurons_path, (columns.neuron_id,))]
    if not chunks:
        raise ValueError("Empty curated neuron universe")
    ids = np.sort(np.concatenate(chunks))
    count = len(ids)
    if count > 1000000 or np.any(ids == 0) or np.any(ids[1:] == ids[:-1]):
        raise ValueError("Curated neurons must have 1..1000000 distinct positive native IDs")
    del chunks
    signs = np.zeros(count, dtype=np.int8)
    for batch in batches(signs_path, (columns.sign_id, columns.sign)):
        sign_ids = _unsigned(batch[columns.sign_id])
        positions = np.searchsorted(ids, sign_ids)
        if np.any(positions >= count) or np.any(ids[np.minimum(positions, count - 1)] != sign_ids):
            raise ValueError("The explicit sign mapping must match the curated universe exactly")
        for index, value in zip(positions, batch[columns.sign]):
            sign = _integer(value, signed=True)
            if signs[index] != 0 or sign not in {-1, 1}:
                raise ValueError("Missing/duplicate/invalid presynaptic sign assumption")
            signs[index] = sign
    if np.any(signs == 0):
        raise ValueError("Every curated neuron needs an explicitly reviewed sign assumption")

    def mapped_batches() -> Iterator[tuple[NDArray[np.uint64], NDArray[np.uint64], NDArray[np.uint64], NDArray[np.bool_], NDArray[np.uint64], NDArray[np.uint64]]]:
        for batch in batches(connections_path, (columns.pre_id, columns.post_id, columns.contacts)):
            pre = _unsigned(batch[columns.pre_id])
            post = _unsigned(batch[columns.post_id])
            weight = _unsigned(batch[columns.contacts])
            if np.any(weight == 0) or np.any(weight > 2**32 - 1) or np.any(pre == 0) or np.any(post == 0):
                raise ValueError("Published edges require positive body IDs and bounded contact counts")
            pre_index = np.searchsorted(ids, pre)
            post_index = np.searchsorted(ids, post)
            inside = ((pre_index < count) & (post_index < count)
                      & (ids[np.minimum(pre_index, count - 1)] == pre)
                      & (ids[np.minimum(post_index, count - 1)] == post))
            yield pre_index, post_index, weight, inside, pre, post

    row_counts = np.zeros(count, dtype=np.uint64)
    source_rows = excluded_rows = excluded_contacts = edge_count = contact_count = 0
    excluded_digest = hashlib.sha256()
    for pre_index, post_index, weight, inside, pre, post in mapped_batches():
        source_rows += len(inside)
        excluded_rows += int(np.count_nonzero(~inside))
        excluded_contacts += int(weight[~inside].sum(dtype=np.uint64))
        edge_count += int(np.count_nonzero(inside))
        contact_count += int(weight[inside].sum(dtype=np.uint64))
        if edge_count > 100000000 or source_rows > 1000000000:
            raise ValueError("Source graph exceeds its declared import resource bound")
        row_counts += np.bincount(post_index[inside].astype(np.int64), minlength=count).astype(np.uint64)
        for a, b, c in zip(pre[~inside], post[~inside], weight[~inside]):
            excluded_digest.update(f"{int(a)},{int(b)},{int(c)}\n".encode("ascii"))
    pointer = np.concatenate([np.array([0], dtype=np.uint64), np.cumsum(row_counts, dtype=np.uint64)])
    parent = secure_directory(destination.parent)
    stage = parent / f".graph-{uuid4().hex}"
    stage.mkdir(mode=0o700)
    try:
        np.save(stage / "neuron_ids.npy", ids, allow_pickle=False)
        np.save(stage / "indptr.npy", pointer, allow_pickle=False)
        np.save(stage / "signs.npy", signs, allow_pickle=False)
        edges = np.lib.format.open_memmap(stage / "indices.npy", mode="w+", dtype="<u4", shape=(edge_count,))
        weights = np.lib.format.open_memmap(stage / "contacts.npy", mode="w+", dtype="<u4", shape=(edge_count,))
        next_slot = pointer[:-1].copy()
        for pre_index, post_index, weight, inside, _pre, _post in mapped_batches():
            # Stable grouping gives contiguous writes without per-edge Python loops.
            pres = pre_index[inside]
            posts = post_index[inside]
            contacts = weight[inside]
            order = np.argsort(posts, kind="stable")
            pres, posts, contacts = pres[order], posts[order], contacts[order]
            boundaries = np.concatenate([[0], np.flatnonzero(posts[1:] != posts[:-1]) + 1, [len(posts)]])
            for start, stop in zip(boundaries[:-1], boundaries[1:]):
                if start == stop:
                    continue
                row = int(posts[start])
                slot = int(next_slot[row])
                edges[slot:slot + stop - start] = pres[start:stop]
                weights[slot:slot + stop - start] = contacts[start:stop]
                next_slot[row] += int(stop - start)
        if not np.array_equal(next_slot, pointer[1:]):
            raise SafetyFence("Two-pass source changed its directed edge inventory")
        for start, stop in zip(pointer[:-1], pointer[1:]):
            lo, hi = int(start), int(stop)
            ordering = np.argsort(edges[lo:hi], kind="stable")
            row = edges[lo:hi][ordering]
            if np.any(row[1:] == row[:-1]):
                raise ValueError("Duplicate published directed edges require an explicit source-table correction")
            weights[lo:hi] = weights[lo:hi][ordering]
            edges[lo:hi] = row
        edges.flush()
        weights.flush()
        del edges, weights
        final_hashes = {name: file_sha256(path) for name, path in source_paths.items()}
        if input_hashes != final_hashes:
            raise SafetyFence("A scientific input changed during compilation")
        spec = {**asdict(columns), "excluded_rows_sha256": excluded_digest.hexdigest(),
                "exclusion_reason": "endpoint_not_in_pinned_curated_neuron_table",
                "edge_orientation": "row=postsynaptic,col=presynaptic",
                "normalization": "performed_by_versioned_kernel_not_by_topology_importer"}
        manifest = GraphManifest(release=release, scope=scope, neurons=count, connections=edge_count,
                                 contacts=contact_count, source_rows=source_rows,
                                 excluded_endpoint_rows=excluded_rows, excluded_endpoint_contacts=excluded_contacts,
                                 source_sha256=input_hashes, import_spec=spec,
                                 files={name: describe_numeric(stage / name) for name in
                                        ("neuron_ids.npy", "indptr.npy", "indices.npy", "contacts.npy", "signs.npy")})
        for path in stage.iterdir():
            os.chmod(path, 0o600)
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        write_private_json(stage / "manifest.json", manifest)
        load_graph(stage)  # The compiler is not allowed to bless its own malformed output.
        os.rename(stage, destination)
        if os.name != "nt":
            fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return manifest
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
