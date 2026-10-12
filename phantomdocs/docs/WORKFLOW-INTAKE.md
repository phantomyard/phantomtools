# Authenticated workflow intake and reviewed adapter interfaces

## Execution extension

See [workflow execution](WORKFLOW-EXECUTION.md) for the subsequent executor API,
started/completed journal, reference deduplication and observed graph export.
The admission commands below retain their original non-executing behavior.

## Scope and trust boundary

`pd workflow sign-request`, `accept` and `status` add authenticated admission to
workflow planning. They do not execute media tools, scan Drive, copy or hash
source bytes, index references, publish derivatives or update PhantomOrg.
The SQLite file is an independent intake ledger, not the PhantomDocs store.
`validated` means an accepted plan has its declared capabilities present;
`blocked` means capabilities are missing. Neither means processed or indexed.
Every result retains `execution_ready: false`.

The operator and installation configuration are trusted. The CLI's `--actor`
is a local execution context, not login authentication. Requester proof comes
from the signed envelope. Keep the ledger and tool configuration private;
filesystem access can bypass CLI guards. SQLite records and hashes detect
inconsistent plans but are not a tamper-proof audit service. Windows deployments
must enforce equivalent private access with filesystem ACLs; POSIX owner/mode
checks do not enforce Windows ACLs.

## Request identity and retries

A signed-request contract contains the complete job, explicit UTC issue/expiry,
crypto version, signer public key and signature. The existing BIP-340 primitives
sign a workflow-specific canonical envelope containing the job hash, organization,
request id, requester and validity. Unknown fields and versions are rejected.
The key must belong to that requester in the supplied PhantomOrg model and be
valid both at issue time and admission time. Future-dated, expired, revoked,
modified and cross-protocol signatures are refused. No key generation, remote
transport or credential discovery is provided. Signing requires the requester's
existing private key in a protected file; never put it in a job or repository.

The durable identity is `(organization, request_id)`. Admission is an immediate
SQLite transaction committing the job and its event together. Concurrent exact
retries yield one record and one admission event. A different job payload under
the same identity is a conflict. Exact retries return the original pinned plan
without reading media or rebuilding the plan, including after a default-persona
change. They still require a currently valid signature and current local write
authorization. After expiry the requester can freshly sign the identical job;
that is still the same record. `status` requires current category read access
and uses a read-only database connection.

`accept --replan` is an explicit authorized refresh of installation/protocol/org
pins and processor selection. It may unblock a plan, but cannot reclassify it
or change owners. An unchanged plan adds no event. A changed plan adds a
`replanned` event while preserving the original admission timestamp. This is
not a stage-resume engine: no execution leases, artifact ledger or completion
state exists yet. An executor must recheck current authorization and pinned
inputs before execution; accepted planning prerequisites are not completion
claims. No invitation-only read gate is added.

## Offline trial commands

Use a separate test environment and a sanitized model with test identity keys.
The job must use an actor actually declared in that model. Replace each variable
with an explicit local path/value; these commands are not a live deployment.

```bash
python3 -m venv .workflow-venv
.workflow-venv/bin/pip install './phantomdocs[dev]'
umask 077
mkdir -p "$TRIAL_DIRECTORY"
chmod 700 "$TRIAL_DIRECTORY"

.workflow-venv/bin/pd workflow sign-request   --job "$JOB_JSON" --org-yaml "$ORG_YAML" --actor "$REQUESTER"   --nsec-file "$EXISTING_PRIVATE_KEY_FILE" --expires-at "$EXPIRY_UTC"   > "$TRIAL_DIRECTORY/request.signed.json"

.workflow-venv/bin/pd workflow accept   --request "$TRIAL_DIRECTORY/request.signed.json"   --ledger "$TRIAL_DIRECTORY/intake.sqlite"   --installation "$INSTALLATION_JSON"   --org-yaml "$ORG_YAML" --actor "$CUSTODIAN"   --phantombot-config "$PHANTOMBOT_CONFIG"   --phantombot-state "$PHANTOMBOT_STATE"

.workflow-venv/bin/pd workflow status   --request-id "$REQUEST_ID" --ledger "$TRIAL_DIRECTORY/intake.sqlite"   --org-yaml "$ORG_YAML" --actor "$AUTHORIZED_READER"
```

