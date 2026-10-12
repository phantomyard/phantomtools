# Installed processing adapter

Use the [read-only runtime inventory](WORKFLOW-RUNTIME-INVENTORY.md) to observe
operator-declared installed file hashes before completing the processing profile.
Matching observations do not replace deployment review or compatibility testing.


## Implemented scope

`pd workflow prepare` runs the generic `ProcessingAdapter` against an already
accepted, pinned job. It downloads authorized binary inputs into private staging,
validates their media, runs explicitly declared transcription/summary/branding
argv, persists backend receipts and exports the processed-results handover.
It stops before archive publication or reference indexing. It does not scan the
inbox, create requests, resolve missing meeting-order metadata, assign archive
sequences, upload, move, rename, delete, modify PhantomOrg or migrate personas.

The supplied AU tool descriptors match the interfaces provided by the operator;
no live AU script, model, brand asset or API was executed here. Actual compatibility,
including document metadata/rendering and credentials, still needs deployment QA.
The package's AU installation example leaves bindings empty. The profile scaffold
has explicit placeholder paths and zero hashes, so it cannot activate by itself.

`examples/workflows/au-tool-bindings.json` is an interface sketch, not executable
configuration: its missing `sha256`/`script_sha256` pins are rejected by the runner.
The additive `au-tool-bindings.inventory-20261012.json` supplies operator-reported
processing pins, but still lacks the indexer pin and complete runtime resources.
Neither file is sufficient to run `prepare` without completing deployment review.

## Configuration and runtime checks

Add an explicit `processing` object to the trusted installation before accepting
a job. Its work_root, time/byte limits, input validators, output formats and pinned
resource list participate in the installation hash. A changed profile requires
an intentional replan before execution begins; it cannot silently replace a profile
for an in-flight job. The adapter checks the installation pin against the plan,
absolute executable/script hashes and every declared resource before processing.
Tool selection comes from installation data, never from the job or persona prose.

`examples/workflows/au-processing-profile.json` is an inactive scaffold:

- Select an existing absolute, unlinked, caller-owned private work directory.
  On POSIX it must have mode 0700. Windows requires equivalent private ACLs.
- Choose time/input/output limits appropriate to the deployment.
- Fill actual trusted executable/script SHA-256 pins in the tool descriptors.
- Supply a verified media probe path/hash for MP4 recordings and its declared
  container list. No ffprobe installation path is assumed to exist.
- List the actual trusted model weights, prompts, rendering scripts, logos,
  templates and other required files in resources with their hashes.
- Verify the selected tools' complete environment and imported dependencies.
  The adapter checks declared files; it cannot discover or prove unlisted
  dependencies, GPU compatibility, renderer configuration or model provenance.

The reviewed AU bindings use `transcribe.py INPUT --model turbo --out OUTPUT`,
`summarize.py INPUT --out OUTPUT`, and `acta_a_pdf.py INPUT OUTPUT --clase ...`.
External class values `transcripcion`/`resumen` remain literal. No implicit
`--summarize`, monolithic post-meeting/process_recording command, upload or cleanup
step is added. Diarization/fusion/minutes require separate declared adapters;
this block does not silently run them. Review source permissions and API disclosure
policy before enabling a summary tool that uses an external service.

Branding still uses the installation's renderer and assets. Meeting titles and
other order metadata are not invented from private staging names; configure and
validate the renderer's required metadata integration during AU deployment QA.
Final archive naming/sequence allocation remains with the independent archiver.
Do not regard the profile scaffold as a completed AU runtime configuration.

## Source boundary and format validation

The installed Drive reader must confirm the exact file ID, non-trashed state,
parent membership in the pinned organizational intake, binary size and declared
MIME type before any media download. All sources are checked before staging.
A requester cannot use this adapter to read arbitrary server file paths or another
Drive folder just because their category permits reading the job. Shared intake
membership is necessary; deployment intake provenance/request binding remains
responsible for proving which authorized requester supplied each source.

For AU, the operator selected a prior upload to an approved Drive intake rather
than a local Jibri adapter. The candidate still selects the declared Pepa inbox
(`documents.inboxes.pepa`). A recording on the Jibri filesystem is not a valid
input to this adapter. Before submitting its signed job, the deployment must
publish it as a binary Drive file directly in the selected intake, retain its
actual Drive ID and bind that ID to the authenticated meeting request. This
package does not upload recordings or infer a different intake folder. Confirm
the intake selection and upload/request provenance in the deployment trial.

MIME tolerance is explicit installation policy: list approved alternatives such
as `video/quicktime` in `inputs.recording.media_types` and retain a pinned probe
and allowed container list. The signed artifact MIME must match actual Drive
metadata exactly; extension-based guesses and blanket `application/octet-stream`
fallback are not accepted. Actual bytes must also pass the declared validator.
Adding a permitted MIME/profile changes the installation pin and requires a
fresh accepted plan or an explicit replan before execution starts.

