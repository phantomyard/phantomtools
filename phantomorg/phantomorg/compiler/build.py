"""
build(): implements the pseudocode of section 8 of the spec.

    for actor in org_spec.actors:
        role = resolve(actor.role, ...)
        department = resolve(role.department, ...)
        access = merge_access(...)
        escalation = escalation_paths_for(...)
        render identity/soul/tools/memory
        write_if_changed(...) / write_if_missing(...)
        ensure_scaffold(...)

Two distinct write strategies, not one:

- `write_if_changed` (IDENTITY.md, SOUL.md, tools.md): merges by blocks
  (see blocks.py). The `ORG:BEGIN/END` sections are always regenerated;
  everything else in the file is preserved. It replaces the previous
  "frozen whole-file" scheme, which was a real gap: it also froze
  spec-derived sections (security/escalation/comms) when the user only
  wanted to add a manual note.

- `write_if_missing` (MEMORY.md): creates the file only if it doesn't
  exist. MEMORY.md accumulates facts during the agent's real operation
  (the runtime writes there), so it must never be regenerated after its
  initial creation — not even with block merging.
"""

from __future__ import annotations

import datetime
import os
import re
import shutil
import tempfile
import warnings
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from ..spec.model import Actor, OrgSpec, Role
from .access import merge_access
from .blocks import has_blocks, merge_content
from .errors import CompileError
from .escalation import escalation_paths_for
from .humans import HUMANS_FILENAME, write_humans
from .i18n import available_languages, get_strings
from .phantomchat_gen import PHANTOMCHAT_FILENAME, phantomchat_config
from .request_id import resolve_request_id_format
from .scopes import SCOPES_FILENAME, derive_scopes, serialize_scopes

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# mkstemp leftovers from a crashed `_atomic_write` (SIGKILL between mkstemp
# and os.replace) are named `.{name}.{6 alnum}` in the output tree. They are
# harmless but accumulate; the build cleans ones older than this cutoff so a
# live writer's temp file is never touched (builds are single-process and
# finish in well under an hour).
_STALE_TMP_MAX_AGE = datetime.timedelta(hours=1)
_STALE_TMP_RE = re.compile(r"^\..+\.[A-Za-z0-9]{6}$")


def _cleanup_stale_tmp(out_dir: Path) -> None:
    """Remove mkstemp leftovers from crashed builds (C, crash-point audit).

    Only files matching the mkstemp shape ``.{name}.{6 alnum}`` and older
    than the cutoff are removed — never directories, symlinks, or fresh
    temps that could belong to a concurrent writer.
    """
    if not out_dir.is_dir():
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    for p in out_dir.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        if not _STALE_TMP_RE.match(p.name):
            continue
        try:
            mtime = datetime.datetime.fromtimestamp(
                p.stat().st_mtime, datetime.timezone.utc
            )
        except OSError:
            continue
        if now - mtime > _STALE_TMP_MAX_AGE:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


_SCAFFOLD_MEMORY_DIRS = ["archive"]
_SCAFFOLD_KB_DIRS = [
    "inbox",
    "concepts",
    "runbooks",
    "procedures",
    "decisions",
    "infra",
    "people",
    "projects",
    "postmortems",
    "templates",
]

