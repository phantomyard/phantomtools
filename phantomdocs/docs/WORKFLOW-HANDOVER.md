# Processing, archive and reference-index handover

The [installed processing adapter](WORKFLOW-PROCESSING.md) now provides explicit
`pd workflow prepare`. Earlier non-executing commands retain their boundaries;
AU runtime configuration and archival publication are not activated automatically.


## Responsibilities

The processing pipeline, organizational archiver and PhantomDocs index exchange
explicit manifests. They remain independently deployable. PhantomOrg continues
to supply actors, categories, owners and document context; it is never modified.
Phantombot's selected instance supplies the initial default processor, independently
of the requester and the department/project responsible for the document.

1. Phantomeet supplies the recording and meeting order. Its requester and
   department/project context must be mapped explicitly into the authorized job.
   The title and other order metadata remain available to the external archiver;
   the job schema does not invent title or sequence fields. Do not fall back to a
   department to make missing context pass validation.
2. Processing uses `WorkflowExecutor.prepare` with an installed trusted adapter.
   It validates inputs, performs the declared transformations and preserves the
   originals. Completed journal receipts are reused on retry. `prepare` stops
   before the terminal index step and exports a processed-results manifest.
3. The existing organizational archiver assigns operational names/sequences and
   publishes derivatives to the authorized archive. It retains the actual Drive
   file IDs and supplies a role/ref/slug mapping. PhantomDocs does not upload,
   move, rename, delete, create folders or assign AU archive sequences.
4. PhantomDocs binds that explicit mapping to its authoritative results and
   independently verifies folder membership, size and SHA-256 before indexing
   existing references. Classification and owners come from the pinned plan.
5. PhantomView can consume authorized declared/observed graph exports. Its actual
   dashboard consumer is still a separate integration.

The product is generic. AU configuration examples keep only the two approved
meeting routes and ordinary category/ownership policy. They now select additive
`meeting-processing@1.0.0`, producing `branded-transcription` and `branded-summary`.
The previous `meeting-publication@1.0.0` version remains available. Other document
types can use other declared protocols; no non-meeting AU route is introduced.

## Manifests and authority

A processed-results manifest contains organization/request id, plan hash, final
artifact roles, references, SHA-256, byte sizes, media types and lineage. Its
`results_hash` binds the entire manifest. `pd workflow results` recomputes this
from completed processing receipts in the private ledger, without reading source
bytes or changing the ledger. An incomplete or mismatched journal is refused.

The archiver supplies an explicit mapping, for example:

```json
[
  {
    "role": "branded-transcription",
    "ref": "gdrive://ACTUAL_ARCHIVED_TRANSCRIPTION_ID",
    "slug": "approved-transcription-slug"
  },
  {
    "role": "branded-summary",
    "ref": "gdrive://ACTUAL_ARCHIVED_SUMMARY_ID",
    "slug": "approved-summary-slug"
  }
]
```

IDs and slugs are operator/archiver values, not placeholders to deploy. Optional
`name` and `md5` fields record existing operational evidence. There are no category,
owner or destination overrides in this mapping. `archive-receipt` adds the pinned
archive ref, hashes and sizes from authoritative results. The receipt is trusted
local operator intent; it is not remote authentication or proof of a completed
upload. The consumer verifies its claimed Drive objects independently.

AU's `AU-<date>-<seq>_<dept>_<title>_EVID.<ext>` convention remains with the existing
archiver. The supplied source says sequence allocation happens during archival;
no allocation interface exists. The product does not infer sequences from observed
examples or use PhantomOrg's communication request-id format as a filename rule.
File names never determine access policy or duplicate detection.

Workflow operation keys identify local journal steps. They are not a Drive lookup
API. Existing upload response loss cannot be safely resolved by a guessed name or
an invented operation-ID query. MD5/size can support operator reconciliation, but
MD5 alone is not authenticated request provenance; ambiguous matches require review.
No upload is retried by this consumer. No `q=md5Checksum` query is used.

## API and commands

Use the same authorized plan and protected ledger created by the intake commands.
The installed processing adapter must implement preflight/perform/reconcile and
must not combine upload/cleanup with a transcription or branding step. The
repository supplies the executor and local runner boundaries, not an automatically
activated AU processing adapter or a replacement for the external archiver.

```python
from phantomdocs.workflow_executor import WorkflowExecutor
from phantomdocs.workflow_ledger import WorkflowLedger

with WorkflowLedger(private_ledger_path) as ledger:
    result = WorkflowExecutor(ledger, ledger.path, processing_adapter).prepare(
        organization_id, request_id, current_org, pinned_processor_actor,
        rebuild_current_authorized_plan,
    )
# result["state"] == "awaiting-archive"; result["results"] is the handover manifest.
```

The processor writes its result to a private file for the independent archiver.
An authorized reader can also export it later without rerunning processing:

```bash
umask 077
pd workflow results --request-id "$REQUEST_ID" --ledger "$PRIVATE_LEDGER" \
  --org-yaml "$ORG_YAML" --actor "$AUTHORIZED_READER" > "$RESULTS_MANIFEST"
```

