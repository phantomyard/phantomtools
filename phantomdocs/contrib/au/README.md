# AU meeting ingestion and recovery

This is **AU deployment glue**, shipped in the fork for review and regression
tests. It replaces AU's local `/opt/phantomdocs/bin/ingest-bandejas.py`; it does
not add a `pd` command, change PhantomDocs ACLs, or edit PhantomOrg. No product
CHANGELOG entry is needed because product behavior and packaging are unchanged.
All deployment commands below are for the operator. The author did not execute
them on the VPS or against Drive.

## Diagnosis and exact grouping rule

The old key removed only the extension, so `X.txt`, `X.resumen.md`, `X.acta.md`,
and `X.transcripcion.pdf` became four groups. No group satisfied its transcript
plus summary requirement. The old code also omitted `--category` (new nodes
would default to category-1), trusted seen markers alone, did not paginate
Drive results, and returned exit 0 even after failed adds.

For recognized legacy artifacts, the new key:

1. Removes the final extension, case-insensitively: txt, md, or pdf.
2. Removes at most one terminal `_YYYY-MM-DD-HH-MM-SS` timestamp and at most
   one terminal dot-class (`.transcripcion`, `.resumen`, `.acta`), in either
   order. Both `X_TIMESTAMP.resumen.md` and `X.resumen_TIMESTAMP.md` become X.
3. Applies Unicode NFC and casefold. It preserves other dots, title words,
   meeting dates, and punctuation; it does not use lossy slugification.

Class names map to transcription, summary, and minutes. An unclassified `.txt`
is a transcription. Unknown classes/extensions are held. The group identity
includes the source and project flag, preventing unrelated inboxes from merging.
An eligible group must contain transcription AND summary; minutes are optional.
A `.transcripcion.pdf` counts as transcription. Already indexed files still
participate in completeness, allowing partially completed batches to resume.

AU branding `AU-<date>-<seq>_<dept>_<title>_EVID.<ext>` is preserved in Drive.
`_EVID` does not identify artifact class or a shared meeting key. Such names
require an operator catalog with an exact file id, current name, source,
meeting key, class, and project flag. No naming or branding conversion occurs.
The manifest slug is normalized and includes an injective encoding of the Drive
id, so duplicate names/case differences do not create versions of another file.

## Approved classification and access

The operator explicitly approved the following policy in this task:

| Material | Declared type | Category | Write owners |
| --- | --- | --- | --- |
| Ordinary AU meeting transcription/summary/minutes | secretariat/minutes | category-2 | chief_of_staff plus requesting domain's declared owners |
| Ordinary ALMAPONIA/GO meeting artifacts | projects/almaponia | category-2 | project_lead, chief_of_staff |

The sanitized model was read in the signed-in browser at
`salvaalba-dev/aquaponics-deploy`, commit
`aacc580419be9cf54ef00200c3f4487c09e092c0` (2026-10-12).
`documents.naming.domains` declares secretariat category 2, owner chief_of_staff,
type minutes; projects category 2, owners project_lead/chief_of_staff/cfo,
type almaponia. The approved meeting policy selects project_lead and the
custodian, rather than adding cfo to every project meeting. Finance/training/
marketing domain categories do not override the approved minutes classification;
only their owners identify the requesting department. Each owner must be an
actual declared role/actor. Missing, changed, or unauthorized policy is refused.

Alma is actor alma, role project_lead, level-2 with categories [1,2] and actor
exception category-4-almaponia. Pepa is chief_of_staff, level-2 [1,2], with role
exceptions category-3/category-4. Thus category-2 artifacts are readable by Alma
and writable by Pepa as custodian. Owners govern writes, **not readers**.
The utility calls the existing `can_read`/`can_write` resolver; it does not
manufacture grants or mutate the org model.

**Per-event invitations are enforced in the delivery layer, not PhantomDocs.**
Category-2 indexing grants read/search to all category-2 actors, currently all
eight AU roles. This is the operator-approved minimum (option A), not a technical
invitee-only gate. Invitations, expiring recording tokens, emails, and delivery
are outside this utility. Do not describe category-2 as "only meeting invitees".

Sensitive almaponia-budget/contracts/partners/identifications retain their
declared category-4-almaponia policy and are outside this ordinary-meeting
utility. Do not select them in a category-2 recovery catalog. Also note that
the supplied model has no explicit category parents: current PhantomDocs can
inherit category-4-almaponia from category-4 using its legacy prefix hierarchy.
Consequently it does **not** enforce the stated separate explicit-project-grant
norm. This patch does not change sensitive categories or that product behavior.

## Sources, destinations, and idempotency