# Seed files matching phantombot's persona scaffold (personaScaffold.ts):
# the memory system expects `memory/people.md` & co. as structured drawer
# FILES (the nightly cycle promotes tagged entries into them), plus a KB
# home page and note templates. Written only if missing, never overwritten.
_SEED_FILES: dict[str, str] = {
    "memory/people.md": (
        "# People\n\nContacts, relationships, dynamics. The nightly cycle "
        "promotes [person]-tagged entries from daily files to here.\n\n"
        "## (no entries yet)\n"
    ),
    "memory/decisions.md": (
        '# Decisions\n\nChoices with rationale. "We chose X because Y." '
        "Promoted from daily files by the nightly cycle.\n\n"
        "## (no entries yet)\n"
    ),
    "memory/lessons.md": (
        "# Lessons\n\nMistakes and learnings. Grows, never shrinks.\n\n"
        "## (no entries yet)\n"
    ),
    "memory/commitments.md": (
        "# Commitments\n\nDeadlines and obligations. The nightly cycle promotes "
        "[commitment]-tagged entries.\n\n"
        "## (no entries yet)\n"
    ),
    "memory/norms.md": (
        "# Norms\n\nRoutine communication patterns used by the operator. "
        "Channels, expected cadence, request-id conventions, and known "
        'counterparties belong here. The ORG:BEGIN/END "norms" block below '
        "is compiled from org.yaml on every build; everything OUTSIDE that "
        "block is owned by the capture/heartbeat/nightly pipeline and is "
        "preserved.\n\n"
        "The full protocol page lives in the KB: see "
        "[[procedures/comunicacion-agentes]].\n\n"
        "<!-- ORG:BEGIN norms -->\n<!-- ORG:END norms -->\n"
    ),
    "kb/Home.md": (
        "---\ntype: index\ntitle: Home\ndescription: Persona knowledge-base index.\naliases: [home]\ntags: [navigation]\ncreated: {today}\nupdated: {today}\n---\n\n"
        "# Home\n\nAtomic notes — one idea per file, linked with [[wikilinks]]. "
        "Every note carries YAML frontmatter (`type`, `tags`, `created`, "
        "`updated`).\n\n## Categories\n\n"
        "- [[concepts/]] — conceptual atoms (definitions, mental models)\n"
        "- [[runbooks/]] — step-by-step ops procedures\n"
        "- [[procedures/]] — repeatable workflows\n"
        "- [[decisions/]] — choices with rationale\n"
        "- [[infra/]] — infrastructure (hosts, services, configs)\n"
        "- [[people/]] — contacts and relationships\n"
        "- [[projects/]] — current work\n"
        "- [[postmortems/]] — incident writeups\n"
        "- [[inbox/]] — quick captures pending nightly filing\n"
        "- [[templates/]] — note skeletons (atomic-note, runbook, decision, "
        "postmortem)\n\n## How to use the KB\n\n"
        '- **Search before writing.** Run `phantombot memory search "topic"` '
        "first to avoid duplicating an existing note.\n"
        "- **One idea per file.** Atomic notes are easier to link, search, and "
        "refactor than mega-notes.\n"
        "- **Link freely.** `[[wikilinks]]` build the graph. The nightly cycle "
        "adds links between newly-related notes.\n"
        "- **Capture in inbox/.** If you're mid-task and have a half-thought, "
        "drop a one-liner into `inbox/`. The nightly cycle files or discards "
        "it.\n"
    ),
    "kb/templates/concept.md": (
        "---\ntype: concept\ntitle: <title>\ndescription: <short description>\naliases: []\ntags: []\ncreated: YYYY-MM-DD\nupdated: YYYY-MM-DD\n---\n\n"
        "# Title\n\nOne idea per note. Link related notes with [[wikilinks]].\n\n"
        "## Why this exists\n\n\n## Notes\n\n\n## Related\n- [[ ]]\n"
    ),
    "kb/templates/runbook.md": (
        "---\ntype: runbook\ntitle: <title>\ndescription: <short description>\naliases: []\ntags: [ops]\ncreated: YYYY-MM-DD\nupdated: YYYY-MM-DD\n---\n\n"
        "# Runbook: <action>\n\n## Trigger\nWhat situation calls for this runbook.\n\n"
        "## Prerequisites\n- [ ] Access to X\n- [ ] Knowledge of Y\n\n## Steps\n"
        "1.\n2.\n3.\n\n## Verification\nHow you confirm it worked.\n\n## Rollback\n"
        "What to do if a step fails.\n\n## Related\n- [[ ]]\n"
    ),
    "kb/templates/decision.md": (
        "---\ntype: decision\ntitle: <title>\ndescription: <short description>\naliases: []\ntags: []\ncreated: YYYY-MM-DD\nupdated: YYYY-MM-DD\n---\n\n"
        "# Decision: <topic>\n\n## Context\nWhat forced this decision now.\n\n"
        "## Options considered\n\n### Option A\n\n\n### Option B\n\n\n## Decision\n"
        "We chose X because Y.\n\n## Trade-offs accepted\n\n\n## Revisit when\n\n"
    ),
    "kb/templates/postmortem.md": (
        "---\ntype: postmortem\ntitle: <title>\ndescription: <short description>\naliases: []\ntags: [incident]\ncreated: YYYY-MM-DD\nupdated: YYYY-MM-DD\n---\n\n"
        "# Postmortem: <incident>\n\n## Timeline\n\n\n## Root cause\n\n\n## Impact\n"
        "\n\n## What went well\n\n\n## What didn't\n\n\n## Action items\n- [ ]\n"
    ),
}


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(enabled_extensions=()),
        trim_blocks=True,
        lstrip_blocks=True,
        # A typo in a template variable (e.g. `t.memory_hint` vs
        # `t.memory_hint_1`) must fail the build loudly, not render an
        # empty string and silently drop content/sections (L5).
        undefined=StrictUndefined,
    )


