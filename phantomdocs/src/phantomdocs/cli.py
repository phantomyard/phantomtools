"""PhantomDocs CLI — `pd` (spec §3).

Commands: init · add · get · search · verify · acl · status.
"""

from __future__ import annotations

import os
import sys

import click

from . import __version__
from .access import can_read, load_org, resolved_categories
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
    node_by_slug,
    node_by_urn,
    save,
    urn_path,
)
from .storage import LocalBackend


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


@main.command()
@click.option("--org", required=True, help="Organization id (from org.yaml).")
@click.option("--namespace", default="docs", show_default=True, help="Namespace name.")
@click.option("--org-pubkey", default="", help="Org Nostr pubkey for the root MAC.")
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def init(org: str, namespace: str, org_pubkey: str, root: str) -> None:
    """Create a new namespace: manifest + local blob store."""
    path = _manifest_path(root)
    if os.path.exists(path):
        raise click.ClickException(f"manifest already exists: {path}")
    mac = root_mac(org_pubkey, namespace)
    os.makedirs(root, exist_ok=True)
    os.makedirs(os.path.join(root, "blobs"), exist_ok=True)
    save(path, empty_manifest(org, namespace, mac))
    click.echo(f"initialized {org}/{namespace}")
    click.echo(f"  root MAC  {full_id(mac)}")
    click.echo(f"  manifest  {path}")


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
@click.option("--folder", default=None, help="Parent folder slug (optional).")
@click.option("--owners", multiple=True, help="PhantomOrg role ids allowed to write.")
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def add(path, slug, category, folder, owners, root):
    """Ingest a document: compute MAC chain, store blob, register node."""
    with open(path, "rb") as f:
        content = f.read()

    manifest = _load_or_die(root)
    meta = manifest["manifest"]
    parent_mac = meta["rootMac"]
    parent_path = ""
    if folder:
        parent = node_by_slug(manifest, folder)
        if parent is None or parent.get("kind") != "folder":
            raise click.ClickException(f"folder not found: {folder}")
        parent_mac = parent["mac"]
        parent_path = urn_path(parent["urn"]) + "/"

    ch = content_hash(content)
    mac = node_mac(parent_mac, component_for_doc(slug, content))
    logical = f"{parent_path}{slug}"
    urn = f"urn:{meta['org']}:doc:{logical}"

    if node_by_urn(manifest, urn) is not None:
        raise click.ClickException(f"node already exists: {urn}")

    blob_path = LocalBackend(root).put(ch, content)
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
            "locations": [{"backend": "local", "path": blob_path}],
            "meta": {"title": slug},
            "relations": {},
        }
    )
    save(_manifest_path(root), manifest)

    click.echo(f"added {logical}")
    click.echo(f"  urn       {urn}")
    click.echo(f"  mac       {full_id(mac)}")
    click.echo(f"  display   {display_id(mac)}")


@main.command()
@click.argument("ref")
@click.option("--cat", is_flag=True, help="Dump raw content to stdout.")
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def get(ref, cat, root):
    """Resolve a document by urn or slug and print its location."""
    manifest = _load_or_die(root)
    node = node_by_urn(manifest, ref) or node_by_slug(manifest, ref)
    if node is None:
        raise click.ClickException(f"not found: {ref}")
    location = node.get("locations", [{}])[0].get("path", "")
    click.echo(f"{node['urn']} -> {location}")
    if cat:
        sys.stdout.buffer.write(LocalBackend(root).get(node["contentHash"]))


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
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def verify(root):
    """Recompute MAC chain + content hashes against the manifest."""
    manifest = _load_or_die(root)
    backend = LocalBackend(root)
    known_macs = {n["mac"] for n in manifest.get("nodes", [])}
    known_macs.add(manifest["manifest"]["rootMac"])

    failures = 0
    for node in manifest.get("nodes", []):
        issues = []
        if node.get("parentMac") not in known_macs:
            issues.append("parent MAC unknown")
        if node.get("kind") == "doc":
            ch = node.get("contentHash")
            if not ch or not backend.has(ch):
                issues.append("blob missing")
            else:
                data = backend.get(ch)
                if content_hash(data) != ch:
                    issues.append("content hash mismatch")
                elif (
                    node_mac(node["parentMac"], component_for_doc(node["slug"], data))
                    != node["mac"]
                ):
                    issues.append("MAC chain mismatch")
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
    total = sum(int(n.get("size") or 0) for n in docs)
    click.echo(f"org       {m['org']}/{m['namespace']}")
    click.echo(f"tenant    {m['tenant']}")
    click.echo(f"root MAC  {full_id(m['rootMac'])}")
    click.echo(f"nodes     {len(nodes)} ({len(docs)} docs)")
    click.echo(f"bytes     {total}")


if __name__ == "__main__":
    main()
