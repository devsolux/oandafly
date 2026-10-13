> Historical report. Commands and observations below describe the earlier delivery.
> Current commands: [production contract](PRODUCTION_BUILD.md). Current evidence:
> [implementation report](IMPLEMENTATION_REPORT.md).

# Oandafly 1.0.0 — source completeness recheck

**Date:** 2026-09-22  
**Input:** `oandafly-1.0.0.zip`  
**Input SHA-256:** `2068b144af443fb29e3fe93d284771bdb6960876d75bf289c8be1f8635bb0b68`  
**Updated source archive:** `oandafly-1.0.0-rechecked.zip`  
**Application version:** `1.0.0` (unchanged)

## Verdict

The input ZIP was not truncated. It contained all files identified by the delivered
source inventory. No application module or package resource was lost during that
packaging step. However, it was not a complete, reproducible, ready-to-run deployment:
fresh dependency locks were missing, their future inclusion was broken in the source
packager, and configuration/deployment handoff materials needed expansion.

This recheck repairs source/build handoff gaps and supplies an independently verifiable
source archive. It does **not** complete the still-open trading software or substitute
for actual graph/model/account qualification.

## Original archive findings

| Inspection | Result |
|---|---|
| ZIP entries | 159 regular files; CRC passed |
| Declared inventory | All 157 file hashes matched; the other two files were the inventory documents |
| Python source modules | 78 including the root package |
| Missing absolute internal imports | None detected |
| Broken local Markdown links | None detected |
| Baseline test rerun | 231 passed, 211 skipped |
| Baseline wheel | All 92 runtime files included |
| Baseline sdist | Omitted `.github/workflows/ci.yml` and `.gitignore` |
| Fresh dependency locks | Missing; original audit script requires `uv.lock` and `requirements.lock` |
| Future lock packaging | Original ZIP allowlist would still omit the locks after generation |

Internal-import inspection is static and covers absolute `oandafly` imports. It is
complemented by an actual installed-wheel import walk; it is not a proof that every
possible runtime condition or external resource is available.

## Changes supplied

The new archive contains **200 files**, including its own per-file manifest.
There are **41 added files**, **10 modified original files**,
and **no removed original files**. All **92 runtime application files are unchanged**.

The source-packaging changes are:

1. `PACKAGE_MANIFEST.json` with exact per-file sizes and SHA-256 values, excluding
   itself to avoid circular hashes. The external ZIP checksum covers the whole delivery.
2. `scripts/project_integrity.py` for standard-library-only file, syntax, internal-import,
   entry-point, package-resource, JSON and local-link checks, with explicit release-lock gates.
3. A corrected `scripts/package_source.py` and `MANIFEST.in`, retaining future locks,
   build inputs, checkout metadata, CI and the new support files. Source packaging refuses
   missing required source files and produces deterministic ZIP bytes for unchanged inputs.
4. `scripts/check_distribution.py` and 40 packaging regression cases, including missing
   files, manifest changes, no-secret/runtime-path selection, lock inclusion and ZIP round trips.
5. `scripts/lock_dependencies.py`, `.python-version`, `requirements-build.in` and updated
   Makefile/CI instructions. Hashed exports omit the local editable project. The helper
   stages resolution and checks exports rather than fabricating lock content.
6. SHADOW and LIVE worker templates, account/credential/trust/reviewer/grant/registry
   templates, training/cost inputs, and an optional documented systemd unit template.
   All authority/evidence/account values remain explicit placeholders requiring review.
7. Documentation distinguishing source completeness, missing dependency artifacts,
   external/operator-owned inputs and unfinished product functionality.

The application code, full-MaleCNS model family, account/risk gates, version and fresh
schema are unchanged. No removed legacy strategy or platform freezer code is restored.
No software license was invented; the existing ownership notice remains in force.

## Verification of the rechecked source

