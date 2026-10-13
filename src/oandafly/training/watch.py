"""Separate automatic candidate production service. No account/vault interfaces."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from oandafly.domain.primitives import sha256_json, utc_now
from oandafly.paths import secure_directory
from oandafly.security.attestations import write_private_json
from oandafly.training.trainer import CostAssumptions, TrainingSpec, read_candles, train_candidate


def watch_training(*, graph: Path, archive: Path, instrument: str, output: Path,
                   spec: TrainingSpec, costs: CostAssumptions, interval_seconds: int = 3600,
                   once: bool = False) -> None:
    if not 60 <= interval_seconds <= 86400:
        raise ValueError("Training observation interval must be between one minute and one day")
    secure_directory(output)
    while True:
        paths = sorted(archive.glob("*.jsonl"))
        if len(paths) > 100000:
            raise ValueError("Training archive exceeds the declared inventory bound")
        window_digest = read_candles(archive, instrument=instrument, maximum_bars=spec.maximum_bars)[1] if paths else "EMPTY"
        identity = sha256_json({"selected_window": window_digest,
                                "graph_manifest": hashlib.sha256((graph / "manifest.json").read_bytes()).hexdigest(),
                                "spec": spec, "costs": costs, "instrument": instrument})
        outcome = output / (identity + ".attempt.json")
        if paths and not outcome.exists():
            try:
                result = train_candidate(graph_directory=graph, archive_directory=archive, instrument=instrument,
                                         destination=output / identity, spec=spec, costs=costs)
            except Exception as exc:
                result = {"status": "CANDIDATE_FAILED", "code": type(exc).__name__}
            # A failed immutable data/spec attempt is not retried indefinitely.
            # New observations or an explicitly changed spec create a new trial.
            write_private_json(outcome, {"input_sha256": identity, "completed_at": utc_now(), "result": result})
        if once:
            return
        time.sleep(interval_seconds)