Rerun the same `accept` command to verify `unchanged: true`; use `--replan`
only for an intentional refresh. Exit 0 means admitted/valid, 1 means invalid,
conflicting or unauthorized, and 2 means admitted but blocked. The signed receipt
contains no private key, but includes document references and should be private.
On POSIX, the ledger must be a regular file owned by its caller, mode 0600 or
stricter, in a directory not writable by group/others. Symlink paths are refused.
An unrelated existing database is refused rather than migrated or overwritten.
These commands do not replace the earlier AU recovery utility: no historical
files are ingested by this block.

## Phantombot instance selection

The reviewed AU deployment stores the default in the selected instance's
`/home/pb-<id>/.local/share/phantombot/state.json`, with config at
`/home/pb-<id>/.config/phantombot/config.toml`. For the existing Pepa instance:

```bash
PHANTOMBOT_CONFIG=/home/pb-pepa/.config/phantombot/config.toml
PHANTOMBOT_STATE=/home/pb-pepa/.local/share/phantombot/state.json
```

These are operator-supplied selectors, not discovery or a global organizational
default. Do not use the inherited `/home/aquaponi` state (default `lisa`) for
this workflow. The generic resolver also supports a declared config-only default
in another installation. A malformed state does not silently select another
persona. Alma's host location does not change her requester identity or project
context; admission supports her signed requests without moving her account.

## Reviewed AU source snapshot and bindings

The user supplied sanitized deployment source at
[aquaponics-deploy pipeline snapshot e9977af](https://github.com/salvaalba-dev/aquaponics-deploy/tree/e9977af5407cdc163bbce43a18976db80868931f/pipeline).
Private deployment scripts, credentials and branding assets are not copied into
this product branch. Earlier deployment README routing statements do not override
the operator's approved two meeting destinations or category/ownership rules.
No non-meeting AU route has been invented.

`examples/workflows/au-tool-bindings.json` is an opt-in descriptor map. It records
reviewed interfaces, not verified installed versions. `snapshot-e9977af` identifies
the source reviewed; the operator must verify runtime binaries, script hashes,
models, assets and environment before an executor can activate them. The default
AU installation example still has empty bindings and therefore remains blocked.
A test installation may explicitly copy this map into its `tools` property;
this only enables presence checks in a plan, not execution.

| Capability | Reviewed command | Implications before activation |
| --- | --- | --- |
| documents.transcribe | Whisper venv Python, `transcribe.py INPUT --model turbo --out OUTPUT` | No implicit `--summarize`; model/runtime and media validation remain required. Diarization/fusion need separate declared adapters. |
| documents.summarize | Whisper venv Python, `summarize.py INPUT --out OUTPUT` | DeepSeek API needs deployment credentials and explicit data-processing policy; no API call occurs in planning/admission. |
| documents.brand | Whisper venv Python, `acta_a_pdf.py INPUT OUTPUT --clase transcripcion/resumen` | Writes HTML beside PDF; requires trusted AU assets, Node and Chrome. Validate metadata and rendered output in isolated work space. |
| documents.index | `/opt/phantomdocs/venv/bin/pd` | Only a capability descriptor; no generic transformation argv or index invocation is provided yet. |

Existing external flags and class values remain literal for compatibility.
The command builder returns an argument list, substitutes only whole `{input}`
and `{output}` tokens, and rejects unsupported variants, flag-like/control-character
path arguments and identical source/output paths. It does not launch a shell or
subprocess. This is not a sandbox: trusted tools may have their own side effects,
and a future executor must enforce controlled paths, validate artifact roles and
formats, preserve sources and reject stale pins.

The reviewed `process_recording.py` and `post-meeting.sh` are deliberately absent
from these bindings. The orchestrator combines transcription, fusion, summarizing,
minutes, branding, Drive operations and server cleanup. `--no-upload` stops before
upload but after other processing and an `--orden-drive` lookup; it is not a pure
transcription adapter. Without it, the script may resolve/create a folder, upload
files, augment Markdown/re-render PDFs, and delete local sources unless `--keep`.
Connecting that command to a single generic step would hide these dependencies.

Branding must produce separately identified derivatives before final indexing.
The existing renderer interfaces are now known, but compatibility, metadata,
resource limits, version pins and output QA still need the executor block.
No AU upload, folder creation, move, rename or deletion is authorized by these
descriptors. PhantomView still receives only the declared graph contract; its
actual ledger consumer and dashboard integration are a subsequent change.