After the existing archiver has assigned names and retained the actual Drive IDs,
write its explicit role/ref/slug mapping to `$ARCHIVE_MAPPING`. Bind it to results:

```bash
pd workflow archive-receipt --request-id "$REQUEST_ID" \
  --mapping "$ARCHIVE_MAPPING" --ledger "$PRIVATE_LEDGER" \
  --org-yaml "$ORG_YAML" --actor "$PROCESSOR_ACTOR" > "$ARCHIVE_RECEIPT"
pd workflow validate archive-receipt "$ARCHIVE_RECEIPT"
```

Consume that receipt with the pinned processor and an explicit private OAuth
access-token file authorized to read the archive:

```bash
pd workflow index-archive --receipt "$ARCHIVE_RECEIPT" \
  --ledger "$PRIVATE_LEDGER" --org-yaml "$ORG_YAML" \
  --actor "$PROCESSOR_ACTOR" --root "$DOCUMENT_STORE" \
  --drive-token-file "$PRIVATE_DRIVE_ACCESS_TOKEN_FILE" \
  --max-reference-bytes "$MAX_REFERENCE_BYTES"
```

Use an existing protected actor key through `--nsec-file` if store mutation signing
requires it. The Drive token file contains only an OAuth access token, mode 0600
and caller-owned on POSIX. No credentials enter manifests or the repository.
Windows requires equivalent private filesystem ACLs. The reader implements no
credential discovery or refresh; the deployment owns token issuance/refresh.
Set a positive byte limit appropriate to the explicitly approved input set.
Tokens are not written to the store, receipts, output or diagnostics.

The reader uses only HTTPS GET against the fixed Drive files endpoint, refuses
redirects carrying Authorization, reads bounded metadata/media and checks binary
sizes. Parent membership is verified from metadata; new refs are downloaded and
SHA-256 checked inside the document service before its manifest commit. Native
Google Docs without a binary size are refused; an export adapter is required.
The declared logical folder must already exist and is different from the Drive
archive ID. No folder is created by receipt consumption.

The Google [files resource](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)
describes metadata fields; [files.list](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list)
describes listing/pagination for external operator reconciliation. This consumer
uses explicit file IDs and does not search for a file by a name or checksum query.

## Retry and conflict behavior

Each indexed artifact has an independent durable journal row bound to the complete
archive receipt and the destination store identity. Exact retries check current
org/write authorization and recorded node policy/hash/ref, then skip Drive GET,
download and byte hashing for confirmed artifacts. Repeating `index-archive`
therefore does not create versions or copied blobs.

If indexing commits before its receipt row is completed, retry verifies metadata
and uses the service's locked reference deduplication. It finds the existing node
without redownloading or re-hashing. A partial set can resume its missing indexes;
confirmed nodes stay unchanged. This is index reconciliation, not upload replay.

Changed refs, slugs, manifest intent, store identity, categories, owners, SHA-256
or archive destinations conflict rather than silently reclassifying or redirecting.
Duplicate roles/refs/slugs, missing final artifacts, changed policy, wrong processor,
wrong actual Drive parent, missing binary size and mismatched bytes fail closed.
The complete unconfirmed metadata set is checked before document mutations begin;
external failures after that check can still leave an explicitly partial index.
Missing previously completed nodes require review rather than automatic rereads.

`pd workflow status --graph` overlays archival index observations on processing
receipts, without exposing file refs or tool paths. A started observation is an
unconfirmed step, not a statement that a process is currently running.

## Installation and AU scope

Install/test the branch at the exact commit recorded in the handover report in a
separate checkout/environment first. Operator deployment of this additive package:

```bash
# Run from the reviewed checkout at the handover commit; deployment team only.
sudo /opt/phantomdocs/venv/bin/pip install ./phantomdocs
```

This does not patch the external processing/archiving scripts, regenerate live
persona wrappers, switch systemd timers or migrate Alma. The deployment team
connects those scripts at the manifest handover points shown above. AU retains
`/opt/phantomdocs/org.yaml` and `/opt/phantomdocs/store`; a real private workflow
ledger path must be selected and owned by the processor. No path is invented as
already deployed. Do not use the inherited Phantombot instance default for this
pipeline; select the intended instance/home and let its declared default resolve.

Alma's location is independent of this design: her signed requester identity and
project context remain intact whether remote or on the VPS. Ordinary ALMAPONIA
meeting policy selects project_lead + chief_of_staff and category-2; category-2
read visibility remains broader than event invitations. Invitations remain in the
existing delivery layer. This block adds no invitee-only gate or project-category
inheritance correction.

Historic documents without workflow processing records use the separate existing
AU recovery utility/runbook; do not fabricate manifests or completion receipts
for them. No recovery, Drive operation or VPS deployment was run by this change.
All code/tests/docs/commit messages are English; external script flags and existing
actor/domain names remain literal for compatibility.
