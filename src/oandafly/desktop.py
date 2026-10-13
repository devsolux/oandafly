"""Windowed entry point; never enrolls or starts an account on launch."""
from __future__ import annotations

import os
import sys


def ensure_standard_streams() -> None:
    # pythonw / the Windows windowed bootloader can set all three streams to None.
    # Errors shown in the GUI are deliberately redacted; secrets do not go here.
    for name, mode in (("stdin", "r"), ("stdout", "w"), ("stderr", "w")):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, mode, encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    import multiprocessing
    multiprocessing.freeze_support()
    ensure_standard_streams()
    from oandafly.macos import require_runtime
    require_runtime()
    from oandafly.__main__ import main as application_main
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--verify-runtime"]:
        from oandafly.runtime_probe import main as runtime_probe
        return runtime_probe()
    if arguments[:1] == ["--verify-build"]:
        from oandafly.ui.verification import run_probe
        return run_probe(arguments[1:])
    return application_main(arguments or ["desktop"])
