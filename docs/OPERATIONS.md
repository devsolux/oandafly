# Operations and recovery

## Control meanings

Commands use a private same-owner durable mailbox. The returned request ID means
**queued**, not that a mutation or flatten has completed. Requests expire after
60 seconds; inspect private status and broker truth rather than treating a queued
request as an acknowledgement.

| Command | Meaning |
|---|---|
| `status --authority ID` | Read the last private worker status; check its timestamp |
| `halt --authority ID` | Block alpha entries; keep reconciliation/protective recovery running |
| `flatten --authority ID` | Request authorized cancellations/reductions toward zero; cannot guarantee immediate fill |
| `resume --authority ID` | Request rechecking of authorization, scope, reconciliation, protection and risk |
| `stop --authority ID` | Stop the worker; does not guarantee closing open positions |
| `desktop --authority ID` | Optional Tk status and command client; window closure is not worker shutdown |

A stopped/dead worker cannot watch protections. Inspect broker-native orders and
retain independent account access. Closing a window, stopping a service, HALT and
FLATTEN are not interchangeable.

## Restart

Use a single owner process per actual broker account and a dedicated OS account.
The worker reacquires canonical ownership, opens its fresh-schema store, reconciles
native history, compares current positions/orders/cash, verifies protective state,
checks current credentials/profile/grants and obtains fresh model/risk admission.
The initial database identity is not user-configurable to create a second account
truth. A different host can still start another process; external deployment must
prevent that split-brain condition.

Unattended restart requires `allow_unattended_resume` in a current signed grant.
Otherwise an explicit RESUME request is required after recovery. Old decisions are
not blindly replayed as new entries. Same-bar target provenance and outstanding
native intents remain durable. Unknown dispatch results remain unknown.

## When entry is fenced

Stale/missing data, immature feature history, model containment/deadline failure,
expired/revoked qualification, profile mismatch, unqualified runtime, unresolved
native outcomes and risk violations prevent new exposure. The safe result can be
remaining flat; lack of a qualifying candidate is not a reason to use a toy fallback.

Loss/drawdown fences are latched. An automatic RESUME is not a reset of the hard
loss record. Investigate the evidence and resolve through reviewed recovery and
renewed authority; this release deliberately has no casual delete-the-fence CLI.
Do not delete the database, fake the frontier, alter risk rows or mint new equivalent
intents to escape an uncertain outcome.

## Unknown native outcome

Retain the intent, exact dispatch bytes, reservation and received facts. Fetch
complete native transaction history and reconcile lineage against the real account.
If submission cannot be proved absent or resolved, keep new exposure fenced and
use separately verified broker/account support and reviewed recovery. Do not
assume HTTP timeout, 5xx, connection loss or process death means rejection.

Prepared recovery actions proven not invoked may resume with the same identity.
Already unknown actions are not blindly retried. Exhaustive recovery lifecycle
coverage remains an integration qualification item, including conservative terminal
NOT_SENT/rejected close/cancel outcomes that may require operator intervention.
There is no guarantee that every contradiction or ambiguous outcome auto-heals.

## Credentials, profiles, qualification and expiration

Observation and execution tokens are purpose/account/environment-bound in the
application, with native/systemd storage and no fallback plaintext token file.
An application grant does not narrow permissions of a broad broker token. Use the
least account access actually supported and protect the service user.

Renew actual eligibility/profile documents, signer validity, credentials, dataset
review, model approval and entry/recovery grants before expiry. The recovery period
is explicit, not unlimited authority after an expired grant. Live qualification
never transfers from a practice account by changing one URL or dropdown.

## State and retention

Runtime state is under the OS-derived `~/.devsolux/oandafly` root, separate from the
source checkout. Authority journals, targets, native histories and candle archives
must be backed up using an actual consistent SQLite backup procedure under reviewed
operations. Never copy only the main `.db` while WAL writes are active or delete
WAL files as cleanup. No backup/restore operator CLI is implemented here.

Reconcile restored state against broker truth before granting entries. The current
archive/journal implementation has explicit size/admission bounds but no complete
long-term data retention/export service; operational retention is a release item.

## Deployment constraints

Linux/libseccomp is the production containment target. An optional desktop client
is not a separately authenticated remote network control plane. Use a single
supervisor with restricted OS permissions; do not run training and signing with
broker authority. REST polling is the integrated observation path; the local rate
governor is not shared across unrelated applications or hosts.
