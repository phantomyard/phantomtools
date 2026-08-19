"""PhantomDocs CLI — `pd` (spec §3).

Commands: init · mkdir · add · get · search · verify · tag · refs · acl ·
audit · derive-manifest · status · update.
"""

from __future__ import annotations

import json
import os
import sys

import click

from . import __version__
from .access import can_read, load_org, resolved_categories
from .audit import append as audit_append
from .audit import read as audit_read
from .derive import derive_manifest as derive_from_org
from .identity import (
    component_for_doc,
    component_for_folder,
    content_hash,
    display_id,
    full_id,
    node_mac,
    root_mac,
)
from .manifest import (
    MANIFEST_FILENAME,
    ManifestError,
    empty_manifest,
    load,
    node_by_mac,
    node_by_path,
    node_by_slug,
    node_by_urn,
    resolve_node,
    save,
    urn_path,
    versions_of,
)
from .storage import LocalBackend, StorageError, resolve_backend
from .update import is_newer, latest_release


@click.group()
@click.version_option(__version__)
def main() -> None:
    """PhantomDocs — agnostic document management for PhantomOrg personas."""


def _manifest_path(root: str) -> str:
    return os.path.join(root, MANIFEST_FILENAME)


def _load_or_die(root: str):
    path = _manifest_path(root)
    if not os.path.exists(path):
        raise click.ClickException(f"no manifest at {path} — run `pd init` first")
    try:
        return load(path)
    except ManifestError as exc:
        raise click.ClickException(str(exc))


def _default_actor() -> str:
    return os.environ.get("USER") or os.environ.get("USERNAME") or "phantom"


def _audit(
    root: str, actor: str, action: str, urn: str, mac: str, ch: str | None
) -> None:
    audit_append(root, actor or _default_actor(), action, urn, mac, ch)


def _resolve_store(root: str, backend: str | None):
    return resolve_backend(backend) if backend else LocalBackend(root)


@main.command()
@click.option("--org", required=True, help="Organization id (from org.yaml).")
@click.option("--namespace", default="docs", show_default=True, help="Namespace name.")
@click.option("--org-pubkey", default="", help="Org Nostr pubkey for the root MAC.")
@click.option("--actor", default=None, help="Audit actor id.")
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def init(org: str, namespace: str, org_pubkey: str, actor: str, root: str) -> None:
    """Create a new namespace: manifest + local blob store."""
    path = _manifest_path(root)
    if os.path.exists(path):
        raise click.ClickException(f"manifest already exists: {path}")
    mac = root_mac(org_pubkey, namespace)
    os.makedirs(root, exist_ok=True)
    os.makedirs(os.path.join(root, "blobs"), exist_ok=True)
    save(path, empty_manifest(org, namespace, mac))
    _audit(root, actor, "init", f"urn:{org}:namespace:{namespace}", mac, None)
    click.echo(f"initialized {org}/{namespace}")
    click.echo(f"  root MAC  {full_id(mac)}")
    click.echo(f"  manifest  {path}")


@main.command("mkdir")
@click.option("--name", required=True, help="Folder name (single path segment).")
@click.option("--parent", default=None, help="Parent folder (logical path or slug).")
@click.option(
    "--category",
    type=int,
    default=1,
    show_default=True,
    help="Default security category for children.",
)
@click.option("--owners", multiple=True, help="PhantomOrg role ids allowed to write.")
@click.option("--actor", default=None, help="Audit actor id.")
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def mkdir(name, parent, category, owners, actor, root):
    """Create a folder node (a link in the chained MAC hierarchy)."""
    manifest = _load_or_die(root)
    meta = manifest["manifest"]
    parent_mac = meta["rootMac"]
    parent_path = ""
    if parent:
        p = node_by_path(manifest, parent) or node_by_slug(manifest, parent)
        if p is None or p.get("kind") != "folder":
            raise click.ClickException(f"parent folder not found: {parent}")
        parent_mac = p["mac"]
        parent_path = urn_path(p["urn"]) + "/"

    mac = node_mac(parent_mac, component_for_folder(name))
    path = f"{parent_path}{name}"
    urn = f"urn:{meta['org']}:folder:{path}"
    if node_by_urn(manifest, urn) is not None:
        raise click.ClickException(f"folder already exists: {urn}")

    manifest["nodes"].append(
        {
            "urn": urn,
            "mac": mac,
            "parentMac": parent_mac,
            "kind": "folder",
            "slug": name,
            "category": category,
            "owners": list(owners),
            "meta": {},
            "relations": {},
        }
    )
    save(_manifest_path(root), manifest)
    _audit(root, actor, "mkdir", urn, mac, None)
    click.echo(f"created {path}")
    click.echo(f"  urn  {urn}")
    click.echo(f"  mac  {full_id(mac)}")


