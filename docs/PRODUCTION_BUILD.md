# Production build contract — implementation status

Oandafly remains **1.0.0 / com.devsolux.oandafly**. This delivery implements the
production build contract and its fail-closed gates. **No production native
artifact is qualified by this source delivery.**

## Commands

- `./dev.sh`: open the desktop without dependency sync or worker startup.
- `./dev.sh setup --resolve`: explicitly resolve/export genuine locks, then sync.
- `./dev.sh setup`: synchronize existing checked locks only.
- `./dev.sh doctor`: report runtime, unsigned-build and signed-build eligibility separately.
- `./dev.sh test [arguments]`: developer tests; a selection cannot qualify a production build.
- `./dev.sh check`: source integrity, shell syntax, Ruff and the complete applicable test contract.
- `./dev.sh build-unsigned`: shared production pipeline, with no Developer ID or notary credential probes.
- `./dev.sh build`: the same pipeline plus Developer ID signing, notarization, stapling and Gatekeeper assessment.
- `./dev.sh release`: exact alias of `build`.
- `./dev.sh verify PATH`: verify a production release directory/archive against its profile and receipt.
- `./dev.sh package --require-locks`: complete source archive; no native-release claim.
- `./dev.sh clean [--all]`: conservative build/cache cleanup; preserve `dist`, source, locks and application state.

Both build commands take no arguments, except side-effect-free `--help`/`-h`.
There are no signing flags, test-selection flags or gate-bypass flags. `dev.ps1`
provides the corresponding Windows commands without Apple environment defaults.

The exact Bash exports remain the only signing-selection defaults:

```bash
export MACOS_SIGN_IDENTITY="${MACOS_SIGN_IDENTITY:-351A9115F928304981211FEB5866E94B6C760AD1}"

export MACOS_NOTARY_PROFILE="${MACOS_NOTARY_PROFILE:-macos-notary}"
```

Unset/empty inputs receive the default; nonempty caller overrides, including spaces,
are preserved. The signed backend has no second fallback. Unsigned children never
receive these settings. PyInstaller assembly uses local ad-hoc sealing on ARM64;
Developer ID signing is a separate explicit inside-out stage. Unsigned Apple trust
stages are `not_applicable_unsigned_profile`, never `passed`.

## One runtime/toolchain policy