Regular scans retain the existing direction, secretariat, finance, volunteering,
marketing, training sources, resolving their actual IDs from
`documents.inboxes` (paco, pepa, roberto, voluntariado, carla, elena).
ALMAPONIA scans its canonical archive, not Alma's 00_INBOX:

| Meeting | Established Drive destination (no moves performed) |
| --- | --- |
| ALMAPONIA/GO | 01_REUNIONES_GO, 1VnnY_IqQaaFJ9gL6imuuskd6G1_C85yN |
| Other AU | 0211_Reuniones_AU, 1u0MWy4VtoNTwSzW5n4MsPFOkCYHgB0Mq |

These are Drive destinations, not PhantomDocs folder identifiers. Index entries
continue under the existing PhantomDocs `reuniones` folder (INGEST_FOLDER may
override it). Destination routing, moves, renames, deletes, uploads, non-meeting
routing, and inbox/ACL changes are not performed. An ALMAPONIA-named artifact
in another inbox requires an explicit catalog rather than guessed routing.

The metadata client uses Drive's read-only scope, follows all page tokens, and
refuses incomplete searches. Only `pd add --ref gdrive://ID` reads bytes for the
existing MAC/hash workflow; it stores the reference, not a document blob. The
product may use temporary staging during hashing; this is not an archive copy.
There is no `--backend` upload or Drive write request. See the primary
[Drive list reference](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list).

Before every add, the utility reads the validated manifest and looks for the
Drive id in current AND historical document locations. A matching indexed ref
skips the subprocess entirely: no download, hash, duplicate, or new version,
even if its name changed or its seen marker is missing. Existing classification
or ownership conflicts stop for review. Orphan seen markers also stop; they
must not silently suppress a missing document. A success marker is written only
after the expected ref/classification is observed in the manifest. Failures
return nonzero and can be retried. Partial batch commits are retained.

Timer and recovery share a Linux flock in the seen directory. Keep the same
root/state for both. Pause unrelated manual writers during recovery: `pd add`
outside this utility does not participate in its ref-deduplication lock. Drive
edits under an existing id are deliberately not rehashed; explicit document
versioning is a separate operation. Do not reset seen markers as a backfill step.
Dry-run writes neither markers, a lock, nor store content, and reads no bytes.

## Operator deployment commands

Run these on the VPS as an administrator. They preserve the installed product
and live org model. No store initialization, reset, or folder recreation occurs.
The existing service is assumed to be phantomdocs-ingest.service, paired with
the provided phantomdocs-ingest.timer; check its current ExecStart first.

```bash
set -euo pipefail
sudo systemctl cat phantomdocs-ingest.service phantomdocs-ingest.timer
git clone --branch fix/au-bandeja-ingest --single-branch \
  https://github.com/salvaalba-dev/phantomyard-tools.git /tmp/au-ingest-source
cd /tmp/au-ingest-source
git log -1 --format='%H %s'
python3 -m venv /tmp/au-ingest-tests
/tmp/au-ingest-tests/bin/pip install './phantomdocs[dev]'
/tmp/au-ingest-tests/bin/python -m pytest phantomdocs/tests/test_au_ingest.py -q
sudo /opt/phantomdocs/venv/bin/python -c \
  'from phantomdocs.manifest import load, node_by_path, node_by_slug; from phantomdocs.access import can_read, can_write'
sudo /opt/phantomdocs/venv/bin/pip install google-auth requests
sudo systemctl stop phantomdocs-ingest.timer
sudo systemctl stop phantomdocs-ingest.service
sudo cp -p /opt/phantomdocs/bin/ingest-bandejas.py \
  /opt/phantomdocs/bin/ingest-bandejas.py.before-au-fix
sudo install -m 0755 phantomdocs/contrib/au/ingest_bandejas.py \
  /opt/phantomdocs/bin/au-ingest.py
sudo tee /opt/phantomdocs/bin/ingest-bandejas.py >/dev/null <<'PY'
#!/usr/bin/env python3
import os
import sys
base = '/home/pb-pepa/.local/share/phantombot/personas/pepa'
args = [
    '--org-yaml', '/opt/phantomdocs/org.yaml',
    '--root', '/opt/phantomdocs/store',
    '--docs', base + '/tools/documents.sh',
    '--actor', 'pepa', '--state', '/var/lib/phantomdocs-ingest/seen',
    '--folder', os.environ.get('INGEST_FOLDER', 'reuniones'),
    '--service-account', base + '/.secure/google-service-account.json',
    '--subject', os.environ.get('INGEST_SUBJECT', 'secretaria@aquaponicsunited.com'),
]
os.execv(sys.executable, [sys.executable, '/opt/phantomdocs/bin/au-ingest.py',
                        *args, *sys.argv[1:]])
PY
sudo chmod 0755 /opt/phantomdocs/bin/ingest-bandejas.py
sudo -u pb-pepa env PATH=/opt/phantomdocs/venv/bin:/usr/local/bin:/usr/bin:/bin \
  /opt/phantomdocs/venv/bin/python /opt/phantomdocs/bin/ingest-bandejas.py --dry-run
```

