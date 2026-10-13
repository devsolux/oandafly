#!/bin/bash
# Oandafly 1.0.0 / com.devsolux.oandafly — native desktop development.
# Adapted from the supplied macOS launcher; compatible with macOS Bash 3.2.
set -euo pipefail
export MACOS_SIGN_IDENTITY="${MACOS_SIGN_IDENTITY:-351A9115F928304981211FEB5866E94B6C760AD1}"

export MACOS_NOTARY_PROFILE="${MACOS_NOTARY_PROFILE:-macos-notary}"
root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
command_name="${1:-run}"
if [[ $# -gt 0 ]]; then shift; fi
fail() { printf 'oandafly dev: %s\n' "$*" >&2; exit 1; }
usage() {
    cat <<'HELP'
Oandafly 1.0.0 — desktop-first development
Native targets: macOS ARM64 and Linux AMD64. Windows AMD64: use .\dev.ps1.
Usage: ./dev.sh [command] [arguments]

  run [--authority ID] [--config PATH]  Open desktop (default); never auto-start trading.
  setup [--python PATH] [--resolve] [--offline]
                                      Sync .venv strictly from uv.lock.
  lock [--offline] [--check]           Resolve/export or verify genuine dependency locks.
  build-unsigned                     Full production checks; no Developer ID/notarization.
  build                              Same checks plus signing, notarization and stapling.
  release                            Exact alias of build; no signing CLI flags.
  verify PATH [--logs PATH]           Test a compiled desktop's visible window and exit.
  test [pytest arguments]             Run tests in the existing environment.
  check                              Shell syntax, source checks, Ruff and tests.
  tk                                 Open the native Tk test window.
  doctor                             Diagnose runtime, unsigned and signed eligibility.
  doctor-worker                      Report stricter worker runtime prerequisites.
  worker --config PATH               Run the authorized headless worker in foreground.
  package [--require-locks]           Create a complete source archive (not a native build).
  clean [--all]                      Delete checkout build/cache files; preserve app data.
  help                               Show this help without Python or uv.

First setup:
  macOS: brew install uv python@3.13 python-tk@3.13
  Linux: install native CPython 3.13 with Tk, uv, X11/XWayland and a desktop session.
  ./dev.sh setup --resolve            Generate missing locks, then sync; network needed.
  ./dev.sh                           Open desktop; no package sync on normal runs.

Setup's --python must be an absolute native Python 3.13 executable path.
No automatic Python downloads, Python 3.14 selection, trading overrides or demo fallback.
Missing locks are not invented: use setup --resolve or lock explicitly.
Build uses PyInstaller on the target OS/architecture, never cross-compiles.
Both builds require all four reviewed locks, full test evidence and final-payload tests.
macOS runtime minimum: 26.0. Reference: Xcode 27.0, Apple clang 21.0.0
(clang-2100.3.34.2), SDK 27.0 (26A425). Baseline 26.0 acceptance is mandatory.
build reads MACOS_SIGN_IDENTITY and MACOS_NOTARY_PROFILE resolved by this launcher.
Unset/empty variables use the declared defaults; nonempty overrides are preserved.
build-unsigned never probes those credentials. Required local ad-hoc sealing is recorded.
Windows release uses a certificate-store thumbprint and RFC3161 timestamp server.
SQLite >=3.51.3 and Linux libseccomp remain worker requirements, not UI startup gates.

clean runs without Python/uv; refuses active/leftover developer lock directories.
clean --all additionally requires lsof and refuses an in-use or symlinked .venv.
Source, Git metadata, .env, locks, and ~/.devsolux/oandafly are preserved.
HELP
}
require_root() {
    [[ "$root" != / && -f "$root/pyproject.toml" && -f "$root/src/oandafly/__init__.py" && -f "$root/scripts/dev.py" ]] ||
        fail "Place dev.sh in the Oandafly project root."
}
clean_checkout() {
    local all=0
    if [[ $# -gt 0 ]]; then
        [[ $# -eq 1 && "$1" == --all ]] || fail "clean accepts only --all."
        all=1
    fi
    local lock
    for lock in .dev-build.lock .dev-environment.lock .dev-use.lock; do
        [[ ! -e "$root/$lock" && ! -L "$root/$lock" ]] || fail "Developer operation lock exists: $lock. Stop its owner; inspect stale locks manually."
    done
    # An atomic operation lock closes the check-to-delete race with setup/build/run.
    mkdir "$root/.dev-environment.lock" 2>/dev/null || fail "Another developer operation is active."
    trap 'rmdir "$root/.dev-environment.lock" 2>/dev/null || true' EXIT
    if [[ "$all" == 1 && ( -e "$root/.venv" || -L "$root/.venv" ) ]]; then
        [[ ! -L "$root/.venv" ]] || fail "Refusing to remove a symlinked .venv."
        command -v lsof >/dev/null 2>&1 || fail "lsof is required for clean --all."
        if lsof -t +D "$root/.venv" >/dev/null 2>&1; then
            fail "The environment is in use; stop desktop/worker processes before cleaning."
        fi
    fi
    cd -- "$root"
    /usr/bin/find -P . \
        \( -name .git -o -name .hg -o -name .svn -o -name .agents -o -name .codex -o -name .venv -o -name dist \) -prune -o \
        \( -name __pycache__ -o -name '*.egg-info' -o -name .eggs -o -name .tox -o -name .nox \
           -o -name __pypackages__ -o -name .pytest_cache -o -name '.mypy_cache*' -o -name .ruff_cache \
           -o -name .hypothesis -o -name .ipynb_checkpoints \) -prune -exec /bin/rm -rf -- {} + -o \
        \( -type f -o -type l \) \( -name '*.pyc' -o -name '*.pyo' -o -name .DS_Store \) -exec /bin/rm -f -- {} +
    /bin/rm -rf -- "$root/build" "$root/htmlcov" "$root/.audit_cache" \
        "$root/.audit_work" "$root/.audit_tools" "$root/.uv_cache"
    /bin/rm -f -- "$root/.coverage" "$root"/.coverage.* "$root/coverage.xml" "$root/coverage.json"
    if [[ "$all" == 1 ]]; then /bin/rm -rf -- "$root/.venv"; fi
    printf 'Clean complete. Source, dependency locks and application storage were preserved.\n'
}
case "$command_name" in help|-h|--help) usage; exit 0;; esac
# Validate production commands before Python, locks, setup or credential discovery.
case "$command_name" in
    build|build-unsigned|release)
        if [[ $# -eq 1 && ( "$1" == --help || "$1" == -h ) ]]; then
            printf '%s: no arguments. Both profiles require all common production gates.\n' "$command_name"
            usage; exit 0
        fi
        [[ $# -eq 0 ]] || fail "$command_name takes no arguments; use $command_name --help."
        ;;
    setup|lock|run|verify|test|check|tk|doctor|doctor-worker|worker|package|clean) ;;
    *) fail "Unknown command: $command_name. See ./dev.sh help.";;
esac
require_root
if [[ "$command_name" == clean ]]; then clean_checkout "$@"; exit 0; fi
case "$(uname -s):$(uname -m)" in
    Darwin:arm64|Linux:x86_64) ;;
    Darwin:*) fail "Use native macOS ARM64; relaunch your shell outside Rosetta.";;
    *) fail "Use native macOS ARM64/Linux AMD64, or dev.ps1 on Windows AMD64.";;
esac
selected=""
# setup --python is honored before running any interpreter, even with a broken .venv.
if [[ "$command_name" == setup ]]; then
    arguments=("$@")
    for ((i=0; i<${#arguments[@]}; i++)); do
        if [[ "${arguments[$i]}" == --python ]]; then
            (( i+1 < ${#arguments[@]} )) || fail "--python requires an absolute executable."
            selected="${arguments[$((i+1))]}"
            [[ "$selected" == /* && -x "$selected" ]] || fail "--python requires an absolute executable."
            break
        fi
    done
fi
if [[ -z "$selected" && -x "$root/.venv/bin/python" && ! -L "$root/.venv" ]]; then selected="$root/.venv/bin/python"; fi
if [[ -z "$selected" ]]; then
    for candidate in /opt/homebrew/opt/python@3.13/bin/python3.13 /opt/homebrew/bin/python3.13; do
        if [[ -x "$candidate" ]]; then selected="$candidate"; break; fi
    done
fi
if [[ -z "$selected" ]]; then selected="$(command -v python3.13 || true)"; fi
[[ -n "$selected" ]] || fail "Native Python 3.13 not found. See ./dev.sh help."
cd -- "$root"
exec "$selected" -I -B "$root/scripts/dev.py" "$command_name" "$@"
