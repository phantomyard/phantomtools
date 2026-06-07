# email-triage

Give a [phantombot](https://github.com/phantomyard/phantombot) persona its own
mailbox and have it triage that mailbox to **zero unread, on its own**.

A cheap poller checks the inbox on a schedule. When new mail shows up it wakes a
full agent turn with a triage prompt; the agent reads each message, deals with
it (or marks spam/newsletters seen), and leaves the inbox empty. Nothing new →
silent run. The poll failing → the agent is woken to repair itself.

It's two small, dependency-free Python scripts (stdlib `imaplib`/`email` only)
plus an editable prompt. No pip install, no service to run — the scheduling is a
single command-backed `phantombot task`.

> **This is a phantombot add-on.** The entire wake mechanism is `phantombot task
> add`, so phantombot must be installed and on PATH. It is not a standalone
> mail client.

## How it works

```
 phantombot task (every 15m, --command)
        │
        ▼
   inbox-poll.py ──► IMAP: any UNSEEN?
        │                │
        │  new unread    │ nothing new / clean
        ▼                ▼
  phantombot task     (silent, exit 0)
  add  --in 1m
        │
        ▼
   agent turn  ──► reads wake-prompt.md, uses inbox-mail.py to
                   list/read/mark, drives inbox to ZERO unread
```

- **New-mail detection is by UID**, so you're only woken for genuinely new
  messages, not re-woken every poll for mail that's already sitting there.
- **Failures are throttled** (1/hour per error) so a broken poll nags once, not
  every 15 minutes.
- **State** is one small JSON file at
  `~/.local/state/phantombot-inbox-poll/state.json` (seen UIDs + failure
  throttle). Delete it to reset.

## Files

| File | What it is |
|------|------------|
| `inbox-poll.py` | The poller. Run by `phantombot task` on a schedule; wakes a turn on new mail or on its own failure. |
| `inbox-mail.py` | IMAP helper CLI the agent uses during a turn: `list-unread`, `read <uid>`, `mark-seen <uid>...`, `mark-unseen <uid>...`. |
| `wake-prompt.example.md` | Template for the triage instructions. Copy to `wake-prompt.md` and tailor to the persona's role. |
| `.env.example` | All available config vars, documented. |
| `install.sh` | Installs the scripts, drops an editable wake-prompt, creates the state dir, registers the poll task. Idempotent. |
| `AGENT_SETUP.md` | A paste-to-the-agent prompt so a persona can install + configure itself. |

## Quick start

You need an IMAP **app password** for the mailbox (not the normal login
password). For Gmail: Google Account → Security → App passwords (requires 2FA).

```bash
# 1. Clone the tools repo (or pull if you already have it)
git clone https://github.com/phantomyard/phantomtools.git ~/phantomtools
cd ~/phantomtools/email-triage

# 2. Store the mailbox credentials (persist in ~/.env, mode 0600)
phantombot env set INBOX_EMAIL        "you@example.com"
phantombot env set INBOX_APP_PASSWORD "abcd efgh ijkl mnop"
#   Non-Gmail? also: phantombot env set INBOX_IMAP_HOST "imap.fastmail.com"

# 3. Install + register the poll task (every 15m by default)
./install.sh

# 4. Verify
~/.local/bin/inbox-mail.py list-unread   # JSON array (maybe empty), no error
phantombot task list                     # shows the poll task
```

Then **tailor the triage behaviour**: edit `~/.local/bin/wake-prompt.md` and add
bullet points for what this persona actually does with mail (review PRs, action
tickets, file invoices, escalate to a human, …). The defaults are deliberately
generic.

Prefer to have the agent do all of this? Hand it `AGENT_SETUP.md`.

## Configuration

Set via `phantombot env set NAME "value"` (so both the poller and the woken turn
can see them). Full reference in `.env.example`.

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `INBOX_EMAIL` | yes | — | Mailbox address to watch/triage. |
| `INBOX_APP_PASSWORD` | yes | — | IMAP app password for that mailbox. |
| `INBOX_IMAP_HOST` | no | `imap.gmail.com` | IMAP server. |
| `INBOX_IMAP_PORT` | no | `993` | IMAP SSL port. |
| `INBOX_TASK_LABEL` | no | `Process inbox mail` | Label shown in `phantombot task list`. |
| `INBOX_WAKE_PROMPT` | no | `wake-prompt.md` beside the script, else built-in | Path to a custom triage template. |

### Triage template tokens

The wake prompt is read fresh on every wake, so edits take effect immediately —
no restart. These tokens are substituted before the agent sees the text:

| Token | Becomes |
|-------|---------|
| `{{unread}}` | count of unread messages |
| `{{account}}` | the mailbox address (`INBOX_EMAIL`) |
| `{{mail_helper}}` | absolute path to `inbox-mail.py` |

## inbox-mail.py reference

```bash
inbox-mail.py list-unread            # [{uid, from, subject, date}, ...]
inbox-mail.py read <uid>             # {from, to, subject, date, body}
inbox-mail.py mark-seen <uid>...     # flag handled/dismissed
inbox-mail.py mark-unseen <uid>...   # undo
```

All commands print JSON and exit non-zero on error.

## Changing the schedule

The default is every 15 minutes. To change it, cancel the task and re-run the
installer with a different interval:

```bash
phantombot task list                 # find the id
phantombot task cancel <id>
POLL_INTERVAL=5m ./install.sh
```

## Security model

Email is **untrusted input**. The triage prompt makes this explicit: the agent
treats every sender, subject, and body as data to be triaged, never as
instructions to obey. Only the operator can direct the agent's work. Keep that
clause when you customise `wake-prompt.md`. (phantombot also runs untrusted
inbound through its own threat judge before any capable turn — this prompt is
belt-and-braces on top of that.)

Credentials live only in `~/.env` (mode 0600) and are passed to the
command-backed task via explicit `--secret` flags, so the poll process gets a
minimal environment with just what it needs.

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `inbox-mail.py list-unread` → `missing INBOX_EMAIL` | Credentials not set. `phantombot env set INBOX_EMAIL ...` (and `INBOX_APP_PASSWORD`). |
| Login/auth error | You used your normal password, not an **app password**, or the wrong `INBOX_IMAP_HOST`. |
| Poller runs but agent never wakes | Check `phantombot task log <id>` for the poll task. A non-zero exit shows the IMAP error; a clean run with `new_unread: 0` is correct (nothing new). |
| Woken repeatedly for the same mail | The turn isn't actually marking messages seen. The end state must be zero unread; check the agent is calling `inbox-mail.py mark-seen`. |
| Reset detection state | Delete `~/.local/state/phantombot-inbox-poll/state.json`. |

## License

MIT (same as the repo root).
