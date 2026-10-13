"""Bounded JSON inference process; production requires a Linux syscall allowlist.

Only trusted application code executes. Model artifacts contain numeric arrays,
not Python/pickle. No broker credential, account document or ledger path is sent
to the child. After loading, seccomp denies filesystem opens, networking, process
creation, ptrace and execution. This is containment, not a claim that the Python
runtime is an independently verified security boundary.
"""
from __future__ import annotations

import asyncio
import ctypes
import errno
import json
import math
import multiprocessing as mp
import os
import sys
import threading
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from oandafly.domain.errors import SafetyFence, Unavailable

MAX_MESSAGE = 65536


def linux_allowlist() -> None:
    if sys.platform != "linux":
        raise SafetyFence("Production inference requires qualified Linux seccomp containment")
    try:
        seccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    except OSError as exc:
        raise SafetyFence("libseccomp.so.2 is required for production inference") from exc
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    seccomp.seccomp_rule_add.restype = ctypes.c_int
    seccomp.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
    seccomp.seccomp_attr_set.restype = ctypes.c_int
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_load.restype = ctypes.c_int
    context = seccomp.seccomp_init(0x00050000 | errno.EPERM)  # SCMP_ACT_ERRNO(EPERM)
    if not context:
        raise SafetyFence("Could not allocate a containment filter")
    try:
        # SCMP_FLTATR_CTL_TSYNC = 4: also cover any existing numerical-library threads.
        if seccomp.seccomp_attr_set(context, 4, 1) != 0:
            raise SafetyFence("Cannot synchronize containment across numerical threads")
        for name in (
            "read", "write", "readv", "writev", "close", "fstat", "lseek", "fcntl",
            "mmap", "mmap2", "munmap", "mremap", "mprotect", "madvise", "brk",
            "futex", "futex_time64", "futex_waitv", "sched_yield", "sched_getaffinity",
            "clock_gettime", "clock_gettime64", "clock_nanosleep", "nanosleep", "gettimeofday", "time",
            "rt_sigaction", "rt_sigprocmask", "rt_sigreturn", "sigaltstack", "restart_syscall",
            "poll", "ppoll", "ppoll_time64", "select", "pselect6", "pselect6_time64",
            "getpid", "getppid", "gettid", "getuid", "geteuid", "getgid", "getegid",
            "getrandom", "getrusage", "exit", "exit_group",
        ):
            number = seccomp.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number >= 0 and seccomp.seccomp_rule_add(context, 0x7FFF0000, number, 0) != 0:
                raise SafetyFence("Could not install a containment syscall rule")
        if seccomp.seccomp_load(context) != 0:
            raise SafetyFence("Operating system refused the inference containment filter")
    finally:
        seccomp.seccomp_release(context)


def _send(connection: Connection, body: dict[str, Any]) -> None:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > MAX_MESSAGE:
        raise ValueError("Inference message exceeds its resource bound")
    connection.send_bytes(raw)


def _receive(connection: Connection) -> dict[str, Any]:
    raw = connection.recv_bytes(MAX_MESSAGE)
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("Duplicate inference message field")
            result[key] = value
        return result
    body = json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite input")))
    if not isinstance(body, dict):
        raise ValueError("Inference message must be an object")
    return body


def _child(connection: Connection, graph: str, model: str, instruments: tuple[str, ...],
           contained: bool, fixture: bool, memory_bytes: int) -> None:
    # Spawn does not inherit the parent's application objects or vault contents.
    os.environ.clear()
    os.environ.update({"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"})
    try:
        import numpy as np
        from threadpoolctl import threadpool_limits
        from oandafly.neural.runtime import FullCNSModel
        _pool_guard = threadpool_limits(limits=1)
        runtime = FullCNSModel(Path(graph), Path(model), allow_fixture=fixture)
        # Exercise allocation/BLAS paths before imposing the syscall filter.
        runtime.infer(np.zeros(runtime.manifest.feature_names.__len__(), dtype=np.float32))
        runtime.kernel.reset()
        runtime.observations = 0
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        if memory_bytes:
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        if contained:
            linux_allowlist()
        states: dict[str, tuple[Any, int]] = {}
        _send(connection, {"type": "READY", "contained": contained,
                           "graph": runtime.graph.fingerprint, "model": runtime.manifest.fingerprint})
        while True:
            body = _receive(connection)
            if body == {"type": "STOP"}:
                return
            if set(body) == {"type", "instrument"} and body["type"] == "RESET":
                if body["instrument"] not in instruments:
                    raise ValueError("Reset instrument is not enrolled")
                states.pop(body["instrument"], None)
                _send(connection, {"type": "RESET_DONE", "instrument": body["instrument"]})
                continue
            if set(body) != {"type", "id", "instrument", "features", "budget_seconds"} or body["type"] != "INFER":
                raise ValueError("Unrecognized inference command")
            instrument = body["instrument"]
            if instrument not in instruments:
                raise ValueError("Inference instrument is not enrolled")
            if type(body["budget_seconds"]) not in (int, float) or not 0 < body["budget_seconds"] <= 30:
                raise ValueError("Invalid inference deadline")
            features = body["features"]
            if not isinstance(features, list) or len(features) != len(runtime.manifest.feature_names):
                raise ValueError("Feature dimension does not match the model")
            if any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 8 for v in features):
                raise ValueError("Feature values violate the numerical boundary")
            if instrument in states:
                runtime.kernel.state, runtime.observations = states[instrument]
            else:
                runtime.kernel.reset()
                runtime.observations = 0
            utilities = runtime.infer(np.asarray(features, dtype=np.float32),
                                deadline=time.monotonic() + float(body["budget_seconds"]))
            observations = runtime.observations
            states[instrument] = (runtime.kernel.state, runtime.observations)
            _send(connection, {"type": "RESULT", "id": body["id"], "utilities": list(utilities),
                               "observations": observations})
    except BaseException as exc:
        try:
            _send(connection, {"type": "ERROR", "code": type(exc).__name__})
        except BaseException:
            pass
    finally:
        connection.close()