def _reports_to_name(spec: OrgSpec, role: Role) -> str | None:
    if role.reports_to:
        return spec.role_by_id(role.reports_to).name
    return None


def _subordinate_names(spec: OrgSpec, role: Role) -> list[str]:
    return [r.name for r in spec.subordinates_of(role.id)]


def _role_name(spec: OrgSpec, role_id: str) -> str:
    """Role name for a matrix id; '*' (any role) stays literal."""
    if role_id == "*":
        return "*"
    try:
        return spec.role_by_id(role_id).name
    except KeyError:
        return role_id


def _norma_context(spec: OrgSpec) -> dict:
    """
    Data for the communication norm template (kb/normas/comunicacion-agentes.md).

    Everything is derived from org.yaml — org name, channels, request-id
    format, hierarchy, roles/responsibilities, escalation matrix — so the
    norm is runtime-agnostic: it is stamped at compile time into whatever
    actors exist, and stays in sync with the org model.
    """
    roles_by_id = {r.id: r for r in spec.roles}
    actors = list(spec.actors)

    # Root role = the one with reports_to None (e.g. ceo). Its
    # reports_to_human is the human the org escalates to (e.g. Board President).
    root = next((r for r in spec.roles if not r.reports_to), None)
    ceo_name = root.name if root else "CEO"
    reports_to_human = root.reports_to_human if root else None

    # Hierarchy tree: roles as nodes, first actor's telegram_bot as the
    # label when available; precomputed indentation (Jinja2 has no
    # string-repetition operator in expressions).
    children: dict[str, list[Role]] = {}
    for r in spec.roles:
        children.setdefault(r.reports_to or "", []).append(r)

    hierarchy: list[dict[str, str]] = []

    def walk(reports_to: str, depth: int) -> None:
        for r in sorted(children.get(reports_to, []), key=lambda x: x.id):
            actor = next((a for a in actors if a.role == r.id), None)
            label = r.name
            if actor and actor.telegram_bot:
                label = f"{actor.telegram_bot} ({r.name})"
            desc = f" — {r.description}" if r.description else ""
            prefix = "  " * depth
            branch = "└── " if depth else ""
            hierarchy.append({"indent": prefix + branch, "label": label + desc})
            walk(r.id, depth + 1)

    walk("", 0)

    # Roles table: role name, actor id (+bot), function (description or
    # functions joined).
    role_rows: list[dict[str, str]] = []
    for r in sorted(spec.roles, key=lambda x: x.id):
        actor = next((a for a in actors if a.role == r.id), None)
        persona = actor.id if actor else "—"
        if actor and actor.telegram_bot:
            persona = f"{actor.telegram_bot} ({actor.id})"
        funcion = r.description or ", ".join(r.functions) or "—"
        role_rows.append({"role": r.name, "persona": persona, "funcion": funcion})

    # Responsibilities table: persona -> role description.
    resp_rows: list[dict[str, str]] = []
    for a in sorted(actors, key=lambda x: x.id):
        role = roles_by_id.get(a.role)
        persona = f"{a.telegram_bot} ({a.id})" if a.telegram_bot else a.id
        gestiona = role.description if role and role.description else "—"
        resp_rows.append({"persona": persona, "gestiona": gestiona})

    escalation_entries = [
        {
            "condition": e.condition,
            "to_name": _role_name(spec, e.to),
            "cross_department": e.cross_department,
        }
        for e in spec.escalation_matrix
    ]

    return {
        "org_name": spec.organization.name,
        "norm_version": spec.communication.norm_version,
        "human_channel": spec.communication.human_channel,
        "agent_channel": spec.communication.agent_channel,
        "request_id_format": resolve_request_id_format(spec),
        "hierarchy": hierarchy,
        "role_rows": role_rows,
        "resp_rows": resp_rows,
        "ceo_name": ceo_name,
        "reports_to_human": reports_to_human or "",
        "escalation_entries": escalation_entries,
        # Envelope (norma v1.5): marker / ttl from spec; max_hops from the
        # communication block. Template renders the anti-loop + bot-loop sections.
        "marker": (
            spec.communication.envelope.marker
            if spec.communication.envelope
            else "[env]"
        ),
        "ttl_hours": (
            spec.communication.envelope.ttl_hours if spec.communication.envelope else 6
        ),
        "max_hops": spec.communication.max_hops,
    }


