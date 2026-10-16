# Oandafly

**Desktop-first full-MaleCNS policy workspace with independently authorized OANDA execution.**

Oandafly is a forex trading research application. It loads the complete published
wiring diagram of the male fruit-fly central nervous system
([MaleCNS](https://male-cns.janelia.org/download/)) into a sparse neural rate
model, learns a readout that turns recent market history into a long/short target
preference, and passes that preference to an independent exact-decimal risk and
execution layer that trades through the OANDA v20 API. The neural model only
proposes. Signed approvals, operator risk limits and reconciliation against the
broker decide what actually happens.

## Experimental Status

This project is **strictly experimental**. It is shared "as-is" for educational or demonstration purposes. 

**There is absolutely no guarantee or promise that this software works, is stable, or will receive future updates.** Use it at your own risk.

## Quick start

Open the native desktop with `./dev.sh` on macOS ARM64/Linux AMD64 or `.\dev.ps1`
on Windows AMD64. No account, config file or credentials are required to open the
workspace; opening it never starts or arms a trading worker.

```sh
./dev.sh setup --resolve   # first explicit setup; native Python 3.13 + Tk and uv required
./dev.sh                   # open desktop; normal runs do not sync packages
```

Windows: `.\dev.ps1 setup --resolve`, then `.\dev.ps1`. Native packaging includes
macOS `.app`, Windows GUI/CLI `.exe` pairs and Linux portable onedir builds. Start with
[desktop setup and capability limits](docs/DESKTOP.md), [production build contract](docs/PRODUCTION_BUILD.md), and [packaging](packaging/README.md).

The macOS minimum is **26.0**. `./dev.sh build-unsigned` and `./dev.sh build` use
the same production quality gates; `build` additionally signs, notarizes and staples
using the launcher environment defaults. Neither takes signing flags. Both are
currently blocked by the missing macOS containment helper and 26.0 acceptance runner.

> **Source delivery, not a fully trading-qualified release.** Desktop support on
> macOS/Windows is not production-inference containment support. Local autonomous
> inference remains Linux/libseccomp-only; macOS/Windows worker start is fenced.
> The actual full MaleCNS dataset, production weights, supported full-suite results
> and native broker qualification are not invented; the four resolved dependency
> locks are included.
> See [engineering status](docs/ENGINEERING_STATUS.md). Previous recheck reports and
> evidence are historical; [the desktop update](docs/DESKTOP_UPDATE.md) records this delivery.

## How it works

```text
Native prices, complete bars, account snapshots and native transaction history
    → causal input window → full MaleCNS graph → learned target preference
    → independent financial risk → durable target planner → one-shot OANDA gateway
    → native facts → ordered reconciliation → actual exposure and risk observations
```

### One decision cycle

The headless worker (`oandafly run`, composed in `worker/application.py`) owns one
broker account and repeats these steps for each authorized instrument:

1. **Observe.** `adapters/` polls OANDA v20 REST for prices, complete bid/ask
   candles, account snapshots and transaction history. Only the pinned fxPractice
   and fxTrade origins are used; URLs are not configurable. `marketdata/` validates
   bars and quotes and computes causal features.
2. **Build the input window.** `neural/window.py` takes a bounded history window
   with no look-ahead. Each decision resets the instrument's neural state and
   replays the same window, so a restart cannot change behavior. Training and live
   inference share this code.
3. **Run the connectome.** `neural/runtime.py` runs the `MALECNS_FULL_RATE_V1`
   model: a frozen signed sparse connection matrix from the imported graph, bounded
   `tanh` rate activation and a leaky Euler update. On Linux it runs in a spawned
   seccomp-confined process (`neural/isolation.py`) with thread, memory and
   deadline limits. It never receives credentials, the database or the order API.
4. **Decode a decision.** `neural/policy.py` turns the learned readout into one of
   four actions, each with an expiry:

   | Action | Meaning |
   |---|---|
   | `ABSTAIN` | No new alpha instruction |
   | `KEEP` | Keep the current target |
   | `SET_TARGET` | Replace the target; carries a preference in [-1, 1] and a confidence |
   | `FLATTEN` | Request zero exposure |

5. **Plan the target.** `portfolio/planner.py` converts the preference into an
   exact, persistent unit target bound to the model, the deployment and the bar it
   consumed. The same completed bar never produces a second instruction, and a
   repeated target does not add exposure.
6. **Check risk.** `execution/risk.py` applies the operator's signed `RiskLimits`
   to the whole account: gross leverage, units per instrument, currency
   concentration, stressed stop loss, margin buffer, spread, quote and account
   staleness, daily loss and drawdown. `portfolio/equity.py` excludes deposits from
   performance, so cash flows cannot reset a loss fence. Loss fences latch.
7. **Dispatch once.** `execution/outbox.py` records a permanent intent and a
   reservation before anything is sent, then the transport submits one immutable
   `OPEN_ONLY` order. A reversal closes the existing trade first and waits for
   reconciliation. If a request times out after it may have been submitted, the
   result stays `UNKNOWN` and is never silently resubmitted.
8. **Reconcile.** `execution/reconciler.py` and `storage/ledger.py` apply native
   transaction history to positions, cash and equity in order, with double-entry
   bookkeeping in SQLite. The broker is the source of truth; conflicting facts fence
   the account rather than being guessed away.

A blocked or failed model means **no new exposure**. There is no fallback to a
conventional strategy, and reconciliation and protective recovery keep running.

### Separation of authority

| Component | Can | Cannot |
|---|---|---|
| Neural model (`neural/`) | Read features; output a preference and confidence | Read secrets, open the database, submit orders, set risk limits |
| Planner and risk (`portfolio/`, `execution/risk.py`) | Size an exact target inside signed limits | Exceed the grant or use float money |
| Execution (`execution/`, `adapters/oanda/`) | Send one-shot entries, exact closes and cancellations | Act without a current grant, verified profile and reconciled account |
| Training (`training/`) | Produce immutable, unqualified candidates | Sign its own validation, change a deployed model in place, raise limits |
| Desktop (`ui/`) | Show status, validate config metadata, queue controls, start a Linux worker after confirmation | Enroll tokens, sign approvals, or stop a worker by closing the window |

Practice and live trading need five independently signed Ed25519 documents
(`ACCOUNT_ELIGIBILITY`, `DATASET_REVIEW`, `MODEL_VALIDATION`, `DEPLOYMENT` and
`TRADING_AUTHORIZATION`), checked against a trust policy. The trading grant fixes
the account, environment, instruments, risk limits and expiry, plus a separate
recovery window. A signature is only an assertion: reviewers must actually perform
the checks they sign.

Scientific arrays use bounded floating point; financial contracts use exact decimal
arithmetic. Full topology means every curated imported neuron and every published
internal connection under the pinned source confidence criterion, with an explicit
excluded-endpoint inventory and independent dataset review. It does not mean every
weight is trained.

### Model lifecycle

1. `graph-import` compiles the curated neurons, every internal connection and a
   reviewed excitatory/inhibitory sign table into a hashed, data-only graph artifact.
2. `train` learns the readout from bid/ask bar labels (next-bar open to horizon
   close, with commission, slippage and financing proxies) and writes an immutable
   **unqualified** candidate. `train-watch` repeats this in a separate credential-free
   process whenever the selected historical window changes.
3. Reviewers sign `MODEL_VALIDATION` and `DEPLOYMENT` documents outside the worker.
4. The worker deploys only signed candidates with a higher generation. It replaces a
   model automatically only while flat, and only if the grant sets
   `allow_automatic_promotion`.

Training never signs its own validation, mutates the deployed model or increases the
account's risk envelope. See [architecture and invariants](docs/ARCHITECTURE.md) for
the full contract.

## What is supported

### Platforms

| Platform | Desktop workspace and native build | Trading worker (local inference) |
|---|---|---|
| macOS 26.0+ on Apple silicon (ARM64) | Yes: `.app`, optional Developer ID signing and notarization | No: worker start is fenced |
| Windows 11 (build 22000+) AMD64 | Yes: windowed `.exe` plus `oandafly-cli.exe` | No: worker start is fenced |
| Linux AMD64, glibc 2.35+ | Yes: portable onedir `.tar.gz`, optional menu entry | Yes: libseccomp containment; account and model qualification still required |

Intel Macs, Windows on ARM, Linux ARM64 and 32-bit Python are not supported. The
macOS and Windows desktops have no remote connection to a Linux worker.

### Runtime requirements

- **Python 3.13** exactly (`>=3.13,<3.14`), with Tcl/Tk from the same interpreter for the desktop
- **SQLite 3.51.3 or newer** for the worker and database. `scripts/build_sqlite.py` can build a private copy on Linux
- **libseccomp** for Linux production inference
- **uv** for the `dev.sh` and `dev.ps1` launchers
- Optional extras: `native-vault` (keyring, plus pywin32 on Windows), `feather` (pyarrow), `desktop-build` (PyInstaller), `dev` (pytest, ruff, build)

`oandafly doctor --desktop` reports desktop prerequisites; `oandafly doctor` uses the
stricter worker gate.

### Trading modes

| Mode | Broker environment | Worker behavior | Broker mutation |
|---|---|---|---|
| PRACTICE | `SANDBOX` (OANDA fxPractice) | Full worker | Separately authorized practice account only |
| SHADOW | `SANDBOX` or `LIVE` | Native observations and qualified graph inference | None, including closes and cancellations; no execution credential or grant allowed |
| LIVE | `LIVE` (OANDA fxTrade) | Full worker | Separately authorized eligible live account only |
| PAPER / REPLAY | `SANDBOX` | Domain and simulator/test facilities | None; no standalone replay-worker CLI is included |

The configuration validator rejects mismatched pairs, such as LIVE mode against the
practice environment. Changing a URL or dropdown never transfers practice
qualification to a live account.

### Markets and data

| Item | Supported |
|---|---|
| Broker | OANDA v20 REST and streaming, fxPractice and fxTrade only |
| Instruments | Spot FX pairs between national ISO 4217 currencies, written `BASE_QUOTE` (for example `EUR_USD`); up to 30 per worker and per grant |
| Excluded instruments | Precious metals, indices, commodities, cryptocurrencies, accounting units and other non-currency instruments |
| Bar granularity | `M1`, `M5` (default), `M15`, `M30`, `H1` |
| Market data path | REST polling every 1–5 seconds plus complete-candle capture. Streaming and incremental adapters exist but are not the integrated scheduling path |
| Graph input | CSV, or Feather/Arrow with the `feather` extra. Column names are configurable (defaults `bodyId`, `bodyId_pre`, `bodyId_post`, `weight`, `sign`) |
| Model family | `MALECNS_FULL_RATE_V1` with the `BOUNDED_CAUSAL_WINDOW_V1` input policy |
| Training | One instrument per candidate run |

### Credential storage

| Platform | Token storage |
|---|---|
| macOS | Keychain |
| Windows | Credential Manager |
| Linux | Secret Service (`native-vault` extra) or systemd credentials |

Observation and execution tokens are bound to a purpose, account, environment and
authority. There is no plaintext token file fallback.

### Not included or not supported yet

- The real MaleCNS dataset, trained production weights, credentials, signed approvals or a ready-to-trade configuration
- Native practice soak or live qualification; no order has been placed against a real account
- A remote Linux-worker connection for the macOS and Windows desktops
- Streaming-driven scheduling, multi-host ownership, multi-instrument training, alert delivery, automatic backup/restore and automatic rollback
- An order-level fill and NAV simulator for model evaluation
- Migration from pre-1.0 stores; old databases are rejected, not rewritten
- Any claim of biological fidelity or profitability

The remaining work is itemized in [engineering status](docs/ENGINEERING_STATUS.md).

## Verify the source archive first

```bash
python3.13 scripts/project_integrity.py --verify-manifest
```

This standard-library check needs no installation or credentials. It verifies the
embedded per-file manifest and reports unresolved dependency locks separately.

GitHub releases attach the source archive, sdist and wheel with `SHA256SUMS-python` and a
Sigstore bundle per file, signed keyless by the release workflow:

```bash
cosign verify-blob --bundle oandafly-1.0.0-source.zip.sigstore.json \
  --certificate-identity-regexp '^https://github\.com/devsolux/oandafly/\.github/workflows/release\.yml@refs/tags/v' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com oandafly-1.0.0-source.zip
```

Native desktop archives are attached only once their builds qualify; see the
[production build contract](docs/PRODUCTION_BUILD.md).

## Start with non-trading checks

```bash
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,native-vault,feather,desktop-build]'
oandafly --version
oandafly doctor
python scripts/check_quality.py
```

Installation needs network access or a reviewed wheelhouse. All four genuine locks
are included; the build/audit/type-check tools are pinned separately in
`requirements-build.lock`. Production builds verify lock freshness without rewriting
it. See [dependency instructions](docs/DEPENDENCIES.md). `doctor` never enrolls an
account or reports trading qualification. [Current implementation status](docs/IMPLEMENTATION_REPORT.md)
is separate from historical reports and from native release acceptance.

After genuine dataset, account, model and deployment qualification, load private
configuration and operate through the headless CLI:

```bash
oandafly validate-config --config /absolute/private/worker.json
oandafly run --config /absolute/private/worker.json
# In another terminal, using the exact configured authority ID:
oandafly status --authority practice-main
oandafly halt --authority practice-main
oandafly desktop --authority practice-main
```

`HALT` blocks new alpha entries but keeps the worker and protective recovery active.
It does not mean the account is flat. `FLATTEN`, `STOP` and `RESUME` have different
semantics; read the [runbook](docs/OPERATIONS.md) before using them.

## Command reference

| Command | Purpose |
|---|---|
| `oandafly`, `oandafly desktop` | Open the Tk workspace; never starts a worker by itself |
| `doctor [--desktop]` | Report desktop and worker prerequisites |
| `validate-config --config FILE` | Validate private worker configuration metadata |
| `run --config FILE` | Run the headless PRACTICE, SHADOW or LIVE worker (Linux only; refused on macOS/Windows) |
| `status`, `halt`, `flatten`, `resume`, `stop --authority ID` | Read worker status or queue a control (queued is not acknowledged) |
| `graph-import` | Compile the MaleCNS graph artifact |
| `train`, `train-watch` | Produce unqualified model candidates |
| `identities` | Print source, runtime, graph and model hashes for external review |
| `credential-enroll`, `credential-name` | Store a scoped token in the native vault; derive a systemd credential name |
| `approval-keygen`, `sign-approval` | Create an encrypted Ed25519 approval key; sign a reviewed claim |

## Documentation

- [Source completeness and external inputs](docs/PROJECT_COMPLETENESS.md)
- [Dependency locks and builds](docs/DEPENDENCIES.md)
- [Bootstrap and data preparation](docs/BOOTSTRAP.md)
- [Architecture and invariants](docs/ARCHITECTURE.md)
- [Operational controls and recovery](docs/OPERATIONS.md)
- [Implementation status / remaining qualification](docs/ENGINEERING_STATUS.md)
- [Test evidence and dependencies](docs/TEST_REPORT.md)

Schemas for worker configuration, model artifacts and every approval payload are
under `examples/schemas/`. They describe structure, not an approval or evidence.
The source contains no ready-to-trade configuration or credential.

## License

Oandafly is released under the [MIT License](LICENSE). Copyright (c) 2026 Devsolux.

The license covers this project's source code. Third-party dependencies and the
MaleCNS dataset keep their own licenses; see [NOTICE.md](NOTICE.md).
