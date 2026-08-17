# PhantomMeet — Status and roadmap for GitHub

_Generated: 2026-08-10 (real audit on the local repo, no push)_

## 1. What we have (inventory)

### Project structure (32 files)

```
phantommeet/
├── bin/                        # Portable CLI wrappers (POSIX + Windows)
│   ├── pm                      # main alias
│   ├── pm.cmd
│   ├── phantommeet
│   └── phantommeet.cmd
├── docs/
│   ├── SPEC.md                 # Complete agnostic specification
│   └── meeting-workflow.md     # Operational meeting flow
├── examples/
│   ├── base.example.yaml          # Base manifest (org-agnostic example)
│   └── example-org.yaml           # Derived manifest (generated)
├── src/phantommeet/
│   ├── cli.py                  # CLI (click): derive, validate, apply, check-infra, discover
│   ├── manifest.py             # Manifest loading + validation (mandatory tokens)
│   ├── derive.py               # Derivation from PhantomForge org model
│   ├── apply.py                # Idempotent application to personas
│   ├── discovery.py            # Persona discovery + interactive prompts
│   ├── infra.py                # check-infra (infrastructure probes)
│   └── templates/
│       ├── kb/                 # protocol.es.md / protocol.en.md
│       ├── memory/             # section.es.md / section.en.md
│       └── tools/              # meeting-invite.sh.j2 (configurable card)
├── tests/
│   ├── fixtures/org.smoke.yaml
│   └── test_smoke.py           # 7 end-to-end tests
├── .github/workflows/ci.yml    # Standalone CI (lint + bandit + tests + smoke)
├── CHANGELOG.md                # v0.1.x → v0.2.0
├── LICENSE                     # MIT
├── README.md
├── install.sh                  # symlink to PATH (portable, macOS/Windows-aware)
└── pyproject.toml              # v0.3.0, Python ≥3.10, deps: PyYAML/Jinja2/click
```

### Current functionality (what it does)

| Capability | Status |
|---|---|
| `pm derive-manifest` (org model → manifest) | ✅ |
| `pm validate` (validation + mandatory card tokens) | ✅ |
| `pm apply` idempotent (dry-run, changes, skip) | ✅ |
| `pm apply --ask-roles` / `--invite-roles` | ✅ |
| `pm apply --ask-card` / `--card-file` (configurable card) | ✅ **today** |
| `invite.card` with branding + tokens `%TITLE% %RECIPIENTS% %DATETIME% %LINK% %ROOM% %PASSWORD_LINE%` | ✅ **today** |
| Unified naming room=file `{YYYY-MM-DD}-{HH-MM}_{topic}` | ✅ **today** |
| `pm check-infra` (probes, host-aware) | ✅ |
| `pm discover` | ✅ |
| Multilingual (en/es) protocol + memory | ✅ |
| Multi-org (agnostic, zero hardcoded data) | ✅ |
| E2E reference: Jibri→Whisper→DeepSeek→Drive upload | ✅ |

### Quality guarantees (verified today)

- **ruff check**: ✅ clean (`All checks passed!`)
- **ruff format --check**: ✅ 12/12 files formatted (just fixed 3)
- **bandit -r src**: ✅ 0 findings
- **pytest**: ✅ 7/7 (incl. card-file one-shot, mandatory tokens)
- **Smoke CI** (derive + validate with fixture): ✅ OK
- **Monorepo CI**: the last red was `pm-lint` (ruff format) — **already fixed locally**
- **Standalone phantommeet CI**: full workflow present (lint+bandit+tests+smoke)

## 2. What we must improve (remaining debt)

### ✅ Resolved on 2026-08-10 (blocking checklist prepared)

1. **README updated** — Status section fixed (no longer says "Published to GitHub"), today's features documented (unified naming, invite.card, --ask-card, mandatory tokens), full Repository Layout. ✅

2. **Version bump 0.2.0 → 0.3.0** — in `pyproject.toml` + `src/phantommeet/__init__.py`. ✅

3. **CHANGELOG updated** — v0.3.0 entry with everything from today (unified naming, invite.card, mandatory tokens, --ask-card/--card-file, AU branding). ✅

4. **7+1 local commits without push** — today's work is reflected locally (✅ as you asked), the local branch is 8 commits ahead of origin/main. **No push until authorized.**

5. **Last GitHub CI was RED** (`pm-lint` due to ruff format) — fixed locally; the next push should leave CI green.

### Pending (non-blocking)

6. **`pm update` not implemented** — the phantombot-style self-update cycle (`pm update --check/--force`). **Depends on GitHub Releases**, so it is post-publication by definition.

7. **SECURITY.md / CONTRIBUTING.md** — PhantomForge does not have them either, non-blocking.

8. **Image logo in the card** — `phantombot notify` does not support media. Requires extending phantombot, not phantommeet.

9. **E2E tests of apply on real personas** — today a fixture is used; manual validation against a real tree.

10. **mypy / type checking** — PhantomForge has mypy 0 errors; PhantomMeet does not. Non-blocking (not in CI).

## 3. What is needed for GitHub with guarantees (checklist)

### Blocking (do not publish without this)

- [x] **README updated** (remove "Published to GitHub", document today's features) — ✅ done
- [x] **Version bump 0.2.0 → 0.3.0** in `pyproject.toml` + `__init__.py` + CHANGELOG — ✅ done
- [x] **CHANGELOG with today's entries** (21d4c63→5df70da) — ✅ done
- [ ] **Push with green CI** (the ruff format fix is already local; the push verifies it) — ⏳ pending authorization
- [ ] **Decide where it lives**: stays in the private `phantomtools` monorepo as subtree (current pattern, like PhantomForge) or standalone public repo `salvaalba-dev/phantommeet`? The subtree already exists and can be split (`git subtree split`) — the standalone CI is already prepared for it. — ⏳ owner decision
- [ ] **Explicit Salvador authorization** for the push (30/07 rule) — ⏳ pending

### Desirable (after publishing, in order)

- [ ] `pm update` (GitHub Releases) — the complete phantombot-style cycle
- [ ] SECURITY.md + CONTRIBUTING.md
- [ ] mypy in CI
- [ ] GitHub Releases + tag v0.3.0
- [ ] Repo page with description, topics and website link (e.g. https://example.org)

## 4. Pending owner decision

**Private monorepo or public standalone repo?**

- **Option A (current pattern):** PhantomMeet stays in `phantomtools` (private) next to PhantomForge. The monorepo CI already covers it (`pm-lint` + `pm-pipeline`). The monorepo README lists it as a tool. Publishing = just pushing.
- **Option B (public standalone):** `git subtree split` of `phantommeet/` → new repo `salvaalba-dev/phantommeet` (public). The repo already has its own standalone CI ready. More visibility, but requires maintaining the sync (subtree push/pull) between monorepo and standalone.
- **Option C (hybrid, the one PhantomForge uses):** PhantomForge lives in the private monorepo AND was published... — actually PhantomForge was consolidated ONLY in the monorepo (the standalone `phantomforge.git` was removed). So the established pattern is **A**.

_Recommendation: follow the PhantomForge pattern (Option A) — the private monorepo as source of truth, and if it ever goes public, split with its standalone CI already prepared._