@main.command()
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--slug", required=True, help="Stable slug (no version; spec §7).")
@click.option(
    "--category",
    type=int,
    default=1,
    show_default=True,
    help="Security category (PhantomOrg security_categories).",
)
@click.option("--folder", default=None, help="Parent folder (logical path or slug).")
@click.option("--owners", multiple=True, help="PhantomOrg role ids allowed to write.")
@click.option("--actor", default=None, help="Audit actor id.")
@click.option(
    "--backend", default=None, help="Backend URI (local:// ssh:// gdrive://)."
)
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def add(path, slug, category, folder, owners, actor, backend, root):
    """Ingest a document: compute MAC chain, store blob, register node."""
    with open(path, "rb") as f:
        content = f.read()

    manifest = _load_or_die(root)
    meta = manifest["manifest"]
    parent_mac = meta["rootMac"]
    parent_path = ""
    if folder:
        parent = node_by_path(manifest, folder) or node_by_slug(manifest, folder)
        if parent is None or parent.get("kind") != "folder":
            raise click.ClickException(f"folder not found: {folder}")
        parent_mac = parent["mac"]
        parent_path = urn_path(parent["urn"]) + "/"

    ch = content_hash(content)
    mac = node_mac(parent_mac, component_for_doc(slug, content))
    logical = f"{parent_path}{slug}"
    urn = f"urn:{meta['org']}:doc:{logical}"

    existing = node_by_urn(manifest, urn)
    if existing is not None and existing.get("contentHash") == ch:
        click.echo(f"unchanged: {urn}")
        return
    previous = existing["mac"] if existing is not None else None

    location = _resolve_store(root, backend).put(ch, content)
    scheme = backend.split("://")[0] if backend and "://" in backend else "local"

    manifest["nodes"].append(
        {
            "urn": urn,
            "mac": mac,
            "parentMac": parent_mac,
            "kind": "doc",
            "slug": slug,
            "category": category,
            "contentHash": ch,
            "size": len(content),
            "owners": list(owners),
            "locations": [{"backend": scheme, "path": location}],
            "meta": {"title": slug},
            "relations": {},
            "previous": previous,
        }
    )
    save(_manifest_path(root), manifest)
    _audit(root, actor, "add" if previous is None else "version", urn, mac, ch)

    verb = "added" if previous is None else "versioned"
    click.echo(f"{verb} {logical}")
    click.echo(f"  urn       {urn}")
    click.echo(f"  mac       {full_id(mac)}")
    click.echo(f"  display   {display_id(mac)}")


@main.command()
@click.argument("ref")
@click.option("--mac", default=None, help="Retrieve a specific version by MAC.")
@click.option("--cat", is_flag=True, help="Dump raw content to stdout.")
@click.option(
    "--backend", default=None, help="Backend URI (local:// ssh:// gdrive://)."
)
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def get(ref, mac, cat, backend, root):
    """Resolve a document by urn, path, slug, ref name, or version MAC."""
    manifest = _load_or_die(root)
    if mac:
        node = node_by_mac(manifest, mac)
        if node is None:
            raise click.ClickException(f"no version with MAC: {mac}")
    else:
        node = resolve_node(manifest, ref)
        if node is None:
            raise click.ClickException(f"not found: {ref}")
    location = node.get("locations", [{}])[0].get("path", "")
    click.echo(f"{node['urn']} -> {location}")
    if cat and node.get("kind") == "doc":
        sys.stdout.buffer.write(_resolve_store(root, backend).get(node["contentHash"]))


