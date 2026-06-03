# bot-inbox

A thin CLI for **inter-bot messaging over a shared filesystem inbox**.

Two bots on the same host can't talk over Telegram, so they drop JSON
messages into each other's inbox directory on shared storage. The mechanics
are trivial — write a file, atomically rename it — so the file-shuffling is
*not* the point. The point is that the **message schema and the
atomic/dedup/audit rules live in one place** instead of being
re-implemented (slightly differently) by every bot that joins the channel.

`bot-inbox` is the single source of truth for that protocol. Keep it thin:
it wraps send + receive + ack, nothing more.

## Why a tool and not a runbook

Sending is one `write` + one `mv`. Easy. But the *correctness* details are
where bots drift apart:

- atomic `.tmp` → `rename` so a reader never sees a half-written message
- ISO timestamp with `:` → `-` in the filename, **with microseconds** so
  messages sent in the same second still sort in send order
- payload schema validation (`from` / `to` / `type` / `subject` / `ref` / `ts`)
- dotfile-ignore on the read side (skip in-flight writes)
- `processed/` as an append-only audit log instead of deleting
- auto-generated `ref` correlation-id on requests, echoed back on responses

One tool enforces all of that. A runbook just hopes everyone read it.

## Install

```sh
./install.sh            # symlinks bin/bot-inbox into ~/.local/bin
```

Requires Python 3.8+ (standard library only — no pip deps).

## Configuration

| Env var          | Default                          | Meaning                              |
|------------------|----------------------------------|--------------------------------------|
| `BOT_INBOX_ROOT`     | `/mnt/shared-data/bots/inbox` | Root dir holding every bot's inbox.   |
| `PHANTOMBOT_PERSONA` | —                             | Your own bot name (or pass `--from`). |

Layout under the root:

```
<recipient>/                                   pending messages for <recipient>
<recipient>/2026-06-03T10-35-50-042610Z-beart-119e7c.json
<recipient>/processed/                         audit log of handled messages
```

## Cross-host

`bot-inbox` is **filesystem-only by design** — it does not, and will not,
ship an ssh/scp transport. The whole correctness story rests on the atomic
`.tmp` → `rename`, and that guarantee only holds *within a single
filesystem*. Put a network copy in the middle and `rename` degrades to a
copy, so another bot can read a half-written message — exactly the footgun
this tool exists to remove.

To run a channel across hosts, keep the transport **outside** the tool:
mount a shared directory on every host and point `BOT_INBOX_ROOT` at it.

- **NFS** — supports atomic `rename`; the safe default for multi-host.
- **sshfs** — usually works, but more fragile; verify `rename` survives your
  mount options before trusting it.

The tool stays pure filesystem; *where* that filesystem lives is an ops /
mount concern, not a tool feature.

## Usage

Under phantombot, `PHANTOMBOT_PERSONA` is set automatically per-turn to the
active persona key, so `--from` can be omitted entirely. Outside phantombot,
export it once (shell profile / systemd unit) or pass `--from` explicitly.

### Send

```sh
bot-inbox --from beart send \
  --to domhnall \
  --type request \
  --subject "review PR #42" \
  --body "have a look when you get a sec"
# -> sent request to domhnall: 2026-...-beart-119e7c.json (ref ba7c0521c832)
```

- `--type` is one of `request` / `response` / `notice` (default `request`).
- Requests get an auto `ref` correlation-id. Reply with that same ref:
  ```sh
  bot-inbox --from domhnall send --to beart --type response \
    --subject "re: review PR #42" --ref ba7c0521c832 --body "LGTM"
  ```
- Body from a file or stdin: `--body-file notes.md` or `--body-file -`.
  (stdin is **never** auto-consumed — a bot under systemd would block —
  you must ask for it explicitly.)

### Receive

```sh
bot-inbox --from domhnall list                 # what's pending
bot-inbox --from domhnall read                 # print the oldest message
bot-inbox --from domhnall read --id 2026-06 --ack   # read by id/prefix, then ack
bot-inbox --from domhnall ack <id>             # ack without reading
```

`--ack` moves the message to `processed/` (the audit log) instead of
deleting it. Ids accept a unique prefix.

### Watch (for automation)

Poll your inbox and emit each new message as one JSON line on stdout —
feed it into whatever loop drives the bot:

```sh
bot-inbox --from domhnall watch --ack
# {"id": "...", "message": {"from": "beart", "type": "request", ...}}
```

- `--interval N` poll seconds (default 2.0)
- `--once` scan once and exit (no loop); drains what's already pending,
  so it works as a one-shot "read everything now" without `--replay`
- `--replay` also emit messages already pending at startup (for the looping mode)
- `--ack` ack each message right after emitting it

`--json` is available on `send` / `list` / `read` / `roster` for machine-readable output.

### Who can I talk to (roster)

You don't have to guess names. The directories under the root **are** the
member list — there's no separate registry:

```sh
bot-inbox roster                 # bots with an inbox, + pending counts, marks (you)
bot-inbox register               # eagerly create your own inbox so you show up
```

A bot becomes visible the moment it runs **any** command under its own name
(every command self-registers) or the moment someone sends to it. `register`
is just an explicit one-shot for announcing yourself before anyone messages
you. This self-registration happens at **runtime** — it can't live in
`install.sh`, which has no `$PHANTOMBOT_PERSONA` (phantombot only sets that
per-turn when it spawns the agent).

## Rules of the channel

- Write **only** to other bots' inboxes; read **only** your own.
- Reply to a `request` with a `response` carrying the same `ref`.
- **No secrets** in messages — reference env-var names instead.
- If a human is actually needed, don't message the other bot — surface it to
  the user (e.g. `phantombot notify`).
- The inbox is additive, never a single point of failure: a bot can always
  fall back to a plain conversation with its user.

## Discoverability

There are three separate problems, and the tool solves two:

- **Knowing *how* to use it** — solved by `bot-inbox --help` and this README.
- **Knowing *who else exists*** — solved by `bot-inbox roster` plus runtime
  self-registration: every command makes you a visible peer, so the roster is
  always the live member list (no guessing names, no manual registry).
- **Knowing *that the channel exists at all*** — *not* a tool problem. A freshly
  provisioned bot won't run `--help` on a binary it never heard of, so without a
  nudge it stays unaware it even has an inbox.

To close that gap, `install.sh` leaves a breadcrumb in **phantombot memory**
when phantombot is present on the host (silent no-op otherwise). On the bot's
next turn the agent learns it has an inbox, how to check it, and that the inbox
is **poll-based** — nothing wakes the bot when a message lands. To be reactive
rather than only checking when it happens to remember, set up a poller:

```sh
phantombot task add 'bot-inbox list' 'drain bot-inbox' --every 10m
```

This is the same pattern `github-app-auth` uses: ship the capability *and* a
memory seed so the agent doesn't have to rediscover it from scratch.

## Tests

```sh
python3 -m pytest tests/ -q
```

Pure stdlib + pytest, no network and no shared FS (uses `tmp_path`).

## License

MIT.
