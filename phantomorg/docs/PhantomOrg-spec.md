# PhantomOrg — Technical specification
**From persona engine to platform for AI agent organizations**
Author: Maki, for Salvador · 2026-08-02 · v4 (with external references, real audit, and bootstrap-ready spec)

> Project name: **PhantomOrg**. CLI: `po` (alias `phantomorg`). Replaces "Forja" in all prior work.

---

## 0. How to read this document

It is written for two readers at once: **Salvador**, who decides scope and priorities, and an **LLM (e.g. Claude Code)** that can take section 6 onward and start generating the repository without needing any more context than this file. That is why there is JSON Schema, pseudocode, and a backlog in ticket format, not just prose.

---

## 1. Executive summary

PhantomOrg is an engine that models **any organization** as departments, roles, actors, access policies, and communication rules — and compiles that into the files that an agent runtime (today, Phantombot) loads. Aquaponics United (AU) is the first real case, with concrete data already audited in this document (section 3).

Two things change compared to v3:

1. **External references.** The design is not invented from scratch: it is cross-checked against protocols and frameworks already proven in production — Google **A2A**, **FIPA-ACL/KQML**, **CrewAI** (hierarchical process), and the **RBAC/ABAC/PBAC** access control models. Every design decision in this document cites which known problem it solves and which known mistake it avoids.
2. **Bootstrap specificity.** It includes AU's real `org.yaml` with its data, the validation JSON Schema, the repository structure, and a ticket backlog concrete enough for an LLM to start writing code today.

---

## 2. External references and what each one contributes

| Reference | What it is | What it contributes to PhantomOrg | What mistake it avoids |
|---|---|---|---|
| **Google A2A Protocol** (Apache-2.0, governed by the Linux Foundation since Jun. 2025) | Open protocol for agents from different vendors to discover, negotiate, and coordinate tasks over HTTP/JSON-RPC, with *Agent Cards* (capability metadata) and *Tasks* with versioned state | The **Agent Card** concept inspires the `capabilities` block of each actor in the spec; the **Task with lifecycle** concept (submitted → working → completed/failed) inspires how an escalation between roles is modeled — not as a loose message but as a unit of work with state | Not reinventing a message format from scratch; reusing a vocabulary already validated in production by multiple vendors |
| **FIPA-ACL / KQML** (90s-2000s standard, IEEE since 2005) | Agent communication language based on "performatives" (inform, request, agree, refuse...) with formal semantics | Confirms that **a small, closed vocabulary of message types** (not an endless list) is what makes an agent protocol usable | FIPA-ACL never achieved real adoption outside academia/defense precisely because of overly rich ontologies and verbose XML encoding — **PhantomOrg limits the internal protocol to 5 message types**, not 12+ performatives |
| **CrewAI — hierarchical vs sequential process** | Agent framework with a "manager delegates to workers" mode | Confirms the **escalation matrix with explicit hierarchy** pattern (manager validates and redirects) | Production reports document that hierarchical delegation without an iteration limit (`max_iter`) can enter an infinite loop, and that only "manager" roles should have `allow_delegation=True` — **PhantomOrg adopts this as `max_hops` and as a rule: only roles with reports_to null or with subordinates may re-escalate** |
| **RBAC vs ABAC vs PBAC** (NIST, IAM practices at AWS/enterprise) | Access control models: by fixed role, by dynamic attributes, or hybrid | Confirms that **a role for every exception** leads to "role explosion" (NIST documents organizations with thousands of roles). The validated solution is a **hybrid model**: RBAC for the base level (department/role) + per-actor exception attributes (ABAC) for cases like "Category 0 only for Salvador" | This is exactly the problem AU already has today: Category 0 lives "by hand" in Paco's SOUL. PhantomOrg models it as an **actor exception attribute**, not as a new role |

**Conclusion of the external audit:** PhantomOrg's design is not original in its parts — it is a deliberate combination of already-validated patterns, avoiding the two documented historical failures (FIPA-ACL: too much semantics, too little adoption; role explosion: too many roles, too little governance).

---

## 3. Audit with real Aquaponics United data

This is what currently exists in AU's 5 personas (alma, elena, paco, pepa, roberto), audited against the references in section 2:

| # | Real finding in AU | Why it is a problem (with reference) | Solution in the PhantomOrg spec |
|---|---|---|---|
| G1 | The "Priority Rule #0" (the main coordination group), Category 1, Zero Infrastructure Disclosure, and the Request-ID format are copied almost identically across the 5 SOUL.md files | Without an *Agent Card* / central spec, each copy diverges over time (this is exactly what A2A solves by centralizing capabilities and message format) | `communication.request_id_format` and `policies.*` are defined **once** in `org.yaml`; the compiler injects them identically into the 5 agents |
| G2 | Elena has Category 3, Alma doesn't — with no clarity on whether it is a decision or an oversight | This is exactly "silent role explosion": an access exception lives implicitly in an individual SOUL, not in an auditable policy | `security_exceptions` as an explicit **role** field, visible and versioned in the spec (see `training_lead` in the section 5.3 `org.yaml`) |
| G3 | Pepa: "send not available" — a tool restriction not documented in any comparable way in the other agents | Without a per-actor capability model (Agent Card), tool restrictions become ad-hoc | Per-actor `tools` block in the spec, with support for explicit restrictions (`tools_excluded`) |
| G4 | Roberto escalates to "Paco, Salvador or Fran" — an escalation route different from the other agents', with no common matrix existing | This is exactly the anti-pattern CrewAI documents as a cause of loops and unpredictable behavior: delegation rules written in free prose, agent by agent | `escalation_matrix` as a single declarative table, validated by the compiler (no cycles, no references to nonexistent roles) — see section 5.4 |
| G5 | Category 0 (Salvador's absolute exception) lives as free text in Paco's SOUL | It is an **actor** exception, not a role one — folding it into the role would duplicate it if a second CEO appears tomorrow in another organization | Modeled as an exception attribute at the **actor** level, following the ABAC pattern (subject attribute, not role attribute) |
| G6 | Shell commands embedded in the SOUL (`bash notebooklm.sh`) | Couples the agent's identity to the implementation — already flagged in the ChatGPT/OrgOS audit, and contrary to A2A's Agent Card principle (declared capabilities, not commands) | `tools.md` declares logical capabilities; the mapping to real commands lives in a separate layer that the compiler resolves, not in the SOUL |
| G7 | No mechanism exists to detect whether an escalation forms a cycle (A escalates to B, B escalates to A) | It has never happened in AU with 5 agents, but it is mathematically inevitable as things grow — CrewAI documents it as a real production failure | The validator runs a **directed acyclic graph (DAG)** check over `escalation_matrix` before any `build` (section 5.5) |

---

## 4. Communication model (internal, not a full A2A implementation)

**Explicit design decision:** PhantomOrg **does not implement A2A's HTTP/JSON-RPC stack** — Phantombot agents are not independent, network-discoverable services; they are personas loaded by a single shared runtime. What is adopted from A2A and FIPA-ACL is the **vocabulary and discipline**, not the transport.

### 4.1 Message types (5, not 12+)

| Type | FIPA equivalent | Use |
|---|---|---|
| `REQUEST` | request | A role asks another role for an action |
| `INFORM` | inform | A role reports a fact, without requesting an action |
| `ESCALATE` | — (does not exist in FIPA; it is the new piece) | A role raises a blocker according to the `escalation_matrix` |
| `CONFIRM` | confirm/agree | Acceptance of a `REQUEST` or closing of an `ESCALATE` |
| `REJECT` | refuse | Explicit rejection, with a reason |

### 4.2 Message envelope

```yaml
request_id: "au-20260802-0042"     # formato definido en org.yaml
type: ESCALATE
from: { actor: alma, role: project_lead, department: operaciones }
to:   { actor: pepa, role: chief_of_staff, department: direccion }
hops: 1                             # se incrementa en cada re-escalado; corta en max_hops
trust: internal                     # internal | external | untrusted (alineado con el security perimeter de Phantombot)
payload: "..."
```

### 4.3 Anti-loop limit

`communication.max_hops` (default: 3) — if an `ESCALATE` exceeds that number of hops without being resolved, the compiler/runtime marks it as `unresolved` and routes it to the department's root role (or to the human). This translates directly the CrewAI production lesson about delegation without `max_iter`.

---

## 5. The specification (`org.yaml`)

### 5.1 High-level structure

```
version: 1
organization: {...}
departments: [...]
roles: [...]
actors: [...]
policies: { access_levels: {...}, security_categories: {...} }
escalation_matrix: [...]
communication: {...}
```

### 5.2 JSON Schema (summarized, for automatic validation)

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "PhantomOrg org.yaml",
  "type": "object",
  "required": ["version", "organization", "departments", "roles", "actors", "policies", "escalation_matrix", "communication"],
  "properties": {
    "version": { "type": "integer", "const": 1 },
    "organization": {
      "type": "object",
      "required": ["id", "name", "sector", "languages"],
      "properties": {
        "id": { "type": "string", "pattern": "^[a-z0-9][a-z0-9_-]*$" },
        "name": { "type": "string" },
        "sector": { "type": "string" },
        "languages": { "type": "array", "items": { "type": "string" } }
      }
    },
    "departments": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id", "name", "parent", "access_policy"],
        "properties": {
          "id": { "type": "string", "pattern": "^[a-z0-9][a-z0-9_-]*$" },
          "name": { "type": "string" },
          "parent": { "type": ["string", "null"] },
          "access_policy": { "type": "string" }
        }
      }
    },
    "roles": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id", "name", "department", "reports_to", "access_level"],
        "properties": {
          "id": { "type": "string", "pattern": "^[a-z0-9][a-z0-9_-]*$" },
          "name": { "type": "string" },
          "department": { "type": "string" },
          "reports_to": { "type": ["string", "null"] },
          "reports_to_human": { "type": ["string", "null"] },
          "functions": { "type": "array", "items": { "type": "string" } },
          "access_level": { "type": "string" },
          "security_exceptions": { "type": "array", "items": { "type": "string" } }
        }
      }
    },
    "actors": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id", "role", "tools"],
        "properties": {
          "id": { "type": "string", "pattern": "^[a-z0-9][a-z0-9_-]*$" },
          "role": { "type": "string" },
          "telegram_bot": { "type": "string" },
          "tools": { "type": "array", "items": { "type": "string" } },
          "tools_excluded": { "type": "array", "items": { "type": "string" } },
          "actor_exceptions": { "type": "array", "items": { "type": "string" } },
          "tone": { "type": "string" }
        }
      }
    },
    "escalation_matrix": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["from", "to", "condition"],
        "properties": {
          "from": { "type": "string" },
          "to": { "type": "string" },
          "condition": { "type": "string" },
          "cross_department": { "type": "boolean", "default": false }
        }
      }
    },
    "communication": {
      "type": "object",
      "required": ["request_id_format", "message_types", "max_hops"],
      "properties": {
        "request_id_format": { "type": "string" },
        "message_types": { "type": "array", "items": { "enum": ["REQUEST", "INFORM", "ESCALATE", "CONFIRM", "REJECT"] } },
        "max_hops": { "type": "integer", "minimum": 1, "default": 3 }
      }
    }
  }
}
```

Every id in org.yaml — `organization.id`, `department.id`, `role.id`,
`actor.id`, and the keys of `access_levels` / `security_categories` —
must match the single identifier grammar `^[a-z0-9][a-z0-9_-]*$`
(lowercase letter or digit first, then lowercase letters, digits, `-`
or `_`; no separators, no `..`, no leading dot or dash). The shape
validator enforces this on every id, and the compiler additionally
refuses (defense-in-depth) any actor whose resolved output path escapes
the requested build directory — ids are used as filesystem path
components (the compiler writes `out_dir/<actor.id>/`), so a traversal
id like `../outside` can never write outside the build boundary.

### 5.3 Real Aquaponics United `org.yaml` (instance, not design)

```yaml
version: 1

