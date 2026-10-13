# Native packaging — Oandafly 1.0.0

One native-host PyInstaller spec supplies macOS ARM64 `.app`, Windows AMD64 GUI/CLI
executables and Linux AMD64 onedir outputs. Product identity remains
`com.devsolux.oandafly`; no ordinary launch starts trading.

```sh
./dev.sh setup
./dev.sh build-unsigned
./dev.sh build
./dev.sh verify /absolute/path/to/profile-release-directory
```

Use `dev.ps1` on Windows. `build-unsigned` and `build` have the same mandatory common
production gates. `build` adds the target's signing/distribution stages. On macOS the
exact launcher defaults or nonempty environment overrides select the Developer ID
identity and notary profile; there are no signing CLI flags. `release` is an exact
alias of `build`. Unsigned assembly uses only necessary local ARM64 ad-hoc sealing,
never Developer ID selection, Keychain identity probes or notarization.

The macOS minimum is **26.0**, with deployment target **26.0**, reference Xcode **27.0**,
Apple clang **21.0.0 (clang-2100.3.34.2)** and SDK **27.0 (26A425)**. The active policy is
[src/oandafly/resources/macos-profile.json](../src/oandafly/resources/macos-profile.json).
Build-host and baseline-runtime evidence are distinct.

Both profiles require genuine checked locks, the reviewed source manifest, clean
build/test environments, complete applicable checks, recursive native/resource
verification and final profile-payload acceptance. Outputs and receipts are staged
outside `dist`; one complete directory rename publishes each profile separately.
Unsigned trust fields explicitly say `not_applicable_unsigned_profile`.

**No macOS production output is currently qualified.** The containment helper and
native denial tests are missing, and no macOS 26.0 interactive runner is configured.
Both production commands report these blockers nonzero. Signing success cannot
replace runtime security or baseline acceptance. Full details, baseline handoff
schema, CI requirements and open software gaps are in
[PRODUCTION_BUILD.md](../docs/PRODUCTION_BUILD.md).

Source packaging remains separate:

```sh
python scripts/project_integrity.py --write-manifest  # only after reviewing intentional edits
python scripts/package_source.py --require-locks --output dist/oandafly-1.0.0-source.zip
python -m build --no-isolation
python scripts/check_distribution.py dist/oandafly-1.0.0.tar.gz dist/oandafly-1.0.0-py3-none-any.whl
```

Use the pinned backend/build environment for the final two commands. Source archive,
wheel and sdist completeness do not establish native or trading qualification.
