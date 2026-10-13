"""Immutable candle chunks for replay and training; no fabricated missing bars."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterable, Iterator

from oandafly.domain.errors import Conflict, SafetyFence
from oandafly.marketdata.contracts import Candle
from oandafly.paths import secure_directory

MAX_CHUNK_BYTES = 32 * 1024 * 1024


class CandleArchive:
    def __init__(self, directory: Path) -> None:
        self.directory = secure_directory(directory)

    def write(self, candles: Iterable[Candle]) -> Path:
        rows = list(candles)
        if not rows or len(rows) > 10000:
            raise ValueError("Archive chunks require 1..10000 complete candles")
        if len({(c.source, c.instrument) for c in rows}) != 1:
            raise ValueError("A chunk must bind one instrument and source")
        if any(a.end > b.start for a, b in zip(rows, rows[1:])):
            raise ValueError("Candle chunk is not chronologically disjoint")
        data = b"\n".join(c.model_dump_json().encode("utf-8") for c in rows) + b"\n"
        if len(data) > MAX_CHUNK_BYTES:
            raise ValueError("Candle chunk exceeds its bound")
        identity = hashlib.sha256(data).hexdigest()
        path = self.directory / f"{identity}.jsonl"
        if path.exists():
            if path.read_bytes() != data:
                raise Conflict("Content-addressed candle chunk changed")
            return path
        temporary = self.directory / ("." + identity + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        if os.name != "nt":
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return path

    @staticmethod
    def read(path: Path) -> Iterator[Candle]:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CHUNK_BYTES:
            raise SafetyFence("Invalid bounded candle chunk")
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != path.stem:
            raise SafetyFence("Candle chunk identity mismatch")
        previous: Candle | None = None
        for line in body.splitlines():
            candle = Candle.model_validate_json(line)
            if previous is not None and (candle.instrument != previous.instrument or candle.source != previous.source or candle.start < previous.end):
                raise SafetyFence("Candle chunk has mixed identity or overlapping observations")
            previous = candle
            yield candle
