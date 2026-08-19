# phantomtools

A grab-bag of self-contained tools and utilities from the phantomyard
ecosystem. Each subdirectory is its own project, with its own README,
deps, and license.

## Tools

| Path                                        | What it is                                                              |
|---------------------------------------------|-------------------------------------------------------------------------|
| [`twilio-voice-agent/`](./twilio-voice-agent) | Twilio ConversationRelay-based voice agent — inbound + outbound calls, low-latency LLM, optional back-end assistant relay. |
| [`github-app-auth/`](./github-app-auth) | GitHub App authentication for `git push` / `fetch` / `pull` — wraps the GitHub API so App installation tokens work transparently. |
| [`bot-inbox/`](./bot-inbox) | Thin CLI for inter-bot messaging over a shared filesystem inbox — one source of truth for the message schema, atomic delivery, dedup, and the `processed/` audit log. |
| [`email-triage/`](./email-triage) | Self-driving inbox triage for a phantombot persona — a cheap IMAP poller wakes a full agent turn on new mail and drives the inbox to zero unread. Dependency-free Python; scheduling is a single `phantombot task`. |
| [`phantommeet/`](./phantommeet) | Meeting layer for PhantomForge deployments — text participation via bridge, recordings, transcription, calendar logistics, per-scope recording custody. `pm` CLI (validate / derive-manifest / apply / check-infra). |

## Tool dependency chain

Some tools are not fully self-contained; they form a chain. Deploy them in
order and keep the dependencies in mind when diagnosing:

- **PhantomOrg** (`phantomorg/`) — produces the `org.yaml` org model. This is
  the single source of truth for roles/actors/escalation.
- **PhantomBridge** (`phantombridge/`) — depends on PhantomOrg's `org.yaml`
  for room/participant admission; carries meeting traffic.
- **PhantomMeet** (`phantommeet/`) — depends on PhantomOrg's `org.yaml` (its
  manifests are *derived* from it) and on PhantomBridge being deployed for
  actual meeting participation. It degrades to an invitation/scheduling layer
  when the bridge is absent.

## Keeping installed copies in sync

Each tool's `install.sh` **symlinks** its `bin/` scripts into your `PATH`
(`~/.local/bin` by default). The repo stays the single source of truth: never
edit the installed copies — edit the source here and re-run `install.sh`.

To catch the case where someone *did* edit an installed copy in place (turning a
symlink into a diverged regular file, a change invisible to git and lost on the
next install), run:

```bash
github-app-auth report-drift            # scan every tool's installed wrappers
github-app-auth report-drift --dry-run  # show the diffs, file nothing
```

It walks every tool that ships a `bin/` + `install.sh`, compares each installed
wrapper to its repo source, and — unless `--dry-run` — opens a de-duplicated
issue (one per drifted script, keyed by a stable marker) so the change gets
folded back in. New tools are picked up automatically; there's nothing to wire
up per tool. The installers also refuse to clobber a diverged copy and point you
at this command.

## Contributing

Most tools here touch a **running phantombot persona** — its identity, its
encrypted secrets, its accumulated memory, its security perimeter. Before opening
a PR, read **[CONTRIBUTING.md](./CONTRIBUTING.md)**: it covers what a tool may and
may not own inside a persona directory, how to emit into the memory system instead
of rebuilding it, OKF conformance for generated knowledge, and the security
perimeter rules (including the ones that are hard blocks). It ends with a PR
checklist.

The one-line version: **a tool is additive — it may create and own things, but it
may never assume it knows the full contents of something a phantom owns.**

## License

MIT, unless a tool's own README says otherwise.
