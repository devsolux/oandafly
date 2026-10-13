# Architecture and invariants

## Authority split

`marketdata/` validates exact bid/ask candles and quotes. `neural/` imports the
complete declared topology and runs numerical state in a separate process.
`training/` learns a readout and publishes immutable candidates. `portfolio/`
turns qualified model preferences into persistent exact targets and maintains
cash-flow-adjusted equity risk. `execution/` owns independent risk, reservations,
one-shot dispatch, native lineage and recovery. `storage/` owns durable facts.
`worker/` composes the headless application. `ui/` only observes and queues controls.

The model never receives a vault, database connection or execution API. Model
failure means no new exposure, not a switch to a hidden conventional strategy.
An authenticated assertion is still only an assertion: reviewers must perform the
science, legal/account and operational checks they sign.

## Full topology and numerical model

`neural/importer.py` reads explicitly mapped CSV or Feather columns. It includes
all curated nodes and every internal published connection, counts excluded
endpoints and contacts, and hashes the neuron, connection and reviewed sign inputs.
An importer cannot independently know whether an input was cropped; a separate
signed dataset review binds completeness and source identities. `TEST_FIXTURE`
artifacts cannot qualify a native trading model.

`MALECNS_FULL_RATE_V1` uses a sparse signed connection operator, normalized recurrent
gain below one, bounded rate activation and a leaky Euler update with `dt <= tau`.
This is a declared engineered rate model, not a claim of biological fidelity.
The encoder is an engineered seeded feature projection; the structural graph is
frozen and the readout can use every neuron. Synapse-level plasticity, receptor
kinetics and validated biological delays are not implemented in this release.

`BOUNDED_CAUSAL_WINDOW_V1` deliberately resets state and replays the same complete
feature window on each decision. It avoids undocumented neural checkpoint changes
on restart. Training and runtime share `neural/window.py`. Qualification must
benchmark the whole replay window. Instrument states remain separate.

Array manifests enforce hashes, shapes, numeric dtypes and size bounds. NPY files
are data-only: object arrays and pickle loading are rejected. Linux inference uses
a spawned process, constrained threads, memory/deadline limits and seccomp. This is
process containment, not a claim that a malicious same-OS-user administrator cannot
alter the trusted worker. Run the worker under a dedicated unprivileged account;
keep signing keys and training execution authority outside it.

## Decisions and execution

`ABSTAIN` means no new alpha instruction; `KEEP` maintains a current target;
`SET_TARGET` replaces it; `FLATTEN` requests zero exposure. Targets have identities,
expiry, model/deployment bindings and consumed-bar provenance. The same completed
bar is not consumed repeatedly as a new instruction.

Native entries are `OPEN_ONLY`. Reversals close the exact existing trade first;
the worker waits for authoritative reconciliation before a new opposite entry.
The final write guard rechecks the live authority scope, profile, risk cut,
reservation, target fingerprint, generation and deployed model binding. Timeouts
after possible submission remain `UNKNOWN`; no equivalent replacement is silently
submitted and no uncertain reservation is treated as free cash.

Recovery has a distinct grant period/capability. Loss of alpha qualification must
not by itself erase already authorized protective recovery. An expired recovery
grant, expired credentials or an ineligible profile can still prevent mutations;
operators need monitored renewal and independent broker access.

## Broker truth and storage

Observed native facts, proven transaction-history coverage and applied economic
projection are different concepts. Only covered history drives ordered position
projection. Late dependency facts can rebuild affected projection from the baseline;
conflicting identities or irreconcilable facts fence the account. Native transaction
numbers need not be contiguous; coverage comes from the broker history response,
not an assumption that every integer exists.

Initial cash and initial native open-trade/position baseline commit together. The
fresh product schema is `oandafly.full-malecns.1`, with checksummed components. The
schema initialization machinery is not an upgrade bridge from the uploaded
pre-release stores. Old stores are rejected rather than silently rewritten.

One canonical OS-account home owns application state; changing `HOME` does not fork
that identity. A native-account ownership lock and an authority lease prevent a
second worker under the same OS account from opening an alternative authority
store for the same native account. This is not distributed multi-host consensus:
deploy a single account owner and external single-instance supervision.

## Observation and orchestration

The integrated worker uses bounded REST snapshot/history reconciliation and pricing
polls, plus complete-candle capture. Streaming and incremental-update adapters are
provided but are not the primary integrated scheduling path. Do not describe this
release as an already qualified streaming market-data service.

The headless application separates safety/reconciliation, decisions, data capture
and controls. A blocked model does not intentionally disable reconciliation. Native
mutations are serialized around fresh account cuts. UI closure is independent of
worker lifetime. A local persisted request governor limits this worker, not every
application sharing an Internet egress IP.

## Finance and economic validation

Decimal-only financial boundaries reject native float money. Midpoints may be
exact half-quantum values although executable bid/ask and orders use native price
precision. Native account observations and pending reservations constrain whole-
account leverage, currency concentration, stressed stop loss, margin, spread,
staleness, daily loss and drawdown. Deposits are removed from equity-performance
changes and cannot become artificial rewards.

The delivered trainer uses explicit next-bar-open/horizon-close bid/ask utility
labels with commission, slippage and financing proxies. It is not an order-level
native-fill simulator and does not train directly from the worker's actual NAV
ledger. A complete portfolio/NAV economic evaluator, empirical confidence
calibration and independently measured qualification remain required.