The Drive reader intentionally refuses all HTTP redirects, including `alt=media`.
Deployment trials must include the intended large recording sizes. If Drive
requires a redirect, the download fails closed; no credentials are forwarded.
Do not disable the redirect handler as a workaround. Any future redirect support
requires a reviewed destination/authentication policy and regression tests.

Downloads use the bounded read-only Drive bridge. Private source snapshots retain
original refs in their receipts; local paths are internal working copies, never
new Drive objects or PhantomDocs blobs. Every new processing step checks its
recorded input snapshot hash and size, then copies it into that step's private
working directory. A tool can alter that working copy only by causing a failure;
it never receives the original Drive object or the only retained source snapshot.

Native validators support UTF-8 text/Markdown, bounded PCM WAV and basic PDF
header/end-marker checks. Other audio/video needs a hash-pinned probe returning
ffprobe-compatible JSON with an allowed container and appropriate stream type.
See the official [ffprobe documentation](https://ffmpeg.org/ffprobe.html).
These are format checks, not full media decoding, malware scanning or publication
rendering QA. Unknown source formats fail closed until a validator is installed.
The one-input/one-output adapter supports the declared transcribe, summarize and
brand capabilities; unsupported capabilities or multi-input/output steps fail.

## Durable processing and retries

A separate private `processing.sqlite` cache in work_root retains operation-bound
receipts and snapshot mappings. File contents and directory entries are synced
before receipts commit. The cache is protected and refuses unrelated databases.
Host-local locks serialize operations sharing a work directory; the workflow
ledger separately serializes its authoritative runner. Multi-host shared writers
remain unsupported.

A backend receipt commits before the executor confirms its step. If the process
stops between those commits, `reconcile` returns that receipt without repeating
the subprocess or downloading the input. Confirmed executor steps are skipped
on exact retry. Executable/resource pins are still checked; this does not require
re-hashing already completed document bytes.

If files exist but no committed backend receipt establishes success, the adapter
refuses to overwrite or rerun them. A timeout, failed tool or partial snapshot may
therefore require operator review. It does not automatically delete intermediate
files or treat an apparently complete PDF as proof that a failed process finished.
Retain staging until the independent archiver has consumed and verified results.

The local runner enforces explicit timeouts and checks source preservation,
nonempty output, output-size limit and supported output format. It is not an OS
or network sandbox; total disk use, CPU/GPU/memory limits, child processes,
credentials, side effects and the trusted tool's dependencies are deployment
responsibilities. Outputs larger than the configured limit are rejected after
execution, not prevented from using disk while a tool runs. Tool diagnostics
are discarded to avoid leaking transcript text or credentials.

## Operator trial

Prepare a reviewed installation JSON with actual tool hashes, processing profile
and resources. Accept a signed job against that exact installation first, using
the intake runbook. Run as its pinned, authorized default processor:

```bash
umask 077
pd workflow prepare --request-id "$REQUEST_ID" --ledger "$PRIVATE_LEDGER" \
  --installation "$REVIEWED_INSTALLATION_JSON" \
  --org-yaml "$ORG_YAML" --actor "$PROCESSOR_ACTOR" \
  --phantombot-config "$INSTANCE_CONFIG" --phantombot-state "$INSTANCE_STATE" \
  --drive-token-file "$PRIVATE_DRIVE_ACCESS_TOKEN_FILE" > "$PREPARATION_RESULT"

pd workflow results --request-id "$REQUEST_ID" --ledger "$PRIVATE_LEDGER" \
  --org-yaml "$ORG_YAML" --actor "$PROCESSOR_ACTOR" > "$RESULTS_MANIFEST"
```

Omit state only for an installation using config-only defaults. A custom registry
can be selected with `--registry`. The Drive token is an explicit private access token file, not a repository secret or a credential discovery mechanism.
`prepare` resolves the configured instance default and refuses a different caller,
stale plan or missing pins. It does not switch the default persona.

The result state is `awaiting-archive`. Give the separate results manifest and
its actual local artifact paths to the authorized archiver. Then follow
[processing/archive handover](WORKFLOW-HANDOVER.md) to bind final Drive IDs and
consume the archive receipt. Do not feed private staging names to the AU archive
as if they were canonical evidence names.

The first deployment trial should include one AU meeting and one ALMAPONIA meeting,
then interruption/retry and read access checks with Pepa and Alma. The repository
tests use a real WAV, actual subprocesses implementing the supplied argv interfaces,
a fake Drive reader and synthetic PDF output. They verify orchestration and crash
recovery, not Whisper/DeepSeek/Chrome behavior or AU visual branding in production.
No VPS, live store, Drive object or persona was changed by this implementation.
