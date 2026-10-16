# Oandafly

**A desktop-first research workspace that runs the full male fruit-fly connectome as a
trading policy, with independently authorized, exact-decimal OANDA execution.**

[![CI](https://github.com/devsolux/oandafly/actions/workflows/ci.yml/badge.svg)](https://github.com/devsolux/oandafly/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/devsolux/oandafly)](https://github.com/devsolux/oandafly/releases)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![Status: experimental](https://img.shields.io/badge/status-experimental-orange)

Oandafly loads the complete published wiring diagram of the male *Drosophila* central
nervous system ([MaleCNS](https://male-cns.janelia.org/download/)) into a sparse neural
rate model, learns a readout that turns recent market history into a long/short target
preference, and hands that preference to a separate risk and execution layer that trades
spot FX through the OANDA v20 API.

The neural model only **proposes**. Signed approvals, operator risk limits and
reconciliation against the broker decide what actually happens.

> [!WARNING]
> **Experimental research software. Not financial advice.** Oandafly is shared as-is for
> research and educational purposes. There is no guarantee that it works, is stable,
> is profitable or will receive updates. Trading foreign exchange on margin carries a
> high risk of loss. No order has been placed against a real account, and the project
> makes no claim of biological fidelity. Use it entirely at your own risk.

## Contents

- [Highlights](#highlights)
- [Project status](#project-status)
- [How it works](#how-it-works)
- [Supported platforms and requirements](#supported-platforms-and-requirements)
- [Getting started](#getting-started)
- [Operating a worker](#operating-a-worker)
- [Command reference](#command-reference)
- [Security model](#security-model)
- [Development](#development)
- [Releases and verification](#releases-and-verification)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)

## Highlights

- **Full-topology neural policy.** Every curated MaleCNS neuron and every published
  internal connection, compiled into a hashed, data-only graph artifact and run as a
  bounded `tanh` rate model (`MALECNS_FULL_RATE_V1`).
- **Model and money are separated.** The model never sees credentials, the database or
  the order API. All financial arithmetic uses exact `Decimal`; floating point is used
  only for scientific arrays.
- **Fail-closed execution.** One immutable order per intent, durable reservations before
  dispatch, and an `UNKNOWN` state that is never silently resubmitted.
- **Broker as source of truth.** Native transaction history is projected in order into a
  double-entry SQLite ledger. Conflicting facts fence the account instead of being
  guessed away.
- **Five independent signatures.** Practice and live trading require separately signed
  Ed25519 approvals for account eligibility, dataset review, model validation,
  deployment and trading authorization.
- **Contained inference.** On Linux, inference runs in a seccomp-confined child process
  with thread, memory and deadline limits.
- **Native desktop.** A Tk workspace for macOS, Windows and Linux that never starts or
  arms a trading worker on its own.
- **Verifiable supply chain.** Hash-locked dependencies, a per-file source manifest and
  Sigstore-signed release artifacts.

## Project status

Oandafly 1.0.0 is a **source release, not a trading-qualified product**.

| Area | Status |
|---|---|
| Source, tests and locked dependencies | Complete; CI runs the locked Linux qualification gate |
| Desktop workspace | Runs on macOS ARM64, Windows AMD64 and Linux AMD64 |
| Headless trading worker | Linux AMD64 only; refused on macOS and Windows |
| Native desktop builds | Build pipeline and gates implemented; no native artifact is qualified yet (macOS containment helper and 26.0 acceptance runner are missing) |
| Broker qualification | No practice soak or live qualification has been performed |
| Dataset, weights and approvals | Not distributed; you supply and review your own |

Not included or not yet supported:

- The real MaleCNS dataset, trained production weights, credentials, signed approvals or
  a ready-to-trade configuration
- A remote Linux-worker connection for the macOS and Windows desktops
- Streaming-driven scheduling, multi-host ownership, multi-instrument training, alert
  delivery, automatic backup/restore and automatic rollback
- An order-level fill and NAV simulator for model evaluation
- Migration from pre-1.0 stores; old databases are rejected, not rewritten

The remaining work is itemized in [engineering status](docs/ENGINEERING_STATUS.md) and the
[production build contract](docs/PRODUCTION_BUILD.md).

## How it works

```text
Native prices, complete bars, account snapshots and native transaction history
    → causal input window → full MaleCNS graph → learned target preference
    → independent financial risk → durable target planner → one-shot OANDA gateway
    → native facts → ordered reconciliation → actual exposure and risk observations
```

### Decision cycle

The headless worker (`oandafly run`, composed in `worker/application.py`) owns one broker
account and repeats these steps for each authorized instrument:

1. **Observe.** `adapters/` polls OANDA v20 REST for prices, complete bid/ask candles,
   account snapshots and transaction history. Only the pinned fxPractice and fxTrade
   origins are used; URLs are not configurable. `marketdata/` validates bars and quotes
   and computes causal features.
2. **Build the input window.** `neural/window.py` takes a bounded history window with no
   look-ahead. Each decision resets the instrument's neural state and replays the same
   window, so a restart cannot change behavior. Training and live inference share this
   code.
3. **Run the connectome.** `neural/runtime.py` runs a frozen signed sparse connection
   matrix from the imported graph with bounded `tanh` activation and a leaky Euler update.
   On Linux it runs in a spawned seccomp-confined process (`neural/isolation.py`).
4. **Decode a decision.** `neural/policy.py` turns the learned readout into one of four
   actions, each with an expiry:

   | Action | Meaning |
   |---|---|
   | `ABSTAIN` | No new alpha instruction |
   | `KEEP` | Keep the current target |
   | `SET_TARGET` | Replace the target with a preference in [-1, 1] and a confidence |
   | `FLATTEN` | Request zero exposure |

5. **Plan the target.** `portfolio/planner.py` converts the preference into an exact,
   persistent unit target bound to the model, the deployment and the bar it consumed.
   The same completed bar never produces a second instruction, and a repeated target does
   not add exposure.
6. **Check risk.** `execution/risk.py` applies the operator's signed `RiskLimits` to the
   whole account: gross leverage, units per instrument, currency concentration, stressed
   stop loss, margin buffer, spread, quote and account staleness, daily loss and drawdown.
   `portfolio/equity.py` excludes deposits from performance, so cash flows cannot reset a
   loss fence. Loss fences latch.
7. **Dispatch once.** `execution/outbox.py` records a permanent intent and a reservation
   before anything is sent, then submits one immutable `OPEN_ONLY` order. A reversal
   closes the existing trade first and waits for reconciliation. A request that may have
   been transmitted stays `UNKNOWN` and is never resubmitted; only a dispatch whose final
   guard proves nothing was sent is recorded as `NOT_SENT`.
8. **Reconcile.** `execution/reconciler.py` and `storage/ledger.py` apply native
   transaction history to positions, cash and equity in order.

A blocked or failed model means **no new exposure**. There is no fallback to a
conventional strategy, and reconciliation and protective recovery keep running.

### Separation of authority

| Component | Can | Cannot |
|---|---|---|
| Neural model (`neural/`) | Read features; output a preference and confidence | Read secrets, open the database, submit orders, set risk limits |
| Planner and risk (`portfolio/`, `execution/risk.py`) | Size an exact target inside signed limits | Exceed the grant or use float money |
| Execution (`execution/`, `adapters/oanda/`) | Send one-shot entries, exact closes and cancellations | Act without a current grant, verified profile and reconciled account |
| Training (`training/`) | Produce immutable, unqualified candidates | Sign its own validation, change a deployed model in place, raise limits |
| Desktop (`ui/`) | Show status, validate configuration metadata, queue controls, start a Linux worker after confirmation | Enroll tokens, sign approvals, or stop a worker by closing the window |

### Model lifecycle

1. `graph-import` compiles the curated neurons, every internal connection and a reviewed
   excitatory/inhibitory sign table into a hashed, data-only graph artifact.
2. `train` learns the readout from bid/ask bar labels (next-bar open to horizon close,
   with commission, slippage and financing proxies) and writes an immutable
   **unqualified** candidate. `train-watch` repeats this in a separate credential-free
   process whenever the selected historical window changes.
3. Reviewers sign `MODEL_VALIDATION` and `DEPLOYMENT` documents outside the worker.
4. The worker deploys only signed candidates with a higher generation. It replaces a
   model automatically only while flat, and only if the grant sets
   `allow_automatic_promotion`.

"Full topology" means every curated imported neuron and every published internal
connection under the pinned source confidence criterion, with an explicit
excluded-endpoint inventory and independent dataset review. It does not mean every
weight is trained. See [architecture and invariants](docs/ARCHITECTURE.md) for the full
contract.

### Source layout

| Path | Contents |
|---|---|
| `src/oandafly/adapters/` | OANDA v20 REST/streaming transport, endpoints, rate governor |
| `src/oandafly/marketdata/` | Bar and quote contracts, causal features, history capture |
| `src/oandafly/neural/` | Graph import, artifacts, rate runtime, containment, policy decoding, qualification |
| `src/oandafly/training/` | Trainer, candidate registry, retraining watcher |
| `src/oandafly/portfolio/` | Target planner, flow-adjusted equity |
| `src/oandafly/execution/` | Risk checks, outbox, reconciler, protective recovery |
| `src/oandafly/storage/` | SQLite database, double-entry ledger, ownership, migrations |
| `src/oandafly/security/` | Signing, attestations, authorization, native credential vaults |
| `src/oandafly/worker/` | Headless worker composition and lifecycle |
| `src/oandafly/ui/` | Tk desktop workspace |
| `tests/` | Unit, integration, fault, packaging and desktop tests |
| `scripts/` | Integrity, quality, lock, build and release tooling |
| `examples/` | Configuration and approval templates plus JSON schemas |
| `packaging/`, `deploy/` | Native packaging spec and systemd service template |

## Supported platforms and requirements

### Platforms

| Platform | Desktop workspace and native build | Trading worker (local inference) |
|---|---|---|
| macOS 26.0+ on Apple silicon (ARM64) | Yes: `.app`, optional Developer ID signing and notarization | No: worker start is fenced |
| Windows 11 (build 22000+) AMD64 | Yes: windowed `.exe` plus `oandafly-cli.exe` | No: worker start is fenced |
| Linux AMD64, glibc 2.35+ | Yes: portable onedir `.tar.gz`, optional menu entry | Yes: libseccomp containment; account and model qualification still required |

Intel Macs, Windows on ARM, Linux ARM64 and 32-bit Python are not supported.

### Runtime requirements

- **Python 3.13** exactly (`>=3.13,<3.14`), with Tcl/Tk from the same interpreter for the
  desktop
- **SQLite 3.51.3 or newer** for the worker and database; `scripts/build_sqlite.py` can
  build a private copy on Linux
- **libseccomp** for Linux production inference
- **uv** for the `dev.sh` and `dev.ps1` launchers
- Optional extras: `native-vault` (keyring, plus pywin32 on Windows), `feather` (pyarrow),
  `desktop-build` (PyInstaller), `dev` (pytest, ruff, build)

`oandafly doctor --desktop` reports desktop prerequisites; `oandafly doctor` applies the
stricter worker gate.

### Trading modes

| Mode | Broker environment | Worker behavior | Broker mutation |
|---|---|---|---|
| PRACTICE | `SANDBOX` (OANDA fxPractice) | Full worker | Separately authorized practice account only |
| SHADOW | `SANDBOX` or `LIVE` | Native observations and qualified graph inference | None, including closes and cancellations; no execution credential or grant allowed |
| LIVE | `LIVE` (OANDA fxTrade) | Full worker | Separately authorized eligible live account only |
| PAPER / REPLAY | `SANDBOX` | Domain and simulator/test facilities | None; no standalone replay-worker CLI is included |

The configuration validator rejects mismatched pairs, such as LIVE mode against the
practice environment. Changing a URL or dropdown never transfers practice qualification to
a live account.

### Markets and data

| Item | Supported |
|---|---|
| Broker | OANDA v20 REST and streaming, fxPractice and fxTrade only |
| Instruments | Spot FX pairs between national ISO 4217 currencies, written `BASE_QUOTE` (for example `EUR_USD`); up to 30 per worker and per grant |
| Excluded instruments | Precious metals, indices, commodities, cryptocurrencies, accounting units and other non-currency instruments |
| Bar granularity | `M1`, `M5` (default), `M15`, `M30`, `H1` |
| Market data path | REST polling every 1–5 seconds plus complete-candle capture; streaming adapters exist but are not the integrated scheduling path |
| Graph input | CSV, or Feather/Arrow with the `feather` extra; column names are configurable (defaults `bodyId`, `bodyId_pre`, `bodyId_post`, `weight`, `sign`) |
| Model family | `MALECNS_FULL_RATE_V1` with the `BOUNDED_CAUSAL_WINDOW_V1` input policy |
| Training | One instrument per candidate run |

## Getting started

Opening the desktop requires no account, configuration file or credentials, and never
starts or arms a trading worker.

### 1. Verify the source

```bash
python3.13 scripts/project_integrity.py --verify-manifest
```

This standard-library check needs no installation or credentials. It verifies the
per-file `PACKAGE_MANIFEST.json` and reports dependency-lock status separately.

### 2. Open the desktop with the launcher

macOS ARM64 or Linux AMD64:

```bash
./dev.sh setup --resolve
```

```bash
./dev.sh
```

Windows AMD64 (PowerShell):

```powershell
.\dev.ps1 setup --resolve
.\dev.ps1
```

`setup` syncs `.venv` strictly from `uv.lock`; normal runs do not sync packages. Run
`./dev.sh help` for every launcher command (`check`, `doctor`, `build-unsigned`, `build`,
`verify`, `package`, `clean`, ...). See [desktop setup](docs/DESKTOP.md) for
per-platform prerequisites.

### 3. Or install into a virtual environment

```bash
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,native-vault,feather,desktop-build]'
oandafly --version
oandafly doctor
```

Installation needs network access or a reviewed wheelhouse. All four dependency locks
(`uv.lock`, `requirements.lock`, `requirements-dev.lock`, `requirements-build.lock`) are
included; see [dependency instructions](docs/DEPENDENCIES.md). `doctor` never enrolls an
account or reports trading qualification.

## Operating a worker

Trading requires real, reviewed inputs that this repository deliberately does not ship:
the MaleCNS dataset, a trained candidate, account credentials and five signed approvals.
Follow [bootstrap and data preparation](docs/BOOTSTRAP.md), start on a dedicated practice
account, and read the [operations runbook](docs/OPERATIONS.md) first.

Configuration templates and JSON schemas live in [`examples/`](examples/README.md). Copy
them **outside the checkout** into an owner-only directory and replace every placeholder
with actual observations and reviewed values. A systemd unit template is in
[`deploy/systemd/`](deploy/systemd/README.md).

On Linux, after dataset, account, model and deployment qualification:

```bash
oandafly validate-config --config /absolute/private/worker.json
oandafly run --config /absolute/private/worker.json
```

From another terminal, using the exact configured authority ID:

```bash
oandafly status --authority practice-main
oandafly halt --authority practice-main
oandafly desktop --authority practice-main
```

| Control | Effect |
|---|---|
| `HALT` | Blocks new alpha entries; reconciliation and protective recovery continue. Does not mean the account is flat |
| `FLATTEN` | Requests authorized reductions toward zero; no fill guarantee |
| `RESUME` | Rechecks authorization, scope, reconciliation, protection and risk before trading again |
| `STOP` | Stops the worker; does not close positions |

Controls are queued in a private durable mailbox. A returned request ID means *queued*, not
completed.

## Command reference

| Command | Purpose |
|---|---|
| `oandafly`, `oandafly desktop`, `oandafly-desktop` | Open the Tk workspace; never starts a worker by itself |
| `doctor [--desktop]` | Report desktop and worker prerequisites |
| `validate-config --config FILE` | Validate private worker configuration metadata |
| `run --config FILE` | Run the headless PRACTICE, SHADOW or LIVE worker (Linux only) |
| `status`, `halt`, `flatten`, `resume`, `stop --authority ID` | Read worker status or queue a control |
| `graph-import` | Compile the MaleCNS graph artifact |
| `train`, `train-watch` | Produce unqualified model candidates |
| `identities` | Print source, runtime, graph and model hashes for external review |
| `credential-enroll`, `credential-name` | Store a scoped token in the native vault; derive a systemd credential name |
| `approval-keygen`, `sign-approval` | Create an encrypted Ed25519 approval key; sign a reviewed claim |

Run `oandafly <command> --help` for the full arguments of each command.

## Security model

- **Credentials** are stored only in the platform vault (macOS Keychain, Windows Credential
  Manager, Linux Secret Service via `native-vault`) or as systemd credentials. There is no
  plaintext token file fallback. Observation and execution tokens are bound to a purpose,
  account, environment and authority.
- **Approvals** are five role-separated Ed25519 documents (`ACCOUNT_ELIGIBILITY`,
  `DATASET_REVIEW`, `MODEL_VALIDATION`, `DEPLOYMENT`, `TRADING_AUTHORIZATION`) checked
  against a trust policy. The trading grant fixes the account, environment, instruments,
  risk limits and expiry, plus a separate recovery window. Signing keys stay off the
  worker.
- **A signature is only an assertion.** Reviewers must actually perform the checks they
  sign; `sign-approval` performs no scientific or legal verification.
- **Source identity is bound to approvals.** Any change under `src/oandafly/` changes the
  source identity, so existing `MODEL_VALIDATION` approvals must be re-signed.

## Development

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e '.[dev,native-vault,feather]'
```

Before every commit:

```bash
.venv/bin/python -m pytest -q -W error
.venv/bin/ruff check src tests scripts
.venv/bin/python -m compileall -q src tests scripts
.venv/bin/python scripts/project_integrity.py --write-manifest
.venv/bin/python scripts/project_integrity.py --verify-manifest
```

Database tests need SQLite 3.51.3 or newer and are skipped otherwise. The release gate
(`scripts/check_quality.py`) fails on skips and xfails. Coding and testing conventions are
documented in [AGENTS.md](AGENTS.md).

### Continuous integration

| Workflow | Trigger | Purpose |
|---|---|---|
| [`ci.yml`](.github/workflows/ci.yml) | Manual dispatch | Source integrity checks and the locked Linux qualification gate with a private SQLite build |
| [`release.yml`](.github/workflows/release.yml) | Manual dispatch (dry run), published release | Build, verify and Sigstore-sign the source archive, sdist and wheel; attach them to the release |
| [`desktop.yml`](.github/workflows/desktop.yml) | Manual dispatch, published release | Native desktop builds on self-hosted runners (opt-in via `OANDAFLY_NATIVE_RUNNERS`) |

All actions are pinned to full commit SHAs. Releases are cut from a green `ci.yml` run on
`main`, tagged `v` plus the `pyproject.toml` version.

## Releases and verification

Each [GitHub release](https://github.com/devsolux/oandafly/releases) attaches the source
archive, sdist and wheel, a `SHA256SUMS-python` checksum file and a Sigstore bundle per
file, signed keyless by the release workflow. To verify a download with
[cosign](https://docs.sigstore.dev/cosign/system_config/installation/):

```bash
cosign verify-blob --bundle oandafly-1.0.0-source.zip.sigstore.json \
  --certificate-identity-regexp '^https://github\.com/devsolux/oandafly/\.github/workflows/release\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com oandafly-1.0.0-source.zip
```

```bash
sha256sum --check --ignore-missing SHA256SUMS-python
```

Native desktop archives are attached only once their builds qualify; see the
[production build contract](docs/PRODUCTION_BUILD.md). Changes are recorded in the
[changelog](CHANGELOG.md).

## Documentation

| Document | Contents |
|---|---|
| [Architecture and invariants](docs/ARCHITECTURE.md) | System design, authority boundaries and the full contract |
| [Desktop](docs/DESKTOP.md) | Desktop launch, per-platform setup and capability limits |
| [Bootstrap](docs/BOOTSTRAP.md) | Dataset preparation and reviewed deployment |
| [Operations](docs/OPERATIONS.md) | Control semantics, recovery and runbook |
| [Dependencies](docs/DEPENDENCIES.md) | Dependency locks and build inputs |
| [Production build](docs/PRODUCTION_BUILD.md) | Native build contract and open blockers |
| [Engineering status](docs/ENGINEERING_STATUS.md) | Implemented work and remaining qualification |
| [Implementation report](docs/IMPLEMENTATION_REPORT.md) | Current implementation evidence |
| [Artifacts](docs/ARTIFACTS.md) | Source files versus external and generated artifacts |
| [Primary sources](docs/PRIMARY_SOURCES.md) | External references for protocol, data and runtime contracts |
| [Packaging](packaging/README.md) | Native PyInstaller packaging |
| [Examples](examples/README.md) | Configuration templates and JSON schemas |
| [systemd deployment](deploy/systemd/README.md) | Linux service template |

Historical reports ([test report](docs/TEST_REPORT.md),
[recheck report](docs/RECHECK_REPORT.md), [desktop update](docs/DESKTOP_UPDATE.md),
[project completeness](docs/PROJECT_COMPLETENESS.md)) describe earlier deliveries and are
kept for reference.

## Contributing

Bug reports and suggestions are welcome through
[GitHub issues](https://github.com/devsolux/oandafly/issues). Please do not post
suspected security vulnerabilities, credentials or account data in public issues. Code
changes should follow [AGENTS.md](AGENTS.md): run the checks above, regenerate
`PACKAGE_MANIFEST.json` and record behavior changes in [CHANGELOG.md](CHANGELOG.md).

## License

Oandafly is released under the [MIT License](LICENSE). Copyright (c) 2026 Devsolux.

The license covers this project's source code. Third-party dependencies and the MaleCNS
dataset keep their own licenses; see [NOTICE.md](NOTICE.md). Oandafly is an independent
project and is not affiliated with or endorsed by OANDA or the Janelia Research Campus.
