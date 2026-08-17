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
        "Humans are external counterparts (not personas). Telegram user ids "
        "and Nostr npubs are filled in as they get registered.",
        "",
        "| id | name | role | telegram_user_id | npub |",
        "|---|---|---|---|---|",
    ]
    for h in spec.humans:
        tg = str(h.telegram_user_id) if h.telegram_user_id is not None else "—"
        npub = h.npub if h.npub else "—"
        lines.append(f"| `{h.id}` | {h.name or '—'} | {h.role or '—'} | {tg} | `{npub}` |")
    return "\n".join(lines) + "\n"


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