@main.command()
@click.argument("ref")
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def versions(ref, root):
    """List the version history (MAC chain) of a document."""
    manifest = _load_or_die(root)
    node = resolve_node(manifest, ref)
    if node is None:
        raise click.ClickException(f"not found: {ref}")
    history = versions_of(manifest, node["urn"])
    if not history:
        click.echo("no versions")
        return
    current = history[-1]["mac"]
    for index, version in enumerate(history, 1):
        marker = " (current)" if version["mac"] == current else ""
        size = version.get("size", 0)
        click.echo(f"v{index}  {display_id(version['mac'])}{marker}  {size} bytes")


@main.command()
@click.argument("query")
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def search(query, root):
    """Search the manifest index (urn, slug, meta)."""
    manifest = _load_or_die(root)
    needle = query.lower()
    hits = []
    for node in manifest.get("nodes", []):
        hay = " ".join(
            [
                str(node.get("urn", "")),
                str(node.get("slug", "")),
                str(node.get("meta", {}).get("title", "")),
            ]
        ).lower()
        if needle in hay:
            hits.append(node)
    if not hits:
        click.echo("no matches")
        return
    for node in hits:
        click.echo(f"{node['kind']:6} {node['urn']}")


@main.command()
@click.option(
    "--backend", default=None, help="Backend URI (local:// ssh:// gdrive://)."
)
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def verify(backend, root):
    """Recompute MAC chain + content hashes against the manifest."""
    manifest = _load_or_die(root)
    store = _resolve_store(root, backend)
    known_macs = {n["mac"] for n in manifest.get("nodes", [])}
    known_macs.add(manifest["manifest"]["rootMac"])

    failures = 0
    for node in manifest.get("nodes", []):
        issues: list[str] = []
        if node.get("parentMac") not in known_macs:
            issues.append("parent MAC unknown")
        if node.get("previous") and node["previous"] not in known_macs:
            issues.append("previous version MAC unknown")
        if node.get("kind") == "doc":
            ch = node.get("contentHash")
            if not ch:
                issues.append("missing contentHash")
            else:
                try:
                    if not store.has(ch):
                        issues.append("blob missing")
                    else:
                        data = store.get(ch)
                        if content_hash(data) != ch:
                            issues.append("content hash mismatch")
                        elif (
                            node_mac(
                                node["parentMac"], component_for_doc(node["slug"], data)
                            )
                            != node["mac"]
                        ):
                            issues.append("MAC chain mismatch")
                except StorageError as exc:
                    issues.append(f"backend error: {exc}")
        else:
            if (
                node_mac(node["parentMac"], component_for_folder(node["slug"]))
                != node["mac"]
            ):
                issues.append("MAC chain mismatch")

        if issues:
            failures += 1
            click.echo(f"FAIL {node['urn']}: {', '.join(issues)}")
        else:
            click.echo(f"OK   {node['urn']}")

    if failures:
        raise click.ClickException(f"{failures} node(s) failed verification")
    click.echo(f"verified {len(manifest.get('nodes', []))} node(s)")


@main.command()
@click.argument("name")
@click.argument("ref")
@click.option("--actor", default=None, help="Audit actor id.")
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def tag(name, ref, actor, root):
    """Point a mutable ref (e.g. `latest`, `approved-<date>`) at a version MAC."""
    manifest = _load_or_die(root)
    node = resolve_node(manifest, ref)
    if node is None:
        raise click.ClickException(f"not found: {ref}")
    manifest.setdefault("refs", {})[name] = node["mac"]
    save(_manifest_path(root), manifest)
    _audit(root, actor, "tag", node["urn"], node["mac"], node.get("contentHash"))
    click.echo(f"{name} -> {display_id(node['mac'])}  ({node['urn']})")


