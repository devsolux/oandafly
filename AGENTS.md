# AGENTS.md

Guidance for coding agents working in this repository. `CLAUDE.md` imports this file.

## Branch policy: `main` only

- Work directly on `main`. Do not create feature branches, topic branches, worktree
  branches or pull requests unless the repository owner explicitly asks for one.
- Start from the current remote state: `git switch main && git pull --ff-only origin main`.
- Commit to `main` and publish with `git push origin main`. Never force-push and never
  rewrite published history.
- Commit or push only when the owner asks for it.

## Project

Oandafly is a Python 3.13 desktop-first research application: a full-MaleCNS neural
model proposes a target, and an independent exact-decimal risk and execution layer
decides what reaches OANDA v20. Code lives in `src/oandafly/` (`adapters`, `domain`,
`execution`, `worker`, `neural`, `security`, `storage`, `ui`, ...), tests in `tests/`,
build/release tooling in `scripts/`, documentation in `docs/`.

## Environment

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
# Optional extras also exercise the embedded-runtime probe test:
uv pip install --python .venv/bin/python -e '.[dev,native-vault,feather]'
```

Python must be exactly 3.13. Database tests need SQLite >= 3.51.3 and skip otherwise.
`./dev.sh setup` (or `.\dev.ps1 setup`) syncs the environment strictly from `uv.lock`.

## Before every commit

```bash
.venv/bin/python -m pytest -q -W error
.venv/bin/ruff check src tests scripts
.venv/bin/python -m compileall -q src tests scripts
.venv/bin/python scripts/project_integrity.py --write-manifest
.venv/bin/python scripts/project_integrity.py --verify-manifest
```

- `PACKAGE_MANIFEST.json` hashes every source, test, doc and workflow file, and CI
  verifies it first. Regenerate it after any change and commit it with the change.
- New top-level files must also be listed in `MANIFEST.in`; check with `python -m build`
  followed by `scripts/check_distribution.py` on the sdist and wheel.
- Record behavior changes in `CHANGELOG.md`.

## Code conventions

- Match the surrounding style. Dense code and semicolon-joined statements in tests are
  intentional; Ruff runs with the `pyproject.toml` configuration (line length 120).
- Money is exact `Decimal` via `oandafly.domain.primitives.money`; never use float.
- Fail closed. Missing evidence blocks entries; never weaken a `SafetyFence` to make a
  test pass.
- A dispatch is `NOT_SENT` only when its final guard proves nothing was transmitted
  (no invocation marker). Anything that may have been sent stays `UNKNOWN` and is never
  resubmitted.
- Never add secrets, tokens, account data or real broker calls to code, tests or
  fixtures. Credentials only come from the native vault or systemd credentials.
- Any change under `src/oandafly/` changes the source identity, so existing
  `MODEL_VALIDATION` approvals must be re-signed.
- The headless worker (`oandafly run`) is Linux-only; macOS and Windows are desktop-only.

## Testing conventions

- `with sqlite3.connect(...)` only commits; wrap raw connections in
  `contextlib.closing`. Create stores with `tests.support.supported_database`.
- The release gate (`scripts/check_quality.py`) fails on skips and xfails. Mark
  platform-specific tests with `posix_only` or `linux_containment` as well as `skipif`.
- Child Python processes keep a minimal environment but must forward `LD_LIBRARY_PATH`
  (see `child_environment` in `tests/domain/test_context.py`); the release allowlist is
  `scripts/release_policy.py`.
- Exercise worker logic with explicit test doubles, as in
  `tests/unit/test_worker_lifecycle.py`.

## CI and releases

- `.github/workflows/ci.yml` (`workflow_dispatch`): source checks plus the locked Linux
  qualification gate with a private SQLite build.
- `.github/workflows/desktop.yml` (`workflow_dispatch`, published releases): native builds
  on self-hosted runners, Sigstore cosign keyless signing, release asset upload.
- Actions are pinned to full commit SHAs with the version in a comment. Resolve a tag with
  `gh api repos/<owner>/<repo>/commits/<tag> --jq .sha`.
- Lint workflows with `uvx --from actionlint-py actionlint` and
  `uvx zizmor --offline .github/workflows/`.
- Start CI with `gh workflow run ci.yml --ref main` and follow it with
  `gh run watch <run-id> --exit-status`.

## Further reading

[README](README.md), [architecture](docs/ARCHITECTURE.md), [operations](docs/OPERATIONS.md),
[production build](docs/PRODUCTION_BUILD.md), [dependencies](docs/DEPENDENCIES.md).
