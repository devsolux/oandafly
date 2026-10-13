> Current production-contract work: [implementation report](IMPLEMENTATION_REPORT.md)
> and [production build contract](PRODUCTION_BUILD.md). Four genuine dependency
> locks are now included. macOS 26.0 containment, baseline acceptance and full
> frozen/native qualification remain software/infrastructure blockers. The earlier
> qualification inventory below is retained and is not a current passing report.

> Desktop update: see [DESKTOP.md](DESKTOP.md) and [DESKTOP_UPDATE.md](DESKTOP_UPDATE.md)
> for current launcher/packaging scope and evidence. The earlier economic/model/
> native-broker qualification gaps below remain open.

# Engineering status — Oandafly 1.0.0

This is an implemented source refactor, **not a completed native trading release**.
It is not justified to call every item in the earlier R00–R12 plan closed.

## Implemented and locally exercised where prerequisites allow

Fresh version/schema/configuration, explicit modes, no-write SHADOW, role-separated
Ed25519 grants/review/deployment, immutable data-only full-topology graph/model
artifacts, complete sparse rate kernel, instrument-contained inference, deterministic
bounded history shared by training/runtime, learned utility/target decoder, immutable
candidate training/watch/approved registry, exact target planner, native read/write
adapters, one-shot dispatch, whole-account risk, flow-adjusted equity/loss fences,
ordered native transaction projection and atomic initial cash/position seeding.

The source includes a headless worker composing observation, capture, decisions,
risk, entry/recovery, model replacement and controls; a desktop-first Tk workspace is separate. Desktop startup/build support was added for
macOS ARM64, Windows AMD64 and Linux AMD64; production inference containment remains
Linux-only. No remote-worker backend for macOS/Windows is provided.
The complete worker/native system has not been exercised against a real account.

## Important software still needed for full plan completion

1. **Native-economic evaluation and learning integration (R07).** The implemented
   trainer uses explicit bid/ask bar labels and cost proxies. A complete order/fill/
   protective-order/financing/home-currency NAV simulator, empirical confidence
   calibration, account-NAV-driven training experiments and independent full-model
   evaluator are not delivered. Signing a ModelValidation document cannot replace
   them. Automatic candidate production is implemented; automatic independent
   evidence generation and approval are not.
2. **Complete recovery/soak and supported-runtime integration (R10–R12).** All
   production SQLite integration tests must run on the real supported runtime,
   including the retained fault scenarios adjusted to covered-history semantics.
   Test restart at every dispatch/recovery state, exact native reject/cancel/close
   behavior, terminal recovery handling, stale cuts, dropped responses, interrupted
   protection and hard-loss control. Fix any failures before qualification. The
   conservative UNKNOWN fence is a feature, not full proof of recovery liveness.
3. **Production operational completeness.** A native streaming-driven composition,
   global cross-host ownership/rate coordination, unattended independently verified
   backup/restore, retention/archival management, alert delivery and multi-instrument
   training are not implemented as production services. The current worker uses
   bounded REST polling and one canonical local account owner. No automatic rollback
   evaluator is implemented; only separately approved candidate selection/replacement
   while flat. A previous model can be redeployed only with a new reviewed generation.
4. **Full-scale data/performance evidence (R05–R06).** Import and numerical tests
   use a labelled tiny graph. Download/reconcile the real pinned graph, establish
   reviewed signs and resource sizing, benchmark whole-window inference under load,
   stress numerical stability and perform full-scale fault/containment testing.
   This release does not ship full connectivity data, production weights or
   scientific evidence. No claim of biological reproduction or profitability exists.
5. **Fresh reproducible release environment (R00).** Resolve/hash/audit the new
   dependencies and build on Linux/Python 3.13/SQLite >=3.51.3/libseccomp. Old lockfiles
   were removed because their package graph described the removed application.
   New target versions were verified as published where updated, but their combined
   environment was not installed/resolved here. The newly supplied CI and SQLite
   helper have not run against the network in this environment.

## External qualification and operator-owned inputs

Real account eligibility and legal-product evidence, correct native instrument
metadata, scoped secret enrollment, public-key trust policy, truthful signed dataset
and model reviews, a bounded operator trading grant, and environment/account-specific
deployment evidence remain required. LIVE additionally needs genuine practice soak
and account-specific live evidence. No credentials were enrolled and no order was
placed during this refactor.

The native worker's intended autonomy is **execution within preauthorized limits**,
not arbitrary self-approval or self-modification. No software can honestly establish
an untested trading edge by a successful graph import or by minting signatures.

## Previous plan mapping

| Plan unit | Status in this source delivery |
|---|---|
| R00 | Fresh dependencies/diagnostics/test harness; locked supported build still open |
| R01 | Mode, profile, grant, qualification and authority contracts implemented |
| R02 | Projection, frontier, half-tick, warmup and baseline defects repaired; focused regression evidence |
| R03 | Native REST/stream/update adapters and integrated REST observation; real account qualification open |
| R04 | Native immutable one-shot entry and exact close/cancel transport; native contract/recovery soak open |
| R05 | Full import/artifact contracts implemented; real graph review/import not performed |
| R06 | Full sparse rate model and Linux containment implemented; tiny-fixture evidence only |
| R07 | Candidate trainer/watch/approved queue implemented; full NAV evaluator/calibration/independent automation open |
| R08 | Persistent targets and independent risk implemented; full native integration open |
| R09 | Headless supervisor, control and bounded resume/promotion implemented; complete operational soak open |
| R10 | Expanded unit/mock/SQL-algorithm tests; production SQLite integration unavailable locally |
| R11 | Genuine native practice soak not performed |
| R12 | Native live qualification not performed; no live authorization supplied |

## Cleanup

Removed the old synthetic-momentum strategy, small projected research strategy,
unavailable-original-kernel bootstrap, legacy IPC/configuration-key code, old demo
and research entry points, obsolete desktop/freezer packaging, stale documentation
and mismatched lockfiles. Shared accounting/security/native observation components,
small labelled simulator fixtures, brand icons and integrity-checking fresh-schema
machinery remain intentionally. These are not runtime legacy fallbacks or a
pre-release database compatibility layer.
