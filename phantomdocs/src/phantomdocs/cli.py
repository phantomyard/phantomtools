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
from .access import (
    can_read,
    load_org,
    normalize_category,
    resolved_categories,
)
from .audit import append as audit_append
from .audit import read as audit_read
from .audit import verify_chain as audit_verify_chain
from .derive import derive_manifest as derive_from_org
from .documents import DocumentError, DocumentService
from .identity import (
    component_for_doc,
    component_for_folder,
    content_hash,
    display_id,
    full_id,
    is_valid_slug,
    node_mac,
    root_mac,
)
from .manifest import (
    MANIFEST_FILENAME,
    ManifestError,
    empty_manifest,
    load,
    node_by_mac,
    ref_target_mac,
    resolve_node,
    save,
    structural_issues,
    versions_of,
)
from .setup import (
    apply_bounded,
    render_documents_protocol,
    render_memory_pointer,
    render_wrapper,
    write_wrapper,
)
from .signing import (
    mutation_envelope,
    npub_to_pubkey_hex,
    verify_mutation,
)
from .storage import (
    LocalBackend,
    StorageError,
    location_uri,
    read_reference,
    resolve_backend,
)
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


PHANTOMDOCS_ACTOR_ENV = "PHANTOMDOCS_ACTOR"

_ACTOR_HELP = (
    "Actor id. Precedence: --actor → $PHANTOMDOCS_ACTOR → OS username (SPEC §9)."
)


def _os_actor() -> str | None:
    """The OS username of the calling process (last-resort identity source).

    Used only as a fallback for deployments that really do run one persona
    per OS account (e.g. the VPS Virtualmin model). Returns None when it
    cannot be resolved.
    """
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, OSError):
        return None


def _resolve_actor(explicit: str | None) -> str | None:
    """Resolve the actor id with layered precedence (issue #29).

    In the PhantomOrg/phantombot deployment model, N personas live as
    directories under ONE OS account and phantombot gives focus to one
    persona at a time, so the OS username is NOT the persona identity.
    The actor is resolved from, in order:

      1. an explicit ``--actor`` flag (per-persona wrapper / caller override);
      2. the ``PHANTOMDOCS_ACTOR`` environment variable (operator-supplied
         override — never injected by phantombot);
      3. the OS username (fallback for one-account-per-persona deployments).

    Returns None when nothing resolves (fail-closed). The candidate is
    validated against org.yaml by ``_require_acl`` — this function only
    proposes, it does not authenticate. The resolved actor is a guardrail
    for the model's tool use (which persona's remit applies), not a
    cryptographic boundary against a malicious process (SPEC §9, issue #30).
    """
    if explicit:
        return explicit
    env = os.environ.get(PHANTOMDOCS_ACTOR_ENV, "").strip()
    if env:
        return env
    return _os_actor()


def _require_acl(org_yaml: str | None, actor: str | None = None):
    """Return ``(actor, org)`` for an ACL-gated command, or raise a
    ClickException (fail-closed) when the org model or actor is absent.

    ``--org-yaml`` must be supplied. The actor is resolved by layered
    precedence (``--actor`` → ``PHANTOMDOCS_ACTOR`` → OS username) and must
    be a declared actor in the org model; an unmapped actor is denied
    (fail-closed). Without all of these, access is denied — never fail-open.
    """
    if not org_yaml:
        raise click.ClickException(
            "access control requires --org-yaml (the authoritative PhantomOrg "
            "org model); refusing to serve/mutate content without it"
        )
    actor = _resolve_actor(actor)
    if not actor:
        raise click.ClickException(
            "access control requires an actor identity; could not resolve "
            "one (pass --actor, set PHANTOMDOCS_ACTOR, or run under a "
            "declared OS account), refusing to serve/mutate content"
        )
    org = load_org(org_yaml)
    known_actors = {a.get("id") for a in org.get("actors", [])}
    if actor not in known_actors:
        raise click.ClickException(
            f"denied: {actor!r} is not an actor in the org model; "
            "PhantomDocs refuses unmapped actors (fail-closed)"
        )
    return actor, org


def _validate_slug(value: str, label: str) -> None:
    """Reject names/slugs/namespaces that break SPEC §7 naming (issue #59)."""
    if not is_valid_slug(value):
        raise click.ClickException(
            f"invalid {label} {value!r}: must be a single lowercase/kebab ASCII "
            "segment (letters, digits, hyphen, dot; no spaces, slashes, or accents)"
        )


