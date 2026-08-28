"""Document application service — the mutating document workflows (issue #46).

The service is the *enforcement point*, not a convenience wrapper around the
CLI (issue #69). It establishes its own security context:

  1. loads the org.yaml from a trusted location and validates its schema;
  2. requires the claimed actor to be a declared actor in that org model;
  3. when signing is configured, derives the signing key's pubkey and verifies
     it maps to the claimed actor's declared ``npub`` **at mutation time** —
     so a caller cannot assert "I am actor X" and sign with an unrelated key,
     and cannot forge the org model to grant itself access.

The caller supplies *intent* (a slug, a category, owners, content); the
service supplies and verifies *authority*. All failures raise
:class:`DocumentError` with a user-facing message; the CLI maps it onto
``click.ClickException``.
"""

from __future__ import annotations

import os
import stat
import time
from typing import Any

from .access import can_write, key_valid_now, load_org, normalize_category
from .audit import append as audit_append
from .identity import component_for_folder, content_hash, doc_version_mac, node_mac
from .manifest import (
    MANIFEST_FILENAME,
    ManifestError,
    ManifestRepository,
    load,
    manifest_lock,
    save,
    urn_path,
)
from .signing import (
    mutation_envelope,
    npub_to_pubkey_hex,
    pubkey_from_nsec,
    sign_mutation,
)
from .storage import (
    LocalBackend,
    location_uri,
    read_reference,
    resolve_backend,
)


class DocumentError(Exception):
    """A domain failure in the document service; the message is user-facing."""


def _manifest_path(root: str) -> str:
    return os.path.join(root, MANIFEST_FILENAME)


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string (seconds precision)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _signing_key(nsec_file: str | None) -> str | None:
    """Resolve the mutation-signing nsec (SPEC §9, issue #30 v2, #76).

    Precedence: ``--nsec-file`` → ``$PHANTOMDOCS_NSEC``. Returns None when the
    operator has not configured signing, in which case mutations are unsigned
    (v1 behavior).

    The nsec file must be private (issue #76.1): a regular file (no symlink),
    owned by the calling user, with mode 0600 or more restrictive. A
    world/group-readable or writable key file is refused fail-closed.
    """
    if nsec_file:
        _check_nsec_file(nsec_file)
        try:
            with open(nsec_file, "r", encoding="utf-8") as f:
                return f.read().strip() or None
        except OSError as exc:
            raise DocumentError(f"cannot read --nsec-file: {exc}")
    return os.environ.get("PHANTOMDOCS_NSEC", "").strip() or None


