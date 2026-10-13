# Implementation report — 2026-09-22

Implementation in progress; this source is not a qualified native production release.
See [the production contract and remaining blockers](PRODUCTION_BUILD.md).

The supplied `oandafly-1.0.0-desktop.zip` was preserved. Its SHA-256 is
`7bd162d90144396ee1b3608e3b4a60eb28f4cba757d21ac8340b8c8816d17f1d`.
All 252 manifest entries and ZIP CRCs verified. The existing extracted checkout had
extensive user changes; it was used as the working source without resetting them.

Observed host: Apple M4, ARM64, macOS 26.6.2, host build 25G83. Xcode 27.0 build
27A266a; Apple clang 21.0.0 build clang-2100.3.34.2; SDK 27.0 build 26A425. The selected
installation requires host macOS 26.6. These identities passed the new strict probe.
They are not observations of a macOS 26.0 runtime.

## Issue ledger

| Severity / category | Location and reproduction | Root cause / change | Regression evidence |
|---|---|---|---|
| High, confirmed | `dev.sh`, `scripts/build_desktop.py`: build selected only two desktop test files | Shared production profiles, complete suite contract, exact environment exports, no build flags | Bash subprocess default/empty/override matrix, common gate/cancellation tests |
| High, confirmed | `packaging/oandafly.spec`: an exported identity enabled signing during candidate assembly | Ad-hoc assembly separated from explicit signed branch; positive child environment allowlist | Unsigned preflight never probes identity/profile; child secret removal |
| High, confirmed | macOS runtime/spec/metadata used 14.0 and lacked strict toolchain identity checks | Central 26.0 policy, runtime guards and independent Xcode/compiler/SDK/host diagnostics | Numeric boundary, identity mismatch, Rosetta and conflicting environment tests |
| High, confirmed | source audit omitted manifest verification | Require existing manifest, copy and hash snapshot, detect later source drift | Existing manifest tampering suite and source snapshot gates |
| High, confirmed | archive was published before sidecars | Stage complete release directory; verify extraction and payload before atomic rename | Corrupt/incomplete publication and prior-output preservation tests |
| High, confirmed | `scripts/check_test_evidence.py`: fixed 482-case floor | Fresh identity collection and phase outcomes with reviewed exclusions | Duplicate/stale/missing/skipped evidence rejection |
| Medium, confirmed | `tests/faults/test_context_outbox_product.py`: 23 baseline failures | Synthetic account fixture omitted synthetic instrument legal-product metadata | All 41 fault tests pass; production risk guard unchanged |
| Medium, confirmed | cleanup deleted `dist` including previous releases | Preserve and prune published release trees | Cleanup source/link/prior-output tests |
| Medium, confirmed | configured Ruff gate reported unused imports and bootstrap-order errors | Remove unused imports, preserve numerical thread guard, move bootstrap constants | Ruff gate run over source/tests/scripts |
| High, software gap | `src/oandafly/neural/isolation.py`: Linux-only security boundary | Added explicit capability interface; macOS helper remains unimplemented | Both macOS production profiles remain blocked; no containment bypass |
| High, external prerequisite | no macOS 26.0 interactive acceptance host supplied | Authenticated payload/profile-bound handoff and ingestion implemented | Wrong OS/profile/payload/stale/signature tests; native acceptance not run |
| High, qualification gap | full frozen Finder/read-only/quarantine/helper acceptance and native component review | Retained explicit blockers | Not certified by source tests or GUI smoke |

## Verification

Baseline: 536 passed, 23 failed, 1 Linux-only skip. The fault fixture correction made
all 41 fault tests pass. The expanded full suite initially returned 661 passed and
one Linux-only skip. A separate sandboxed attempt aborted in native Tk; the same
suite completed in the logged-in session outside the sandbox. Final configured `./dev.sh check`: **684 passed**, **685 collected**, with the one
Linux-only containment case explicitly excluded by the reviewed marker contract.
No unexpected skips, xfails or collection errors occurred. The source digest was
unchanged across the passing check. Both source and frozen-runtime probe logic have
regressions; no frozen application was built or certified.

No real broker, credential enrollment, account data change, signing-key operation,
notarization submission or publication to an external service was performed.

Additional checks: Ruff passed over `src`, `tests` and `scripts`; mypy 1.19.1 passed
for the three new policy/capability interfaces. Bash syntax, Python compilation and
`git diff --check` passed. The four locks passed fresh offline resolver/export
checking without changes. The pinned pip-audit queries returned no known
vulnerabilities and exact coverage of 37 applicable runtime/development packages and
42 build-tool packages. This excludes native component advisory/license review.

A real `./dev.sh build-unsigned` invocation exited **2** at production preflight,
listing the missing macOS containment helper and macOS 26.0 acceptance runner.
No successful native build, Developer ID signature, notarization, stapling or
Gatekeeper distribution assessment is claimed. Signed orchestration failure paths
were tested with labelled synthetic fakes only. The source archive, wheel and sdist
are source deliverables, not a substitute for either production build.

The embedded runtime probe exercises real NumPy/SciPy, PyArrow, ephemeral
cryptography, bundled CA loading, vault-backend import without credential access,
and SQLite WAL concurrent reads, rollback and reopen. Its source exercise cannot
satisfy the frozen flag. It does not claim full crash-recovery or containment proof.

Relevant platform references: [Apple Xcode requirements](https://developer.apple.com/support/xcode/),
[PyInstaller 6.22.3 ARM64/ad-hoc signing notes](https://pyinstaller.org/en/stable/feature-notes.html),
[Apple nested signing guidance](https://developer.apple.com/library/archive/technotes/tn2206/_index.html),
and [uv checked-lock semantics](https://docs.astral.sh/uv/concepts/projects/sync/).
