# Desktop-first Oandafly 1.0.0

## What launches

`oandafly`, `python -m oandafly`, `oandafly-desktop`, `./dev.sh` and `./dev.ps1`
open the native Tk workspace. No mandatory authority/config argument, empty-console
exit, automatic enrollment, automatic worker start or silently armed LIVE mode.
The command `oandafly run --config PRIVATE.json` remains the headless worker.

The workspace provides configuration selection, metadata validation, status attachment,
freshness/error reporting, independent desktop/worker diagnostics and queued HALT,
FLATTEN, RESUME and STOP controls. Local I/O runs on a daemon thread; Tk is only
updated on its main thread. Account workers are independent processes and closing
the window does not kill them. STOP is not FLATTEN. Process startup is not readiness.

## Supported desktop targets versus worker capability

| Target | Native desktop/build recipe | Local production inference |
|---|---|---|
| macOS ARM64 | `.app` with icons, GUI + console helper, optional Developer ID/notarization | Not implemented/qualified; no containment bypass |
| Windows AMD64 | Portable windowed `.exe` + console helper; optional Authenticode/shortcut | Not implemented/qualified; no containment bypass |
| Linux AMD64 | Portable onedir + symlink-preserving `.tar.gz`, optional menu entry | Linux libseccomp implementation; full-model/account qualification still required |

Build metadata targets macOS **26.0 ARM64**, Windows 11 and glibc 2.35+ Linux.
macOS builds require Xcode 27.0 / Apple clang 21.0.0 (clang-2100.3.34.2) /
SDK 27.0 (26A425), with actual identities verified separately. Compilation on a
newer host does not establish 26.0 runtime compatibility. See the
[production contract](PRODUCTION_BUILD.md) and [current evidence](IMPLEMENTATION_REPORT.md).

This delivery does not provide a remote Linux-worker connection for macOS/Windows.
Those desktops can inspect locally available authorized status and configurations,
but their Start control remains disabled without a production inference backend.
Do not mistake desktop distribution support for three-platform autonomous trading.

## First setup from source

Python is deliberately **3.13**, matching `pyproject.toml`; the supplied launcher's
3.14 default was inconsistent and has been removed. Tcl/Tk must belong to that
same interpreter. SQLite >=3.51.3 is enforced for worker/database use, not for an
unconnected desktop opening its setup/diagnostics screen.

macOS (native ARM64 Terminal, not Rosetta):

```sh
brew install uv python@3.13 python-tk@3.13
./dev.sh setup --resolve
./dev.sh
```

Linux (native AMD64): install CPython 3.13 including Tk and uv using your supported
OS packaging or managed Python build; then:

```sh
./dev.sh setup --python /absolute/path/to/python3.13 --resolve
./dev.sh
```

Windows (native AMD64 PowerShell, native CPython 3.13 with Tcl/Tk and uv installed):

```powershell
.\dev.ps1 setup --python C:\Python313\python.exe --resolve
.\dev.ps1
```

Replace example interpreter paths with real installations. The launchers do not
change PowerShell execution policy, download another Python, delete an existing
environment, or embed your credentials. `setup --resolve` explicitly generates
missing dependency locks; failures do not create placeholder files. Later setups
use `setup` and `uv sync --locked`. Normal launches never sync or resolve packages.

All four genuine lockfiles are included. The production pipeline checks their
freshness and uses clean environments. Setup uses the runtime/dev graph; the
isolated production environment also installs the pinned build/audit/type tools.
See [dependencies](DEPENDENCIES.md).

## Worker selection and launch

Open a private, complete `WorkerConfig` JSON. Only metadata is loaded; the desktop
never enrolls tokens, signs approvals or accesses the native vault. Configuration
selection does not imply valid signatures, eligibility or a trading grant.
On supported Linux runtimes, explicitly confirm Start. The child then independently
performs existing admission, ownership, account, graph/model and risk checks.
A second UI start does not override the canonical account ownership lock.

Worker logs/state remain in the canonical OS-account-derived store, not under the
checkout. Status attachment is read-only and does not create a directory. Authority
storage folders are `sha256(authority_id UTF-8)`, preserving case-distinct IDs and
avoiding Windows colon/ADS/reserved-name issues. This is a fresh project layout;
**no legacy-store migration or auto-discovery of old directories is provided**.

The CLI executable in a native bundle is `oandafly-cli` / `oandafly-cli.exe`. Use it
for diagnostic commands and explicit worker execution. Frozen child processes do
not invoke a bundled executable as though it were a Python interpreter. A separate
process environment is used for workers that outlive the UI.

## Build and verification

```sh
./dev.sh build-unsigned           # complete common gates; no Developer ID/notary probes
./dev.sh build                    # same gates plus signing/notarization/stapling
./dev.sh verify /path/to/production-release-directory
```

Both build commands are currently blocked by the missing macOS containment helper
and baseline runner, as detailed in [PRODUCTION_BUILD.md](PRODUCTION_BUILD.md).

Use the equivalent `dev.ps1` commands on Windows. Build on the requested native
OS/architecture. macOS and Windows require an interactive logged-in GUI session.
Linux requires X11/XWayland; `xvfb-run -a` can exercise a virtual display but is not
physical-display qualification. See [packaging](../packaging/README.md).

A verification-only GUI mode creates the actual workspace with a backend that
cannot load configuration, read account state or start a worker. The parent gate
checks a bounded nonce/PID heartbeat, native window visibility, at least three
seconds of continued main-thread updates, clean shutdown, and a frozen runtime.
`python scripts/verify_build.py --source` explicitly produces **source smoke**
evidence instead; it can never pass as a compiled artifact. The desktop smoke is
not an OS malware sandbox and does not establish trading/model correctness.

## Cleanup

`./dev.sh clean` works without Python, uv or dependency locks. `clean --all` also
removes a nonlinked, unused `.venv` after an `lsof` check. Developer/build mutexes
block cleanup. Inspect stale locks only after confirming no owner is active; do
not delete them automatically. Bash 3.2 is sufficient. Windows `dev.ps1 clean`
uses the standard-library helper; `--all` selects an external Python and checks
running process paths. Neither launcher terminates a worker to force cleanup.

Published `dist/` releases, source, Git metadata, `.env`, dependency locks and `~/.devsolux/oandafly` are preserved.
