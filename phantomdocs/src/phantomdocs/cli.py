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
    key_valid_at,
    key_valid_now,
    load_org,
    normalize_category,
    resolved_categories,
)
from .audit import append as audit_append
from .audit import head as audit_head
from .audit import max_seq as audit_max_seq
from .audit import read as audit_read
from .audit import reconcile as audit_reconcile
from .audit import sequence_issues as audit_sequence_issues
from .audit import verify_chain as audit_verify_chain
from .derive import derive_manifest as derive_from_org
from .documents import DocumentError, DocumentService
from .identity import (
    component_for_folder,
    content_hash,
    display_id,
    doc_version_mac,
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
    mutation_sequence_issues,
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
    CRYPTO_VERSION,
    mutation_envelope,
    npub_to_pubkey_hex,
    pubkey_from_nsec,
    seal_envelope,
    sign_seal,
    verify_mutation,
    verify_seal,
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


def _org_pubkey_hex(pubkey: str) -> str:
    """The x-only pubkey hex for ``--org-pubkey`` (npub1... or 64-hex)."""
    if pubkey.startswith("npub1"):
        return npub_to_pubkey_hex(pubkey)
    if len(pubkey) == 64 and all(c in "0123456789abcdef" for c in pubkey):
        return pubkey
    raise click.ClickException(
        f"--org-pubkey must be an npub1... or a 64-hex pubkey, got {pubkey!r}"
    )


@main.command()
@click.option("--org", required=True, help="Organization id (from org.yaml).")
@click.option("--namespace", default="docs", show_default=True, help="Namespace name.")
@click.option("--org-pubkey", default="", help="Org Nostr pubkey for the root MAC.")
@click.option(
    "--require-signatures",
    is_flag=True,
    default=False,
    help="Production security profile: reject unsigned mutations (see "
    "`pd require-signatures`).",
)
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def init(
    org: str,
    namespace: str,
    org_pubkey: str,
    require_signatures: bool,
    root: str,
) -> None:
    """Create a new namespace: manifest + local blob store."""
    _validate_slug(namespace, "namespace")
    path = _manifest_path(root)
    if os.path.exists(path):
        raise click.ClickException(f"manifest already exists: {path}")
    mac = root_mac(org, org_pubkey, namespace)
    os.makedirs(root, exist_ok=True)
    os.makedirs(os.path.join(root, "blobs"), exist_ok=True)
    # Audit-first ordering (issue #74): write the init audit entry, then
    # commit the manifest with the audit head recorded, so the manifest never
    # claims an audit history the log does not actually contain.
    line_hash = audit_append(
        root,
        _resolve_actor(None) or "phantom",
        "init",
        f"urn:{org}:namespace:{namespace}",
        mac,
        None,
        seq=0,
    )
    data = empty_manifest(org, namespace, mac, require_signatures=require_signatures)
    data["manifest"]["auditSeq"] = 1
    data["manifest"]["auditHead"] = line_hash
    save(path, data)
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
    actor_id, _org = _require_acl(org_yaml, actor)
    try:
        service = DocumentService(root, org_yaml, actor_id, nsec_file)
        result = service.create_folder(
            name=name,
            parent=parent,
            category=category,
            owners=list(owners),
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
        try:
            content, ref_location = read_reference(ref)
        except StorageError as exc:
            raise click.ClickException(str(exc))
    else:
        with open(path, "rb") as f:
            content = f.read()
        ref_location = None

    actor_id, _org = _require_acl(org_yaml, actor)
    try:
        service = DocumentService(root, org_yaml, actor_id, nsec_file)
        result = service.add_document(
            content=content,
            ref_location=ref_location,
            slug=slug,
            category=category,
            folder=folder,
            owners=list(owners),
            backend=backend,
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
        try:
            if "ref" in loc:
                data = read_reference(location_uri(loc))[0]
            else:
                data = _resolve_store(root, backend).get(node["contentHash"])
        except StorageError as exc:
            raise click.ClickException(str(exc))
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
@click.option(
    "--org-pubkey",
    default=None,
    help="Org pubkey (npub1... or 64-hex) to recompute the root MAC and "
    "verify the head seal against (issues #70/#71).",
)
@click.option(
    "--expected-head-seq",
    type=int,
    default=None,
    help="The known-current head sequence (external trust root): fails if the "
    "manifest head has been rolled back below it (issue #70).",
)
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def verify(backend, org_yaml, org_pubkey, expected_head_seq, root):
    """Recompute MAC chain + content hashes against the manifest."""
    manifest = _load_or_die(root)
    # Crypto agility (audit decision 3): the namespace declares its crypto
    # suite version; an unsupported version is refused fail-closed (we cannot
    # interpret the node/seal signatures for it).
    _crypto = manifest.get("manifest", {}).get("cryptoVersion")
    if _crypto is not None and _crypto != CRYPTO_VERSION:
        raise click.ClickException(
            f"unsupported crypto version {_crypto!r} (supported: v{CRYPTO_VERSION})"
        )
    store = _resolve_store(root, backend)
    known_macs = {n["mac"] for n in manifest.get("nodes", [])}
    known_macs.add(manifest["manifest"]["rootMac"])

    # Declared actor keys (from org.yaml) for the signature authorization
    # check. Only enforced when --org-yaml is supplied (issue #30 v2). The org
    # model is kept so a signature is validated against the *specific* actor's
    # key *at the mutation timestamp* — honoring rotation windows and
    # revocations (issue #76), not merely "some current declared key".
    org_model = load_org(org_yaml) if org_yaml else None
    require_sig = bool(manifest.get("manifest", {}).get("requireSignatures"))

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
                            doc_version_mac(
                                node["parentMac"],
                                node.get("previous"),
                                node["slug"],
                                data,
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
                                    doc_version_mac(
                                        node["parentMac"],
                                        node.get("previous"),
                                        node["slug"],
                                        data,
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
        # Crypto agility (audit decision 3): the declared node version is
        # authenticated state and must be validated for *every* node — signed
        # or unsigned — so an unsigned node declaring an unsupported version
        # fails closed rather than printing OK. A missing ``cryptoVersion`` is
        # the legacy pre-crypto-agility v1 node: it verifies as v1 but over the
        # legacy envelope (no ``crypto_version`` field), so existing namespaces
        # stay verifiable across the upgrade.
        node_crypto = node.get("cryptoVersion")
        unsupported_node_crypto = (
            node_crypto is not None and node_crypto != CRYPTO_VERSION
        )
        if unsupported_node_crypto:
            issues.append(
                f"unsupported crypto version {node_crypto!r} "
                f"(supported: v{CRYPTO_VERSION})"
            )

        if require_sig and sig is None and sig_pubkey is None:
            issues.append(
                "unsigned mutation in a signatures-required namespace "
                "(manifest.requireSignatures=true)"
            )
        elif sig is not None or sig_pubkey is not None:
            if not sig or not sig_pubkey:
                issues.append("incomplete mutation signature")
            elif not unsupported_node_crypto:
                envelope = mutation_envelope(
                    mac=node["mac"],
                    actor=node.get("actor", ""),
                    action=node.get("action", ""),
                    category=node.get("category", ""),
                    owners=node.get("owners"),
                    locations=node.get("locations"),
                    urn=node.get("urn", ""),
                    seq=node.get("seq"),
                    prev_head=node.get("prevHead"),
                    ts=node.get("ts"),
                    policy_hash=node.get("policyHash"),
                    crypto_version=node_crypto,
                )
                if not verify_mutation(sig_pubkey, sig, envelope):
                    issues.append("signature invalid")
                elif org_model is not None:
                    actor = node.get("actor")
                    ts = node.get("ts")
                    if not actor:
                        issues.append("signature has no actor")
                    elif ts is not None:
                        if not key_valid_at(org_model, actor, sig_pubkey, ts):
                            issues.append(
                                "signature key is not valid for the actor at "
                                "mutation time (undeclared, rotated out, or revoked)"
                            )
                    else:
                        # Legacy node predating #76: no mutation timestamp, so
                        # the rotation window cannot be evaluated. Fall back to
                        # the CURRENT key registry: the node stays verifiable
                        # while the key is still declared and not revoked
                        # (revocation remains fail-closed).
                        if not key_valid_now(org_model, actor, sig_pubkey):
                            issues.append(
                                "signature key is not a currently-valid key for "
                                "the actor (legacy node without ts; re-sign or rotate)"
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
            # Same fail-closed version check as the node path: the declared ref
            # version is authenticated state and must be validated even when the
            # ref is unsigned. A missing ``cryptoVersion`` is the legacy v1 ref
            # (verifies over the legacy envelope without ``crypto_version``).
            ref_crypto = value.get("cryptoVersion")
            unsupported_ref_crypto = (
                ref_crypto is not None and ref_crypto != CRYPTO_VERSION
            )
            if unsupported_ref_crypto:
                issues.append(
                    f"unsupported crypto version {ref_crypto!r} "
                    f"(supported: v{CRYPTO_VERSION})"
                )
            if require_sig and sig is None and sig_pubkey is None:
                issues.append(
                    "unsigned tag in a signatures-required namespace "
                    "(manifest.requireSignatures=true)"
                )
            elif (sig is None) != (sig_pubkey is None):
                issues.append("incomplete ref signature")
            elif sig is not None and not unsupported_ref_crypto:
                envelope = mutation_envelope(
                    mac=mac,
                    actor=value.get("actor", ""),
                    action=value.get("action", "tag"),
                    category=node.get("category", ""),
                    owners=node.get("owners"),
                    locations=node.get("locations"),
                    urn=node.get("urn", ""),
                    ref=ref_name,
                    seq=value.get("seq"),
                    prev_head=value.get("prevHead"),
                    ts=value.get("ts"),
                    policy_hash=value.get("policyHash"),
                    crypto_version=ref_crypto,
                )
                if not verify_mutation(sig_pubkey, sig, envelope):
                    issues.append("ref signature invalid")
                elif org_model is not None:
                    actor = value.get("actor")
                    ts = value.get("ts")
                    if not actor:
                        issues.append("ref signature has no actor")
                    elif ts is not None:
                        if not key_valid_at(org_model, actor, sig_pubkey, ts):
                            issues.append(
                                "ref signature key is not valid for the actor at "
                                "mutation time (undeclared, rotated out, or revoked)"
                            )
                    else:
                        # Legacy ref predating #76: see the node path above.
                        if not key_valid_now(org_model, actor, sig_pubkey):
                            issues.append(
                                "ref signature key is not a currently-valid key "
                                "for the actor (legacy ref without ts; re-sign or rotate)"
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

    sequence_problems = mutation_sequence_issues(manifest)
    for problem in sequence_problems:
        failures += 1
        click.echo(f"FAIL sequence: {problem}")

    audit_problems = audit_verify_chain(root)
    for problem in audit_problems:
        failures += 1
        click.echo(f"FAIL audit: {problem}")

    # Mutation-sequence contiguity (issue #73): the audit log is the
    # authoritative record of every mutation (add/mkdir/tag/rollback); its
    # ``seq`` must be exactly contiguous (init = 0, then 1, 2, ...). A gap,
    # reset, or duplicate breaks the "exactly one valid successor relation"
    # invariant.
    for problem in audit_sequence_issues(root):
        failures += 1
        click.echo(f"FAIL audit: {problem}")

    # headSeq must agree with the audit log's authoritative counter: a node
    # whose seq/headSeq drifted from the audit log (with a valid signature)
    # is a divergence the strictly-increasing node check alone cannot catch.
    audit_max = audit_max_seq(root)
    if audit_max is not None:
        head_seq = manifest.get("manifest", {}).get("headSeq")
        if head_seq is not None and (
            not isinstance(head_seq, int) or isinstance(head_seq, bool)
        ):
            failures += 1
            click.echo(f"FAIL audit: manifest.headSeq {head_seq!r} is not an integer")
        elif head_seq is not None and head_seq != audit_max:
            failures += 1
            click.echo(
                f"FAIL audit: manifest.headSeq {head_seq} does not match "
                f"audit max seq {audit_max}"
            )

    # Audit head anchor (issue #74): the manifest records the expected audit
    # entry count and last-line hash. A crash between the audit append and the
    # manifest commit, or a truncated/rolled-back audit log, makes the actual
    # log diverge from the record — which must be reported, not silently
    # accepted.
    manifest_header = manifest.get("manifest", {})
    expected_seq = manifest_header.get("auditSeq")
    expected_head = manifest_header.get("auditHead")
    if expected_seq is not None:
        actual_count, actual_head = audit_head(root)
        if actual_count != expected_seq:
            failures += 1
            click.echo(
                f"FAIL audit: {actual_count} entries found, "
                f"manifest expects {expected_seq}"
            )
        if expected_head is not None and actual_head != expected_head:
            failures += 1
            click.echo("FAIL audit: head hash does not match manifest.auditHead")

    # Root anchor + head seal (issues #70/#71): when the operator supplies the
    # org pubkey, recompute the root MAC from the org identity + namespace and
    # verify the org's seal over the monotonic head. A forged root, a deleted
    # version, or a rolled-back/truncated audit head all change the sealed
    # envelope and fail here — and only the org (holder of the org key) can
    # re-seal, so this is a trust root the attacker cannot rewrite alongside
    # the manifest.
    manifest_header = manifest.get("manifest", {})
    if expected_head_seq is not None:
        raw_head_seq = manifest_header.get("headSeq")
        if raw_head_seq is not None and (
            not isinstance(raw_head_seq, int) or isinstance(raw_head_seq, bool)
        ):
            failures += 1
            click.echo(
                f"FAIL head: manifest.headSeq {raw_head_seq!r} is not an integer"
            )
        else:
            current_head_seq = raw_head_seq if raw_head_seq is not None else 0
            if current_head_seq < expected_head_seq:
                failures += 1
                click.echo(
                    f"FAIL head: rolled back to headSeq {current_head_seq} "
                    f"(expected at least {expected_head_seq})"
                )

    if org_pubkey:
        pubkey_hex = _org_pubkey_hex(org_pubkey)
        m = manifest_header
        recomputed_root = root_mac(m["org"], org_pubkey, m["namespace"])
        if recomputed_root != m["rootMac"]:
            failures += 1
            click.echo("FAIL root: rootMac does not match the org identity")

        signed = m.get("signedRootMac")
        seal_pubkey = m.get("sealPubkey")
        if signed is None and seal_pubkey is None:
            failures += 1
            click.echo(
                "FAIL seal: manifest has no head seal (run `pd seal` with the org key)"
            )
        elif signed is None or seal_pubkey is None:
            failures += 1
            click.echo("FAIL seal: incomplete seal (missing signature or pubkey)")
        else:
            envelope = seal_envelope(
                root_mac=m["rootMac"],
                head_seq=int(m.get("headSeq") or 0),
                head_mac=m.get("headMac") or m["rootMac"],
                audit_seq=int(m.get("auditSeq") or 0),
                audit_head=m.get("auditHead"),
                # ``None`` => legacy pre-crypto-agility seal envelope (no
                # ``crypto_version`` field), preserving verification of seals
                # made before the crypto-agility upgrade.
                crypto_version=m.get("cryptoVersion"),
            )
            if seal_pubkey != pubkey_hex:
                failures += 1
                click.echo("FAIL seal: seal was not made by the declared org key")
            elif not verify_seal(pubkey_hex, signed, envelope):
                failures += 1
                click.echo(
                    "FAIL seal: head seal signature invalid (forged root, "
                    "deleted version, or rolled-back audit head)"
                )
            elif m.get("sealedHeadSeq") != m.get("headSeq"):
                failures += 1
                click.echo(
                    "FAIL seal: head advanced past the last seal "
                    "(mutations since `pd seal`)"
                )
    else:
        # Fail-closed trust anchor (audit decision 2): a *sealed* namespace
        # verified without --org-pubkey would silently skip the root + seal
        # anchor, so the verification is refused rather than silently weaker.
        m = manifest_header
        if m.get("sealPubkey") is not None or m.get("signedRootMac") is not None:
            failures += 1
            click.echo(
                "FAIL seal: namespace is sealed but --org-pubkey was not "
                "supplied; refusing to skip the trust anchor"
            )

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
    actor_id, _org = _require_acl(org_yaml, actor)
    try:
        service = DocumentService(root, org_yaml, actor_id, nsec_file)
        result = service.set_ref(name=name, ref=ref)
    except DocumentError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"{result['name']} -> {display_id(result['mac'])}  ({result['urn']})")


@main.command()
@click.argument("urn")
@click.option(
    "--to-mac", required=True, help="The version MAC to restore (see `pd versions`)."
)
@click.option(
    "--backend", default=None, help="Backend URI (local:// ssh:// gdrive://)."
)
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
def rollback(urn, to_mac, backend, org_yaml, actor, nsec_file, root):
    """Restore an older version's content as a new, current version.

    Creates a new version whose content equals the target version's content,
    chained off the current version (``previous = current MAC``). Because the
    version identity binds the predecessor (issue #44), the new version gets a
    fresh MAC rather than colliding with the target. History is never
    rewritten or deleted.
    """
    actor_id, _org = _require_acl(org_yaml, actor)
    try:
        service = DocumentService(root, org_yaml, actor_id, nsec_file)
        result = service.rollback(
            urn=urn,
            to_mac=to_mac,
            backend=backend,
        )
    except DocumentError as exc:
        raise click.ClickException(str(exc))
    click.echo(f"rolled back {result['urn']} to {display_id(result['mac'])}")


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


@main.command()
@click.option(
    "--nsec-file",
    required=True,
    help="File containing the org's nsec (the trust-root key, issue #70/#71).",
)
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def seal(nsec_file, root):
    """Seal the namespace head with the org key (issues #70/#71).

    Signs the root MAC together with the head (``headSeq``, ``headMac``,
    ``auditSeq``, ``auditHead``) and records the signature
    (``signedRootMac``), the org pubkey (``sealPubkey``) and the sealed head
    sequence (``sealedHeadSeq``) in the manifest header. ``verify
    --org-pubkey`` then checks the seal, so a forged root, a deleted version,
    or a rolled-back/truncated audit head no longer verifies — only the org
    (holder of the org key) can re-seal.
    """
    path = _manifest_path(root)
    data = _load_or_die(root)
    try:
        with open(nsec_file, "r", encoding="utf-8") as f:
            nsec = f.read().strip()
    except OSError as exc:
        raise click.ClickException(f"cannot read --nsec-file: {exc}")
    if not nsec:
        raise click.ClickException("empty --nsec-file")

    m = data["manifest"]
    envelope = seal_envelope(
        root_mac=m["rootMac"],
        head_seq=int(m.get("headSeq") or 0),
        head_mac=m.get("headMac") or m["rootMac"],
        audit_seq=int(m.get("auditSeq") or 0),
        audit_head=m.get("auditHead"),
        # Match ``verify``: a legacy manifest (no ``cryptoVersion``) is sealed
        # over the legacy envelope, so re-sealing a pre-upgrade namespace does
        # not silently produce a seal it can no longer verify.
        crypto_version=m.get("cryptoVersion"),
    )
    pubkey = pubkey_from_nsec(nsec)
    m["signedRootMac"] = sign_seal(nsec, envelope)
    m["sealPubkey"] = pubkey
    m["sealedHeadSeq"] = int(m.get("headSeq") or 0)
    save(path, data)
    click.echo(f"sealed {m['org']}/{m['namespace']} at headSeq {m['sealedHeadSeq']}")
    click.echo(f"  seal pubkey {pubkey}")


@main.command("require-signatures")
@click.argument("mode", type=click.Choice(["on", "off"], case_sensitive=False))
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def require_signatures(mode, root):
    """Set the namespace signing profile (production security mode).

    ``on`` — production profile: every mutation must carry a valid actor
    signature; an unsigned mutation is rejected fail-closed and ``pd verify``
    flags any unsigned node or ref. ``off`` — legacy/development profile:
    signing is optional and unsigned mutations are accepted (v1 behavior).

    The setting is recorded in the manifest header (``requireSignatures``),
    so it persists for the namespace and is enforced by every mutating
    command and by ``verify``.
    """
    path = _manifest_path(root)
    data = _load_or_die(root)
    data["manifest"]["requireSignatures"] = mode == "on"
    save(path, data)
    click.echo(f"requireSignatures: {mode}")


@main.command("recover")
@click.option("--root", default=".", show_default=True, help="Local backend root.")
def recover(root):
    """Recover from an interrupted transaction (audit-first ordering, #74).

    The only recoverable crash state is *audit ahead of manifest*: a mutation
    was appended to the audit log but its manifest commit did not land. The
    orphaned audit tail is discarded to re-align the log with the manifest.

    Any other divergence (manifest ahead, a broken hash chain, a head-hash
    mismatch, or a sequence gap) is evidence of tampering or manual edits, not
    a crash — it is reported and the namespace is left untouched (fail-closed).
    """
    data = _load_or_die(root)
    header = data.get("manifest", {})
    expected = header.get("auditSeq")
    if expected is None:
        raise click.ClickException(
            "manifest has no auditSeq anchor; nothing to recover"
        )

    try:
        discarded = audit_reconcile(root, int(expected), header.get("auditHead"))
    except ValueError as exc:
        raise click.ClickException(
            "refusing to recover (tampering, not a crash): " + str(exc)
        )

    if discarded == 0:
        click.echo("nothing to recover: audit log is consistent with the manifest")
    else:
        click.echo(
            f"discarded {discarded} orphaned audit "
            f"entr{'y' if discarded == 1 else 'ies'} "
            f"(mutation{'s' if discarded != 1 else ''} committed to the audit log "
            f"but not the manifest)"
        )


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
