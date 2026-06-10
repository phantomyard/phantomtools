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

## License

MIT, unless a tool's own README says otherwise.
