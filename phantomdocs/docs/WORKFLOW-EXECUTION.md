# Workflow execution and reference deduplication

## Implemented boundary

The product now includes `WorkflowExecutor`, an explicit trusted adapter interface,
a durable execution journal, a local command runner and `pd add --skip-existing-ref`.
The executor is a Python integration API, not a newly installed AU daemon or a
`pd workflow run` command. No adapter discovery from jobs is allowed. AU's reviewed
bindings are still inactive: their snapshot labels are not verified runtime pins.
No live tool, API, Drive object, VPS service or persona was changed.

A deployment adapter must explicitly implement:

| Method | Contract |
| --- | --- |
| `preflight(plan)` | Verify supported protocol, installed binary/script/model/asset versions, media validators, credentials, access and resource controls. Return exactly `True`, or refuse. |
| `perform(key, operation, inputs, plan)` | Perform one controlled operation using its stable idempotency key; commit a durable backend receipt before returning. |
| `reconcile(key, operation, inputs, plan)` | Look up an already committed result without re-executing. Return its receipt, or `None` if the result cannot be established. |

The executor trusts this installed code and its receipts. The fake Drive in tests
verifies orchestration behavior, not AU deployment compatibility. There is no
production AU publisher in this block. Adapters must not pass an unvalidated job
directly to a monolithic pipeline or claim format validation from a filename.

## Execution journal and interruptions

The executor creates the additive `workflow_execution` table in the private intake
ledger. Operations have `started` or `complete` states, a plan hash, timestamp and
hashed receipt. A separate host-local nonblocking runner lock serializes execution
for that ledger and releases on process exit. This supports one authoritative host;
shared multi-host writers are unsupported.

Before starting, the executor checks current caller write access, the pinned
processor, signed intent and requester key lifecycle, current rebuilt plan pins,
missing capabilities and adapter preflight. Request expiry bounds admission;
after admission execution still checks key validity. A changed policy, registry,
installation or default processor requires explicit resolution before execution.
Once any execution operation starts, replanning is forbidden. Do not edit database
records to force a new plan; resolve the operation and use a new authorized request
if intent/policy must change. Trusted adapters must enforce current authorization
at external writes as well as the initial runner checks.

Each operation is recorded as started **before** external effects. Complete
receipts are reused without invoking a tool, reading source bytes or hashing them
again. If a process stops after a backend commit but before the journal commit,
the next run consults `reconcile` with the identical operation key. If no receipt
can prove the result, execution stops for review. It does not blindly transcribe,
summarize, upload or index again. Operator review tooling is not implemented yet.

Source validation is its own durable operation, `$inputs`. It must return one
validated artifact per source role, preserving the original ref and format.
Transformation receipts include `role`, `ref`, `media_type`, `sha256`, positive
`size` and `derived_from` source refs. Receipts cannot overwrite an original or
reuse another artifact reference. A protocol may admit arbitrary document types;
this executor version requires one artifact per role. Batch/set adapters need a
future explicit contract rather than silently choosing one input.

Protocol dependency order governs execution. Every transformation must finish
before the terminal index step. Index receipts must confirm exactly all final
references. Completion state is reported as `indexed` only after that receipt is
persisted. This does not prove behavior of an untrusted adapter or make the local
SQLite file a tamper-proof audit service.

## Integration API

An installation-owned adapter is passed explicitly. It is not a job field:

```python
from phantomdocs.workflow_executor import WorkflowExecutor
from phantomdocs.workflow_ledger import WorkflowLedger

with WorkflowLedger(private_ledger_path) as ledger:
    result = WorkflowExecutor(ledger, ledger.path, installed_adapter).run(
        organization_id, request_id, current_org, pinned_processor_actor,
        rebuild_current_authorized_plan,
    )
```

`rebuild_current_authorized_plan(job)` uses the current trusted configuration,
registry, organization and instance default to call the existing planner. The
executor rejects any changed plan hash. The journal retains intake status
(`validated`/`blocked`) separately from actual execution observations.

## Local transformation runner