def _norm_drawer_bullets(spec: OrgSpec, t: dict) -> list[str]:
    """One flat bullet string per norm entry, so the drawer renders one
    bullet per line. The Jinja env uses trim_blocks=True, which eats the
    newline after any block tag — building the bullets here (instead of
    wrapping each in `{% if %}`/`{% endif %}`) keeps them on separate lines
    so phantombot's drawer-ingest files them as clean, stable entries."""
    root = next((r for r in spec.roles if not r.reports_to), None)
    ceo_name = root.name if root else "CEO"
    reports_to_human = root.reports_to_human if root else None
    marker = (
        spec.communication.envelope.marker if spec.communication.envelope else "[env]"
    )
    bullets: list[str] = []
    hc = spec.communication.human_channel
    if hc:
        line = f"**{t['norm_drawer_human']}**: {hc.platform}"
        if hc.group:
            line += f" — {hc.group}"
        if hc.chat_id:
            line += f" (`{hc.chat_id}`)"
        bullets.append(line)
    ac = spec.communication.agent_channel
    if ac:
        line = f"**{t['norm_drawer_agent']}**: {ac.platform}"
        if ac.relay:
            line += f" — relay `{ac.relay}`"
        bullets.append(line)
    bullets.append(f"**{t['norm_drawer_rid']}**: `{resolve_request_id_format(spec)}`")
    bullets.append(t["norm_drawer_no_private"])
    if reports_to_human:
        bullets.append(
            t["norm_drawer_escalate_full"].format(
                ceo_name=ceo_name, reports_to_human=reports_to_human
            )
        )
    else:
        bullets.append(t["norm_drawer_escalate_short"].format(ceo_name=ceo_name))
    if marker:
        bullets.append(t["norm_drawer_envelope"].format(marker=marker))
    return bullets


def _render_norm_protocol(norma_md: str, spec: OrgSpec, t: dict) -> str:
    """Wrap the rendered communication norm in OKF frontmatter for the KB
    protocol page. ``type``/``title``/``description``/``aliases`` give it the
    high-weight BM25F fields so recall queries actually surface it."""
    org = spec.organization.name
    org_slug = re.sub(r"[^a-z0-9]+", "-", org.lower()).strip("-")
    today = datetime.datetime.now(tz=datetime.timezone.utc).date().isoformat()
    lang = resolve_lang(spec)
    if lang == "es":
        title = f"Norma de comunicación entre agentes — {org}"
        description = "Canales, cadencia, formato de request-id y reglas "
        "anti-bucle de la comunicación entre agentes de la organización."
        aliases = "[norma, comunicación, request-id, canales, protocolo]"
    else:
        title = f"Agent communication norm — {org}"
        description = "Channels, cadence, request-id format and anti-loop "
        "rules for agent-to-agent communication in the organization."
        aliases = "[norm, communication, request-id, channels, protocol]"
    return (
        "---\n"
        "type: procedure\n"
        f"title: {title}\n"
        f"description: {description}\n"
        f"tags: [comunicacion, norma, {org_slug}]\n"
        f"aliases: {aliases}\n"
        f"created: {today}\n"
        f"updated: {today}\n"
        "---\n\n" + norma_md.rstrip() + "\n"
    )


