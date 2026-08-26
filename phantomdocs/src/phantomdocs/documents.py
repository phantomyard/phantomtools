"""Document application service — the mutating document workflows (issue #46).

The CLI resolves the actor and the PhantomOrg org model (infrastructure) and
delegates the domain workflow — authorize, resolve the parent, compute the
MAC chain, store the blob, build and sign the node, mutate the manifest, and
append the audit entry — to this service. Commands only render the result.

All failures raise :class:`DocumentError` with a user-facing message; the CLI
maps it onto ``click.ClickException`` so the messages stay byte-identical to
the pre-service behavior.
"""

from __future__ import annotations

import os
from typing import Any

from .access import can_write, normalize_category
from .audit import append as audit_append
from .identity import component_for_doc, component_for_folder, content_hash, node_mac
from .manifest import (
    MANIFEST_FILENAME,
    ManifestError,
    ManifestRepository,
    load,
    manifest_lock,
    save,
    urn_path,
)
from .signing import mutation_envelope, pubkey_from_nsec, sign_mutation
from .storage import LocalBackend, resolve_backend


class DocumentError(Exception):
    """A domain failure in the document service; the message is user-facing."""


def _manifest_path(root: str) -> str:
    return os.path.join(root, MANIFEST_FILENAME)


def _signing_key(nsec_file: str | None) -> str | None:
    """Resolve the mutation-signing nsec (SPEC §9, issue #30 v2).

    Precedence: ``--nsec-file`` → ``$PHANTOMDOCS_NSEC``. Returns None when the
    operator has not configured signing, in which case mutations are unsigned
    (v1 behavior).
    """
    if nsec_file:
        try:
            with open(nsec_file, "r", encoding="utf-8") as f:
                return f.read().strip() or None
        except OSError as exc:
            raise DocumentError(f"cannot read --nsec-file: {exc}")
    return os.environ.get("PHANTOMDOCS_NSEC", "").strip() or None


def _sign_fields(
    nsec: str | None,
    *,
    mac: str,
    actor: str,
    action: str,
    category: str,
    owners: list[str] | None,
    locations: list[dict] | None,
    urn: str,
    ref: str | None = None,
) -> dict[str, str]:
    """The ``sig`` / ``sigPubkey`` fields for a mutation, or ``{}`` unsigned.

    The signature binds the node MAC **and** the authorization-relevant fields
    (actor, action, category, owners, locations, urn) by signing the canonical
    mutation envelope (issue #30 v2). ``ref`` is bound only for ``tag``
    mutations, tying the mutable ref name to its target MAC.
    """
    if not nsec:
        return {}
    envelope = mutation_envelope(
        mac=mac,
        actor=actor,
        action=action,
        category=category,
        owners=owners,
        locations=locations,
        urn=urn,
        ref=ref,
    )
    return {
        "sig": sign_mutation(nsec, envelope),
        "sigPubkey": pubkey_from_nsec(nsec),
    }


