# PhantomOrg review fixes

Implemented review hardening in the supplied project.

## GPT review fixes (applied + verified)

- Trust generation: bridge and peer actors are no longer promoted to principal trust; onboarding is not pre-seeded.
- Security anchor: SOUL.md now carries the platform trust/threat-judge/prompt-injection rules outside ORG-owned blocks.
- KB vocabulary: generated index/template frontmatter uses `index`, `title`, `description`, and `aliases`; `concept.md` replaces `atomic-note.md`.
- Installer: `po` is installed, with `pf` retained as a compatibility alias.
- `missing-phantomchat` status is now emitted before invoking the phantomchat CLI.

## Additional hardening (post-GPT, per the phantomyard PR #25 review)

- **Additive deploy** (the "Remaining" item is now DONE): a normal `po deploy`
  writes only the files PhantomOrg owns, in place, atomically per file. The live
  persona directory is never moved, replaced, or archived. `identity.json`,
  `vault.sqlite`, `memory/`, `kb/` notes, and all other runtime-owned files are
  preserved. Files being overwritten are backed up per-file into
  `personas-archive/`, and `po rollback` restores them. There is no
  whole-directory replacement mode: a fresh persona is a runtime-owned
  lifecycle operation, never a compiler deploy.
- **Prune reverts only owned regions**: pruning an actor no longer in the spec
  archives and removes only PhantomOrg-owned content — plain files removed,
  merge files keep everything outside the ORG markers, and seed/runtime files
  stay byte-for-byte. The persona directory is never removed.
- **Principal-only trust**: `allowed_npubs` holds only the explicit
  `principal_npubs` (empty by default, fail-closed). `human_npubs`, the bridge
  and relays are delivery endpoints, never trusted.
- **memory/norms.md is seed-only**: the drawer belongs to the
  capture/heartbeat/nightly pipeline; the compiler seeds it once (a pointer)
  and never overwrites it. The communication norm lives in the KB as an
  OKF-frontmatter procedure.
- `phantomchat.json` is seeded once and never overwritten (the allowlist is
  runtime state).
- Collision-safe data-file backup names (UUID suffix, no timestamp clobber).
- `build()` reconciles stale actors and obsolete derived artifacts when reusing
  an output directory.
- Non-zero exit code for partial `deploy-all` / `build-all`.
- `po` command installed (was only the `pf` alias).
- CI wired at the repository root (ruff + format + bandit + mypy + tests).
- Synthetic fictional fixtures throughout organizations/, docs/, CHANGELOG, and
  tests — fictional names AND newly generated synthetic npubs (no real keys).

See `docs/pr-reviews/pr25-phantomorg-review-response.md` for the full review
cross-reference.