organization:
  id: aquaponics-united
  name: "Aquaponics United"
  sector: ngo
  languages: [es, en]
  default_language: es

departments:
  - { id: direccion,   name: "Dirección",   parent: null,      access_policy: level-3 }
  - { id: operaciones, name: "Operaciones", parent: direccion, access_policy: level-2 }
  - { id: formacion,   name: "Formación",   parent: direccion, access_policy: level-2 }
  - { id: finanzas,    name: "Finanzas",    parent: direccion, access_policy: level-2 }

roles:
  - id: ceo
    name: "CEO"
    department: direccion
    reports_to: null
    reports_to_human: "Salvador"
    functions: [vision, liderazgo, wikipedia_au, tools]
    access_level: level-3

  - id: chief_of_staff
    name: "Chief of Staff"
    department: direccion
    reports_to: ceo
    functions: [coordinacion, escalado, seguimiento]
    access_level: level-2

  - id: cfo
    name: "CFO"
    department: finanzas
    reports_to: ceo
    functions: [finanzas, reporting]
    access_level: level-2

  - id: project_lead
    name: "Project Lead"
    department: operaciones
    reports_to: chief_of_staff
    functions: [proyectos, seguimiento_campo]
    access_level: level-2

  - id: training_lead
    name: "Training Lead"
    department: formacion
    reports_to: chief_of_staff
    functions: [formacion, documentacion]
    access_level: level-2
    security_exceptions: [category-3]     # previously maintained by hand in Elena's SOUL; now explicit and auditable (resolves G2)

actors:
  - id: paco
    role: ceo
    telegram_bot: "@CEO_bot"
    tools: [email, drive, calendar, notebooklm, printing]
    actor_exceptions: [category-0]        # Salvador's absolute exception, modeled as an actor attribute, not a role one (resolves G5)
    tone: formal-cercano

  - id: pepa
    role: chief_of_staff
    telegram_bot: "@COS_bot"
    tools: [email, drive, calendar]
    tools_excluded: [send]                # antes era una nota suelta ("send not available"); ahora es un campo declarado (resuelve G3)

  - id: roberto
    role: cfo
    telegram_bot: "@CFO_bot"
    tools: [email, drive, calendar, sheets]

  - id: alma
    role: project_lead
    telegram_bot: "@PL_bot"
    tools: [email, drive, calendar, notebooklm]

  - id: elena
    role: training_lead
    telegram_bot: "@Training_bot"
    tools: [email, drive, notebooklm]

policies:
  access_levels:
    level-3: { label: "Ejecutivo", categories: [1, 2, 3] }
    level-2: { label: "Operativo", categories: [1, 2] }
    level-1: { label: "Restringido", categories: [1] }
  security_categories:
    category-0: { label: "Excepción absoluta", scope: actor, owner: "Salvador" }
    category-1: { label: "Público / interno bajo" }
    category-2: { label: "Confidencial" }
    category-3: { label: "Credenciales / financiero sensible" }