def resolve_lang(spec: OrgSpec) -> str:
    """
    Real language of the organization, for the fixed template strings —
    previously `default_language`/`languages` existed in the model and
    the schema but no template read them (the output was always Spanish,
    no matter what the spec said). Priority:
    explicit default_language > first language in `languages` > "en".
    """
    if spec.organization.default_language:
        return spec.organization.default_language
    if spec.organization.languages:
        return spec.organization.languages[0]
    return "en"


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically and durably.

    Content goes to a temp file in the same directory, is flushed and
    fsynced, then ``os.replace``d over the target — a crash, SIGKILL or
    ENOSPC mid-write can never leave a truncated file (the previous
    complete content stays in place until the replace). This is the same
    pattern the deploy layer uses; the compiler's merge helpers read-
    modify-write the live file, so a non-atomic write here would destroy
    hand-edited annotations on interruption.

    ``os.replace`` also never follows a symlink at the final path: if the
    target is a symlink, it is replaced by the regular file rather than
    written through (see the explicit refusal in the write helpers).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _refuse_symlink(path: Path) -> None:
    """Refuse to read/write through a file-level symlink.

    The output tree may live in a shared/tmp location; a planted symlink
    (e.g. ``out/<actor>/SOUL.md -> ~/.bashrc``) would otherwise make the
    next build overwrite an arbitrary file the user can write. Mirrors
    the deploy layer's ``_assert_no_symlinks`` for the tree the compiler
    produces.
    """
    if path.is_symlink():
        raise CompileError(f"refusing to overwrite symlink: {path}")


def _assert_no_symlink_components(path: Path, root: Path) -> None:
    """Refuse any symlink among the path components below ``root``.

    ``_refuse_symlink`` only checks the final file; a symlinked
    intermediate directory (``out/alice -> elsewhere``) would pass it
    while redirecting every write. Walk the literal (unresolved)
    components from ``root`` down so a planted link anywhere in the
    chain is caught before mkdir/read/write (H2, adversarial review
    v0.5.5). The anchor is ``root.resolve()``: a legitimately symlinked
    output root (build dir on another disk) stays allowed; links below
    it are not.
    """
    anchor = root.resolve()
    current = anchor
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise CompileError(
                f"refusing to build: symlink in output path component {current}"
            )


def write_if_changed(path: Path, content: str) -> bool:
    """
    Returns True if it wrote (new or changed file), False if the file
    didn't need to be touched.

    If the file already exists and contains ORG:BEGIN/END blocks,
    merge_content is applied (section by section: block content is
    regenerated, outside content is preserved). If the file exists but
    has no recognizable blocks, it is preserved whole (deliberate opt-out).
    """
    if path.exists():
        _refuse_symlink(path)
        existing = path.read_text(encoding="utf-8")
        merged = merge_content(existing, content)
        if merged == existing:
            # F5: a file that exists but has no ORG blocks is treated
            # as a manual opt-out — but if it also differs from the
            # fresh render, the spec changes are silently not being
            # applied. Surface that instead of staying quiet.
            if existing != content and not has_blocks(existing):
                warnings.warn(
                    f"{path.name} exists but has no ORG blocks — preserved "
                    f"whole (manual opt-out); spec changes are NOT applied. "
                    f"Delete the file or add ORG:BEGIN/END markers to "
                    f"re-enable generation.",
                    stacklevel=2,
                )
            return False
        _atomic_write(path, merged)
        return True
    _atomic_write(path, content)
    return True


def write_plain_if_changed(path: Path, content: str) -> bool:
    """
    Plain write: overwrites if the content changed, without block
    merging. Used for the metadata file (.phantomorg.yaml), which has
    no hand-editable sections — it must always reflect the current state
    of the spec (organization_id/actor_id/role_id), so applying
    merge_content here would be wrong: if the file had no ORG blocks
    (it never does), it would stay frozen forever and never update if,
    for example, the actor changes role.
    """
    if path.exists():
        _refuse_symlink(path)
        existing = path.read_text(encoding="utf-8")
        if existing == content:
            return False
    _atomic_write(path, content)
    return True


