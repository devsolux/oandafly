> Historical report. Commands and observations below describe the earlier delivery.
> Current commands: [production contract](PRODUCTION_BUILD.md). Current evidence:
> [implementation report](IMPLEMENTATION_REPORT.md).

# Test report and release prerequisites

## Original refactor baseline

Executed against the refactored source with Python 3.13.5, SQLite 3.46.1,
pydantic 2.13.4, httpx 0.28.1, cryptography 46.0.4, numpy 2.3.5, scipy 1.17.0,
threadpoolctl 3.6.0, pytest 9.0.2 and pytest-asyncio 1.3.0 on Linux.

```text
231 passed, 211 skipped
```

The 211 skips are explicit unsupported-production-SQLite prerequisites. They are
not 211 passing tests. The actual `Database` admission guard was never patched or
bypassed. A separate test confirms that this old runtime is rejected. One process-
death integration test also skips before launching its unsupported child runtime.

New focused tests cover graph completeness accounting/artifact tampering, real
Linux-contained tiny-graph inference, per-instrument state and restart equivalence,
causal candidate training/publication, explicit warmup/gaps/half-tick midpoints,
mode/authorization isolation, signed approvals/revocation, native mocked one-shot
entry/close requests, no hidden HTTP retries, exact risk, target deduplication,
reversals and flow-adjusted equity. Fixture graphs and synthetic bars do not
substitute for a real connectome or a native practice account.

The isolated SQL algorithm host in `tests/refactor/test_sql_algorithms.py` checks
all permutations of open/reduce/close facts, backward frontier prevention,
contradiction fencing, atomic baseline, persistent targets and loss fences. It is
not the production Database and does not qualify WAL, process locking, supported
SQLite behavior, or complete end-to-end native execution.

Logs, JUnit XML and runtime doctor JSON are in `docs/evidence/`. The doctor reports
runtime prerequisites false and trading qualified false. Compilation/import/CLI
checks are recorded separately. No complete supported-runtime suite, full-model
benchmark, interactive Tk session, external dependency audit, real account call or
broker mutation was completed in this environment.

## Target dependency environment

`pyproject.toml` contains fresh package requirements; they differ from the local
installed package set above. Network installation failed in the editing environment.
There is deliberately no misleading recycled `uv.lock` or hashed requirements file.
After resolving in the actual target environment, review and commit fresh locks:

```bash
python scripts/lock_dependencies.py
python scripts/lock_dependencies.py --check
python scripts/audit_dependencies.py
```

The auditor uses an isolated pinned pip-audit tool, writes raw evidence outside the
source checkout and requires hashed exact requirements. A successful advisory query
is not exploitability, wheel integrity, scientific or trading certification. These
commands were not successfully run against the new dependency graph here.

SQLite 3.51.3 is the preserved application minimum; the official release notes
include a WAL-reset corruption fix. Use a genuine linked runtime at or above that
version and appropriate serialized build options, not a reported version override.
The optional private build helper verifies the official SHA3-256 of `sqlite3.c`.

## Required release run

```bash
python -m compileall -q src tests scripts
pytest -q --junitxml=test-results.xml
python scripts/check_test_evidence.py test-results.xml
```

The evidence checker rejects skips and missing tests as well as failures/errors.
Run the supplied CI on the real target dependency environment and investigate every
failure. CI contains no live credentials and must never be repurposed into an
unreviewed account-arming step. Unit tests alone cannot satisfy native practice
soak, full-model economics or live account qualification.

## Source-package recheck

The original test result was reproduced during the source completeness recheck.
Additional packaging tests and distribution checks are recorded in
`evidence/recheck/`. See [source completeness](PROJECT_COMPLETENESS.md) and
[dependency instructions](DEPENDENCIES.md) for the corrected export/build workflow.