escalation_matrix:
  - { from: project_lead,   to: chief_of_staff, condition: "bloqueo operativo o desacuerdo de campo" }
  - { from: training_lead,  to: chief_of_staff, condition: "contenido fuera de alcance de formación" }
  - { from: cfo,            to: ceo,            condition: "gasto por encima del umbral de política financiera" }
  - { from: chief_of_staff, to: ceo,            condition: "bloqueo no resuelto en su nivel" }
  - { from: "*",            to: ceo,            condition: "excepción Category 0 solicitada", cross_department: true }
  # note: today's real route ("Roberto escalates to Paco, Salvador or Fran") resolves
  # explicitly here as "cfo → ceo"; "Salvador" and "Fran" stay out of the matrix
  # because they are people, not roles — they must be defined as `reports_to_human` or as
  # a human escalation exception, never as free text (resolves G4)

communication:
  request_id_format: "{org_id}-{yyyymmdd}-{seq4}"
  message_types: [REQUEST, INFORM, ESCALATE, CONFIRM, REJECT]
  max_hops: 3
```

### 5.4 Escalation matrix — validation rules

1. Every `from` and `to` must exist in `roles[].id`, except the `"*"` wildcard.
2. No cycle `A → B → ... → A` may exist (directed acyclic graph check).
3. Every entry with `cross_department: true` must cross to a different `department` between the source role and the destination role; if it does not cross a department, it is a spec error (the entry is redundant).
4. No route may exceed `communication.max_hops` without reaching a role with `reports_to: null` or a `reports_to_human`.

### 5.5 Security — hybrid model (avoids role explosion)

- **Base level (RBAC)**: `access_level` on the role, inherited from the department's `access_policy`.
- **Role exception**: `security_exceptions` — valid for *any actor* holding that role (e.g. Category 3 for whoever is Training Lead).
- **Actor exception (ABAC)**: `actor_exceptions` — valid only for that specific persona, not for the role (e.g. Category 0 only for Paco/Salvador, not for a future second CEO).

This is exactly the documented recommendation against "role explosion": do not create a new role for every exception; instead, separate role-exception from actor-exception.

---

## 6. Repository structure (for LLM bootstrap)

```
phantomorg/
├── pyproject.toml
├── phantomorg/
│   ├── cli.py                  # entrypoint `po` / `phantomorg`
│   ├── wizard/
│   │   ├── new_org.py
│   │   ├── add_department.py
│   │   ├── add_role.py
│   │   └── add_actor.py
│   ├── spec/
│   │   ├── schema.json         # JSON Schema for section 5.2
│   │   ├── loader.py           # loads and validates org.yaml
│   │   └── model.py            # dataclasses: Organization, Department, Role, Actor
│   ├── validator/
│   │   ├── graph.py            # DAG check on escalation_matrix
│   │   ├── refs.py             # cross-references (roles/departments exist)
│   │   └── budgets.py          # file size limits (SOUL, MEMORY)
│   ├── compiler/
│   │   ├── identity.py         # generates IDENTITY.md
│   │   ├── soul.py             # generates SOUL.md
│   │   ├── tools.py            # generates tools.md
│   │   ├── memory.py           # generates MEMORY.md + memory/kb scaffold
│   │   └── templates/          # Jinja2 templates per section
│   └── deploy/
│       └── phantombot_target.py  # copies to ~/.local/share/phantombot/personas/
├── organizations/
│   └── aquaponics-united/
│       └── org.yaml            # real instance, section 5.3
└── tests/
    ├── test_schema.py
    ├── test_validator_cycles.py
    └── test_compiler_au.py     # snapshot test contra el org.yaml de AU