def write_if_missing(path: Path, content: str) -> bool:
    """
    Writes the file only if it doesn't exist yet. Used for MEMORY.md:
    once created, the runtime keeps enriching it with durable facts and
    PhantomOrg must not touch it again on any later build.
    """
    if path.exists():
        _refuse_symlink(path)
        return False
    _atomic_write(path, content)
    return True


def ensure_scaffold(actor_dir: Path) -> None:
    """
    Create the OpenClaw-shaped memory/kb layout phantombot expects, plus
    the seed files its memory system reads (drawers, KB home, templates).

    Phantombot's own scaffold (personaScaffold.ts) creates memory/archive/
    and the kb/ category dirs, then stamps the seeds idempotently — a seed
    that already exists is never overwritten. Mirror that behaviour:
    directories are mkdir(exist_ok), seeds use write_if_missing.
    """
    for d in _SCAFFOLD_MEMORY_DIRS:
        (actor_dir / "memory" / d).mkdir(parents=True, exist_ok=True)
    for d in _SCAFFOLD_KB_DIRS:
        (actor_dir / "kb" / d).mkdir(parents=True, exist_ok=True)
    today = datetime.datetime.now(tz=datetime.timezone.utc).date().isoformat()
    for rel, body in _SEED_FILES.items():
        write_if_missing(actor_dir / rel, body.format(today=today))


