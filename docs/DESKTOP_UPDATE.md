> Historical report. Commands and observations below describe the earlier delivery.
> Current commands: [production contract](PRODUCTION_BUILD.md). Current evidence:
> [implementation report](IMPLEMENTATION_REPORT.md).

# Oandafly 1.0.0 — desktop-first source update

## Delivery and scope

Updated the user-supplied `oandafly-1.0.0-rechecked.zip`, adapting `dev(9).sh` and
`packaging(1).zip` to the actual current codebase. Project version remains **1.0.0**.
This is a source-project delivery with native desktop build recipes, not a delivery
of signed/compiled native applications or a qualified autonomous trading release.

The application opens its desktop by default without an authority, configuration,
credential, database or graph. Existing explicit CLI operations remain available.
Choosing a configuration does not start trading. Starting an account worker is an
explicit, confirmed operation; its existing independent admission and authorization
checks remain authoritative. The UI does not issue broker orders itself.

## Platform scope

| Target | Implemented source/build support | Exercised here | Local production inference |
|---|---|---|---|
| macOS ARM64 | Native Tk frontend, `.app` + console helper, icons, explicit Developer ID/notary workflow | Static/contract tests and plan only | Not implemented/qualified; Start remains disabled |
| Windows AMD64 | Native Tk frontend, windowed `.exe` + console helper, portable ZIP, shortcut and explicit Authenticode workflow | Static/contract tests and plan only; no PowerShell runtime | Not implemented/qualified; Start remains disabled |
| Linux AMD64 | Native Tk frontend, portable onedir/tar archive preserving links, menu installer | Actual source GUI under Xvfb and Linux source tests | Existing libseccomp backend only; production qualification still outstanding |

There is no remote Linux-worker connector in this delivery. Three-platform desktop
support must not be represented as three-platform autonomous execution. No native
application was compiled, signed, notarized, installed or qualified on any target.

## Implementation changes

### Desktop startup and account boundaries

- Added an actual configuration/status/diagnostics/setup workspace and GUI entry point.
- No-subcommand `oandafly`/`python -m oandafly` opens the desktop; `run --config` stays
  the explicit headless worker command. Native bundles contain a distinct console helper.
- Local I/O stays off the Tk thread. Failed presentation callbacks are redacted and
  do not stop the result pump. Status is authority-scoped and visibly current/stale.
- HALT, FLATTEN, RESUME and STOP require confirmation and are reported as queued until
  acknowledged. STOP is not flatten. Closing the window does not kill the account worker.
- Frozen workers use the sibling console executable rather than Python interpreter
  flags; early multiprocessing dispatch prevents recursive GUI/spawn startup.
- Authority storage uses SHA-256 of the exact authority ID, avoiding Windows reserved
  names, colons/alternate-stream syntax and case-fold collisions. Status reads do not
  create storage. This is a fresh layout, with no legacy migration/discovery bridge.
- Preserved all existing financial, credential, SQLite and Linux containment guards.

### Developer launchers

- Adapted the supplied Bash launcher for macOS ARM64 and Linux AMD64; added a native
  Windows PowerShell launcher backed by the shared standard-library workflow.
- Matched Python **3.13** and current extras to pyproject.toml; removed references to
  old `research`, demo, self-check and worker flags instead of restoring removed code.
- Removed the supplied personal signing fingerprint and default notary profile.
  `build` makes a local candidate; only explicit `release` may submit to Apple/sign Windows.
- Explicit first `setup --resolve`, later locked sync, no dependency sync on ordinary
  launches, no automatic Python download, cache isolation, operation locks and conservative
  checkout cleanup. Source, credentials, application storage and real locks are preserved.

### Packaging and verification

- Adapted the supplied metadata/entrypoint/iconset into one native-host PyInstaller
  onedir recipe with separate GUI/console executables. Retained the supplied iconset;
  excluded Apple archive metadata and stale hidden imports.
- Added exact binary header checks (ARM64 Mach-O, AMD64 PE GUI/console, AMD64 ELF),
  source/icon/resource parity, app identity and frozen console diagnostics.
- Kept real Python sources beside the archive for scientific source fingerprinting;
  an absent source inventory now fences instead of producing an empty-tree identity.
- Added nonce/PID-bound main-thread heartbeat verification, independent native window
  visibility, a three-second responsive hold and bounded clean shutdown. Source smoke
  is explicitly labelled and cannot qualify a frozen binary.
- Mac release checks Developer ID, notarization acceptance, stapling and Gatekeeper;
  Windows release signs both project executables with explicit certificate-store and
  timestamp settings. Linux release archives have checksums, not claimed code signing.