def _check_nsec_file(path: str) -> None:
    """Enforce private-file hygiene on a signing key file (issue #76.1).

    Rejects: symlinks, non-regular files, group/other access bits, and — on
    POSIX — a file not owned by the calling user.
    """
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise DocumentError(f"cannot read --nsec-file: {exc}")
    if stat.S_ISLNK(st.st_mode):
        raise DocumentError(f"refusing symlinked --nsec-file: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise DocumentError(f"--nsec-file is not a regular file: {path}")
    if os.name == "posix":
        if st.st_uid != os.geteuid():
            raise DocumentError(
                f"--nsec-file is not owned by the calling user: {path}"
            )
        if stat.S_IMODE(st.st_mode) & 0o077:
            raise DocumentError(
                f"--nsec-file must be 0600 (or more restrictive), got "
                f"{oct(stat.S_IMODE(st.st_mode))}: {path}"
            )


def _declared_npub(org: dict[str, Any], actor_id: str) -> str | None:
    """The ``npub`` declared for ``actor_id`` in the org model, or None."""
    for a in org.get("actors", []):
        if isinstance(a, dict) and a.get("id") == actor_id:
            npub = a.get("npub")
            return npub if isinstance(npub, str) and npub.strip() else None
    return None


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
    seq: int | None = None,
    prev_head: str | None = None,
    ts: str | None = None,
) -> dict[str, str]:
    """The ``sig`` / ``sigPubkey`` fields for a mutation, or ``{}`` unsigned.

    The signature binds the node MAC **and** the authorization-relevant fields
    (actor, action, category, owners, locations, urn) by signing the canonical
    mutation envelope (issue #30 v2). ``ref`` is bound only for ``tag``
    mutations, tying the mutable ref name to its target MAC. ``seq`` and
    ``prev_head`` bind the mutation to a specific committed state (issue #73),
    so a replayed or out-of-order mutation no longer verifies. ``ts`` is the
    mutation timestamp, bound so key rotation/revocation (issue #76) is judged
    against the moment the mutation was authorized.
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
        seq=seq,
        prev_head=prev_head,
        ts=ts,
    )
    return {
        "sig": sign_mutation(nsec, envelope),
        "sigPubkey": pubkey_from_nsec(nsec),
    }


class DocumentService:
    """Mutating document workflows: create a folder, add/version a doc, set a ref.

    The service resolves and verifies the security context itself (issue #69);
    it does not accept a caller-supplied org model or a caller-asserted actor
    as authority.
    """

    def __init__(
        self,
        root: str,
        org_yaml_path: str,
        actor_id: str,
        nsec_file: str | None = None,
    ):
        self.root = root
        self.org = self._load_org(org_yaml_path)
        self.actor_id = self._require_declared_actor(actor_id)
        self.nsec = _signing_key(nsec_file)
        self._bind_key_to_actor()

    # -- security context (issue #69) --

    def _load_org(self, org_yaml_path: str) -> dict[str, Any]:
        """Load the org model from a *trusted location* and validate its schema.

        The org model is the root of the authorization decision; the service
        loads it itself rather than trusting a caller-supplied dict, so a
        caller cannot forge a permissive org to grant itself access.
        """
        try:
            return load_org(org_yaml_path)
        except (OSError, ValueError) as exc:
            raise DocumentError(
                f"cannot load org model from {org_yaml_path!r}: {exc}"
            ) from exc

    def _require_declared_actor(self, actor_id: str) -> str:
        """The actor must be declared in the org model (fail-closed)."""
        known = {
            a.get("id")
            for a in self.org.get("actors", [])
            if isinstance(a, dict) and a.get("id")
        }
        if actor_id not in known:
            raise DocumentError(
                f"denied: {actor_id!r} is not an actor in the org model; "
                "PhantomDocs refuses unmapped actors (fail-closed)"
            )
        return actor_id

    def _bind_key_to_actor(self) -> None:
        """Verify the signing key maps to the claimed actor (issue #69, #76).

        When signing is configured, the nsec's pubkey must be a *currently
        valid* key for the actor — declared, not revoked, and within its
        rotation window (issue #76.2/#76.3). This is enforced at mutation time
        (not opt-in at ``verify``), so a caller cannot assert one actor and
        sign with another actor's key, nor sign with a revoked/expired key.
        An actor with no declared npub cannot sign.
        """
        if not self.nsec:
            return
        signing_pubkey = pubkey_from_nsec(self.nsec)
        npub = _declared_npub(self.org, self.actor_id)
        if not npub:
            raise DocumentError(
                f"denied: actor {self.actor_id!r} has no declared npub in the "
                "org model; cannot sign mutations on its behalf"
            )
        if not key_valid_now(self.org, self.actor_id, signing_pubkey):
            raise DocumentError(
                f"denied: signing key does not match a currently-valid declared "
                f"npub for actor {self.actor_id!r} (revoked, rotated out, or "
                "undeclared)"
            )

    # -- repository plumbing --

    def _commit_transaction(
        self,
        repo: ManifestRepository,
        *,
        seq: int,
        action: str,
        urn: str,
        mac: str,
        ch: str | None,
        sig: str | None = None,
        sig_pubkey: str | None = None,
        head_mac: str | None = None,
    ) -> None:
        """Commit one mutation as manifest + audit, under the caller's lock.

        Ordering (issue #74): the audit entry is written *first*, then the
        manifest is committed with the audit head anchor (``auditSeq`` +
        ``auditHead``) and the monotonic mutation head (``headSeq`` +
        ``headMac``). A crash between the two leaves the audit one entry
        ahead of the manifest — a *detectable* state `verify` flags — instead
        of a committed mutation with no matching audit entry (lost evidence).

        ``seq`` is the monotonic mutation sequence this commit advances the
        head to (issue #73); the caller computes it from the current head
        before signing, so the signed envelope and the committed head agree.
        """
        audit_seq = int(repo.data["manifest"].get("auditSeq") or 0) + 1
        line_hash = audit_append(
            self.root,
            self.actor_id,
            action,
            urn,
            mac,
            ch,
            sig,
            sig_pubkey,
            seq=seq,
        )
        m = repo.data["manifest"]
        m["headSeq"] = seq
        if head_mac is not None:
            m["headMac"] = head_mac
        m["auditSeq"] = audit_seq
        m["auditHead"] = line_hash
        save(_manifest_path(self.root), repo.data)

    def _next_head(self, repo: ManifestRepository) -> tuple[int, str]:
        """The ``(seq, prev_head)`` a new mutation binds to (issue #73).

        ``seq`` is the current head sequence + 1; ``prev_head`` is the
        committed head MAC (``headMac``, falling back to ``rootMac`` before
        the first mutation). Binding these into the signed envelope makes a
        replay or an out-of-order re-insertion fail to verify.
        """
        m = repo.data["manifest"]
        prev_head = m.get("headMac") or m["rootMac"]
        seq = int(m.get("headSeq") or 0) + 1
        return seq, prev_head

    def _load_repo(self) -> ManifestRepository:
        """Load and wrap the manifest, or raise a user-facing error."""
        path = _manifest_path(self.root)
        if not os.path.exists(path):
            raise DocumentError(f"no manifest at {path} — run `pd init` first")
        try:
            return ManifestRepository(load(path))
        except ManifestError as exc:
            raise DocumentError(str(exc))

    def _sign_fields(
        self,
        *,
        mac: str,
        action: str,
        category: str,
        owners: list[str] | None,
        locations: list[dict] | None,
        urn: str,
        ref: str | None = None,
        seq: int | None = None,
        prev_head: str | None = None,
        ts: str | None = None,
    ) -> dict[str, str]:
        return _sign_fields(
            self.nsec,
            mac=mac,
            actor=self.actor_id,
            action=action,
            category=category,
            owners=owners,
            locations=locations,
            urn=urn,
            ref=ref,
            seq=seq,
            prev_head=prev_head,
            ts=ts,
        )

    # -- workflows --

    def create_folder(
        self,
        *,
        name: str,
        parent: str | None,
        category: str,
        owners: list[str],
    ) -> dict[str, Any]:
        """Create a folder node (a link in the chained MAC hierarchy)."""
        category = normalize_category(category)
        if not can_write(self.org, self.actor_id, category, list(owners)):
            raise DocumentError(f"denied: {self.actor_id} cannot write {category}")

        with manifest_lock(_manifest_path(self.root)):
            repo = self._load_repo()
            seq, prev_head = self._next_head(repo)
            ts = _now_iso()
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
                "actor": self.actor_id,
                "action": "mkdir",
                "seq": seq,
                "prevHead": prev_head,
                "ts": ts,
            }
            node.update(
                self._sign_fields(
                    mac=mac,
                    action="mkdir",
                    category=category,
                    owners=list(owners),
                    locations=None,
                    urn=urn,
                    seq=seq,
                    prev_head=prev_head,
                    ts=ts,
                )
            )
            repo.add_node(node)
            self._commit_transaction(
                repo,
                seq=seq,
                action="mkdir",
                urn=urn,
                mac=mac,
                ch=None,
                sig=node.get("sig"),
                sig_pubkey=node.get("sigPubkey"),
                head_mac=mac,
            )

        return {"path": path, "urn": urn, "mac": mac}

    def add_document(
        self,
        *,
        content: bytes,
        ref_location: dict[str, Any] | None,
        slug: str,
        category: str | None,
        folder: str | None,
        owners: list[str],
        backend: str | None,
    ) -> dict[str, Any]:
        """Ingest a document: compute the MAC chain, store the blob, register
        the node (or version an existing node)."""
        with manifest_lock(_manifest_path(self.root)):
            repo = self._load_repo()
            seq, prev_head = self._next_head(repo)
            ts = _now_iso()
            parent_mac = repo.root_mac
            parent_path = ""
            if folder:
                parent = repo.node_by_path(folder) or repo.node_by_slug(folder)
                if parent is None or parent.get("kind") != "folder":
                    raise DocumentError(f"folder not found: {folder}")
                parent_mac = parent["mac"]
                parent_path = urn_path(parent["urn"]) + "/"

            ch = content_hash(content)
            logical = f"{parent_path}{slug}"
            urn = f"urn:{repo.org}:doc:{logical}"

            existing = repo.node_by_urn(urn)
            if existing is not None and existing.get("contentHash") == ch:
                return {"unchanged": True, "urn": urn}
            previous = existing["mac"] if existing is not None else None
            # Version identity binds the predecessor (issue #44): the first
            # version chains off the tree parent; later versions chain off the
            # previous version, so the history is cryptographically chained and
            # a rollback to older content gets a distinct identity.
            mac = doc_version_mac(parent_mac, previous, slug, content)

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
            if not can_write(
                self.org, self.actor_id, effective_category, effective_owners
            ):
                raise DocumentError(
                    f"denied: {self.actor_id} cannot write "
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
                "actor": self.actor_id,
                "action": "add" if previous is None else "version",
                "seq": seq,
                "prevHead": prev_head,
                "ts": ts,
            }
            node.update(
                self._sign_fields(
                    mac=mac,
                    action="add" if previous is None else "version",
                    category=effective_category,
                    owners=effective_owners,
                    locations=locations,
                    urn=urn,
                    seq=seq,
                    prev_head=prev_head,
                    ts=ts,
                )
            )
            repo.add_node(node)
            self._commit_transaction(
                repo,
                seq=seq,
                action="add" if previous is None else "version",
                urn=urn,
                mac=mac,
                ch=ch,
                sig=node.get("sig"),
                sig_pubkey=node.get("sigPubkey"),
                head_mac=mac,
            )

        return {
            "verb": "added" if previous is None else "versioned",
            "logical": logical,
            "urn": urn,
            "mac": mac,
        }

    def set_ref(self, *, name: str, ref: str) -> dict[str, Any]:
        """Point a mutable ref (e.g. `latest`) at a version MAC."""
        with manifest_lock(_manifest_path(self.root)):
            repo = self._load_repo()
            seq, prev_head = self._next_head(repo)
            ts = _now_iso()
            node = repo.resolve_node(ref)
            if node is None:
                raise DocumentError(f"not found: {ref}")
            if not can_write(
                self.org, self.actor_id, node.get("category", 0), node.get("owners")
            ):
                raise DocumentError(
                    f"denied: {self.actor_id} cannot write "
                    f"{normalize_category(node.get('category', 0))} ({node['urn']})"
                )
            sig_fields = self._sign_fields(
                mac=node["mac"],
                action="tag",
                category=node.get("category", ""),
                owners=node.get("owners"),
                locations=node.get("locations"),
                urn=node["urn"],
                ref=name,
                seq=seq,
                prev_head=prev_head,
                ts=ts,
            )
            record = {
                "mac": node["mac"],
                "actor": self.actor_id,
                "action": "tag",
                "seq": seq,
                "prevHead": prev_head,
                "ts": ts,
            }
            if sig_fields:
                record.update(sig_fields)
            repo.set_ref(name, record)
            self._commit_transaction(
                repo,
                seq=seq,
                action="tag",
                urn=node["urn"],
                mac=node["mac"],
                ch=node.get("contentHash"),
                sig=record.get("sig"),
                sig_pubkey=record.get("sigPubkey"),
            )

        return {"name": name, "mac": node["mac"], "urn": node["urn"]}

    def rollback(self, *, urn: str, to_mac: str, backend: str | None) -> dict[str, Any]:
        """Restore an older version's content as a new, current version.

        Creates a new version whose content equals the target version's
        content, chained off the current version (``previous = current MAC``).
        Because the version identity binds the predecessor (issue #44), the new
        version gets a fresh MAC rather than colliding with the target. History
        is never rewritten or deleted.
        """
        with manifest_lock(_manifest_path(self.root)):
            repo = self._load_repo()
            seq, prev_head = self._next_head(repo)
            ts = _now_iso()
            target = repo.node_by_mac(to_mac)
            if target is None:
                raise DocumentError(f"no version with MAC: {to_mac}")
            if target.get("kind") != "doc":
                raise DocumentError(
                    f"rollback target is not a document: {target['urn']}"
                )
            current = repo.resolve_node(urn)
            if current is None:
                raise DocumentError(f"not found: {urn}")
            if current["mac"] == target["mac"]:
                raise DocumentError(f"already at version {to_mac}")
            # The rollback target must belong to the document being rolled
            # back. Otherwise an actor who can write a low-category document
            # could mint a new HEAD version of *any* document whose MAC they
            # know, inheriting the low category (ACL bypass + downgrade).
            if target["urn"] != current["urn"]:
                raise DocumentError(
                    f"rollback target {to_mac} belongs to {target['urn']}, not {urn}"
                )

            category = current.get("category", 0)
            if not can_write(self.org, self.actor_id, category, current.get("owners")):
                raise DocumentError(
                    f"denied: {self.actor_id} cannot write "
                    f"{normalize_category(category)} ({current['urn']})"
                )

            # Read the target version's content and verify it against its hash.
            ch = target["contentHash"]
            loc = target.get("locations", [{}])[0]
            if "ref" in loc:
                data = read_reference(location_uri(loc))[0]
            else:
                store = resolve_backend(backend) if backend else LocalBackend(self.root)
                data = store.get(ch)
            if content_hash(data) != ch:
                raise DocumentError("rollback target content hash mismatch")

            # The new version chains off the current version, so restoring old
            # content yields a fresh identity (issues #44/#55). Derive slug and
            # urn from `current` (the document being rolled back), not `target`,
            # so the node's tree position always matches its URN path.
            parent_mac = current["parentMac"]
            mac = doc_version_mac(parent_mac, current["mac"], current["slug"], data)
            effective_owners = list(current.get("owners", []) or [])
            new_locations = list(target.get("locations", []) or [])
            node = {
                "urn": current["urn"],
                "mac": mac,
                "parentMac": parent_mac,
                "kind": "doc",
                "slug": current["slug"],
                "category": category,
                "contentHash": ch,
                "size": len(data),
                "owners": effective_owners,
                "locations": new_locations,
                "meta": dict(current.get("meta", {})),
                "relations": dict(current.get("relations", {})),
                "previous": current["mac"],
                "actor": self.actor_id,
                "action": "rollback",
                "seq": seq,
                "prevHead": prev_head,
                "ts": ts,
            }
            node.update(
                self._sign_fields(
                    mac=mac,
                    action="rollback",
                    category=category,
                    owners=effective_owners,
                    locations=new_locations,
                    urn=current["urn"],
                    seq=seq,
                    prev_head=prev_head,
                    ts=ts,
                )
            )
            repo.add_node(node)
            self._commit_transaction(
                repo,
                seq=seq,
                action="rollback",
                urn=current["urn"],
                mac=mac,
                ch=ch,
                sig=node.get("sig"),
                sig_pubkey=node.get("sigPubkey"),
                head_mac=mac,
            )

        return {"urn": current["urn"], "mac": mac}
