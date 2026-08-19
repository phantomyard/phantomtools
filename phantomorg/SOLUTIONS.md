# PhantomOrg review fixes

Implemented review hardening in the supplied project.

## GPT review fixes (applied + verified)

- Trust generation: bridge and peer actors are no longer promoted to principal trust; onboarding is not pre-seeded.
- Security anchor: SOUL.md now carries the platform trust/threat-judge/prompt-injection rules outside ORG-owned blocks.
- Threat-judge norms: `memory/norms.md` is seeded and operational communication rules are marker-merged there.
- KB vocabulary: generated index/template frontmatter uses `index`, `title`, `description`, and `aliases`; `concept.md` replaces `atomic-note.md`.
- Installer: `po` is installed, with `pf` retained as a compatibility alias.
- `missing-phantomchat` status is now emitted before invoking the phantomchat CLI.

## Additional hardening (post-GPT, per the phantomyard PR #25 review)

- **Additive deploy** (the "Remaining" item below is now DONE): a normal `po deploy`
  writes only the files PhantomOrg owns, in place, atomically per file. The live
  persona directory is never moved, replaced, or archived. `identity.json`,
  `vault.sqlite`, `memory/`, `kb/` notes, and all other runtime-owned files are
  preserved. Files being overwritten are backed up per-file into
  `personas-archive/`, and `po rollback` restores them. The destructive
  whole-directory replacement is now an explicit `po deploy --reset`.
- `phantomchat.json` is seeded once and never overwritten (the allowlist is
  runtime state).
- Collision-safe data-file backup names (UUID suffix, no timestamp clobber).
- `build()` reconciles stale actors and obsolete derived artifacts when reusing
  an output directory.
- Non-zero exit code for partial `deploy-all` / `build-all`.
- `po` command installed (was only the `pf` alias).
- CI wired at the repository root (ruff + format + bandit + mypy + tests).
- Synthetic fictional fixtures throughout organizations/, docs/, CHANGELOG, and
  tests (no real org/person/project material).

See `docs/pr-reviews/pr25-phantomorg-review-response.md` for the full review
cross-reference.
