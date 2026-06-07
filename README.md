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

## License

MIT, unless a tool's own README says otherwise.
