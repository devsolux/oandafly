# Bootstrap and reviewed deployment

No step below supplies preapproved live credentials, investment parameters or
qualification. Begin on a dedicated eligible practice account after reviewing the
engineering status. Native account eligibility must reflect the actual contracting
entity, legal product, account and permissions; never change a region or product
label merely to pass a validator.

## 1. Build an actual supported runtime

Use Linux, Python 3.13, libseccomp and actual SQLite >= 3.51.3. Install core
packages plus `native-vault` for an unlocked Secret Service session or use systemd
credentials. Install `feather` to consume official Feather files; CSV needs no
Arrow dependency. Tkinter is optional for the local control client.

```bash
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,native-vault,feather]'
python -m oandafly doctor
```

If the interpreter links an older SQLite, use a reviewed Python/runtime build that
includes the required version. `scripts/build_sqlite.py` alternatively builds a
private pinned SQLite 3.51.3 shared library on Linux after checking the official
amalgamation source digest. It is an optional helper, not a script executed or
qualified in this delivery. It never overwrites system libraries.

```bash
python scripts/build_sqlite.py --prefix /absolute/private/sqlite-3.51.3
export LD_LIBRARY_PATH=/absolute/private/sqlite-3.51.3/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
python -c 'import sqlite3; print(sqlite3.sqlite_version)'
oandafly doctor
```

An interpreter that statically links SQLite needs to be rebuilt; environment
variables cannot change that fact. Never monkeypatch `sqlite3.sqlite_version_info`
or disable `Database` admission. Run the complete suite, reject prerequisite skips,
resolve fresh dependency locks with `scripts/lock_dependencies.py` and audit them;
see [dependency instructions](DEPENDENCIES.md).

## 2. Obtain the real graph and make assumptions explicit

The official MaleCNS download page currently identifies `male-cns:v1.0` and exposes
curated body annotations and complete segment connection weights. Download using
the official data instructions, retain original files and license/provenance,
and inspect their actual schema. No full data download was performed here.

Prepare a reviewed `bodyId,sign` table with a justified sign for every curated
neuron. The program does not silently infer neurotransmitter/receptor behavior or
replace missing signs with an excitatory default. The sign assumptions need a
content-addressed review report. The exact neuron/edge column names must be chosen
from the downloaded release, not guessed from this example.

```bash
umask 077
oandafly graph-import   --neurons /absolute/data/curated-neurons.feather   --connections /absolute/data/published-connections.feather   --signs /absolute/data/reviewed-signs.csv   --release male-cns:v1.0   --neuron-column bodyId --pre-column bodyId_pre --post-column bodyId_post   --weight-column weight --sign-id-column bodyId --sign-column sign   --output /absolute/private/graphs/male-cns-v1
```

The example mappings are parameters, not a statement that those columns were
verified in the current official Feather files. Inspect and reconcile the import
manifest, all source hashes, all curated neurons, internal edges/contact totals
and excluded endpoints. Import publication is immutable. Obtain an independent
DATASET_REVIEW approval; do not sign a cropped graph as complete.

## 3. Prepare read-only account access and causal history

Use JSON schemas in `examples/schemas/` for `CredentialReference`,
`VerifiedAccountProfile`, `EligibilityEvidence`, `TrustPolicy` and `WorkerConfig`.
Runtime metadata and approval files must be owner-only regular files (0600), with
private directories and no symlink traversal. Decimal financial JSON fields are
strings. Credentials contain only metadata; never put a token in the JSON.

For native Secret Service storage:

```bash
oandafly credential-enroll --reference /absolute/private/observation-reference.json
```

The CLI prompts for the token without echoing it. For a headless systemd service,
choose `SYSTEMD` and obtain the expected LoadCredential name using
`credential-name --reference ...`; provision its value with systemd, not an
environment variable or application plaintext fallback. Scope observation and
execution metadata separately to exact authority, account, environment and expiry.
Application purpose scope does not narrow the broker's own token permissions.