class DocumentService:
    """Mutating document workflows: create a folder, add/version a doc, set a ref."""

    def __init__(self, root: str):
        self.root = root

    def _load_repo(self) -> ManifestRepository:
        """Load and wrap the manifest, or raise a user-facing error."""
        path = _manifest_path(self.root)
        if not os.path.exists(path):
            raise DocumentError(f"no manifest at {path} — run `pd init` first")
        try:
            return ManifestRepository(load(path))
        except ManifestError as exc:
            raise DocumentError(str(exc))

    def create_folder(
        self,
        org: dict[str, Any],
        actor_id: str,
        *,
        name: str,
        parent: str | None,
        category: str,
        owners: list[str],
        nsec_file: str | None,
    ) -> dict[str, Any]:
        """Create a folder node (a link in the chained MAC hierarchy)."""
        category = normalize_category(category)
        if not can_write(org, actor_id, category, list(owners)):
            raise DocumentError(f"denied: {actor_id} cannot write {category}")

        with manifest_lock(_manifest_path(self.root)):
            repo = self._load_repo()
            parent_mac = repo.root_mac
            parent_path = ""
            if parent:
                p = repo.node_by_path(parent) or repo.node_by_slug(parent)
                if p is None or p.get("kind") != "folder":
                    raise DocumentError(f"parent folder not found: {parent}")
                parent_mac = p["mac"]
                parent_path = urn_path(p["urn"]) + "/"

            mac = node_mac(parent_mac, component_for_folder(name))
            path = f"{parent_path}{name}"
            urn = f"urn:{repo.org}:folder:{path}"
            if repo.node_by_urn(urn) is not None:
                raise DocumentError(f"folder already exists: {urn}")

            node = {
                "urn": urn,
                "mac": mac,
                "parentMac": parent_mac,
                "kind": "folder",
                "slug": name,
                "category": category,
                "owners": list(owners),
                "meta": {},
                "relations": {},
                "actor": actor_id,
                "action": "mkdir",
            }
            node.update(
                _sign_fields(
                    _signing_key(nsec_file),
                    mac=mac,
                    actor=actor_id,
                    action="mkdir",
                    category=category,
                    owners=list(owners),
                    locations=None,
                    urn=urn,
                )
            )
            repo.add_node(node)
            save(_manifest_path(self.root), repo.data)

        audit_append(
            self.root,
            actor_id,
            "mkdir",
            urn,
            mac,
            None,
            node.get("sig"),
            node.get("sigPubkey"),
        )
        return {"path": path, "urn": urn, "mac": mac}

    def add_document(
        self,
        org: dict[str, Any],
        actor_id: str,
        *,
        content: bytes,
        ref_location: dict[str, Any] | None,
        slug: str,
        category: str | None,
        folder: str | None,
        owners: list[str],
        backend: str | None,
        nsec_file: str | None,
    ) -> dict[str, Any]:
        """Ingest a document: compute the MAC chain, store the blob, register
        the node (or version an existing node)."""
        with manifest_lock(_manifest_path(self.root)):
            repo = self._load_repo()
            parent_mac = repo.root_mac
            parent_path = ""
            if folder:
                parent = repo.node_by_path(folder) or repo.node_by_slug(folder)
                if parent is None or parent.get("kind") != "folder":
                    raise DocumentError(f"folder not found: {folder}")
                parent_mac = parent["mac"]
                parent_path = urn_path(parent["urn"]) + "/"

            ch = content_hash(content)
            mac = node_mac(parent_mac, component_for_doc(slug, content))
            logical = f"{parent_path}{slug}"
            urn = f"urn:{repo.org}:doc:{logical}"

            existing = repo.node_by_urn(urn)
            if existing is not None and existing.get("contentHash") == ch:
                return {"unchanged": True, "urn": urn}
            previous = existing["mac"] if existing is not None else None

            # Category: a new node uses --category (default 1); versioning an
            # existing node always preserves the existing node's category. A
            # reclassification must be a separate, explicitly-authorized
            # operation, never a side effect of `add`.
            if existing is not None:
                effective_category = existing["category"]
                if category is not None and normalize_category(
                    category
                ) != normalize_category(effective_category):
                    raise DocumentError(
                        f"denied: cannot reclassify {urn} from "
                        f"{normalize_category(effective_category)} to "
                        f"{normalize_category(category)} via add "
                        "(reclassification is a separate operation)"
                    )
            else:
                effective_category = (
                    "category-1" if category is None else normalize_category(category)
                )

            # Write ACL: base category access; when versioning an existing
            # node, the actor must be one of its declared owners.
            effective_owners = (
                list(existing.get("owners", []) or []) if existing else list(owners)
            )
            if not can_write(org, actor_id, effective_category, effective_owners):
                raise DocumentError(
                    f"denied: {actor_id} cannot write "
                    f"{normalize_category(effective_category)} "
                    f"{'(owner required)' if effective_owners else ''}"
                )

            if ref_location is not None:
                locations = [ref_location]
            else:
                store = resolve_backend(backend) if backend else LocalBackend(self.root)
                location = store.put(ch, content)
                scheme = (
                    backend.split("://")[0] if backend and "://" in backend else "local"
                )
                if scheme == "gdrive":
                    locations = [{"backend": "gdrive", "ref": location}]
                else:
                    locations = [{"backend": scheme, "path": location}]

            node = {
                "urn": urn,
                "mac": mac,
                "parentMac": parent_mac,
                "kind": "doc",
                "slug": slug,
                "category": effective_category,
                "contentHash": ch,
                "size": len(content),
                "owners": effective_owners,
                "locations": locations,
                "meta": {"title": slug},
                "relations": {},
                "previous": previous,
                "actor": actor_id,
                "action": "add" if previous is None else "version",
            }
            node.update(
                _sign_fields(
                    _signing_key(nsec_file),
                    mac=mac,
                    actor=actor_id,
                    action="add" if previous is None else "version",
                    category=effective_category,
                    owners=effective_owners,
                    locations=locations,
                    urn=urn,
                )
            )
            repo.add_node(node)
            save(_manifest_path(self.root), repo.data)

        audit_append(
            self.root,
            actor_id,
            "add" if previous is None else "version",
            urn,
            mac,
            ch,
            node.get("sig"),
            node.get("sigPubkey"),
        )
        return {
            "verb": "added" if previous is None else "versioned",
            "logical": logical,
            "urn": urn,
            "mac": mac,
        }

    def set_ref(
        self,
        org: dict[str, Any],
        actor_id: str,
        *,
        name: str,
        ref: str,
        nsec_file: str | None,
    ) -> dict[str, Any]:
        """Point a mutable ref (e.g. `latest`) at a version MAC."""
        with manifest_lock(_manifest_path(self.root)):
            repo = self._load_repo()
            node = repo.resolve_node(ref)
            if node is None:
                raise DocumentError(f"not found: {ref}")
            if not can_write(
                org, actor_id, node.get("category", 0), node.get("owners")
            ):
                raise DocumentError(
                    f"denied: {actor_id} cannot write "
                    f"{normalize_category(node.get('category', 0))} ({node['urn']})"
                )
            sig_fields = _sign_fields(
                _signing_key(nsec_file),
                mac=node["mac"],
                actor=actor_id,
                action="tag",
                category=node.get("category", ""),
                owners=node.get("owners"),
                locations=node.get("locations"),
                urn=node["urn"],
                ref=name,
            )
            record = {
                "mac": node["mac"],
                "actor": actor_id,
                "action": "tag",
            }
            if sig_fields:
                record.update(sig_fields)
            repo.set_ref(name, record)
            save(_manifest_path(self.root), repo.data)

        audit_append(
            self.root,
            actor_id,
            "tag",
            node["urn"],
            node["mac"],
            node.get("contentHash"),
            record.get("sig"),
            record.get("sigPubkey"),
        )
        return {"name": name, "mac": node["mac"], "urn": node["urn"]}