@dataclass(frozen=True)
class InferenceResult:
    utilities: tuple[float, float]
    observations: int


class InferenceProcess:
    def __init__(self, *, graph: Path, model: Path, instruments: tuple[str, ...],
                 require_containment: bool = True, allow_fixture: bool = False,
                 startup_timeout: float = 120, memory_bytes: int = 8 * 1024**3) -> None:
        if not instruments or len(set(instruments)) != len(instruments) or len(instruments) > 30:
            raise ValueError("Invalid inference instrument universe")
        from oandafly.domain.instruments import currency_pair
        for instrument in instruments:
            currency_pair(instrument)
        if not 1 <= startup_timeout <= 600 or not 1024**3 <= memory_bytes <= 128 * 1024**3:
            raise ValueError("Inference resource bounds are outside the qualified envelope")
        self._lock = threading.Lock()
        self._sequence = 0
        self._closed = False
        if require_containment:
            from oandafly.neural.containment import require_capability
            require_capability()
        self._parent, child = mp.get_context("spawn").Pipe(duplex=True)
        self._process = mp.get_context("spawn").Process(target=_child, daemon=True,
            args=(child, str(graph.absolute()), str(model.absolute()), instruments,
                  require_containment, allow_fixture, memory_bytes), name="oandafly-full-malecns")
        self._process.start()
        child.close()
        try:
            if not self._parent.poll(startup_timeout):
                raise Unavailable("Full MaleCNS startup deadline expired")
            ready = _receive(self._parent)
            if ready.get("type") != "READY" or (require_containment and ready.get("contained") is not True):
                raise SafetyFence("Full MaleCNS artifact loading or containment failed")
            self.graph_sha256, self.model_sha256 = ready["graph"], ready["model"]
            self.contained = ready["contained"]
        except BaseException:
            self.close()
            raise

    def reset(self, instrument: str) -> None:
        """Begin a deterministic bounded causal replay, not an assumed continuation."""
        with self._lock:
            if self._closed or not self._process.is_alive():
                raise Unavailable("Inference process is unavailable")
            _send(self._parent, {"type": "RESET", "instrument": instrument})
            if not self._parent.poll(3):
                self.close()
                raise Unavailable("Inference reset deadline expired")
            if _receive(self._parent) != {"type": "RESET_DONE", "instrument": instrument}:
                self.close()
                raise SafetyFence("Inference reset protocol failed")

    def infer(self, instrument: str, features: Any, *, timeout: float = 3) -> InferenceResult:
        if not 0 < timeout <= 30:
            raise ValueError("Inference deadline must be within (0,30] seconds")
        with self._lock:
            if self._closed or not self._process.is_alive():
                raise Unavailable("Inference process is unavailable; warmup is required after restart")
            self._sequence += 1
            _send(self._parent, {"type": "INFER", "id": self._sequence, "instrument": instrument,
                                "features": [float(value) for value in features], "budget_seconds": timeout})
            if not self._parent.poll(timeout):
                self.close()
                raise Unavailable("Full MaleCNS inference deadline expired; no fallback strategy is allowed")
            body = _receive(self._parent)
            if body.get("type") != "RESULT" or body.get("id") != self._sequence:
                self.close()
                raise Unavailable("Inference failed its bounded protocol")
            values = body.get("utilities")
            count = body.get("observations")
            if (not isinstance(values, list) or len(values) != 2 or
                any(type(v) not in (int, float) or not math.isfinite(v) for v in values)
                or type(count) is not int or count < 1):
                self.close()
                raise SafetyFence("Inference returned an invalid action payload")
            return InferenceResult((float(values[0]), float(values[1])), count)

    async def infer_async(self, instrument: str, features: Any, *, timeout: float = 3) -> InferenceResult:
        return await asyncio.to_thread(self.infer, instrument, features, timeout=timeout)

    def close(self) -> None:
        self._closed = True
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=2)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(timeout=2)
        self._parent.close()
