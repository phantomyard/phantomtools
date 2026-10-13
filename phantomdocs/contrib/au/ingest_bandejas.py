#!/usr/bin/env python3
"""AU deployment glue: index meeting artifacts without modifying Drive.

Not a general inbox router. See README.md for the approved AU policy.
"""

from __future__ import annotations

import argparse
import json
import re

# Calls the configured pd wrapper with an argv list, never a shell.
import subprocess  # nosec B404
import sys
import unicodedata
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

from phantomdocs.access import can_read, can_write, load_org, normalize_category
from phantomdocs.manifest import load as load_manifest
from phantomdocs.manifest import node_by_path, node_by_slug

GO_ARCHIVE = "1VnnY_IqQaaFJ9gL6imuuskd6G1_C85yN"
AU_ARCHIVE = "1u0MWy4VtoNTwSzW5n4MsPFOkCYHgB0Mq"
ID = re.compile(r"^[A-Za-z0-9_-]+$")
STAMP = re.compile(r"_\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}$")
CLASSES = {
    "transcripcion": "transcription",
    "resumen": "summary",
    "acta": "minutes",
    "grabacion": "recording",
    "recording": "recording",
}
SOURCES = {
    "direction": ("paco", "direction"),
    "secretariat": ("pepa", "secretariat"),
    "finance": ("roberto", "finance"),
    "volunteering": ("voluntariado", "volunteering"),
    "marketing": ("carla", "marketing"),
    "training": ("elena", "training"),
    "almaponia": (None, "projects"),
}


def artifact(name):
    """Return (meeting key, class) for recognized legacy artifacts, else None.

    Remove the final extension; then remove one timestamp and one recognized
    dot-class suffix in either order. Preserve all other dots/title/date text.
    A bare .txt is a transcription; a bare .mp4 is an original recording.
    Keys use NFC + casefold, not slugification.
    Branded _EVID names need an explicit catalog, not a guessed classification.
    """
    stem, sep, ext = name.rpartition(".")
    if not sep or ext.lower() not in {"txt", "md", "pdf", "mp4"}:
        return None
    kind = None
    timestamp_removed = False
    for _ in range(2):
        match = STAMP.search(stem)
        if match and not timestamp_removed:
            stem = stem[: match.start()]
            timestamp_removed = True
            continue
        head, dot, suffix = stem.rpartition(".")
        if dot and suffix.casefold() in CLASSES and kind is None:
            kind = CLASSES[suffix.casefold()]
            stem = head
            continue
        break
    if stem.upper().endswith("_EVID"):
        return None
    kind = kind or {"txt": "transcription", "mp4": "recording"}.get(ext.lower())
    if (kind == "recording") != (ext.lower() == "mp4"):
        return None
    if not kind or not stem:
        return None
    return unicodedata.normalize("NFC", stem).casefold(), kind


def slug_for(name, file_id):
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower() or "document"
    # The stable Drive id distinguishes identical names and lossy slug collisions.
    return f"{text}-{file_id.encode('ascii').hex()}"


def domains(org):
    entries = org["documents"]["naming"]["domains"]
    result = {d["id"]: d for d in entries}
    if len(result) != len(entries):
        raise ValueError("duplicate naming domain")
    return result


def policy(org, source, project, actor):
    declared = domains(org)
    domain = declared["projects" if project else "secretariat"]
    type_id = "almaponia" if project else "minutes"
    types = [t for t in domain["types"] if t["id"] == type_id]
    if len(types) != 1:
        raise ValueError(f"missing or ambiguous declared type: {type_id}")
    category = normalize_category(types[0].get("category", domain["category"]))
    if (
        category != "category-2"
        or category not in org["policies"]["security_categories"]
    ):
        raise ValueError(
            "approved meeting classification must remain declared category-2"
        )
    custodian = declared["secretariat"]["owners"]
    if custodian != ["chief_of_staff"]:
        raise ValueError(
            "Secretaria ownership changed; operator policy review required"
        )
    requesters = ["project_lead"] if project else declared[SOURCES[source][1]]["owners"]
    if project and "project_lead" not in declared["projects"]["owners"]:
        raise ValueError("project_lead is not a declared projects owner")
    owners = sorted(set(custodian + requesters))
    known = {r["id"] for r in org["roles"]} | {a["id"] for a in org["actors"]}
    if not requesters or not set(owners) <= known:
        raise ValueError("unknown or empty meeting owners")
    if not can_write(org, actor, category, owners):
        raise ValueError(f"{actor} cannot write the approved meeting classification")
    if project and not can_read(org, "alma", category):
        raise ValueError("Alma cannot read the approved project classification")
    return category, owners