def build_actor(
    spec: OrgSpec, actor: Actor, out_dir: Path, env: Environment
) -> list[Path]:
    role = spec.role_by_id(actor.role)
    department = spec.department_by_id(role.department)
    access = merge_access(spec, role, actor)
    escalation = escalation_paths_for(spec, role.id)

    lang = resolve_lang(spec)
    if lang not in available_languages():
        warnings.warn(
            f"language {lang!r} of organization {spec.organization.id!r} has no "
            f"translation — compiled output will be English (available: "
            f"{', '.join(available_languages())})",
            stacklevel=2,
        )

    # Defense-in-depth path containment: shape validation already enforces
    # the identifier grammar on actor ids (so `actor.id` can never be a
    # separator-containing path), but build() can be called with specs
    # constructed outside load_org_yaml. Refuse any id that would escape
    # the requested output directory.
    actor_dir = (out_dir / actor.id).resolve()
    out_resolved = out_dir.resolve()
    if actor_dir != out_resolved and out_resolved not in actor_dir.parents:
        raise CompileError(
            f"refusing to build actor {actor.id!r}: resolved output path "
            f"{actor_dir} escapes the requested output directory {out_resolved}"
        )
    # Defense in depth (H2): resolve() above already refuses links that
    # escape the tree, but a link pointing INSIDE the tree (e.g.
    # out/alice -> out/other) would pass it while redirecting alice's
    # files into other's directory. Reject any symlink in the literal
    # path components before anything is created or written.
    _assert_no_symlink_components(out_dir / actor.id, out_dir)
    written: list[Path] = []

    reports_to_name = _reports_to_name(spec, role)
    t = get_strings(resolve_lang(spec))

    identity_md = env.get_template("identity.j2").render(
        t=t,
        actor=actor,
        role={
            "name": role.name,
            "functions": role.functions,
            "reports_to_role_name": reports_to_name,
            "reports_to_human": role.reports_to_human,
        },
        department=department,
    )
    p = actor_dir / "IDENTITY.md"
    if write_if_changed(p, identity_md):
        written.append(p)

    soul_md = env.get_template("soul.j2").render(
        t=t,
        org_name=spec.organization.name,
        role=role,
        department=department,
        access=access,
        reports_to_name=reports_to_name,
        subordinates=_subordinate_names(spec, role),
        role_exceptions=access.role_exceptions,
        actor_exceptions=access.actor_exceptions,
        actor_id=actor.id,
        escalation_paths=escalation,
        max_hops=spec.communication.max_hops,
        request_id_format=resolve_request_id_format(spec),
        message_types=spec.communication.message_types,
    )
    p = actor_dir / "SOUL.md"
    if write_if_changed(p, soul_md):
        written.append(p)

    tools_md = env.get_template("tools.j2").render(
        t=t,
        actor_id=actor.id,
        tools=actor.tools,
        tools_excluded=actor.tools_excluded,
    )
    p = actor_dir / "tools.md"
    if write_if_changed(p, tools_md):
        written.append(p)

    memory_md = env.get_template("memory.j2").render(
        t=t,
        actor_id=actor.id,
        role_name=role.name,
        org_name=spec.organization.name,
    )
    p = actor_dir / "MEMORY.md"
    if write_if_missing(p, memory_md):
        written.append(p)

    # Source metadata — not runtime content, it's what lets `po deploy`
    # detect collisions between different organizations sharing the same
    # actor id (see deploy/target.py).
    meta_yaml = (
        f"organization_id: {spec.organization.id}\n"
        f"actor_id: {actor.id}\n"
        f"role_id: {role.id}\n"
    )
    p = actor_dir / ".phantomorg.yaml"
    if write_plain_if_changed(p, meta_yaml):
        written.append(p)

    ensure_scaffold(actor_dir)

    # The communication norm is written in TWO places:
    #   1. kb/procedures/comunicacion-agentes.md — the full, human-readable
    #      protocol page (OKF frontmatter so it ranks on recall).
    #   2. memory/norms.md — a CONCISE operational summary, block-merged as
    #      an `<!-- ORG:BEGIN norms -->` block. That drawer is read in full
    #      by the threat judge, so it must be briefed on what routine
    #      agent-to-agent traffic looks like — otherwise the org's own
    #      coordination gets scored as anomalous. The block is rendered on
    #      every build (empty when the org declares no channels, so a
    #      channel drop empties it instead of leaving a stale block).
    if spec.communication.human_channel or spec.communication.agent_channel:
        norma_md = env.get_template("norma.j2").render(t=t, **_norma_context(spec))
        # Human-readable protocol page in the canonical KB category, with
        # OKF frontmatter (title/type/aliases) so it ranks on recall.
        procedures_dir = actor_dir / "kb" / "procedures"
        procedures_dir.mkdir(parents=True, exist_ok=True)
        procedure_path = procedures_dir / "comunicacion-agentes.md"
        procedure_body = _render_norm_protocol(norma_md, spec, t)
        if write_plain_if_changed(procedure_path, procedure_body):
            written.append(procedure_path)

        drawer_md = env.get_template("norma_drawer.j2").render(
            today=datetime.datetime.now(tz=datetime.timezone.utc).date().isoformat(),
            bullets=_norm_drawer_bullets(spec, t),
        )
    else:
        drawer_md = "<!-- ORG:BEGIN norms -->\n<!-- ORG:END norms -->\n"
    p = actor_dir / "memory" / "norms.md"
    if write_if_changed(p, drawer_md):
        written.append(p)

    # Phantomchat config (phantomchat.json): compiled from org.yaml when the
    # org declares an agent channel AND the actor declares an npub. Same
    # semantics as the norm — derived state, plain overwrite, no block merge.
    if spec.communication.agent_channel and actor.npub:
        pc = phantomchat_config(spec, actor)
        if pc is not None:
            p = actor_dir / PHANTOMCHAT_FILENAME
            if write_plain_if_changed(p, pc.to_json()):
                written.append(p)
    return written


