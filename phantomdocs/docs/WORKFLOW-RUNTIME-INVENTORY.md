# Read-only runtime inventory before AU activation

## Purpose and limits

`pd workflow inventory` observes SHA-256, size, mode and resolved paths for explicitly
declared executable/script/probe/resource files. It does not launch tools, import
deployment scripts, call Drive, discover credentials, scan directories or change
configuration, permissions, ledgers, work directories, stores or defaults.

This fills the runtime-data gap without allowing the implementation team to access
the live VPS. The deployment team runs the command under the intended processor
account and supplies the sanitized JSON report. Only that team can verify the
actual installed files and resource inventory against its trusted deployment source.

A matching hash is an observation, not automatic approval. Reports always retain
`execution_ready: false` and `requires_operator_review: true`. They explicitly leave
source provenance, imported dependencies, unlisted models, rendering/metadata QA,
credentials/disclosure policy, OS quotas and live compatibility unverified.
No package version label is treated as verified runtime proof.

## Select the files explicitly

Use a reviewed installation JSON, or combine a base installation with the provided
AU tool descriptors and an operator-completed processing profile. The optional
`--tools-file` and `--processing-profile` overlays affect only the in-memory candidate
being inspected. They never rewrite the base installation or save a candidate.
The report includes both the original and candidate installation hashes.

For the processing profile, select actual model/prompt/template/logo/renderer and
other required resource files. A directory is refused as a resource; list the real
files rather than asking the inventory command to scan a deployment tree.
Missing/zero pins are reported with observed hashes where possible. They remain
blockers until reviewed pins are explicitly placed in the installation data.

Known AU script paths come from the supplied interfaces. Unknown model/probe/work
paths must be supplied by the operator, not invented. The repository's profile
scaffold is inactive and intentionally contains unknown paths/zero hashes.

Credential-named files such as `.env`, access tokens, credential JSON and private
key files are excluded before their contents are opened. Their paths are omitted
from observations. The command does not need a Drive token, org signing key or
credentials. Do not place secrets in argv or resource descriptors. Unusually named
credential files still must be excluded by the operator; no filename filter can
prove that arbitrary resources contain no secrets.

## Run from an isolated reviewed environment

Use the exact branch commit in the accompanying handover report. A separate audit
environment avoids replacing the production venv or changing live services:

```bash
# Deployment team only; choose separate checkout/environment locations.
python3 -m venv "$AUDIT_VENV"
"$AUDIT_VENV/bin/pip" install "$REVIEWED_CHECKOUT/phantomdocs"

umask 077
PYTHONDONTWRITEBYTECODE=1 "$AUDIT_VENV/bin/pd" workflow inventory \
  --installation "$REVIEWED_INSTALLATION_JSON" > "$PRIVATE_RUNTIME_INVENTORY"
```

The setup commands write only the chosen audit environment. The inventory command
itself observes the declared runtime files. Redirection explicitly saves the report
in an operator-selected private location. Run with the intended processor account
so work-directory ownership/access checks reflect its actual runtime context.

If the base installation deliberately has empty tool bindings, inspect explicit
candidate overlays without editing it:

```bash
PYTHONDONTWRITEBYTECODE=1 "$AUDIT_VENV/bin/pd" workflow inventory \
  --installation "$REVIEWED_CHECKOUT/phantomdocs/examples/workflows/au-installation.json" \
  --tools-file "$REVIEWED_AU_TOOL_DESCRIPTORS" \
  --processing-profile "$OPERATOR_PROCESSING_PROFILE" \
  > "$PRIVATE_RUNTIME_INVENTORY"
```

Operator variables are explicit paths, not already deployed state. The tools
file may start from `examples/workflows/au-tool-bindings.json`; the processing
profile must replace placeholders with actual intended paths. You can observe
files with missing hashes first; this returns a report with blockers rather than
silently approving or filling them.

Exit codes:

- 0: declared file pins match and the selected private directory is present;
  operator review and compatibility testing remain required.
- 1: the configuration/report cannot be loaded or structurally validated.
- 2: the inventory completed but has missing, unconfigured, mismatched, unreadable,
  excluded, non-regular, non-absolute or non-executable entrypoints, or an
  unavailable private work root.

Reports do not create a missing work directory or fix its mode. A symlinked
interpreter may be observed through its resolved target, matching the runner's
binary-byte pin behavior. File changes observed during hashing are reported as
`changed-during-read`; do not use a candidate hash from that attempt.
Identical resolved files are hashed once within an invocation. Memory use remains
bounded by a 1 MiB read buffer; large model files can take time to inventory.

## Review and next steps

1. Review the declared paths and observation statuses against trusted deployment
   source. Resolve missing/unconfigured hashes and list omitted resources.
2. Explicitly update the candidate installation outside this read-only command.
   Reinventory it; never promote observed bytes to trusted bytes merely because
   they were found on disk.
3. Inspect imported dependencies, rendering metadata and actual output quality,
   resource controls, external-summary disclosure policy and credentials separately.
4. Accept/replan a fresh authorized job before execution starts; the processing
   profile participates in its installation pin. Never replace an in-flight plan.
5. Run isolated AU and ALMAPONIA trials using the real tools, interrupt and retry,
   then validate the independent archive receipt and category-filtered reads.
6. Switch production services only after deployment review. No timer or live
   workflow is changed by inventory generation.

Supply a sanitized report through the deployment repository if requested. Review
its absolute paths/modes before sharing; no report is published automatically.
Actual PhantomView UI integration also requires its accessible repository/interface:
it is absent from this checkout. Existing authorized graph exports are the product
boundary; no PhantomView-specific manifest, URL or code location is guessed here.