def source_folder(org, source):
    inbox_actor = SOURCES[source][0]
    if inbox_actor is None:
        return GO_ARCHIVE
    folder_id = org["documents"]["inboxes"][inbox_actor]["id"]
    if not isinstance(folder_id, str) or not ID.fullmatch(folder_id):
        raise ValueError("invalid declared inbox id")
    return folder_id


class Drive:
    """Metadata listing only. Byte reads are delegated to pd add --ref."""

    def __init__(self, service_account, subject):
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2.service_account import Credentials

        credentials = Credentials.from_service_account_file(
            service_account,
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
            subject=subject,
        )
        self.session = AuthorizedSession(credentials)

    def list_files(self, folder_id):
        params = {
            "q": f"'{folder_id}' in parents and trashed=false",
            "fields": "nextPageToken,incompleteSearch,files(id,name,mimeType)",
            "pageSize": 1000,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        items, tokens = [], set()
        while True:
            response = self.session.get(
                "https://www.googleapis.com/drive/v3/files",
                params=params,
                timeout=60,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("incompleteSearch"):
                raise ValueError("Drive returned an incomplete search")
            items.extend(data["files"])
            token = data.get("nextPageToken")
            if not token:
                return items
            if token in tokens:
                raise ValueError("Drive repeated a page token")
            tokens.add(token)
            params["pageToken"] = token


def references(manifest):
    result = defaultdict(list)
    # Include historical versions: missing marker/renaming/rollback must not
    # cause the same immutable ingest identity to be read and hashed again.
    for node in manifest["nodes"]:
        if node["kind"] != "doc":
            continue
        for location in node.get("locations", []):
            if location.get("backend") == "gdrive":
                result[location["ref"].removeprefix("gdrive://")].append(node)
    return result


def read_store(root, org):
    manifest = load_manifest(str(Path(root) / "manifest.yaml"))
    header = manifest["manifest"]
    if (
        header["org"] != org["organization"]["id"]
        or header["namespace"] != org["documents"]["namespace"]
    ):
        raise ValueError("store and org model identify different namespaces")
    return manifest


def plan(org, inventory, catalog, actor):
    groups = defaultdict(list)
    observed, used = {}, set()
    for entry in inventory:
        source = entry["source"]
        if source not in SOURCES:
            raise ValueError("unknown source")
        file_id, name = entry["id"], entry["name"]
        if not isinstance(file_id, str) or not ID.fullmatch(file_id):
            raise ValueError("invalid Drive file id")
        if entry["mimeType"] == "application/vnd.google-apps.folder":
            continue
        previous = observed.setdefault(file_id, entry)
        if previous != entry:
            raise ValueError(
                "file appears in multiple sources; choose one canonical source"
            )
        if previous is not entry:
            continue
        choice = catalog.get(file_id)
        if choice:
            used.add(file_id)
            if choice["name"] != name or choice["source"] != source:
                raise ValueError("catalog name/source no longer matches Drive metadata")
            key, kind, project = choice["meeting"], choice["class"], choice["project"]
            if not isinstance(project, bool) or kind not in CLASSES.values() or not key:
                raise ValueError("invalid catalog meeting/class/project")
        else:
            parsed = artifact(name)
            if parsed is None:
                print(f"HOLD unclassified artifact: {source}/{name}", file=sys.stderr)
                continue
            key, kind = parsed
            project = source == "almaponia"
            if not project and re.search(r"(?i)\balmaponia\b", key.replace("_", " ")):
                raise ValueError(
                    "ALMAPONIA artifact outside canonical archive needs explicit catalog"
                )
        if source == "almaponia" and not project:
            raise ValueError(
                "canonical GO archive cannot classify a meeting as general AU"
            )
        groups[(source, key, project)].append((entry, kind))
    if set(catalog) != used:
        raise ValueError(
            "catalog contains files not found in the selected source inventory"
        )
    actions = []
    for (source, key, project), items in sorted(groups.items()):
        if not {"transcription", "summary"} <= {kind for _, kind in items}:
            print(f"HOLD incomplete meeting: {source}/{key}", file=sys.stderr)
            continue
        category, owners = policy(org, source, project, actor)
        for entry, _ in items:
            actions.append(
                {
                    **entry,
                    "category": category,
                    "owners": owners,
                    "slug": slug_for(entry["name"], entry["id"]),
                }
            )
    return actions


def prepare_catalog(inventory, destination):
    """Operator selection for opaque branded names; metadata only."""
    items = [
        f for f in inventory if f["mimeType"] != "application/vnd.google-apps.folder"
    ]
    for index, item in enumerate(items, 1):
        print(f"{index}: {item['source']} | {item['name']} | {item['id']}")
    selected = input("File numbers to recover (comma-separated): ")
    indices = [int(i.strip()) - 1 for i in selected.split(",")]
    if (
        not indices
        or len(set(indices)) != len(indices)
        or any(i < 0 or i >= len(items) for i in indices)
    ):
        raise ValueError("invalid recovery selection")
    catalog = {}
    for index in indices:
        item = items[index]
        parsed = artifact(item["name"])
        print(
            f"Classify {item['name']} (do not select sensitive or non-meeting material)"
        )
        meeting = input(f"Meeting key [{parsed[0] if parsed else ''}]: ") or (
            parsed[0] if parsed else ""
        )
        kind = input(
            f"Class recording/transcription/summary/minutes [{parsed[1] if parsed else ''}]: "
        ) or (parsed[1] if parsed else "")
        if not meeting or kind not in CLASSES.values():
            raise ValueError("an explicit meeting key and supported class are required")
        catalog[item["id"]] = {
            "name": item["name"],
            "source": item["source"],
            "meeting": meeting,
            "class": kind,
            "project": item["source"] == "almaponia",
        }
    # Exclusive creation: do not silently replace a reviewed recovery catalog.
    with destination.open("x", encoding="utf-8") as output:
        json.dump(catalog, output, indent=2, ensure_ascii=False)
        output.write("\n")
    print(f"Review the catalog before ingestion: {destination}")


@contextmanager
def ingest_lock(state):
    # Linux AU deployment: timer and recovery share this lock. The store's
    # own transaction lock remains exclusively managed by pd.
    import fcntl

    state.mkdir(parents=True, exist_ok=True)
    with (state / "ingest.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def execute(
    org,
    actions,
    root,
    state,
    docs,
    org_yaml,
    actor,
    folder,
    dry_run,
    run=subprocess.run,
):
    initial = read_store(root, org)
    parent = node_by_path(initial, folder) or node_by_slug(initial, folder)
    if parent is None or parent["kind"] != "folder":
        raise ValueError("configured PhantomDocs folder does not exist")

    # Validate every known conflict before starting a batch. Recheck each ref
    # immediately before add as well, to catch commits by other callers.
    def check_existing(action, refs):
        file_id = action["id"]
        if file_id in refs:
            for node in refs[file_id]:
                if (
                    normalize_category(node["category"]) != action["category"]
                    or sorted(node["owners"]) != action["owners"]
                ):
                    raise ValueError(
                        "existing ref has different classification/owners; review required"
                    )
        elif (state / file_id).exists():
            raise ValueError(f"orphan seen marker: {file_id}; review required")

    for action in actions:
        check_existing(action, references(initial))
    errors = 0
    for action in actions:
        file_id = action["id"]
        refs = references(read_store(root, org))
        check_existing(action, refs)
        if file_id in refs:
            print(f"SKIP indexed ref: gdrive://{file_id}")
            continue
        command = [
            docs,
            "add",
            "--ref",
            f"gdrive://{file_id}",
            "--slug",
            action["slug"],
            "--folder",
            folder,
            "--category",
            action["category"],
            "--org-yaml",
            str(org_yaml),
            "--actor",
            actor,
            "--root",
            str(root),
        ]
        for owner in action["owners"]:
            command.extend(["--owners", owner])
        if dry_run:
            print(json.dumps({"command": command}, ensure_ascii=False))
            continue
        result = run(command, capture_output=True, text=True)
        if result.returncode:
            print(
                f"ERROR pd add for {file_id}: exit {result.returncode}", file=sys.stderr
            )
            errors += 1
            continue
        indexed = references(read_store(root, org)).get(file_id, [])
        if not indexed or any(
            normalize_category(n["category"]) != action["category"]
            or sorted(n["owners"]) != action["owners"]
            for n in indexed
        ):
            raise ValueError(
                "pd returned success without the expected indexed ref/classification"
            )
        # Legacy-compatible marker, only after the manifest proves the commit.
        (state / file_id).touch(exist_ok=True)
        print(f"OK indexed ref: gdrive://{file_id}")
    return 1 if errors else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org-yaml", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--docs", required=True)
    parser.add_argument("--actor", default="pepa")
    parser.add_argument("--folder", default="reuniones")
    parser.add_argument(
        "--state", type=Path, default=Path("/var/lib/phantomdocs-ingest/seen")
    )
    parser.add_argument("--service-account", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--source", action="append", choices=sorted(SOURCES))
    parser.add_argument(
        "--catalog", type=Path, help="explicit branded/recovery artifact classification"
    )
    parser.add_argument(
        "--inventory",
        action="store_true",
        help="print metadata only; do not read store",
    )
    parser.add_argument(
        "--prepare-catalog",
        type=Path,
        help="interactively select metadata; create a recovery catalog",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="plan only; no bytes, store or state writes",
    )
    args = parser.parse_args(argv)
    try:
        org = load_org(str(args.org_yaml))
        if (
            org["organization"]["id"] != "aquaponics-united"
            or org["documents"]["namespace"] != "au"
        ):
            raise ValueError("this deployment utility only supports AU")
        drive = Drive(args.service_account, args.subject)
        inventory = []
        for source in args.source or SOURCES:
            inventory.extend(
                {**f, "source": source}
                for f in drive.list_files(source_folder(org, source))
            )
        if args.inventory:
            print(json.dumps(inventory, indent=2, ensure_ascii=False))
            return 0
        if args.prepare_catalog:
            prepare_catalog(inventory, args.prepare_catalog)
            return 0
        catalog = (
            json.loads(args.catalog.read_text(encoding="utf-8")) if args.catalog else {}
        )
        if args.catalog and (not isinstance(catalog, dict) or not catalog):
            raise ValueError("recovery catalog must be a non-empty file-id map")
        # Catalog limits a recovery pass to the exact selected file ids. Still
        # list the source freshly and check every pinned name before any add.
        if args.catalog:
            inventory = [f for f in inventory if f["id"] in catalog]
        actions = plan(org, inventory, catalog, args.actor)
        kwargs = {
            "org": org,
            "actions": actions,
            "root": args.root,
            "state": args.state,
            "docs": args.docs,
            "org_yaml": args.org_yaml,
            "actor": args.actor,
            "folder": args.folder,
            "dry_run": args.dry_run,
        }
        if args.dry_run:
            return execute(**kwargs)
        with ingest_lock(args.state):
            return execute(**kwargs)
    except (KeyError, TypeError, ValueError, OSError, EOFError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- redact third-party errors at the CLI boundary
        # Credential and HTTP libraries may include response bodies in errors.
        # Keep those (and any token details) out of service logs.
        print(f"ERROR backend failure: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