[src/oandafly/resources/macos-profile.json](../src/oandafly/resources/macos-profile.json)
is the active machine-readable policy. Runtime minimum and deployment target are
**26.0**. Reference Xcode is **27.0**, Apple clang **21.0.0**
(**clang-2100.3.34.2**), SDK **27.0 (26A425)**. Compiler, SDK, Xcode and host build
identities are observed separately. Xcode 27.0 requires a build host of at least
macOS 26.6 according to [Apple's Xcode support matrix](https://developer.apple.com/support/xcode/);
the installation's own minimum is also checked. This does not raise the app minimum.

The launcher and Finder/CLI entrypoints reject unsupported macOS runtimes. Installed
application startup does not inspect Xcode or require signing credentials. Production
preflight validates selected tools, Rosetta status, absolute `DEVELOPER_DIR`, compiler
and SDK provenance, and conflicting SDK/toolchain/deployment overrides without
changing the global developer selection.

## Input and artifact integrity

Production checks verify the existing source manifest, copy a content-addressed
snapshot, check lock/export freshness, and install hash-locked runtime/dev/build
packages into a clean environment. They never regenerate the source manifest to
accept an unexpected change. After intentional source edits, review the changes and
explicitly run `python scripts/project_integrity.py --write-manifest` before building.

The shared pipeline includes Ruff, scoped mypy checks for the new policy interfaces,
complete portable accounting/risk/recovery tests, real Tk tests, and strict advisory
queries over development/runtime and build-tool closures. `packaging/release-tests.json`
and the pytest plugin bind exact fresh collection and phase outcomes to the run,
source, lock, test contract, interpreter and host identities. Only reviewed
platform-inapplicable markers are excluded. Missing prerequisites, skips, xfails,
collection errors and unexpected deselection fail qualification.

Recursive Mach-O auditing checks ARM64 slices, deployment load commands, nested app
metadata, permissions, dependency/rpath closure and escaping links. SDK stamps are
separate from minimum OS fields. A load-command audit cannot prove API availability;
actual baseline runtime acceptance remains mandatory.

The final profile payload is tested, archived, extracted and tested again. The console
probe requires a frozen runtime and exercises numerical/native packages, temporary
SQLite WAL behavior, cryptography, CA resources and vault-backend import without
reading credentials. These probes do not replace full helper/Finder acceptance. Payload
identity includes file hashes, modes and symlink destinations independently of archive
bytes. Sidecars, checksums, provenance and evidence are staged before one directory
rename publishes `dist/<target>/production-{unsigned,signed}/<run-id>/`. Failed staging
stays in `build/desktop/`; prior releases are retained. Timestamped signing/notary
outputs are not claimed to be byte-for-byte reproducible.

## Baseline runner handoff

An operator-reviewed absolute `OANDAFLY_BASELINE_CONFIG` file contains:

```json
{"executable":"/absolute/path/to/approved-baseline-transport","ed25519_public_key":"<64 hexadecimal characters>"}
```

The executable receives two arguments: the request JSON path and the response JSON
path. It must transfer the exact archive to an existing, trusted, interactive macOS
26.0 ARM64 runner, execute the requested checks and return an Ed25519-signed response.
The transport and runner are external infrastructure; this delivery does not create
machines, change boot settings, install macOS or provide a ready-made remote runner.

The request binds a fresh nonce, timestamp, profile, source/locks/contract, exact
application payload and archive hashes. The signed `result` binds `request_sha256`,
`completed_at`, `runtime_os`, `host_build`, `architecture`, `hardware`, `virtualized`,
`interactive_session`, `payload_sha256` and named check outcomes. Canonical signature
bytes are UTF-8 `json.dumps(result, sort_keys=True, separators=(",", ":"))`. The envelope
has `result` and hex `signature`. Checks and validation are defined in
[scripts/baseline_acceptance.py](../scripts/baseline_acceptance.py). A VM can establish
compatibility, but cannot establish physical M4 performance. No other profile or
newer OS result can substitute for the required final-payload acceptance.

## Open software and qualification blockers

1. **macOS containment helper is not implemented.** Linux seccomp remains available;
   the new capability interface explicitly rejects unsupported backends. A supported,
   reviewed App Sandbox helper/IPC design and actual native denial/lifecycle tests are
   still required. A plain Python process is not substituted. Both macOS production
   builds therefore fail preflight, before assembly or publication.
2. **No macOS 26.0 acceptance runner was supplied.** Handoff and authenticated result
   ingestion are implemented and unit-tested, but no baseline runtime was exercised.
3. **Frozen acceptance remains incomplete.** Ordinary Finder/configuration paths,
   read-only installation, quarantine/translocation and helper containment need native
   end-to-end coverage beyond the existing GUI smoke. Native library license/security
   review is not replaced by Python advisory results. These are release blockers.
4. **Apple trust branch is unqualified.** Signing/notary orchestration is implemented;
   no actual Developer ID signing, notarization or stapling was performed for this
   delivery. Invalid credentials cannot fall back to unsigned.
5. Full MaleCNS scientific/model qualification and account-specific practice/live
   authorization remain independent. No broker secrets, account enrollment, real
   OANDA calls or orders are part of these checks.

CI uses explicitly configured interactive self-hosted native runners, with a
protected signed environment restricted to the trusted main branch. The unsigned job
has no signing inputs and runs the same common gates. Failed jobs upload diagnostics
under a separate unverified label. Linux/Windows source/native job scopes remain
explicit; neither was natively qualified on this Mac.
