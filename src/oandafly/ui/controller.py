"""Desktop orchestration through the same durable worker authority boundary.

Opening the desktop does not access credentials or initialize a trading database.
Only explicit Start requests spawn a worker; the worker performs all admission.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from oandafly.platforms import local_worker_available
from oandafly.ui.model import validate_authority


def worker_command(configuration: Path, *, executable: str | None = None, frozen: bool | None = None) -> list[str]:
    exe = Path(executable or sys.executable)
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if frozen:
        # Both executables share the onedir payload; never treat a frozen exe as Python.
        sibling = exe.with_name("oandafly-cli.exe" if exe.suffix.lower() == ".exe" else "oandafly-cli")
        return [str(sibling), "run", "--config", str(configuration.absolute())]
    if sys.platform == "win32" and exe.name.lower() == "pythonw.exe":
        exe = exe.with_name("python.exe")
    return [str(exe), "-I", "-m", "oandafly", "run", "--config", str(configuration.absolute())]


class DesktopBackend:
    def __init__(self) -> None:
        self._children: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def diagnostics(self) -> dict:
        from oandafly.__main__ import doctor
        return doctor()

    def configuration(self, path: Path) -> dict:
        from oandafly.worker.configuration import load_config
        config = load_config(path)
        return {"authority_id": validate_authority(config.authority_id), "environment": config.environment,
                "mode": config.mode, "account_id": config.account_id, "instruments": list(config.instruments),
                "schema_valid": True, "authorization_verified": False,
                "configuration_sha256": hashlib.sha256(config.model_dump_json().encode()).hexdigest(),
                "local_worker_platform_available": local_worker_available()}

    def status(self, authority: str) -> dict:
        from oandafly.worker.control import read_status
        return read_status(validate_authority(authority))

    def control(self, authority: str, operation: str) -> str:
        from oandafly.worker.control import send_control
        return send_control(validate_authority(authority), operation)

    def start(self, configuration: Path, expected: dict) -> dict:
        from oandafly.__main__ import doctor
        # Re-read before spawn; the worker revalidates again. No GUI approval shortcuts.
        current = self.configuration(configuration)
        if current != expected:
            raise ValueError("Configuration changed after confirmation; review it again.")
        if not doctor()["runtime_prerequisites_satisfied"]:
            raise RuntimeError("Worker prerequisites unavailable; inspect Diagnostics.")
        authority = current["authority_id"]
        with self._lock:
            child = self._children.get(authority)
            if child is not None and child.poll() is None:
                return {"state": "PROCESS_ALREADY_RUNNING", "pid": child.pid, "trading_ready": False}
            # Refuse duplicate starts for a recently reporting external worker.
            try:
                from oandafly.ui.model import status_view
                if status_view(self.status(authority), authority)["freshness"] == "CURRENT OBSERVATION":
                    return {"state": "EXISTING_WORKER_OBSERVED", "trading_ready": False}
            except (OSError, ValueError):
                pass
            env = dict(os.environ)
            for key in list(env):
                if key.startswith(("PYTHON", "OANDA_", "OANDAFLY_TOKEN", "AWS_", "AZURE_", "MACOS_SIGN_", "MACOS_NOTARY_", "WIN_SIGN_")):
                    env.pop(key, None)
            env["PYTHONNOUSERSITE"] = "1"
            if getattr(sys, "frozen", False):
                env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            options: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                                       "stderr": subprocess.DEVNULL, "env": env, "close_fds": True,
                                       "cwd": str(configuration.absolute().parent)}
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            else:
                options["start_new_session"] = True
            child = subprocess.Popen(worker_command(configuration), **options)
            self._children[authority] = child
            # Reap after shutdown even if the UI is still open. No forced kill on window close.
            threading.Thread(target=child.wait, daemon=True, name="oandafly-worker-reaper").start()
            return {"state": "PROCESS_STARTED_WAITING_FOR_STATUS", "pid": child.pid, "trading_ready": False}

    def process_status(self) -> dict:
        with self._lock:
            return {authority: {"pid": child.pid, "exit_code": child.poll()}
                    for authority, child in self._children.items()}


class VerificationBackend:
    """No account paths, config loading, credential access or worker mutation."""
    def diagnostics(self) -> dict:
        from oandafly.platforms import host_report
        return {**host_report(), "verification_only": True, "worker_access": False}

    def __getattr__(self, name: str):
        raise RuntimeError("Verification desktop cannot access a worker")