| Check | Result |
|---|---|
| Compilation | Source, tests and scripts passed |
| Full available-environment suite | **271 passed, 211 skipped, zero failures/errors** |
| Additional packaging tests | **40 passed** |
| Python module import walk from an installed wheel outside the checkout | **77 child modules imported; zero import errors** |
| Local wheel content | Every one of the **92** application runtime files present and byte-matched |
| Local source distribution | Selected checkout files present and byte-matched, including CI and metadata |
| Deterministic ZIP regression | Passed |
| Release evidence gate | Correctly remains blocked by prerequisite skips |
| Dependency resolver | Timed out; no substitute lockfiles created |

The local build used **setuptools 82.0.1**, without build isolation,
not the pinned target backend. The wheel was installed without dependencies for source
verification. These are packaging/import checks, not a successful installation of the
declared full dependency environment.

The available Python interpreter reports SQLite 3.46.1, below the application's
minimum 3.51.3. The 211 skips are unsupported-production-SQLite integration cases,
not passes. The application guard was not modified. A complete supported-runtime suite,
CI run, actual service activation, full graph benchmark and native practice/live tests
were not completed during this recheck.

## Still absent: reproducible dependency outputs

The following release files remain unresolved:

```text
uv.lock
requirements.lock
requirements-dev.lock
requirements-build.lock
```

The first two were already absent in the previous refactor; the latter two separate
development and build-tool dependencies in the corrected workflow. The new
`requirements-build.in` is an input, not a lock. Both direct package-index access and
actual resolution were attempted; network/name-resolution/timeouts prevented completion.
There is no recycled, empty, or manually invented dependency lock in the delivery.

On a network-enabled Python 3.13 build host with uv available:

```bash
python scripts/lock_dependencies.py
python scripts/lock_dependencies.py --check
python scripts/audit_dependencies.py
```

Review the generated artifacts and audit results before committing/deploying them.
Generating locks is not a security certification or a trading qualification. The locked
CI gate and `--require-locks` intentionally fail while these artifacts are missing.

## Deliberately external: not missing source files

Real MaleCNS source data, compiled full-graph arrays, production model weights,
private historical bars, credentials, actual account profiles, measured cost/evaluation
reports and signed qualification/authorization documents are not included. They must be
obtained or generated from real inputs. Private signing keys do not belong on the worker.
The archive contains templates and schemas, not fabricated authority or evidence.

Python, third-party packages, SQLite/libseccomp and an offline wheelhouse are not bundled.
This is a source distribution, not an offline installer. No connected account was enrolled,
queried or traded during this recheck.

## Functionality still open

The existing `docs/ENGINEERING_STATUS.md` remains accurate: a complete native-fill,
protection, financing and home-currency NAV economic evaluator; empirical confidence
calibration; independent qualification evidence generation; complete operational services;
full-scale performance validation; and genuine native practice/live qualification remain
open. Packaging repairs do not implement or verify those items.

## Verify the extracted delivery

Before editing, run from the extracted root:

```bash
python3.13 scripts/project_integrity.py --verify-manifest
```

It requires only Python's standard library. It should report a passing source check
and list the unresolved release dependency files separately. For stricter release
checks, after genuine lock generation/review:

```bash
python scripts/project_integrity.py --verify-manifest --require-locks
python scripts/lock_dependencies.py --check
```

After intentional edits or adding locks, compare changes against the delivery, then
regenerate the manifest before packaging. A regenerated manifest describes the edited
checkout; it does not prove equivalence to the original delivery.

## Evidence locations

`docs/evidence/recheck/` contains the original inventory check, new JUnit/test output,
runtime doctor, release-gate result, dependency-resolution attempts, unchanged-runtime
comparison, installed-wheel import results, distribution checks and this recheck's
validation/change summary. The older `docs/evidence/` files remain historical evidence
of the first refactor. `PACKAGE_MANIFEST.json` covers the current rechecked archive.

Primary tooling references consulted during this recheck:
https://docs.astral.sh/uv/concepts/projects/sync/
https://docs.astral.sh/uv/reference/cli/#uv-export
https://setuptools.pypa.io/en/latest/userguide/miscellaneous.html
https://systemd.io/CREDENTIALS/