def _audit(
    root: str,
    actor: str,
    action: str,
    urn: str,
    mac: str,
    ch: str | None,
    sig: str | None = None,
    sig_pubkey: str | None = None,
) -> None:
    audit_append(root, actor, action, urn, mac, ch, sig, sig_pubkey)


def _resolve_store(root: str, backend: str | None):
    return resolve_backend(backend) if backend else LocalBackend(root)


@main.command()
@click.option("--org", required=True, help="Organization id (from org.yaml).")
@click.option("--namespace", default="docs", show_default=True, help="Namespace name.")
@click.option("--org-pubkey", default="", help="Org Nostr pubkey for the root MAC.")
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def init(org: str, namespace: str, org_pubkey: str, root: str) -> None:
    """Create a new namespace: manifest + local blob store."""
    _validate_slug(namespace, "namespace")
    path = _manifest_path(root)
    if os.path.exists(path):
        raise click.ClickException(f"manifest already exists: {path}")
    mac = root_mac(org, org_pubkey, namespace)
    os.makedirs(root, exist_ok=True)
    os.makedirs(os.path.join(root, "blobs"), exist_ok=True)
    save(path, empty_manifest(org, namespace, mac))
    _audit(
        root,
        _resolve_actor(None) or "phantom",
        "init",
        f"urn:{org}:namespace:{namespace}",
        mac,
        None,
    )
    click.echo(f"initialized {org}/{namespace}")
    click.echo(f"  root MAC  {full_id(mac)}")
    click.echo(f"  manifest  {path}")


@main.command("mkdir")
@click.option("--name", required=True, help="Folder name (single path segment).")
@click.option("--parent", default=None, help="Parent folder (logical path or slug).")
@click.option(
    "--category",
    type=str,
    default="category-1",
    show_default=True,
    help="Security category id (e.g. 'category-2', 'category-4-almaponia').",
)
@click.option("--owners", multiple=True, help="PhantomOrg role ids allowed to write.")
@click.option(
    "--org-yaml", default=None, help="PhantomOrg org.yaml (authoritative ACL)."
)
@click.option("--actor", default=None, help=_ACTOR_HELP)
@click.option(
    "--nsec-file",
    default=None,
    help="File containing the actor's nsec (issue #30 v2 signing).",
)
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def mkdir(name, parent, category, owners, org_yaml, actor, nsec_file, root):
    """Create a folder node (a link in the chained MAC hierarchy).

    Access-controlled: requires --org-yaml + an authenticated OS identity; the
    actor must be able to write the folder's category (fail-closed).
    """
    _validate_slug(name, "name")
    actor_id, org = _require_acl(org_yaml, actor)
    service = DocumentService(root)
    try:
        result = service.create_folder(
            org,
            actor_id,
            name=name,
            parent=parent,
            category=category,
            owners=list(owners),
            nsec_file=nsec_file,
        )
    except DocumentError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"created {result['path']}")
    click.echo(f"  urn  {result['urn']}")
    click.echo(f"  mac  {full_id(result['mac'])}")


@main.command()
@click.argument("path", required=False)
@click.option("--slug", required=True, help="Stable slug (no version; spec §7).")
@click.option(
    "--category",
    type=str,
    default=None,
    help="Security category id (e.g. 'category-2', 'category-4-almaponia'). "
    "Defaults to category-1 for new nodes; versioning an existing node "
    "preserves its category.",
)
@click.option("--folder", default=None, help="Parent folder (logical path or slug).")
@click.option("--owners", multiple=True, help="PhantomOrg role ids allowed to write.")
@click.option(
    "--org-yaml", default=None, help="PhantomOrg org.yaml (authoritative ACL)."
)
@click.option(
    "--backend", default=None, help="Backend URI (local:// ssh:// gdrive://)."
)
@click.option(
    "--ref",
    default=None,
    help="Index an existing external object by reference "
    "(gdrive://<file_id>, file:///path, ssh://host/path) instead of "
    "ingesting a local file. Read to hash, but NOT copied.",
)
@click.option("--actor", default=None, help=_ACTOR_HELP)
@click.option(
    "--nsec-file",
    default=None,
    help="File containing the actor's nsec (issue #30 v2 signing).",
)
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def add(
    path, ref, slug, category, folder, owners, org_yaml, actor, nsec_file, backend, root
):
    """Ingest a document: compute MAC chain, store blob, register node.

    Access-controlled: requires --org-yaml + an authenticated OS identity; the
    actor must be able to write the document's category, and when versioning an
    existing node, be one of its owners (fail-closed).
    """
    if (path is None) == (ref is None):
        raise click.ClickException(
            "provide exactly one of: PATH (ingest a local file) or --ref URI "
            "(index an external object by reference)"
        )

    _validate_slug(slug, "slug")

    if ref:
        content, ref_location = read_reference(ref)
    else:
        with open(path, "rb") as f:
            content = f.read()
        ref_location = None

    actor_id, org = _require_acl(org_yaml, actor)
    service = DocumentService(root)
    try:
        result = service.add_document(
            org,
            actor_id,
            content=content,
            ref_location=ref_location,
            slug=slug,
            category=category,
            folder=folder,
            owners=list(owners),
            backend=backend,
            nsec_file=nsec_file,
        )
    except DocumentError as exc:
        raise click.ClickException(str(exc))

    if result.get("unchanged"):
        click.echo(f"unchanged: {result['urn']}")
        return
    click.echo(f"{result['verb']} {result['logical']}")
    click.echo(f"  urn       {result['urn']}")
    click.echo(f"  mac       {full_id(result['mac'])}")
    click.echo(f"  display   {display_id(result['mac'])}")


