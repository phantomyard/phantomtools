"""Runtime discovery for PhantomMeet.

PhantomMeet is installation-agnostic: instead of hardcoding who is who, the
installer *discovers* the ground truth at apply time:

- the **persona host**: which personas are actually installed under the
  target directory (phantombot persona root),
- the **PhantomOrg org model** (when reachable): the org's actors and
  roles, so the installer can cross-check the persona list against the
  organization's declared hierarchy.

The human operator then decides (interactively) who may schedule meetings;
that decision is persisted in the manifest as ``invite.roles``. Nothing is
hardcoded in the package itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Files whose presence marks a directory as a real persona (not a stray
# folder). A persona must have at least one of these.
PERSONA_MARKERS = ("identity.json", "SOUL.md", "phantomchat.json")


@dataclass
class Persona:
    """A persona discovered on the persona host."""

    id: str
    path: Path
    has_identity: bool = False
    has_soul: bool = False
    has_phantomchat: bool = False
    role: str = ""  # org-model role id, when the org model is reachable

    @property
    def markers(self) -> list[str]:
        return [
            name
            for name, present in (
                ("identity.json", self.has_identity),
                ("SOUL.md", self.has_soul),
                ("phantomchat.json", self.has_phantomchat),
            )
            if present
        ]


@dataclass
class OrgModel:
    """A loaded PhantomOrg org model (org.yaml)."""

    path: Path
    org_id: str
    actors: dict[str, dict[str, Any]] = field(default_factory=dict)

    def role_of(self, persona_id: str) -> str:
        actor = self.actors.get(persona_id)
        if not actor:
            return ""
        return str(actor.get("role", ""))


@dataclass
class Discovery:
    """Everything the installer learned about the installation."""

    target: Path
    personas: list[Persona] = field(default_factory=list)
    org_model: OrgModel | None = None

    @property
    def persona_ids(self) -> list[str]:
        return [p.id for p in self.personas]

    def render(self) -> str:
        lines = ["Discovery:"]
        lines.append(f"  persona host : {self.target}")
        if self.org_model:
            lines.append(
                f"  org model    : {self.org_model.path} (org {self.org_model.org_id})"
            )
        else:
            lines.append("  org model    : not found")
        if self.personas:
            lines.append("  personas     :")
            for p in self.personas:
                markers = ", ".join(p.markers) or "no markers"
                role = f" (org role: {p.role})" if p.role else ""
                lines.append(f"    - {p.id}{role} [{markers}]")
        else:
            lines.append("  personas     : none found")
        return "\n".join(lines)


def discover_personas(target: str | Path) -> list[Persona]:
    """Scan ``target`` for persona directories.

    A directory counts as a persona when it contains at least one of the
    marker files (identity.json, SOUL.md, phantomchat.json).
    """
    root = Path(target)
    if not root.is_dir():
        return []
    personas: list[Persona] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        p = Persona(id=child.name, path=child)
        p.has_identity = (child / "identity.json").is_file()
        p.has_soul = (child / "SOUL.md").is_file()
        p.has_phantomchat = (child / "phantomchat.json").is_file()
        if p.markers:
            personas.append(p)
    return personas


def find_org_model(candidates: list[str | Path] | None = None) -> Path | None:
    """Locate a PhantomOrg org model on this machine.

    ``candidates`` may be explicit paths (e.g. from ``--org``); otherwise a
    few conventional locations are probed. Returns the first org.yaml found.
    """
    if candidates:
        for c in candidates:
            p = Path(c).expanduser()
            if p.is_file() and p.name == "org.yaml":
                return p
            if p.is_dir():
                for candidate in sorted(p.rglob("org.yaml")):
                    return candidate

    home = Path.home()
    conventional = [
        home / "Desktop" / "phantomorg" / "organizations",
        home / "phantomorg" / "organizations",
        home / ".phantomorg" / "organizations",
    ]
    for base in conventional:
        if base.is_dir():
            for candidate in sorted(base.rglob("org.yaml")):
                return candidate
    return None


def load_org_model(path: str | Path) -> OrgModel:
    """Load a PhantomOrg org model, tolerant of missing bits."""
    p = Path(path)
    raw: dict[str, Any] = {}
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    org = raw.get("organization") or {}
    org_id = org.get("id") if isinstance(org, dict) else None
    actors: dict[str, dict[str, Any]] = {}
    for actor in raw.get("actors") or []:
        if isinstance(actor, dict) and actor.get("id"):
            actors[str(actor["id"])] = actor
    return OrgModel(path=p, org_id=str(org_id or "?"), actors=actors)


def discover(
    target: str | Path, org_paths: list[str | Path] | None = None
) -> Discovery:
    """Full discovery: personas on the host + optional org model."""
    target_path = Path(target)
    d = Discovery(target=target_path, personas=discover_personas(target_path))
    org_file = find_org_model(org_paths)
    if org_file is not None:
        model = load_org_model(org_file)
        for p in d.personas:
            p.role = model.role_of(p.id)
        d.org_model = model
    return d


def prompt_for_roles(
    discovery: Discovery, existing: list[str] | None = None
) -> list[str]:
    """Interactively ask the operator who may schedule meetings.

    ``existing`` pre-selects the current ``invite.roles`` (if any). Returns
    the chosen persona ids in the order they were offered.
    """
    import click

    ids = discovery.persona_ids
    if not ids:
        raise click.ClickException(
            "no personas discovered under the target; cannot ask who may schedule"
        )

    click.echo("")
    click.echo("Who may schedule meetings (create invitations)?")
    click.echo("  (pick one or more personas by number or name, comma-separated)")
    click.echo("")
    current = set(existing or [])
    for i, pid in enumerate(ids, start=1):
        mark = "x" if pid in current else " "
        role = ""
        if discovery.org_model:
            org_role = discovery.org_model.role_of(pid)
            role = f"  (org role: {org_role})" if org_role else ""
        click.echo(f"  [{mark}] {i:>2}) {pid}{role}")
    click.echo("")

    def _parse(raw: str) -> list[str] | None:
        """Return the chosen persona ids, or None when the input is invalid."""
        chosen: list[str] = []
        by_id = {pid.lower(): pid for pid in ids}
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            if token.lower() in by_id:  # name form: pepa
                pid = by_id[token.lower()]
            else:  # number form: 3
                try:
                    idx = int(token) - 1
                except ValueError:
                    return None
                if idx < 0 or idx >= len(ids):
                    return None
                pid = ids[idx]
            if pid not in chosen:
                chosen.append(pid)
        return chosen

    while True:
        raw = click.prompt("Selection", default="", show_default=False).strip()
        if not raw and existing:
            return list(existing)
        if not raw:
            click.echo("No selection made; nothing will be granted.")
            return []
        chosen = _parse(raw)
        if chosen is None or not chosen:
            click.echo(
                f"Invalid selection: {raw!r} — enter numbers or names separated by commas (e.g. 1,3,5 or pepa,paco)."
            )
            continue
        return chosen


BASE_CARD_EXAMPLE = """📅 Meeting: %TITLE%
👥 Recipients: %RECIPIENTS%
🕐 %DATETIME%
🔗 %LINK%
%PASSWORD_LINE%"""


REQUIRED_CARD_TOKENS_DOC = "%TITLE%, %DATETIME%, %LINK%"


def prompt_for_card(
    existing: str | None = None,
    base_card: str = BASE_CARD_EXAMPLE,
    required: str = REQUIRED_CARD_TOKENS_DOC,
) -> str | None:
    """Interactively ask the operator for the announcement card format.

    Shows the current/base card, then lets the operator:

    - keep it as-is (empty input),
    - paste a fully custom card (multi-line input terminated by ``.`` on its
      own line, or a single line when the card has no newlines),
    - ``base`` to restore the built-in base template,
    - ``clear`` to remove the custom card (use the built-in format).

    Returns the chosen card text, or ``None`` when the operator aborts
    (Ctrl-C / abort). The mandatory tokens are checked by the caller
    (manifest validation) — a card missing them is rejected with a clear
    error at apply time.
    """
    import click

    click.echo("")
    click.echo("Announcement card (the meeting invitation message format)")
    click.echo(f"  Mandatory tokens: {required}")
    click.echo("  Optional tokens: %RECIPIENTS%, %ROOM%, %PASSWORD_LINE%")
    click.echo("")
    click.echo("Current card:")
    click.echo("----------------------------------------")
    click.echo(existing if existing else "(built-in language-aware format)")
    click.echo("----------------------------------------")
    click.echo("")
    click.echo(
        "Options: empty = keep current, 'base' = restore base template, "
        "'clear' = built-in format, or paste your card."
    )
    click.echo("Multi-line cards: end with a line containing only a dot ('.').")
    click.echo("")

    while True:
        click.echo("Your card (or option):")
        try:
            first = click.prompt("> ", default="", show_default=False).strip()
        except click.Abort:
            click.echo("\nAborted.")
            return None
        if first == "":
            return existing  # keep as-is (may be None -> built-in)
        if first == "base":
            return base_card
        if first == "clear":
            return None
        # Single-line card: no further input needed.
        if "%" in first and "\n" not in first and not first.endswith("."):
            return first
        # Multi-line: keep reading until a lone dot.
        lines = [first]
        click.echo("Continue (end with a line containing only '.'):")
        while True:
            try:
                line = click.prompt("> ", default="", show_default=False)
            except click.Abort:
                click.echo("\nAborted.")
                return None
            if line.strip() == ".":
                break
            lines.append(line)
        card = "\n".join(lines).strip("\n")
        if card:
            return card
        click.echo("Empty card; try again.")
