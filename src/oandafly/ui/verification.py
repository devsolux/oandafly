"""Bounded GUI smoke mode: no canonical storage or worker access, source or frozen."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from pathlib import Path


def _write(path: Path, body: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(body, handle, sort_keys=True)
    os.replace(temporary, path)


def run_probe(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="oandafly --verify-build")
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(arguments)
    request = args.request.absolute()
    # Only an existing bounded regular request in a disposable directory. It is
    # not an environment override for account storage; normal startup never reads it.
    if request.is_symlink() or not request.is_file() or request.stat().st_size > 4096:
        return 2
    body = json.loads(request.read_text(encoding="utf-8"))
    if (set(body) != {"format", "nonce", "require_frozen"} or body["format"] != "oandafly.gui-probe.1"
            or not re.fullmatch(r"[0-9a-f]{64}", body["nonce"]) or type(body["require_frozen"]) is not bool):
        return 2
    frozen = bool(getattr(sys, "frozen", False))
    if body["require_frozen"] and not frozen:
        return 2
    output = request.parent / "heartbeat.json"
    acknowledgement = request.parent / "ack.json"
    from oandafly import __version__
    import tkinter as tk
    from oandafly.ui.app import DesktopApplication
    root = tk.Tk()
    app = DesktopApplication(root, verification=True)
    state = {"format": "oandafly.gui-heartbeat.1", "nonce": body["nonce"], "pid": os.getpid(),
             "version": __version__, "frozen": frozen, "main_thread": True, "ticks": 0,
             "mapped": False, "completed": False, "no_worker_access": True,
             "python": sys.version.split()[0], "tcl_tk": str(root.tk.call("info", "patchlevel"))}
    started, acknowledged = time.monotonic(), None
    failed = False
    root.update_idletasks()
    root.deiconify()
    root.lift()
    def heartbeat() -> None:
        nonlocal acknowledged, failed
        if app.closed:
            return
        now = time.monotonic()
        state.update(ticks=state["ticks"]+1, mapped=bool(root.winfo_ismapped()),
                     main_thread=threading.current_thread() is threading.main_thread(),
                     width=root.winfo_width(), height=root.winfo_height(),
                     window_id=int(root.wm_frame(), 0), elapsed_seconds=now-started)
        if getattr(app, "verification_error", None) or now-started > 30:
            state["error"] = getattr(app, "verification_error", "CHILD_WATCHDOG")
            failed = True
            _write(output, state)
            app.close()
            return
        if acknowledged is None and acknowledgement.is_file():
            try:
                ack = json.loads(acknowledgement.read_text(encoding="utf-8"))
                if ack == {"nonce": body["nonce"], "pid": os.getpid(), "window_observed": True}:
                    acknowledged = now
            except (OSError, ValueError):
                pass
        if acknowledged is not None and now-acknowledged >= 3:
            state["completed"] = True
            state["held_after_ack_seconds"] = now-acknowledged
            _write(output, state)
            app.close()
            return
        _write(output, state)
        root.after(100, heartbeat)
    root.after(50, heartbeat)
    root.mainloop()
    return 0 if state["completed"] and not failed else 2
