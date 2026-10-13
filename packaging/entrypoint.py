"""Shared frozen entry. Multiprocessing is diverted before any GUI imports."""
if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    import sys
    # Only the explicit developer source-smoke command can inject its source root.
    # This switch is forbidden in a compiled application.
    if sys.argv[1:2] == ["--source-checkout"]:
        if getattr(sys, "frozen", False) or len(sys.argv) < 5 or sys.argv[3] != "--verify-build":
            raise SystemExit(2)
        from pathlib import Path
        root = Path(sys.argv[2]).resolve()
        if Path(__file__).resolve() != root / "packaging/entrypoint.py":
            raise SystemExit(2)
        sys.path.insert(0, str(root / "src"))
        del sys.argv[1:3]
    from oandafly.desktop import main
    raise SystemExit(main())