```

## 7. CLI commands (exact contract)

| Command | Flags | Effect |
|---|---|---|
| `po setup` | `--phantombot-dir`, `--org`, `--base` | Guided installation: detects existing personas, assigns each one a department + role (audit suggests, user confirms), optionally adds new personas, writes the org.yaml |
| `po new-org` | `--id`, `--name`, `--sector`, `--lang` | Creates a minimal `organizations/<id>/org.yaml` |
| `po add-department` | `--org`, `--id`, `--parent`, `--access-policy` | Adds an entry to `departments` |
| `po add-role` | `--org`, `--id`, `--department`, `--reports-to`, `--access-level` | Adds an entry to `roles` |
| `po add-actor` | `--org`, `--id`, `--role`, `--tools` | Adds an entry to `actors` |
| `po build` | `--org`, `--only <actor_id>` (optional) | Runs the compiler over all actors or just one |
| `po validate` | `--org` | Runs the full validator (schema + DAG + refs + budgets), exit code ≠0 on failure |
| `po deploy` | `--from <dir>`, `--target <path>`, `--force`, `--prune`, `--yes` | Copies the compiled output to the runtime destination; asks a final `[y/N]` confirmation (skip with `--yes`); archives overwritten personas to `personas-archive/` |
| `po deploy-all` | `--base`, `--dist-base`, `--target`, `--force`, `--prune`, `--yes` | Deploys every organization under `--base`; same confirmation/archive contract as `po deploy`; records ONE aggregated session (a single `po rollback` undoes the whole invocation) |
| `po rollback` | `--target`, `--list`, `--yes` | Undoes the last deploy session and restores the system to exactly the state before it (see §7.2) |

### 7.1 Setup flow (`po setup`)

`po setup` is the guided installation path over a phantombot installation
(or a fresh start). It is one pass — afterwards the user works with the
regular `validate` / `build` / `deploy` cycle.

1. **Locate the personas root.** Detected at
   `~/.local/share/phantombot/personas/` (override with `--phantombot-dir`);
   if missing, the user is asked. Personas are the subdirectories that
   contain `SOUL.md` or `IDENTITY.md`.
2. **Decide the org source.** If an existing `org.yaml` is provided
   (`--org` or answered), its departments/roles are reused. Otherwise a
   fresh organization is created: id, name, sector, languages, then the
   departments are defined interactively (empty name finishes; access
   policy defaults to `level-2`).
3. **Reassign existing personas (priority).** For each detected persona,
   `import-audit` reads its `IDENTITY.md` / `SOUL.md` / `tools.md` and
   suggests a department and a role name; the user confirms or overrides.
   Roles are shared: several personas accepting the same suggested role
   reuse one role entry instead of creating duplicates.
4. **Add new personas (optional).** After the existing ones, the user may
   add brand-new personas, each with department + role.
5. **Apply.** A new org is written from the plan; an existing one is
   mutated (departments/roles/actors added, never removed). The result
   passes `po validate` immediately.

Design decisions:

- **Reassign first, add later.** Existing personas take priority; new ones
  are an opt-in second step. Works for installations with one or many
  personas — the user decides what fits their needs.
- **Suggested, not inferred.** The wizard never writes a guess silently:
  audit suggestions are shown and the user confirms (same principle as
  `import-audit` — "don't fill gaps with assumptions").
- **Role id default.** The suggested role name is slugified
  (`"Project Lead"` → `project_lead`); without a suggestion the default is
  `<actor_id>_role` (never the actor id itself, which would collide with
  the org-wide id uniqueness rule).
- **Memories are untouched.** `po setup` only writes the org.yaml; it
  never touches `MEMORY.md` or any persona content. `po deploy` later
  preserves manual content outside the FORJA blocks.

### 7.2 Rollback & safety

Every write is preceded by an explicit confirmation and a backup of what
is about to change. The contract:

- **Final confirmation.** `deploy`, `deploy-all` and `setup` print a
  summary of the pending plan (target, actors, what will be archived)
  and require an explicit `[y/N]` before writing anything. A negative
  answer writes nothing and exits 1 with `Cancelled — no changes were
  made.` `deploy`/`deploy-all` accept `--yes` to skip the prompt for
  scripting/CI.
- **Persona archiving (phantombot-compatible).** Before an existing
  persona is overwritten — same organization, or `--force` — the whole
  directory is moved to the sibling `personas-archive/` directory using
  phantombot's exact naming convention:

  ```
  personas/<name>/  ->  personas-archive/<name>-<YYYY-MM-DDTHH-MM-SS-mmmZ>/
  ```

  Same-millisecond collisions get a numeric suffix (`-N`), exactly like
  phantombot's own `personaScaffold.ts`. Because the format matches,
  `phantombot import-persona` lists and restores these archives without
  any PhantomOrg-specific tooling.
- **Prune archives instead of deleting.** `deploy --prune` moves orphaned
  actors of the same organization to `personas-archive/` rather than
  `rmtree`; removing an actor from the spec stays reversible.
- **Archive creation is announced.** The first time `personas-archive/`
  is created, the CLI prints its path; every archived persona is listed
  with its exact destination.
- **org.yaml backup.** Every mutation (`add-*`, `remove-*`, `rename-*`)
  and `setup` on an existing org writes `org.yaml.bak-<timestamp>`
  (UTC, microsecond precision) beside the file before writing, announced
  on stderr. Undo = `cp org.yaml.bak-<ts> org.yaml`. New-org creates a
  fresh file (no backup needed; it refuses to overwrite an existing one).
- **Deploy session manifest.** Every `deploy`/`deploy-all` that changes
  something appends a *session* record to
  `personas-archive/.phantomorg-manifest.json` (a dotfile phantombot
  ignores — its archive listing only reads directories). The record
  contains: command, target, orgs, deployed/created/pruned lists,
  `(name, archive_dir)` pairs for every archived persona, whether
  `personas-archive/` pre-existed, whether the target pre-existed, and
  optional `org.yaml` sha256 digests for drift detection. One session
  per invocation; `deploy-all` aggregates all organizations into one.
- **Transactional rollback (`po rollback`).** Undoes the LAST session:
  1. every archived persona directory is moved back to
     `target/<name>/` (the post-deploy version, if present, is moved to
     a trash dir first — the backup is *consumed*, not left behind);
  2. every persona the deploy *created* is removed (also via the trash);
  3. the trash (a `._pf_trash_*` dir inside `personas-archive/`) is
     deleted — but only now, because everything before it succeeded;
  4. if `personas-archive/` did not exist before that deploy and holds
     nothing else (no other sessions, no phantombot archives), it is
     deleted; likewise, if the target did not exist before and is now
     empty, it is deleted;
  5. only then is the session entry dropped from the manifest.
  After a full rollback the system is exactly as it was before the
  deploy. Rollback is stack-based (one session per run); `--list` shows
  the recorded sessions; `--yes` skips confirmation. It refuses to run
  when an archived persona is missing (an incomplete rollback is worse
  than none) and warns when an `org.yaml` recorded in the session has
  drifted since the deploy.

  Rollback retry semantics: a rollback interrupted mid-restore (after
  consuming some archives, e.g. a crash while restoring the 2nd of 5
  personas) is NOT a dead end. The evidence of an interrupted attempt is
  the `._pf_trash_*` dir left inside `personas-archive/` (every restore
  of an existing persona discards the replaced version to the trash
  first). When some recorded archives are missing:
  - missing archives + no trash dir = they were removed OUTSIDE
    PhantomOrg; refuse (the historical behavior, kept intact);
  - missing archives + trash dir + the missing persona IS back in the
    target = a previous rollback consumed them; the plan continues and
    finishes the job (restoring what remains);
  - missing archives + trash dir + the missing persona is NOT in the
    target = the pre-deploy version is genuinely lost; refuse with a
    manual-recovery message.

  Duplicate archives in a COMMITTED session are deduped exactly like
  the in_progress reconcile: a deploy-all --force where two orgs share
  an actor id records the same persona name twice (S1 = the pre-session
  version, S2 = an in-session artifact). Only the FIRST recorded archive
  per name is restored; later ones go to the discard list. Restoring
  both in recorded order would clobber the freshly restored pre-session
  version with the in-session one.

  Corrupt manifest: if `personas-archive/.phantomorg-manifest.json`
  exists but cannot be read or parsed (truncated write, manual edit,
  disk corruption), it is NEVER treated as "no sessions": overwriting
  it would silently destroy the rollback history of every earlier
  deploy. The file is preserved (moved aside as
  `.phantomorg-manifest.json.corrupt-<stamp>`, never overwritten) and
  every operation that would rewrite it — `deploy`, `deploy-all`,
  `rollback`, session commits/discards — refuses with a clear error
  (exit 1). The archived personas remain in place and can be restored
  manually (move them back into the target); the rollback history is
  simply unavailable until the file is resolved or deliberately
  deleted. A corrupt manifest also keeps the archive root alive at the
  end of a rollback (it may record sessions we cannot read).

  Failure semantics (the manifest is dropped LAST, so a mid-rollback
  failure is never a dead end):
  - if the failure happens before the restore finished, the session
    stays recorded and the still-present archives allow a retry;
  - if the failure happens after the archives were consumed (e.g. the
    trash could not be removed), the next `po rollback` becomes a
    *cleanup-only* plan — it finishes removing created personas, the
    trash and the disposable directories;
  - whatever the rollback replaced or removed is always recoverable
    from the trash dir until the rollback fully succeeds.

  Concurrency: session records are written under an advisory file lock
  (`.phantomorg-manifest.lock`), so two concurrent deploys cannot
  lose each other's session entry. Manifest writes are atomic (temp
  file + rename).

  Durability (a crash can never leave an untraceable mutation):
  - before the first write to the target, `deploy`/`deploy-all` append
    a journal entry with state `in_progress` and the planned archived/
    created/pruned lists; only after the whole operation succeeds is it
    transitioned to `committed` (real archive dirs filled in). If the
    process dies mid-deploy, `po rollback` sees the `in_progress`
    session and *reconciles* it: whatever the attempt archived is
    restored, whatever it created is discarded, then the entry is
    dropped. `deploy`/`deploy-all` refuse to run while an unresolved
    `in_progress` session exists for the target (the rollback hint
    names it). The reconcile scans the WHOLE archive root (regex-
    validated `<name>-<stamp>` dirs with stamp >= the session id), not
    just the pre-planned names: archives whose persona name is in the
    session's `planned_created` were created inside this same session
    (e.g. org B archived a persona that org A had just created in a
    deploy-all) and are discarded instead of restored, so no in-session
    artifact is resurrected and none is left orphaned in the archive
    root. When the same persona name was archived more than once within
    one session (two orgs sharing an actor id, both with `--force`), only
    the OLDEST archive (the pre-session version) is restored; later
    archives of that name are in-session artifacts and are discarded
    instead — restoring both would clobber the pre-session version with
    an in-session one.
  - the whole deploy / deploy-all / rollback holds a *transaction
    lock* (`.phantomorg.lock` in the runtime dir, a dotfile
    phantombot ignores) in addition to the manifest lock, so a
    rollback can never race a concurrent deploy on the same target.
    After the user confirms, rollback re-plans under the lock and
    refuses if a new session was recorded in the meantime.
  - the journal plan (planned_archived/created/pruned,
    archive_root_pre_existed, target_pre_existed) is computed INSIDE
    the transaction lock, never before it: a pre-lock snapshot could
    go stale under a concurrent deploy (a persona planned as
    ``created`` may already exist by the time the lock is held), and
    an interrupted rollback would then misclassify it and discard its
    archive instead of restoring it — losing the pre-deploy version.
  - the in_progress reconcile only ever touches archives whose name
    appears in the session's planned lists (archived/created/pruned).
    A valid `<name>-<stamp>` dir whose name is in NO planned list was
    not created by this deploy (the deploy never archives outside the
    plan): it is foreign — left EXACTLY as found, reported as
    "left untouched", and it keeps the archive root alive so nothing
    of it is ever deleted.

  Portability: both locks are advisory flock locks and degrade to
  no-ops on platforms without fcntl (e.g. Windows); PhantomOrg
  targets Linux/phantombot runtimes, where they are fully effective.

  Confinement: every path read from the manifest is validated before
  being used — persona names must be a single safe directory
  component (`name == Path(name).name`, no `.`/`..`, separators or
  absolute paths) and each archive dir must be an absolute direct
  child of `personas-archive/`. A corrupt or tampered manifest can
  never make rollback move or delete content outside the personas
  tree; symlinked archives are rejected too.

  Symlinks: PhantomOrg never moves or copies through symlinks. A
  symlink inside a compiled actor, a target entry that is a symlink,
  or an archived persona that is a symlink aborts the operation with a
  clear error instead of being followed outside the tree.

Scope of backups: everything PhantomOrg modifies. It never touches
`vault.sqlite`, `phantomchat.json` or memory indexes — those belong to
phantombot and are out of scope.

### 7.3 Phantomchat identity verification (`po phantomchat-check`)

Bots talk to each other over the Nostr NIP-17 layer (phantomchat), not
Telegram — a Telegram group is the human audit mirror, and bots do not
receive other bots' messages there. For an actor to be reachable over
phantomchat it needs a runtime identity (per-persona `identity.json`,
holding its private key) whose derived npub matches the one declared in
`org.yaml`.

- **`actors[].npub`** (optional, NIP-19 bech32, validated incl.
  checksum at `po validate`). Declaring it lets `po build` warn when an
  actor has none, and lets the deploy side generate a contact directory
  (section 8).
- **`po phantomchat-check --org <org.yaml>`** reads each persona's
  runtime state and contrasts it with the spec:

  - `ok` — identity exists, phantomchat.json present, declared npub
    matches the runtime identity;
  - `mismatch` — declared npub differs from the runtime identity;
  - `missing-identity` — no usable identity.json (or no nsec inside);
  - `missing-phantomchat` — no phantomchat.json (non-fatal when the
    npub matches);
  - `not-declared` — org.yaml declares no npub (nothing to verify);
  - `error` — the binary could not be run for this actor.

  Non-invasive by design: it reads files and runs
  `phantombot phantomchat --persona X` (a read-only subcommand) but
  never writes or modifies anything. Exit code 0 when every declared
  npub verifies (not-declared counts as OK), 1 otherwise. `--json`
  prints the full manifest.

  Usage: `po phantomchat-check --org organizations/au/org.yaml
  [--personas-dir ~/.local/share/phantombot/personas] [--bin phantombot]`.

- **Build warning.** `po build` emits a `no-npub` warning per actor
  without a declared npub (non-fatal; the npub is optional for
  non-phantomchat orgs). The warning reminds the operator that the
  actor cannot be reached over the bot-to-bot layer.

### 7.4 Telegram bot verification (`po telegram-check`)

Each actor declares the Telegram handle citizens use to reach it in the
"cadena de personas" (`actors[].telegram_bot`, e.g. `@CEO_bot`). The
handle is *declared* state (org.yaml), but the authoritative value is
what the bot token actually resolves to — Telegram's `getMe` is the
source of truth. A handle can silently drift (bot renamed, token
re-bound to another persona, org.yaml edited aspirationally) without
breaking any local validation.

- **`actors[].telegram_bot`** (optional). At `po validate` only the
  *shape* is checked (`@` + 5..32 chars of `[A-Za-z0-9_]`) — enough to
  catch typos, not enough to know whether the handle exists.
- **`po telegram-check --org <org.yaml>`** reads the runtime config
  (`config.toml`) and contrasts every declared handle with the live bot:

  - token resolution mirrors phantombot's runtime: a sub-persona token
    (`[channels.telegram.personas.<id>].token`) wins; otherwise the
    main token (`[channels.telegram].token`) is used when the actor is
    the default persona. `state.json`'s `default_persona` overrides
    `config.toml`'s, exactly like the runtime does (a stale
    `state.json` default is detectable this way);
  - `ok` — declared handle matches the live `getMe` username
    (case-insensitive, `@`-agnostic);
  - `mismatch` — declared handle differs from the live username
    (org.yaml out of sync, or the token is bound to another persona);
  - `no-token` — declared but no token exists for that actor;
  - `not-declared` — org.yaml declares no handle (nothing to verify);
  - `error` — `getMe` failed (network, timeout, invalid token).

  Non-invasive: it reads `config.toml`/`state.json` and calls the public
  Telegram Bot API. `getMe` works from any host with internet — no need
  to run it on the phantombot host. Exit code 0 when every declared
  handle verifies (not-declared counts as OK), 1 otherwise. `--json`
  prints the full manifest.

  Usage: `po telegram-check --org organizations/au/org.yaml
  [--config ~/.config/phantombot/config.toml]
  [--state ~/.local/share/phantombot/state.json]`.

## 8. Compiler algorithm (pseudocode)

```
function build(org_spec):
    validate(org_spec)  # aborta si falla
    for actor in org_spec.actors:
        role = resolve(actor.role, org_spec.roles)
        department = resolve(role.department, org_spec.departments)
        access = merge_access(department.access_policy, role.access_level,
                               role.security_exceptions, actor.actor_exceptions)
        escalation = escalation_paths_for(role, org_spec.escalation_matrix)

        identity_md = render("identity.j2", actor, role, department)
        soul_md     = render("soul.j2", role, access, escalation, org_spec.communication)
        tools_md    = render("tools.j2", actor.tools, actor.tools_excluded)
        memory_md   = render("memory.j2")  # semilla <2KB

        write_if_changed(actor.id, identity_md, soul_md, tools_md)  # merge por bloques FORJA
        write_if_missing(actor.id, memory_md)  # MEMORY.md: solo si no existe
        ensure_scaffold(actor.id)  # drawers memory/*.md + kb/ + seeds, idempotente
