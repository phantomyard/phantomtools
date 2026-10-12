# Document workflows: contracts and installed protocol registry

## Execution extension

The subsequent [execution block](WORKFLOW-EXECUTION.md) adds a trusted-adapter
executor API, durable receipts, local command runner and reference deduplication.
The planning/admission CLI still does not execute transformations automatically.

## Implemented scope

The product provides generic, read-only `pd workflow validate` and
`pd workflow plan` commands, plus signed request admission and a private ledger
through `sign-request`, `accept` and `status`. The product consumes PhantomOrg and Phantombot
configuration without editing either. It does not depend on an AU organization,
a particular persona, a department name or a Drive folder embedded in code.

It validates document jobs, resolves installed protocol versions, checks a
declared dependency graph, resolves the default processor and its authorization,
and exports a pinned plan or declared graph. The package ships JSON Schema
definitions and versioned Markdown protocol instructions. A generated persona
wrapper understands `documents.sh workflow plan/sign-request/accept/status` and injects local actor/org
options after the nested command.

It does **not** scan an inbox, provide a network transport, execute tools,
read/hash document bytes, apply branding, run a processing queue, upload/move
Drive objects, change the document store, or migrate personas. Signed requests
authenticate intent using existing declared keys; accepted plans are durable
intake records, not processing-completion records. Plans always contain
`execution_ready: false`; a tool's presence does not prove its version or actual
runtime behavior. This deliberately establishes the common contract before
connecting operational adapters.

The older `fix/au-bandeja-ingest` branch remains a separate, AU-specific recovery
patch. This generic product branch starts from upstream/main and does not deploy
or replace that patch automatically.

## Responsibilities

| Layer | Authority |
| --- | --- |
| PhantomOrg | Actors, roles, logical tools/exclusions, categories, domain/type vocabulary, domain owners and inbox declarations |
| Installed protocol registry | Versioned steps, capabilities, input/output artifact roles, final results and instructions |
| Installation configuration | One shared intake selector, protocol bindings, existing archive destinations, logical folders, branding profile identifier and installed tool bindings |
| Phantombot configuration/state | Initial processor selection via `default_persona` |
| Job contract | Request id, requester claim, explicit target/requesting context and existing input references |
| Signed intake and private ledger | Declared requester signature, validity, atomic admission, conflicts, pinned plans and explicit replanning |
| Future executor | Byte validation, actual transformations, stage retries and completion evidence |
| PhantomView adapter | Read-only visualization of declared dependencies and separately observed execution |

Do not copy executable commands into persona identities or allow jobs to supply
tool paths, category overrides, owners or arbitrary destinations. Those belong
to trusted installation policy and are checked against PhantomOrg. Instruction
files are installed and local, not fetched from a mutable repository branch at
execution time. References may not contain passwords, queries or bearer-token
fragments; temporary delivery credentials belong outside job contracts.

## Job contract

The portable schema is packaged at
`src/phantomdocs/workflow_assets/contract.json`, with definitions `job`,
`registry` and `installation`. Unknown fields and unsupported schema versions
are rejected. Job artifact ids and references must be unique.

```json
{
  "version": 1,
  "organization": "example-org",
  "request_id": "request-001",
  "requester": "project-lead-persona",
  "context": {
    "domain": "projects",
    "document_type": "report",
    "requesting_domain": "projects"
  },
  "artifacts": [
    {
      "id": "source-document",
      "role": "original",
      "ref": "gdrive://EXISTING_FILE_ID",
      "media_type": "application/pdf"
    }
  ]
}
```

`requester` in an unsigned job is a claim. Signed intake binds that claim to
the requester's current declared key and checks authorization before admission. Neither `--actor` nor an
unsigned JSON file establishes remote identity. Planning also checks the
requester's existing category/owner authorization, but cannot prove who submitted
the file. The CLI's local planner actor must be able to read the classification.

Requester, processor and document owners are distinct. A custodian may process a
project's document without becoming its requester. Moving a persona to another
host does not change its actor id or project context.

Three independent concepts must stay separate:

* **Document type:** the declared PhantomOrg domain/type, such as a report.
* **Artifact role:** a protocol-local function, such as original, recording,
  transcription or published-summary. Custom protocols may declare other roles.
* **Security category:** resolved from the domain/type declaration; never
  inferred from file names, roles, departments or requester prose.

PDFs, images, text, audio and other media types are admitted by the same contract.
The reference-document protocol does not require a transcription or summary.
A media type is supplied metadata; an executor must validate actual bytes before
transforming or claiming their format. Unsupported/unconfigured document types
are refused rather than routed by guesswork.

## Registry, dependencies and branding

The built-in registry is installed with the package:

* `reference-document@1.0.0`: original -> reference indexing, with no mandatory
  transformation. Originals of any media type remain unchanged.
* `meeting-publication@1.0.0`: recording -> transcription -> summary; separate
  branding steps produce published transcription and summary; indexing depends
  on both published results. Minutes need an explicit protocol extension.

Each protocol declares initial/final artifact roles and steps with `needs`,
`inputs`, `outputs` and a logical capability. Dependencies must be acyclic and
refer to known steps. Inputs must be supplied initially or produced by an
ancestor. Roles have one producer and cannot overwrite initial originals.
Exactly one terminal `documents.index` step must consume the final results,
after all processing steps. These rules prevent indexing intermediate bytes
before branding or discarding an unrelated processing failure.

The meeting example is a protocol proposal for this architecture, not a claim
that the existing AU transcription/branding scripts implement its interfaces.
It requires a declared branding profile identifier. Profile templates, their
versions, supported media and handling of already branded inputs still need a
validated adapter. Signed originals, third-party invoices and evidentiary files
must not be silently restyled: a separately identified derivative is needed if
policy requires a presentation copy. No branding operation is performed here.

A custom registry JSON can sit next to its versioned Markdown instructions.
Instruction paths must be relative and contained within that registry directory.
The planner pins both the protocol object and instruction bytes. A changed
instruction file produces a different plan hash even if its version label was
not changed; a future executor must reject stale pins rather than silently
replan an in-flight job.

## Default processor and policy checks

The resolver reads `default_persona` from an explicitly supplied Phantombot
state.json when that field is present, otherwise from config.toml. This matches
the precedence in PhantomOrg's existing Phantombot integration. A malformed
state/default is an error, not a fallback to another actor. No automatic account
or config-file discovery is performed.

The resolved actor must have category read/write authorization for the selected
owners and the protocol's root logical tools (for example `documents`), without
matching tool exclusions. Being default does not create permissions. A change
of default produces a new plan; an existing plan retains its recorded processor.
The ledger pins it; changing it requires an explicit authorized `accept --replan`.
Replanning cannot change classification or owners.

Every route selects an exact declared domain/type and protocol id/version.
Category comes only from the type override or domain default and must be a
declared security category. `owner_domains` names explicit declared domains,
with optional `$requesting_domain` substitution. An optional `owners` list may
only select a non-empty subset of those declared owners; no new owner identities
are invented. Unknown/duplicate routes, missing types and denied actors fail
closed. Existing category inheritance remains unchanged.

Tool bindings declare id, version label and a local entrypoint. The planner
checks entrypoint presence but never invokes it, accepts a command from a job,
or emits it in the graph. Version labels are operator declarations and must be
verified by the future adapter before execution. JSON/schema/protocol validation
does not imply compatibility with an existing transcription script.

## AU configuration examples

`examples/workflows/au-installation.json` contains only the approved meeting
rules. The shared intake selects `documents.inboxes.pepa`; this is a planning
example, not a change to the deployed inbox topology. Default processor is read
from Phantombot rather than hardcoded to Pepa.

* Ordinary AU meetings: secretariat/minutes; category from the live org model;
  chief_of_staff plus the requesting domain owners; established AU archive.
* Ordinary ALMAPONIA meetings: projects/almaponia; project_lead and
  chief_of_staff selected from declared owners; established GO archive.

The supplied AU declarations give both ordinary types category-2. Sensitive
project document types are not bound to these routes. Category-2 grants read to
all category-2 actors; event invitations remain in the delivery layer. No
invitee-only gate or correction to project-category inheritance is claimed.

