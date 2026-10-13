# Dependency resolution and build inputs

The source delivery includes four real resolved artifacts: `uv.lock`,
`requirements.lock`, `requirements-dev.lock` and `requirements-build.lock`.
Resolution and export checking use pinned **uv 0.12.17**, with Python **>=3.13,<3.14**.
Application dependency versions are unchanged by the macOS baseline change.

The runtime export covers core, native-vault and Feather extras. The development
export adds developer and PyInstaller dependencies. The universal build export pins
setuptools, wheel, build, PyInstaller, its hooks, pip-audit, mypy, uv and their
transitive packages, preserving platform markers. Build requirements are not shipped
as application dependencies. No fabricated or empty lock is accepted.

```sh
./dev.sh setup --resolve       # explicit resolution and setup
./dev.sh setup                 # existing checked locks only
python scripts/lock_dependencies.py --check
```

Resolution stages all four outputs before replacement; failures preserve prior
locks. Production builds validate reviewed freshness without rewriting inputs,
create a clean isolated environment and install hashed artifacts. They do not use
a developer editable environment as the frozen runtime. Runtime requirement checks
include actual Python-linked SQLite **>=3.51.3** and a real Tk window. macOS runtime
minimum is **26.0**; toolchain and deployment settings follow the
[production profile](PRODUCTION_BUILD.md).

The production pipeline audits both the runtime/development and build closures via
the hash-locked pip-audit tool. Unavailable advisory data, findings, skipped packages
or incomplete coverage block the gate. This is an advisory query, not proof that
native components are vulnerability-free. CPython, SQLite, Tcl/Tk and bundled
numerical-library provenance/license review and frozen tests remain separate.

For independent checks without signing:

```sh
python scripts/check_quality.py
python scripts/check_test_evidence.py /path/to/tests.json --binding /path/to/binding.json
```

Raw JUnit alone cannot certify complete collection. The JSON report binds each
collected/executed/excluded identity and phase outcome to the fresh run and source.
Production additionally binds locks, profile, interpreter, host and final payload.

References consulted for this implementation:
[uv locking and sync](https://docs.astral.sh/uv/concepts/projects/sync/),
[PyInstaller feature notes](https://pyinstaller.org/en/stable/feature-notes.html), and
[Apple Xcode requirements](https://developer.apple.com/support/xcode/).