`workflow_runner.run_transform` executes an explicit descriptor via argv, with no
shell. Bindings must use absolute executable/script paths and matching `sha256`
and `script_sha256` pins. Verify and record these hashes from trusted installed
files; do not add guessed hashes to the AU example.

The caller supplies a private staging snapshot and a fresh output path in the same
unlinked directory, the expected output media type, a positive timeout and output
size limit. The runner checks source bytes remain unchanged, subprocess success
and output size/format. Initial output validators support UTF-8 text/Markdown and
PDF header/end-marker checks. They do not replace PDF rendering QA or rigorous
malware/content validation. Unknown output formats fail until a validator exists.

Timeouts and nonzero exits require reconciliation. Outputs are not automatically
removed or overwritten on retry. Tool diagnostics are discarded to avoid exposing
document contents/credentials. The runner is not an OS/network sandbox: an operator
must configure resource limits, private staging permissions and trusted assets;
the size limit checks the output after execution and does not cap total disk usage.
Models, Node, Chrome and branding assets need adapter-specific pins and checks.
Do not run it on the only copy of an original or let a signed document be restyled
in place. Derived files require explicit publication and reference receipts.

## Reference indexing with no reread on retry

The additive service API `DocumentService.index_reference_once` and CLI option
`--skip-existing-ref` check exact refs under the manifest write lock before reading
external bytes. New references are read once to hash and indexed in their existing
location, without copying into the blob store. Concurrent retries serialize and
reuse the same document identity. Reference skips are not a fresh integrity
verification: use `pd verify` when re-reading current external bytes is intended.

Explicit category and nonempty owners are mandatory. Existing classification,
ownership or logical-folder conflicts fail closed; a slug already used for
another document cannot be silently versioned through this path. An existing ref
with multiple document identities also fails. A different proposed slug for the
same correctly classified ref does not rename the indexed document. No automatic
folder creation, move, rename, deletion or reclassification occurs. The original
`pd add` behavior is retained when the option is omitted.

For an already approved, existing object and existing logical folder:

```bash
pd add --ref "gdrive://$EXISTING_FILE_ID" --skip-existing-ref \
  --slug "$DOCUMENT_SLUG" --folder "$EXISTING_LOGICAL_FOLDER" \
  --category "$DECLARED_CATEGORY" --owners "$DECLARED_OWNER_ONE" \
  --owners "$DECLARED_OWNER_TWO" --actor "$PROCESSOR_ACTOR" \
  --org-yaml "$ORG_YAML" --root "$DOCUMENT_STORE"
```

Values must come from the authorized plan and deployment, not filename guesses.
These are handover commands, not commands executed against AU here. A deployment
index adapter should use this service boundary and return a durable receipt; if
it crashes after indexing, reconciliation can confirm the existing ref without
re-hashing it. Existing historical references are still recovered by the separate
AU recovery utility; this block does not initiate a recovery pass.

## Observations for PhantomView

`pd workflow status --graph` exports the declared graph with timestamped journal
observations after current category authorization. Components report `complete`
or `started-unconfirmed` when a receipt exists, otherwise `not-observed`.
Started does not mean the process is still running. Graphs omit document references
and executable paths. This is the consumer contract; the PhantomView dashboard
integration remains to be implemented.

## Verification and next integration step

Tests cover fake Drive meeting publication, original preservation, branding before
index, restart retries without repeated operations, backend/journal crash windows,
uncertain result refusal, stale pins, processor authorization, lock exclusion,
replanning refusal and authorized graph observations. Actual subprocess tests
check executable/script pins, output validation, source mutation and timeouts.
Reference tests use the real document service and a fake Drive reader to verify
concurrent deduplication, no reread/hash, policy conflicts and CLI behavior.
The full existing product suite must pass on the deployed POSIX target.

Next: implement installation-owned AU source/tool/publication/index adapters with
immutable staging snapshots and durable receipts, validate the supplied scripts
and assets, then connect the PhantomView consumer. Non-meeting AU routing and
classification still require explicit operator policy. Alma migration remains a
separate deployment operation preserving identity and avoiding dual writers.