def _reconcile_stale_output(spec: OrgSpec, out_dir: Path) -> None:
    """Remove stale PhantomOrg-owned actors and derived artifacts from a
    REUSED output directory (P1: build() only visits current actors, so a
    ``remove-actor -> build --out same-dir`` left the removed actor's
    directory behind and the next deploy redeployed it).

    Only entries that PhantomOrg OWNS are removed, confined by metadata:

    - An actor directory is removed only when its ``.phantomorg.yaml``
      declares ``organization_id == this org`` AND its ``actor_id`` is no
      longer in the spec (it belongs to this org but was removed). Actor
      dirs of OTHER orgs, unmanaged dirs, or dirs without our metadata are
      left untouched.
    - Derived artifacts (``phantomchat.json``, the communication norm
      ``kb/procedures/comunicacion-agentes.md`` and the ``memory/norms.md``
      marker block, ``HUMANS.md``) are removed from a current actor when the
      org model no longer produces them (npub dropped, no channels, no
      humans block).

    This runs only on the build OUTPUT (regenerable state), never on a live
    persona directory.
    """
    current_ids = {a.id for a in spec.actors}

    # 1. Stale actor directories owned by this org.
    for child in sorted(out_dir.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        meta = _read_build_meta(child)
        if meta is None:
            continue
        if meta.get("organization_id") == spec.organization.id:
            actor_id = meta.get("actor_id")
            if actor_id not in current_ids:
                _remove_tree(child)

    # 2. Obsolete derived artifacts inside a CURRENT actor directory.
    for actor in spec.actors:
        actor_dir = out_dir / actor.id
        if not actor_dir.is_dir():
            continue
        if not (spec.communication.agent_channel and actor.npub):
            _remove_if_exists(actor_dir / PHANTOMCHAT_FILENAME)
        if not (spec.communication.human_channel or spec.communication.agent_channel):
            _remove_if_exists(
                actor_dir / "kb" / "procedures" / "comunicacion-agentes.md"
            )

    # 3. Obsolete org-level HUMANS.md (no humans block in the model).
    if not spec.humans:
        _remove_if_exists(out_dir / HUMANS_FILENAME)


def _read_build_meta(actor_dir: Path) -> dict | None:
    """Best-effort read of a build-output actor's .phantomorg.yaml.
    Returns None when absent/unreadable/invalid (callers treat None as
    'not ours — leave it alone')."""
    meta_path = actor_dir / ".phantomorg.yaml"
    if not meta_path.exists():
        return None
    try:
        data = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _remove_tree(path: Path) -> None:
    """Recursively remove a build-output entry (regenerable state)."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _remove_if_exists(path: Path) -> None:
    if path.exists():
        _remove_tree(path)


def build(
    spec: OrgSpec,
    out_dir: Path,
    only: str | None = None,
    scope_rule: str = "chain",
) -> dict[str, list[Path] | list[dict[str, str]]]:
    """
    Compiles all actors (or just one if `only` is specified).
    Returns a dict actor_id -> list of written files (empty if there were
    no changes, thanks to write_if_changed), plus the reserved keys
    ``__warnings__`` (list of warning dicts) and, when ``only`` is None,
    ``__scopes__`` / ``__humans__`` (lists of written paths).

    When `only` is None, also derives the org-wide memory scopes from the
    org model and writes them to ``out_dir/scopes.json`` (see
    compiler/scopes.py). The file is derived state: it holds no runtime
    data, so it is written with write_plain_if_changed (plain overwrite
    when the content changes, never block-merged).
    """
    env = _env()
    result: dict[str, list[Path] | list[dict[str, str]]] = {}
    warnings_out: list[dict[str, str]] = []
    actors = spec.actors if only is None else [spec.actor_by_id(only)]
    _cleanup_stale_tmp(out_dir)
    for actor in actors:
        result[actor.id] = build_actor(spec, actor, out_dir, env)
        if not actor.npub:
            warnings_out.append(
                {
                    "actor": actor.id,
                    "code": "no-npub",
                    "message": (
                        f"actor {actor.id!r} declares no npub: it cannot be "
                        f"reached over phantomchat (bot-to-bot layer). Declare "
                        f"its NIP-19 npub in org.yaml or run `po phantomchat-check` "
                        f"to inspect the runtime identities."
                    ),
                }
            )

    result["__warnings__"] = warnings_out

    if only is None:
        _reconcile_stale_output(spec, out_dir)

        scopes = derive_scopes(spec, rule=scope_rule)
        scopes_path = out_dir / SCOPES_FILENAME
        if write_plain_if_changed(
            scopes_path, serialize_scopes(spec, scopes, scope_rule)
        ):
            result["__scopes__"] = [scopes_path]

        humans_path = write_humans(spec, out_dir)
        if humans_path is not None:
            result["__humans__"] = [humans_path]

    return result
