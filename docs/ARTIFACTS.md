# Source files versus external and generated artifacts

The source ZIP is not a broker account, trained policy or dataset distribution.
Keeping operator secrets and personal account history outside the archive is intentional.
The per-file manifest lists delivered source content; it does not attest to these external
inputs or to the completion of the software work in ENGINEERING_STATUS.md.

| Artifact | How it is supplied | Included? |
|---|---|---|
| Python modules, tests, schemas, icons, build/configuration tooling | This archive | Yes |
| Practice/live/shadow metadata examples | `examples/*.template` | Yes, placeholders only |
| Production dependency locks | Resolver on a network-enabled/reviewed build host | No; blocked by network resolution here |
| Third-party wheels and Python interpreter | Reviewed environment or wheelhouse | No |
| SQLite shared library and libseccomp | Target OS/runtime build | No |
| Complete curated neurons, connection table and reviewed signs | Actual pinned MaleCNS release plus explicit scientific review | No |
| Compiled graph manifest and arrays | `oandafly graph-import` from actual full inputs | Generated externally |
| Causal historical bars | Real account/data capture; kept private | Generated externally |
| Candidate encoder/readout/bias arrays and training receipt | `oandafly train` / `train-watch` | Generated externally |
| Qualified model and independently measured evaluation | Scientific/economic/latency validation | Not bundled or performed here |
| Account profile, actual instrument rules and eligibility evidence | Account-specific observation and review | Operator-owned |
| Credential reference UUIDs and exact scoped token | Secret enrollment or OS-managed credential delivery | Operator-owned |
| Public trust policy and signed claims | Independent role-separated review/authorization | Operator-owned |
| Approval private signing keys | Offline reviewer infrastructure | Must not be on the worker |
| Ledger, outbox, status, control queue and model state | Runtime under the actual service-account home | Generated externally |

Graph and model array names are enforced by `neural/artifacts.py`; their manifests bind
exact bytes, shapes and hashes. Do not create zero-filled or small graph arrays under
production filenames to satisfy file-existence checks. Small in-test arrays remain
labelled `TEST_FIXTURE` and cannot qualify the production strategy.

**Missing software is not fixed by supplying files.** Native NAV/economic simulation,
confidence calibration, independent evidence generation, production operational
services and genuine practice/live qualification remain as documented in
[engineering status](ENGINEERING_STATUS.md). No connected account was used during
this source packaging recheck.