```

`write_if_changed` is key for incremental regeneration: it only rewrites files whose computed content differs from the existing one, and it respects blocks marked as manually edited (same pattern as Phantombot's `ensurePersonaScaffold`).

`MEMORY.md` uses `write_if_missing` instead: once created, the runtime keeps enriching it with durable facts, so PhantomOrg must never regenerate it — not even with block merging.

The scaffold mirrors Phantombot's `personaScaffold.ts` exactly: `memory/` gets the four structured drawers as FILES (`people.md`, `decisions.md`, `lessons.md`, `commitments.md`) plus `archive/`, `kb/` gets the ten category dirs, and the seed files (`kb/Home.md`, `kb/templates/*.md`) are stamped idempotently — an existing seed is never overwritten.

## 9. Validator — executable checklist

- [ ] The YAML validates against the JSON Schema (section 5.2)
- [ ] Every `role.department` exists in `departments`
- [ ] Every `actor.role` exists in `roles`
- [ ] `escalation_matrix` contains no cycles (DAG check)
- [ ] Every `cross_department: true` entry actually crosses a department
- [ ] No escalation chain exceeds `max_hops` without reaching a root or a human
- [ ] Generated SOUL.md ≤ the line budget defined per role (default 300)
- [ ] Generated MEMORY.md ≤ 2 KB
- [ ] No two actors share the same `telegram_bot`
- [ ] `actor_exceptions` is not used as a substitute for role `security_exceptions` (if all actors of a role have the same exception, the validator suggests moving it to the role)

## 10. Bootstrap backlog (tickets, not vague phases)

**Epic 0 — Minimal engine**
- T0.1: Define `model.py` (dataclasses) from the 5.2 JSON Schema
- T0.2: `loader.py` — load YAML, validate against the schema, readable error on failure
- T0.3: `po new-org` + `po add-role` + `po add-actor` (minimal wizard, 1 actor)
- T0.4: Minimal `compiler/identity.py` + `compiler/soul.py`, without escalation or multi-department
- T0.5: Smoke test: generate 1 persona and validate that `phantombot doctor` loads it

**Epic 1 — Multi-department and escalation**
- T1.1: `validator/graph.py` — DAG check over `escalation_matrix`
- T1.2: `validator/refs.py` — cross references
- T1.3: Load AU's real `org.yaml` (section 5.3) as a test fixture
- T1.4: `compiler` generates AU's 5 agents and passes the snapshot test

**Epic 2 — Security and communication**
- T2.1: `merge_access()` — hybrid model RBAC + role exception + actor exception
- T2.2: Message envelope (4.2) documented in the generated `tools.md`, with `request_id_format` injected
- T2.3: `max_hops` applied in the SOUL template (explicit instruction to the agent on when to stop re-escalating)

**Epic 3 — Scale**
- T3.1: `--template ngo` template with sensible defaults
- T3.2: `po deploy` with multi-organization support
- T3.3: Import-audit: read an existing SOUL.md and propose a reverse `org.yaml`

---

## 11. Risks (updated with the references)

- **Over-specifying the communication protocol** (FIPA-ACL's historical mistake): keep the 5 message types from section 4.1 as a ceiling, not as a starting point for adding more "performatives".
- **Unbounded delegation** (the error CrewAI documents in production): `max_hops` is not optional from day 1, not even in Epic 0's MVP.
- **Role explosion** (the error NIST documents in ABAC/RBAC): the validator (T2.1 and the section 9 checklist) must actively warn when an exception repeats across all actors of a role, suggesting moving it from `actor_exceptions` to `security_exceptions`.

---

## 12. Decisions I need from you

1. **Where to build**: local repo in the workspace, or VPS?
2. **Stack**: Python 3 (`questionary`/`rich` + `pydantic` for the schema) vs Bun/TypeScript (same stack as Phantombot). This document's pseudocode is in Python for readability, but it is portable 1:1.
3. **Name confirmation**: PhantomOrg, CLI `po`.
4. **Kickoff**: should I start with Epic 0 (T0.1–T0.5) this week, using alma as a smoke pilot before touching AU's 5 real personas?

With a green light, this document (sections 5–10) is enough for an LLM in Claude Code to bootstrap the repo without any more context than this file.