@main.command()
@click.argument("ref")
@click.option("--mac", default=None, help="Retrieve a specific version by MAC.")
@click.option("--cat", is_flag=True, help="Dump raw content to stdout.")
@click.option(
    "--backend", default=None, help="Backend URI (local:// ssh:// gdrive://)."
)
@click.option(
    "--org-yaml", default=None, help="PhantomOrg org.yaml (authoritative ACL)."
)
@click.option("--actor", default=None, help=_ACTOR_HELP)
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def get(ref, mac, cat, backend, org_yaml, actor, root):
    """Resolve a document by urn, path, slug, ref name, or version MAC.

    Access-controlled: requires --org-yaml + an authenticated OS identity;
    content in a category the actor cannot read is denied (fail-closed).
    """
    manifest = _load_or_die(root)
    if mac:
        node = node_by_mac(manifest, mac)
        if node is None:
            raise click.ClickException(f"no version with MAC: {mac}")
    else:
        node = resolve_node(manifest, ref)
        if node is None:
            raise click.ClickException(f"not found: {ref}")

    actor_id, org = _require_acl(org_yaml, actor)
    if not can_read(org, actor_id, node.get("category", 0)):
        raise click.ClickException(
            f"denied: {actor_id} cannot read "
            f"{normalize_category(node.get('category', 0))} ({node['urn']})"
        )

    location = node.get("locations", [{}])[0]
    if "ref" in location:
        click.echo(f"{node['urn']} -> {location_uri(location)}")
    else:
        click.echo(f"{node['urn']} -> {location.get('path', '')}")
    if cat and node.get("kind") == "doc":
        loc = node.get("locations", [{}])[0]
        if "ref" in loc:
            data = read_reference(location_uri(loc))[0]
        else:
            data = _resolve_store(root, backend).get(node["contentHash"])
        sys.stdout.buffer.write(data)


