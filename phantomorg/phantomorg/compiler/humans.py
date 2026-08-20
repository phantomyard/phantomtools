"""
Human registry generation: compile HUMANS.md from the org model.

org.yaml's optional ``humans:`` block declares the org's external human
counterparts (Board president, treasurer, secretary...) with their
Telegram user ids and Nostr npubs (both nullable until registered). This
module renders a single org-wide ``HUMANS.md`` registry that summarizes
them — the artifact a runtime can consult to know who the humans are and
how to reach them.

Like ``scopes.json``, HUMANS.md is org-level derived state (not
per-actor), written to ``out_dir / HUMANS.md`` by build() and deployed
next to the runtime data dir by `po deploy`.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..spec.model import OrgSpec

HUMANS_FILENAME = "HUMANS.md"


def render_humans(spec: OrgSpec) -> str:
    """Render the org-wide human registry as Markdown."""
    if not spec.humans:
        return ""
    org = spec.organization
    lines = [
        f"# Human Registry — {org.name}",
        "",
        f"Organization: `{org.id}`",
        "",
        (
            "Humans are external counterparts (not personas). Telegram user ids "
            "and Nostr npubs are filled in as they get registered"
        ),
        "",
        "| id | name | role | telegram_user_id | npub |",
        "|---|---|---|---|---|",
    ]
    for h in spec.humans:
        tg = str(h.telegram_user_id) if h.telegram_user_id is not None else "—"
        npub = h.npub if h.npub else "—"
        lines.append(
            f"| `{h.id}` | {h.name or '—'} | {h.role or '—'} | {tg} | `{npub}` |"
        )
    return "\n".join(lines) + "\n"


_HUMANS_SECTION_SEP = "\n\n---\n\n"
_ORG_ID_RE = re.compile(r"^Organization:\s*`([^`]+)`\s*$", re.MULTILINE)


def _extract_org_id(text: str) -> str | None:
    m = _ORG_ID_RE.search(text)
    return m.group(1) if m else None


def merge_humans_markdown(existing: str, incoming: str) -> str:
    """Merge two HUMANS.md registries for a shared data dir (deploy-all).

    deploy-all may compile several orgs into one data dir, so the last org
    must not overwrite the earlier orgs' registry. Each registry is keyed
    by its ``Organization: `<id>``` line and the merge is an UPSERT: a
    re-run replaces the same org's section rather than duplicating it, so
    the result is idempotent across repeated deploy-all runs and keeps each
    org's section fresh.
    """
    incoming_id = _extract_org_id(incoming)
    sections: dict[str, str] = {}
    for block in existing.split(_HUMANS_SECTION_SEP):
        oid = _extract_org_id(block)
        if oid:
            sections[oid] = block
    if incoming_id:
        sections[incoming_id] = incoming
        return (
            _HUMANS_SECTION_SEP.join(sections[oid] for oid in sorted(sections)) + "\n"
        )
    # No org id in the incoming registry (malformed/legacy): preserve both.
    return existing.rstrip() + _HUMANS_SECTION_SEP + incoming.lstrip("\n")


def write_humans(spec: OrgSpec, out_dir: Path) -> Path | None:
    """Write ``out_dir / HUMANS.md`` if the org declares humans.

    Returns the written path, or None when the org has no humans block
    (nothing to write). Uses plain overwrite semantics — derived state,
    no block merging (same policy as scopes.json / the norma).
    """
    if not spec.humans:
        return None
    from .build import write_plain_if_changed  # local import, avoid cycle

    p = out_dir / HUMANS_FILENAME
    if write_plain_if_changed(p, render_humans(spec)):
        return p
    return None