A SHADOW configuration must omit execution credential, authorization grant and
deployment approval. It still needs a real, eligible account/profile to capture
native history. Missing model prerequisites block alpha, not an intentionally
separate data-capture loop. The worker stores completed candles at
`~/.devsolux/oandafly/authorities/<sha256-authority-id>/candles/<instrument>/` using the
OS-account home, not a caller-supplied HOME override. Choose an archive with enough
chronological data; never mix synthetic, practice and live sources in one run.

## 4. Train candidates without account secrets

Create private TrainingSpec and CostAssumptions documents from the included
schemas. Cost assumptions need real measured/reviewed evidence; zero or invented
costs do not establish an edge. Examples of command structure:

```bash
oandafly train --graph /absolute/private/graphs/male-cns-v1   --archive /absolute/private/history/EUR_USD   --instrument EUR_USD --spec /absolute/private/training.json   --costs /absolute/private/costs.json --output /absolute/private/candidates/trial-001

oandafly train-watch --graph /absolute/private/graphs/male-cns-v1   --archive /absolute/private/history/EUR_USD   --instrument EUR_USD --spec /absolute/private/training.json   --costs /absolute/private/costs.json --output /absolute/private/candidates   --interval-seconds 3600
```

Each candidate includes data-only arrays, manifest and training evidence. The
readout produces separate net long/short utility estimates; the confidence field
is an engineered separation score, not an already calibrated win probability.
A training result is **UNQUALIFIED**. Empirical economic evaluation, confidence
calibration, full-window latency and native qualification are not produced by
inventing hashes or signing the trainer's proxy score.

The current trainer fits one instrument per invocation. The deployment contract
can declare multiple instruments but cross-instrument generalization must be
validated for each declared instrument; this is not a multi-instrument training
pipeline. Begin qualification with a single instrument.

## 5. Establish independent review and authority

`approval-keygen` creates a passphrase-encrypted Ed25519 private key and prints its
public key. Keep private keys off the trading worker. Each role needs a distinct
public key: ACCOUNT_ELIGIBILITY, TRADING_AUTHORIZATION, DATASET_REVIEW,
MODEL_VALIDATION and DEPLOYMENT. Their public keys and validity/revocation data
form a private operator-installed TrustPolicy.

```bash
oandafly approval-keygen --output /offline/reviewer/model-validation.pem
oandafly identities --graph /absolute/private/graphs/male-cns-v1   --model /absolute/private/candidates/trial-001
oandafly sign-approval --key /offline/reviewer/model-validation.pem   --payload /offline/reviewed/model-validation.json --role MODEL_VALIDATION   --key-id reviewer-model-1 --output /offline/reviewed/model-validation.signed.json
```

Signing is not validation. The qualification contract binds graph/model/source/
runtime identities, training receipt, costs/evaluation evidence and all named
scenarios. A production runtime/package change invalidates its identity binding.
An authorization grant defines exact account, mode, instruments, risk envelope,
entry/recovery expiry and permitted automatic resume/promotion. A deployment
approval binds the qualified model and generation to that account/grant. LIVE
requires separate account-specific evidence and native practice-soak evidence.

Schema generation does not create claims: inspect `ModelValidation` and
`DeploymentApproval` requirements and complete the actual tests. Evidence hashes
must refer to retained reviewable evidence. A human signer or separately governed
qualification service may issue truthful approvals; the trainer cannot.

## 6. Run native practice, then separately qualify live

Create `WorkerConfig` from the schema or the intentionally non-executable
`examples/worker.practice.json.template`. No-write SHADOW and separate LIVE templates
are also provided; see [template guidance](../examples/README.md). Set real private paths and account
metadata. Validate schema, then run after qualification:

```bash
oandafly validate-config --config /absolute/private/worker.json
oandafly run --config /absolute/private/worker.json
```

The worker can arm new entries only after scoped qualification, grant, model
health, current account/market/risk cuts and recovery prerequisites hold. A valid
initial PRACTICE or LIVE grant is real authority to trade; do not start a write
configuration merely to explore the UI. SHADOW provides an explicit no-write mode.

Automatic candidate selection uses a separately reviewed registry of
CandidateReference documents and strictly increasing deployment generations. It
requires an explicit automatic-promotion grant and a flat, reconciled account.
The registry is not the raw train-watch output directory. Return to practice/native
qualification when material graph, numerical, cost, runtime or source behavior changes.