The installation example intentionally leaves tool bindings empty, so its plans
return blocked. The separately reviewed `au-tool-bindings.json` descriptors
record the supplied interfaces without activating them. See
[authenticated intake and adapter review](WORKFLOW-INTAKE.md). Recording IDs are explicit placeholders,
not live objects, and planning never downloads them. No non-meeting AU routing
or new categories are configured. Another installation declares its own actors,
domains, intake, routes, branding and bindings with the same generic contract.

## Read-only commands

From a development checkout:

```bash
python3 -m venv .venv
.venv/bin/pip install './phantomdocs[dev]'
.venv/bin/pd workflow validate job phantomdocs/examples/workflows/au-meeting.json
.venv/bin/pd workflow validate installation phantomdocs/examples/workflows/au-installation.json
.venv/bin/pd workflow validate registry phantomdocs/src/phantomdocs/workflow_assets/registry.json
```

For an operator planning against their installed AU model, set the real,
readable Phantombot config/state paths for the selected instance/home:

```bash
pd workflow plan \
  --job phantomdocs/examples/workflows/almaponia-meeting.json \
  --installation phantomdocs/examples/workflows/au-installation.json \
  --org-yaml /opt/phantomdocs/org.yaml --actor pepa \
  --phantombot-config "$PHANTOMBOT_CONFIG" \
  --phantombot-state "$PHANTOMBOT_STATE"
```

Omit `--phantombot-state` if that installation uses config.toml only. The local
actor context is not a remote login. A regenerated persona wrapper can supply
actor/org for `documents.sh workflow plan`; it is not regenerated on the VPS
by this change. These commands require the updated package installed in the
selected test environment; do not replace the live venv as part of a design test.

Exit status: 0 = planned/structurally valid, 1 = invalid or unauthorized,
2 = a valid plan blocked on missing tool capabilities. Exit 0 still does not
mean execution-ready. Add `--graph` to emit the declared dependency contract.
Redirect stdout to a file when needed; the planner itself does not write one.

## PhantomView and execution roadmap

The graph output carries organization/request id, plan hash, classification,
processor, step components and dependency relations. It deliberately omits raw
artifact references and tool entrypoints. `observation: declared-only` and
`runtime: not-observed` prevent a declared mapping from looking like a running
service. This is an adapter contract, **not** an already supported PhantomView
installation manifest or a deployed dashboard integration.

Next steps, in order:

1. Verify installed tool versions and assets against the reviewed AU interfaces.
   Signed intake and durable plan admission are implemented; the same request
   id with different intent is a conflict and identical retries retain pins.
2. Extend the intake ledger with executor leases and actual stage states before
   claiming processing, complete-artifacts, indexed, needs-review or failed.
3. Add validated adapters that preserve originals, record derived-from links,
   publish a complete artifact manifest and deduplicate producer results by
   request/role/revision as well as index refs by Drive id. Do not pass a stale
   plan directly to an executor after changing org/config/default persona.
4. Add a read-only PhantomView consumer for declared dependencies plus timestamped
   ledger observations, with category-based access filtering for each viewer.
5. Test AU and ALMAPONIA end to end and retry interrupted stages before deployment.
6. Plan Alma's optional move to the VPS separately: preserve the identity/bound
   vault, memory, tool config and role; stop dual writers; verify before cutover.
   Remote requesters must remain supported regardless of this host choice.
7. Recover historical refs without moving them; do not reinterpret the old
   marker files as workflow completion records.

No implementation step may invent non-meeting AU destinations or expand access
to make a default processor succeed. Such policy decisions require the operator.
PhantomDocs remains the index and integrity layer, not a video transcription
engine or a replacement for PhantomOrg.

Validation uses the packaged draft-2020-12 schema through
[jsonschema](https://python-jsonschema.readthedocs.io/en/stable/validate/).
TOML uses [tomllib](https://docs.python.org/3/library/tomllib.html) on Python
3.11+, with the declared tomli dependency for supported Python 3.10 installs.