- Added dedicated native interactive CI recipes. Linux source/full-test CI now supplies
  a display for GUI tests. These workflows are supplied but were not executed on CI here.
- Source ZIPs and source distributions retain all launchers, specs, platform scripts,
  icons, CI, tests and documentation; source ZIP shell scripts carry executable bits.

## Verification performed

| Check | Result |
|---|---|
| Full suite under Linux/Xvfb | **349 passed, 211 skipped, zero failures/errors** |
| Added desktop tests | **78 passed** as part of the full suite |
| Desktop plus existing packaging tests | **118 passed**, zero skips/failures/errors |
| Actual source GUI startup | Visible 1080×760 Tk window, main-thread heartbeat, clean exit 0 |
| GUI startup/hold observation | Approximately 0.72 seconds startup; 3.02 seconds continued updates after acknowledgement |
| Source syntax | Python compilation and both Bash/shell syntax checks passed |
| Package imports | **82 child modules imported**, zero import errors |
| Wheel installed without dependencies in isolated target | All **97 runtime files** byte-matched; 82 child modules imported outside checkout |
| Source distribution | Expected source-file parity passed using the available backend |
| Native plans | All three target plans generated; each truthfully reports `built: false` |
| Native build attempt | Correctly blocked by four missing dependency locks; no prior release overwritten |
| Dependency resolution | Attempted; failed on package-index DNS, no placeholder locks produced |

The GUI exercise is source-only on a virtual Linux X server, not a macOS/Windows,
physical-display, compiled-runtime, scientific-model or broker execution qualification.
The native Windows and Apple visibility/signing code remains unexecuted on those OSes.

The full-suite skips are the existing production SQLite prerequisites: this container
has **SQLite 3.46.1**, below the preserved **3.51.3** minimum. No guard was bypassed.
Available dependencies differ from several declared target pins. Wheel/sdist were
built using available **setuptools 82.0.1**, not the pinned isolated build environment.
Ruff, PyInstaller, native Windows PowerShell, and native Apple signing tools were not
available. This is not a claim that lint, native build or the full release gates passed.

Machine-readable results, logs, input hashes, source screenshot and implementation
change inventory are under `docs/evidence/desktop/`. Earlier `docs/evidence` snapshots
are retained as historical records, not current test counts. The final delivery
manifest and embedded PACKAGE_MANIFEST.json describe the delivered source archive.

## Remaining prerequisites and existing gaps

Four genuine reviewed dependency files are still absent: `uv.lock`,
`requirements.lock`, `requirements-dev.lock`, `requirements-build.lock`. Generate,
review and audit them on a network-enabled native Python 3.13 host. Do not substitute
empty/stale files. Native builds remain blocked until generation and checking succeeds.

Native compiled/frozen testing on all three actual target systems remains necessary.
Build against the oldest declared supported OS/library baseline; a newer Linux glibc
build does not prove older-system compatibility. Mac/Windows production inference
containment requires separate engineering before enabling local autonomous execution.

The earlier trading gaps in ENGINEERING_STATUS.md are unchanged: full native economic
NAV/fill/financing evaluation, confidence calibration, independent qualification,
real full-graph performance, operational recovery and practice/live account qualification.
Real datasets, model weights, secrets and truthful signed approvals are not fabricated
or embedded. No broker was contacted, no account enrolled and no order placed.

## Start and verify

Use the current `docs/DESKTOP.md` for setup. On macOS/Linux, after installing a native
Python 3.13 with Tcl/Tk and uv:

```sh
python3.13 scripts/project_integrity.py --verify-manifest
./dev.sh setup --resolve
./dev.sh
./dev.sh doctor
./dev.sh build
```

On Windows AMD64, use a native CPython 3.13 with Tcl/Tk, uv and a PowerShell session
whose existing policy permits your reviewed local scripts:

```powershell
py -3.13 scripts/project_integrity.py --verify-manifest
.\dev.ps1 setup --resolve
.\dev.ps1
.\dev.ps1 doctor
.\dev.ps1 build
```

Setup needs package-index access or a complete verified local cache. Use
`setup --python ABSOLUTE_INTERPRETER_PATH` to choose the matching native interpreter.
`doctor` evaluates desktop prerequisites; `doctor-worker` evaluates stricter engine
prerequisites. Neither output constitutes trading authorization.

Resolving locks or editing code changes the source inventory. Review the changes,
then regenerate PACKAGE_MANIFEST.json with `scripts/project_integrity.py --write-manifest`
before committing a CI source snapshot. Only the explicit release command invokes
configured signing/notarization. `packaging/README.md` documents native outputs and gates.