Review dry-run output: category-2 and approved owner roles must be explicit.
Any ERROR stops deployment for review; HOLD means an incomplete or unclassified
artifact, not a successful ingest. The replacement logs to stdout/stderr, captured
by systemd/journald; it does not append the old custom log file.
Keep the timer stopped through the recovery pass below, then:

```bash
sudo systemctl start phantomdocs-ingest.timer
sudo systemctl start phantomdocs-ingest.service
sudo journalctl -u phantomdocs-ingest.service -n 100 --no-pager
```

To restore the old glue if necessary (indexed refs remain):

```bash
sudo systemctl stop phantomdocs-ingest.timer phantomdocs-ingest.service
sudo cp -p /opt/phantomdocs/bin/ingest-bandejas.py.before-au-fix \
  /opt/phantomdocs/bin/ingest-bandejas.py
sudo systemctl start phantomdocs-ingest.timer
```

## Exact recovery procedure: last ALMAPONIA meeting plus Avila

The supplied facts contain folder ids, but not the three archived Drive file
ids or their exact branded names. The metadata-only selection step discovers
those IDs rather than fabricating them. As the administrator, open a shell as
the actual ingestion account so wrapper identity checks and credentials match:

```bash
sudo -u pb-pepa -H bash
set -euo pipefail
export PATH=/opt/phantomdocs/venv/bin:$PATH
INGEST=/opt/phantomdocs/bin/ingest-bandejas.py
PY=/opt/phantomdocs/venv/bin/python
RECOVERY=/var/lib/phantomdocs-ingest/recovery-2026-10-12.json
"$PY" "$INGEST" --source almaponia --source secretariat --inventory
"$PY" "$INGEST" --source almaponia --source secretariat --prepare-catalog "$RECOVERY"
"$PY" -m json.tool "$RECOVERY"
"$PY" "$INGEST" --source almaponia --source secretariat --catalog "$RECOVERY" --dry-run
"$PY" "$INGEST" --source almaponia --source secretariat --catalog "$RECOVERY"
"$PY" "$INGEST" --source almaponia --source secretariat --catalog "$RECOVERY"
```

At selection, choose exactly the three files of the last ALMAPONIA meeting and
the Avila set (`2026-10-09-10-30_coordinacion_nodo_avila`) in Secretaria's inbox.
For the three branded project artifacts, enter the SAME meeting key for all
three (for example `last-almaponia-2026-10-12-recovery`, just a local grouping
label), and their actual transcription/summary/minutes classes. Do not infer
classes solely from .pdf/.md or select sensitive project documents. Avila legacy
keys/classes are offered as defaults. Every selected group still needs both a
transcription and summary: recovery does not bypass completeness. The generated
catalog is created exclusively, so use a new filename to replace a mistaken
selection; review it before executing add. Only catalog-selected ids are ingested.
The last command must print SKIP indexed ref, with no new adds or downloads.

Check from each persona's own authenticated account; no --actor impersonation:

```bash
exit
sudo -u pb-alma -H env PATH=/opt/phantomdocs/venv/bin:/usr/local/bin:/usr/bin:/bin \
  /home/pb-alma/.local/share/phantombot/personas/alma/tools/documents.sh search almaponia
sudo -u pb-pepa -H env PATH=/opt/phantomdocs/venv/bin:/usr/local/bin:/usr/bin:/bin \
  /home/pb-pepa/.local/share/phantombot/personas/pepa/tools/documents.sh search avila
sudo -u pb-pepa -H env PATH=/opt/phantomdocs/venv/bin:/usr/local/bin:/usr/bin:/bin \
  /home/pb-pepa/.local/share/phantombot/personas/pepa/tools/documents.sh status
```

Search by a distinctive original title if branded names do not contain
`almaponia`. `pd search` searches manifest metadata, not transcription bodies.
`pd refs` lists mutable version refs and is not an external-object inventory.
Do not run `pd verify` to demonstrate zero recovery downloads: verify deliberately
reads indexed content. Routine integrity verification can be run separately.

Future opaque branded bundles need reviewed catalogs too; the timer holds them
until classified. A producer-supplied meeting/class manifest would allow further
automation, but no such convention has been invented here. Non-meeting documents
are held; department routing needs a separate operator decision.