@main.command()
@click.argument("ref")
@click.option(
    "--org-yaml", default=None, help="PhantomOrg org.yaml (authoritative ACL)."
)
@click.option("--actor", default=None, help=_ACTOR_HELP)
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def versions(ref, org_yaml, actor, root):
    """List the version history (MAC chain) of a document."""
    manifest = _load_or_die(root)
    node = resolve_node(manifest, ref)
    if node is None:
        raise click.ClickException(f"not found: {ref}")
    actor_id, org = _require_acl(org_yaml, actor)
    if not can_read(org, actor_id, node.get("category", 0)):
        raise click.ClickException(
            f"denied: {actor_id} cannot read "
            f"{normalize_category(node.get('category', 0))} ({node['urn']})"
        )
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
@click.option(
    "--org-yaml", default=None, help="PhantomOrg org.yaml (authoritative ACL)."
)
@click.option("--actor", default=None, help=_ACTOR_HELP)
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def search(query, org_yaml, actor, root):
    """Search the manifest index (urn, slug, meta).

    Access-controlled: only nodes the actor may read are returned (fail-closed).
    """
    manifest = _load_or_die(root)
    actor_id, org = _require_acl(org_yaml, actor)
    needle = query.lower()
    hits = []
    for node in manifest.get("nodes", []):
        if not can_read(org, actor_id, node.get("category", 0)):
            continue
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
@click.option(
    "--org-yaml",
    default=None,
    help="Optional PhantomOrg org.yaml to also check mutation signatures "
    "against declared actor npubs (issue #30 v2).",
)
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def verify(backend, org_yaml, root):
    """Recompute MAC chain + content hashes against the manifest."""
    manifest = _load_or_die(root)
    store = _resolve_store(root, backend)
    known_macs = {n["mac"] for n in manifest.get("nodes", [])}
    known_macs.add(manifest["manifest"]["rootMac"])

    # Declared actor pubkeys (from org.yaml) for the signature authorization
    # check. Only enforced when --org-yaml is supplied (issue #30 v2). The
    # mapping is actor id -> pubkey hex so a signature is validated against
    # the *specific* actor recorded on the node, not merely "some" declared
    # actor.
    declared_pubkeys: dict[str, str] | None = None
    if org_yaml:
        org = load_org(org_yaml)
        declared_pubkeys = {}
        for a in org.get("actors", []):
            npub = a.get("npub")
            actor_id = a.get("id")
            if npub and actor_id:
                try:
                    declared_pubkeys[actor_id] = npub_to_pubkey_hex(npub)
                except ValueError:
                    pass

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
                loc = node.get("locations", [{}])[0]
                if loc.get("ref"):
                    try:
                        data = read_reference(location_uri(loc))[0]
                        if content_hash(data) != ch:
                            issues.append("content hash mismatch (reference)")
                        elif (
                            node_mac(
                                node["parentMac"],
                                component_for_doc(node["slug"], data),
                            )
                            != node["mac"]
                        ):
                            issues.append("MAC chain mismatch")
                    except StorageError as exc:
                        issues.append(f"reference read failed: {exc}")
                else:
                    try:
                        if not store.has(ch):
                            issues.append("blob missing")
                        else:
                            try:
                                data = store.get(ch)
                            except StorageError as exc:
                                issues.append(f"{exc}")
                                data = None
                            if data is not None:
                                if content_hash(data) != ch:
                                    issues.append("content hash mismatch")
                                elif (
                                    node_mac(
                                        node["parentMac"],
                                        component_for_doc(node["slug"], data),
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

        # Mutation signature (issue #30 v2): if the node carries a signature,
        # it must verify against its pubkey over the *canonical mutation
        # envelope* (rebuilding the exact actor + authorization fields from the
        # stored node); and if --org-yaml is supplied, the signing key must
        # belong to the specific actor recorded on the node — not merely to
        # some declared actor.
        sig = node.get("sig")
        sig_pubkey = node.get("sigPubkey")
        if sig is not None or sig_pubkey is not None:
            if not sig or not sig_pubkey:
                issues.append("incomplete mutation signature")
            else:
                envelope = mutation_envelope(
                    mac=node["mac"],
                    actor=node.get("actor", ""),
                    action=node.get("action", ""),
                    category=node.get("category", ""),
                    owners=node.get("owners"),
                    locations=node.get("locations"),
                    urn=node.get("urn", ""),
                )
                if not verify_mutation(sig_pubkey, sig, envelope):
                    issues.append("signature invalid")
                elif declared_pubkeys is not None:
                    declared = declared_pubkeys.get(node.get("actor"))
                    if declared is None:
                        issues.append("signature from undeclared actor")
                    elif sig_pubkey != declared:
                        issues.append(
                            "signature key does not match the actor's declared npub"
                        )

        if issues:
            failures += 1
            click.echo(f"FAIL {node['urn']}: {', '.join(issues)}")
        else:
            click.echo(f"OK   {node['urn']}")

    # Mutable refs (issue #30 v2 / PR #38): a signed `tag` stores a record
    # whose signature binds the ref name -> target MAC -> actor. Rebuild the
    # envelope with the *current* ref name and verify it, so renaming or
    # repointing ``manifest.refs`` after tagging invalidates the signature.
    for ref_name, value in (manifest.get("refs") or {}).items():
        issues: list[str] = []
        mac = ref_target_mac(value)
        node = node_by_mac(manifest, mac) if mac else None
        if node is None:
            issues.append("ref points at unknown MAC")
        elif isinstance(value, dict):
            sig = value.get("sig")
            sig_pubkey = value.get("sigPubkey")
            if (sig is None) != (sig_pubkey is None):
                issues.append("incomplete ref signature")
            elif sig is not None:
                envelope = mutation_envelope(
                    mac=mac,
                    actor=value.get("actor", ""),
                    action=value.get("action", "tag"),
                    category=node.get("category", ""),
                    owners=node.get("owners"),
                    locations=node.get("locations"),
                    urn=node.get("urn", ""),
                    ref=ref_name,
                )
                if not verify_mutation(sig_pubkey, sig, envelope):
                    issues.append("ref signature invalid")
                elif declared_pubkeys is not None:
                    declared = declared_pubkeys.get(value.get("actor"))
                    if declared is None:
                        issues.append("ref signature from undeclared actor")
                    elif sig_pubkey != declared:
                        issues.append(
                            "ref signature key does not match actor's declared npub"
                        )
        if issues:
            failures += 1
            click.echo(f"FAIL ref:{ref_name}: {', '.join(issues)}")
        else:
            click.echo(f"OK   ref:{ref_name}")

    structure_problems = structural_issues(manifest)
    for problem in structure_problems:
        failures += 1
        click.echo(f"FAIL structure: {problem}")

    audit_problems = audit_verify_chain(root)
    for problem in audit_problems:
        failures += 1
        click.echo(f"FAIL audit: {problem}")

    if failures:
        raise click.ClickException(f"{failures} node(s) failed verification")
    click.echo(f"verified {len(manifest.get('nodes', []))} node(s)")


@main.command()
@click.argument("name")
@click.argument("ref")
@click.option(
    "--org-yaml", default=None, help="PhantomOrg org.yaml (authoritative ACL)."
)
@click.option("--actor", default=None, help=_ACTOR_HELP)
@click.option(
    "--nsec-file",
    default=None,
    help="File containing the actor's nsec (issue #30 v2 signing).",
)
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def tag(name, ref, org_yaml, actor, nsec_file, root):
    """Point a mutable ref (e.g. `latest`, `approved-<date>`) at a version MAC.

    Access-controlled: the actor must be able to write the target node's
    category (fail-closed). A signed tag stores a signed record whose
    signature binds the ref name to its target MAC (issue #30 v2), so
    renaming or repointing ``manifest.refs`` after tagging is detected by
    ``verify``.
    """
    actor_id, org = _require_acl(org_yaml, actor)
    service = DocumentService(root)
    try:
        result = service.set_ref(org, actor_id, name=name, ref=ref, nsec_file=nsec_file)
    except DocumentError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"{result['name']} -> {display_id(result['mac'])}  ({result['urn']})")


@main.command()
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def refs(root):
    """List mutable refs."""
    manifest = _load_or_die(root)
    if not manifest.get("refs"):
        click.echo("no refs")
        return
    for name, value in manifest.get("refs", {}).items():
        mac = ref_target_mac(value)
        node = node_by_mac(manifest, mac) if mac else None
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
    "--category", type=str, default=None, help="Check read access for this category id."
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
        cat = normalize_category(category)
        verdict = "ALLOW" if can_read(org, actor, cat) else "DENY"
        click.echo(f"  read {cat}: {verdict}")


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
    "--org-yaml",
    required=True,
    help="PhantomOrg org.yaml (authoritative ACL + inboxes).",
)
@click.option(
    "--actor", required=True, help="Persona id to render the update package for."
)
@click.option(
    "--persona-dir",
    required=True,
    help="Persona installation root (contains kb/, MEMORY.md, tools/).",
)
@click.option("--namespace", default="docs", show_default=True, help="Namespace name.")
@click.option(
    "--org-pubkey", default="", help="Org Nostr pubkey (documented in Documents.md)."
)
def setup(org_yaml, actor, persona_dir, namespace, org_pubkey):
    """Apply the §13 update package onto a persona installation.

    Renders kb/procedures/Documents.md (protocol), a MEMORY.md pointer, and
    tools/documents.sh (wrapper pinning --actor), all bounded by
    ``<!-- phantomdocs:start/end -->`` markers. Idempotent and re-runnable.
    """
    org = load_org(org_yaml)
    known_actors = {a.get("id") for a in org.get("actors", [])}
    if actor not in known_actors:
        raise click.ClickException(
            f"denied: {actor!r} is not an actor in the org model (fail-closed)"
        )

    os.makedirs(os.path.join(persona_dir, "kb", "procedures"), exist_ok=True)
    os.makedirs(os.path.join(persona_dir, "tools"), exist_ok=True)

    documents_md = os.path.join(persona_dir, "kb", "procedures", "Documents.md")
    memory_md = os.path.join(persona_dir, "MEMORY.md")
    wrapper = os.path.join(persona_dir, "tools", "documents.sh")

    apply_bounded(
        documents_md,
        render_documents_protocol(org, actor, namespace, org_pubkey),
    )
    apply_bounded(memory_md, render_memory_pointer(namespace, actor))
    write_wrapper(wrapper, render_wrapper(org_yaml, actor))

    click.echo(f"updated persona {actor!r} for namespace {namespace!r}")
    click.echo(f"  protocol   {documents_md}")
    click.echo(f"  pointer    {memory_md}")
    click.echo(f"  wrapper    {wrapper}")


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
