# Linux service deployment template

`oandafly.service.template` is an optional reviewed-deployment starting point, not a
qualified installation or a command to start trading. No service is enabled by the
package. The worker requires Linux, Python 3.13, actual SQLite >=3.51.3 and libseccomp.

## Resolve paths and authority before installation

Use a dedicated unprivileged `oandafly` OS account whose **passwd home** is
`/var/lib/oandafly`, not merely an overridden HOME environment variable. The worker
resolves the real account home and places private state below
`/var/lib/oandafly/.devsolux/oandafly/`. Install read-only application code and its
reviewed environment at `/opt/oandafly`; keep mutable history, ledgers and models
inside the approved state tree. A shared ledger is not a cross-host failover design.

Create the configuration directory under that account's home with mode 0700. Its
worker metadata, references and public trust-policy files must be regular files owned
by the service user with mode 0600. Private signing keys must remain off the worker.

The example references `worker.shadow.json` and assumes `SYSTEMD` credential storage.
Derive the exact observation credential name with the existing CLI, encrypt/provision
its value outside the source tree, and replace the commented credential directive.
A PRACTICE/LIVE configuration needs its separately scoped execution credential and
truthful approvals. SHADOW must not receive execution authority.

The application token parser expects the exact token bytes; do not introduce a trailing
newline when provisioning. Use a systemd credential store rather than plaintext values
in a world-readable unit or inherited environment. The runtime credential ownership
and mount behavior must be tested on the actual host with this application's strict
file reader; this template has not been exercised against native credentials here.

## Validate without starting a service

After substituting actual executable paths, home and metadata paths, copy the unit to
a local `.service` filename and run `systemd-analyze verify` against that file. Check
that `ExecStartPre` loads the intended SQLite shared library. Inspect permissions and
run the source/runtime tests before installing the reviewed unit. This package does
not include an auto-enable script, an example plaintext token or a privileged container.

Stopping a process is **not** confirmation that a broker account is flat. A systemd
stop or SIGTERM must not be described as a completed FLATTEN. Use the worker controls,
verify acknowledgement and reconcile native positions under the
[operations runbook](../../docs/OPERATIONS.md). Restart remains subject to the existing
application grant and recovery fences; `Restart=on-failure` cannot bypass them.

Production restart, secrets delivery, fault recovery and protection handling still
require a genuine practice-host qualification. Host-specific resource limits must be
sized from full-model measurements, not a tiny test graph.

Primary documentation: https://systemd.io/CREDENTIALS/ (consulted 2026-09-22).
