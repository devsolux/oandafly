> Historical recheck of the prior archive. For the current desktop-first update,
> use [DESKTOP_UPDATE.md](DESKTOP_UPDATE.md) and the current PACKAGE_MANIFEST.json.

# Oandafly 1.0.0 source-package completeness recheck

The recheck verifies the uploaded source archive and repairs source/build handoff gaps.
It does not complete or qualify all trading functionality.

## Original archive integrity

The original refactored ZIP contained 159 regular files and passed the ZIP CRC check.
All 157 hashes in its source inventory matched; the other two files were the inventory
JSON documents excluded from their own snapshot. Static checks found 78 Python source
modules, no missing absolute internal imports, and no broken local Markdown links.
The 78-module source count includes the root package; a package walk imports 77 child modules.

Its full test rerun reproduced 231 passed and 211 skipped. The source builds a wheel
containing every application file on the available local backend. These results show
that this was not a truncated ZIP or a delivery missing some hidden source folder.

## Handoff gaps repaired here

* The source packager did not allow future `uv.lock` or `requirements.lock` files,
  even though the dependency-audit workflow requires them. All four intended release
  lock names and the build input are now supported explicitly.
* The original sdist omitted `.github/workflows/ci.yml` and `.gitignore`. The new
  MANIFEST.in includes workflow, checkout metadata, support templates and future locks.
* Added no-write SHADOW and separately scoped LIVE worker templates, reviewer/credential
  payload templates, training/cost templates and a documented optional systemd unit.
* Added a dependency-free source checker, regenerated per-file manifest, wheel/sdist
  parity checker and packaging regression tests. Packaging fails on missing required
  source files instead of silently producing a smaller archive.
* Added a staged dependency-lock helper, explicit build-tool input, Python version
  marker, release completeness checks and updated checkout instructions.

Application version remains 1.0.0. Runtime application source and trading safeguards
are unchanged. Removed legacy strategy/UI/freezer paths are not restored. Existing
`docs/evidence/` inventory/test files describe the earlier refactor snapshot; the new
`PACKAGE_MANIFEST.json` is authoritative for this rechecked archive. New evidence is in
`docs/evidence/recheck/`.

## Verify your extracted archive before editing

```bash
python3.13 scripts/project_integrity.py --verify-manifest
```

This command needs only Python's standard library. It checks required source paths,
Python syntax, internal absolute imports, console entry points, package resources,
local documentation links, JSON validity and per-file size/SHA-256 correspondence.
It reports missing release locks separately and never states that trading is qualified.

After deliberate source changes, verify/review the changes and regenerate the manifest
with `--write-manifest` or by packaging. Regeneration trusts the local checkout; it
cannot prove an edited checkout matches an earlier release. Keep the external ZIP
SHA-256 and archived original manifest as the independent comparison.

```bash
python scripts/project_integrity.py --require-locks
python scripts/lock_dependencies.py --check
python scripts/check_quality.py
```

All four genuine locks are now included and checked. Fresh collection evidence rejects
unexpected prerequisite skips; reviewed platform exclusions remain explicit. Native
production qualification is separately blocked as described in
[PRODUCTION_BUILD.md](PRODUCTION_BUILD.md).

## Scope of completeness

**All selected source files present** is not the same claim as **fully installed,
fully implemented or live-qualified**. The archive still needs real dependencies,
supported native libraries, graph data, weights, account-specific metadata and truthful
approvals. Some planned software is not implemented. See [dependencies](DEPENDENCIES.md),
[external artifacts](ARTIFACTS.md), and [engineering status](ENGINEERING_STATUS.md).