@main.command()
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def refs(root):
    """List mutable refs."""
    manifest = _load_or_die(root)
    if not manifest.get("refs"):
        click.echo("no refs")
        return
    for name, mac in manifest.get("refs", {}).items():
        node = node_by_mac(manifest, mac)
        urn = node["urn"] if node else "(unknown)"
        click.echo(f"{name:20} {display_id(mac)}  {urn}")


@main.command()
@click.option("--limit", default=50, show_default=True, help="Number of entries.")
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def audit(limit, root):
    """Show the append-only audit log."""
    for entry in audit_read(root, limit):
        click.echo(json.dumps(entry))


@main.command("derive-manifest")
@click.option("--org-yaml", required=True, help="Path to a PhantomOrg org.yaml.")
@click.option("--namespace", default="docs", show_default=True, help="Namespace name.")
@click.option("--org-pubkey", default="", help="Org Nostr pubkey for the root MAC.")
@click.option("--out", required=True, help="Output manifest path.")
def derive_manifest(org_yaml, namespace, org_pubkey, out):
    """Derive a manifest from a PhantomOrg org model (source of truth)."""
    org = load_org(org_yaml)
    manifest = derive_from_org(org, namespace, org_pubkey)
    save(out, manifest)
    click.echo(f"derived {manifest['manifest']['org']}/{namespace} -> {out}")
    click.echo(f"  root MAC  {full_id(manifest['manifest']['rootMac'])}")


@main.command()
@click.option("--org-yaml", required=True, help="Path to a PhantomOrg org.yaml.")
@click.option("--actor", required=True, help="Actor id to resolve.")
@click.option(
    "--category", type=int, default=None, help="Check read access for this category."
)
def acl(org_yaml, actor, category):
    """Resolve an actor's access from a PhantomOrg org.yaml (spec §9)."""
    org = load_org(org_yaml)
    categories = resolved_categories(org, actor)
    if not categories:
        click.echo(f"{actor}: no access (unknown actor or no categories)")
        return
    click.echo(f"{actor}: categories {categories}")
    if category is not None:
        verdict = "ALLOW" if can_read(org, actor, category) else "DENY"
        click.echo(f"  read category-{category}: {verdict}")


@main.command()
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def status(root):
    """Namespace summary."""
    manifest = _load_or_die(root)
    m = manifest["manifest"]
    nodes = manifest.get("nodes", [])
    docs = [n for n in nodes if n.get("kind") == "doc"]
    folders = [n for n in nodes if n.get("kind") == "folder"]
    total = sum(int(n.get("size") or 0) for n in docs)
    click.echo(f"org       {m['org']}/{m['namespace']}")
    click.echo(f"tenant    {m['tenant']}")
    click.echo(f"root MAC  {full_id(m['rootMac'])}")
    click.echo(f"nodes     {len(nodes)} ({len(docs)} docs, {len(folders)} folders)")
    click.echo(f"refs      {len(manifest.get('refs', {}))}")
    click.echo(f"bytes     {total}")


@main.command()
@click.option(
    "--repo",
    default=None,
    help="GitHub repo (owner/name). Defaults to $PHANTOMDOCS_UPDATE_REPO.",
)
@click.pass_context
def update(ctx, repo):
    """Check for a newer release (exit 0 up-to-date, 1 available, 2 error)."""
    repo = repo or os.environ.get("PHANTOMDOCS_UPDATE_REPO", "")
    if not repo:
        click.echo("no update repository configured (set PHANTOMDOCS_UPDATE_REPO)")
        ctx.exit(2)
    latest = latest_release(repo, os.environ.get("GITHUB_TOKEN"))
    if latest is None:
        click.echo(f"no release found for {repo}")
        ctx.exit(2)
    if is_newer(latest, __version__):
        click.echo(f"update available: {latest} (installed {__version__})")
        ctx.exit(1)
    click.echo(f"up to date: {__version__}")
    ctx.exit(0)


if __name__ == "__main__":
    main()
