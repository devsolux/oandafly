# Changelog

## 1.0.0 — fresh full-MaleCNS architecture

Fresh schema and contracts. No pre-release database migration or legacy strategy
fallback is supported. Preserved exact accounting, scope/epoch ownership, durable
reservations, immutable dispatch and native-truth reconciliation foundations.

Replaced the synthetic strategy and fragmented research path with full-topology
import, a sparse contained neural runtime, common causal state-window semantics,
learned policy decoding, immutable candidate production and independent deployment
qualification. Added native practice/live adapters, scoped signed grants, persistent
portfolio targets, whole-account risk, a headless supervisor and private controls.

Repaired out-of-order native projection, backward frontiers, exact half-tick
midpoints, hidden warmup, atomic initial cash/position seeding and repeat-target
execution boundaries. Removed obsolete bootstrap, IPC/configuration crypto,
legacy desktop packaging, old research/demo commands, documentation and locks.

The version does not mark completion of native qualification or full-scale economic
validation. The remaining work is itemized in `docs/ENGINEERING_STATUS.md`.

## 1.0.0 source-package completeness recheck

Rechecked all original inventory hashes and reproduced the baseline tests. Added
source integrity/manifest tooling, packaging regressions, wheel/sdist parity checks,
fresh lock-generation support, build inputs and deployment/configuration templates.
Fixed future lockfile omissions in ZIP packaging and workflow/metadata omissions in
sdist. Trading application modules and version are unchanged. Dependency resolution
and runtime/live qualification remain incomplete; no placeholder locks are shipped.

## 1.0.0 desktop-first update

- Adapt the supplied Bash 3.2 launcher and packaging to the current source architecture.
- Add native Windows PowerShell and shared developer tooling, explicit setup/lock/build/release commands.
- Open a real unconnected desktop by default; separate desktop capability from trading readiness.
- Keep independent worker ownership, explicit confirmed starts and durable queued controls.
- Add native GUI/CLI spec, source/icon parity, binary header checks and visible-window build gating.
- Remove hard-coded signing identity, missing legacy commands/extras and Apple source metadata.
- Preserve executable shell permissions and all packaging files in source distributions.
- Use hashed authority directory names for portable, case-distinct fresh-project storage.
- Keep Linux-only production inference containment explicit; no macOS/Windows bypass.

## 1.0.0 review fixes

- Record entries and recovery actions that fail before their final guard as `NOT_SENT`
  instead of `UNKNOWN`, so a transient credential or deadline failure no longer halts
  trading or parks a protective close forever. Transport results without the guard stay ambiguous.
- Retry proven-unsent protective closes and cancellations under new deterministic
  identities with capped backoff; one failing order or trade no longer starves
  protection of the others.
- Fence entries when the current trading grant's ID or instruments differ from the
  deployment's qualified scope; promotion requires a current grant; compare loaded model
  and graph fingerprints with the qualification.
- Consume an operator RESUME on the first healthy cycle; fall back to renewed approvals
  at the configured paths when the persisted deployment no longer qualifies.
- Bound the drawdown fence by the flow-adjusted endowment; stamp `NO_BASELINE` fence
  resolution with the resolution time; candle chunk writes survive a crashed writer's
  temp file and a concurrent identical publish.
- Configure private runtime storage before importing the worker; reject `..` in the
  systemd credentials directory; add CLI help for control and training commands.
- Close SQLite connections in the runtime probe and tests, set the pytest-asyncio loop
  scope, and skip the embedded-runtime probe test when optional extras are absent.

## 1.0.0 remaining review findings

- `oandafly run` and `run_worker` refuse macOS/Windows as documented, so the egress
  quota never runs without a Linux boot identity (a reboot latched its clock fence).
- Native adapters map `ValidationFailure` from malformed decimals and naive timestamps
  to the sanitized `Unavailable` boundary.
- Price a same-pair pending bundle at its most adverse member bound and require the
  held reservation to cover, not equal, the recomputed need; a sibling prepared at
  another bound no longer strands a prepared entry.
- Re-verify the point-in-time source retirement proof live only until a target's first
  arm; later re-arms require the unaltered durable proof, so a TRADING→TRADING handover
  no longer locks out disarm→arm once the target trades.
- Worker lifecycle tests for RESUME consumption, per-item protection isolation, expired
  promotion grants, the deployment-path fallback and the qualified graph check.

## 1.0.0 CI and release signing

- Update workflow actions to their latest releases (checkout v7.0.1, setup-python v7.0.0,
  upload-artifact v7.0.2, download-artifact v8.0.2, cosign-installer v4.1.2 with cosign
  v3.1.3), pinned to full commit SHAs; checkouts no longer persist credentials, and runs
  use concurrency groups. Both workflows keep `workflow_dispatch`.
- `desktop.yml` also runs when a GitHub release is published; build jobs previously
  skipped every non-`main` ref, including release tags.
- Sign qualified desktop archives, checksums and build receipts with Sigstore cosign
  keyless signing after verifying per-build checksums and the release version; attach
  archives, bundles and a combined `SHA256SUMS` to published releases.
- Add failure diagnostics for the Developer ID job and an actionlint runner-label config.
- Forward a validated `LD_LIBRARY_PATH` to Linux release tests and builds; the CI-built
  private SQLite was stripped from the child environment, skipping 225 tests and failing
  the runtime probe. The unsigned-preflight test no longer depends on host SQLite, and
  the authority process tests keep `LD_LIBRARY_PATH` in their minimal child environment.
- Upload only the quality evidence files from CI; the first fully green `ci.yml` run
  passed both jobs with 709 tests in the locked gate.
- Add `AGENTS.md` and `CLAUDE.md`: `main`-only branch policy, environment setup,
  pre-commit checks, code/testing conventions and CI usage for coding agents.
