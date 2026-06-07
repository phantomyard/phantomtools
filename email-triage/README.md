# email-triage

Give a [phantombot](https://github.com/phantomyard/phantombot) persona its own
mailbox and have it triage that mailbox to **zero unread, on its own**.

A cheap poller checks the inbox on a schedule. When new mail shows up it wakes a
full agent turn with a triage prompt; the agent reads each message, deals with
it (or marks spam/newsletters seen), and leaves the inbox empty. Nothing new →
silent run. The poll failing → the agent is woken to repair itself.

It's two small Python scripts plus an editable prompt. No pip install, no
service to run — the scheduling is a single command-backed `phantombot task`.

### Two backends, in one poller

| Backend | For | Needs |
|---------|-----|-------|
| `imap` | any IMAP mailbox (Gmail app password, Fastmail, M365, …) | pure stdlib `imaplib`/`email` — nothing to install |
| `gog`  | Gmail / Google Workspace via OAuth | the [`gog`](https://github.com/phantomyard/gogcli) CLI, already authenticated |

You can configure **one IMAP mailbox, any number of `gog` mailboxes, or a mix** —
all watched by a single poller, with one combined wake. A persona that has both a
Workspace inbox and an IMAP inbox is the motivating case.

> **This is a phantombot add-on.** The entire wake mechanism is `phantombot task
> add`, so phantombot must be installed and on PATH. It is not a standalone
> mail client. The `gog` backend additionally needs the `gog` CLI.

## How it works

```
 phantombot task (every 15m, --command)
        │
        ▼
   inbox-poll.py ──► each account: any unread? (IMAP UNSEEN / gog is:unread)
        │                │
        │  new unread    │ nothing new / clean
        ▼                ▼
  phantombot task     (silent, exit 0)
  add  --in 1m
        │
        ▼
   agent turn  ──► reads wake-prompt.md, uses inbox-mail.py to
                   list/read/mark each account, drives all to ZERO unread
```

- **New-mail detection is by id, per account**, so you're only woken for
  genuinely new messages, not re-woken every poll for mail already sitting there.
- **Each account is polled independently** — one flaky account never blinds the
  others; the working ones still get triaged.
- **Failures are throttled** (1/hour per error signature) so a broken poll nags
  once, not every 15 minutes.
- **State** is one small JSON file at
  `~/.local/state/phantombot-inbox-poll/state.json` (seen ids per account +
  failure throttle). Delete it to reset.

## Files

| File | What it is |
|------|------------|
| `inbox-poll.py` | The poller. Run by `phantombot task` on a schedule; wakes a turn on new mail or on its own failure. |
| `inbox-mail.py` | Mail helper CLI the agent uses during a turn (IMAP or gog): `list-unread`, `read <uid>`, `mark-seen <uid>...`, `mark-unseen <uid>...`. Pick the mailbox with `--account`. |
| `wake-prompt.example.md` | Template for the triage instructions. Copy to `wake-prompt.md` and tailor to the persona's role. |
| `.env.example` | All available config vars, documented. |
| `install.sh` | Installs the scripts, drops an editable wake-prompt, creates the state dir, registers the poll task. Idempotent. |
| `AGENT_SETUP.md` | A paste-to-the-agent prompt so a persona can install + configure itself. |

## Quick start

```bash
# 1. Clone the tools repo (or pull if you already have it)
git clone https://github.com/phantomyard/phantomtools.git ~/phantomtools
cd ~/phantomtools/email-triage

# 2. Store credentials for at least one backend (persist in ~/.env, mode 0600)

#    --- IMAP mailbox (needs an app password, not your login password;
#        for Gmail: Google Account → Security → App passwords, requires 2FA) ---
phantombot env set INBOX_EMAIL        "you@example.com"
phantombot env set INBOX_APP_PASSWORD "abcd efgh ijkl mnop"
#    Non-Gmail? also: phantombot env set INBOX_IMAP_HOST "imap.fastmail.com"

#    --- and/or one or more gog (Google OAuth) mailboxes, already authed in gog ---
phantombot env set INBOX_GOG_ACCOUNTS "you@yourdomain.com,you@gmail.com"
#    phantombot env set GOG_KEYRING_PASSWORD "..."   # if your gog keyring needs it

# 3. Install + register the poll task (every 15m by default)
./install.sh

# 4. Verify each configured account
~/.local/bin/inbox-mail.py list-unread                        # the IMAP account
~/.local/bin/inbox-mail.py --account you@yourdomain.com list-unread  # a gog account
phantombot task list                                          # shows the poll task
```

Then **tailor the triage behaviour**: edit `~/.local/bin/wake-prompt.md` and add
bullet points for what this persona actually does with mail (review PRs, action
tickets, file invoices, escalate to a human, …). The defaults are deliberately
generic.

Prefer to have the agent do all of this? Hand it `AGENT_SETUP.md`.

## Configuration

Set via `phantombot env set NAME "value"` (so both the poller and the woken turn
can see them). Full reference in `.env.example`.

Configure at least one backend (IMAP, gog, or both).

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `INBOX_EMAIL` | one backend | — | IMAP mailbox address to watch/triage. |
| `INBOX_APP_PASSWORD` | with IMAP | — | IMAP app password for that mailbox. |
| `INBOX_IMAP_HOST` | no | `imap.gmail.com` | IMAP server. |
| `INBOX_IMAP_PORT` | no | `993` | IMAP SSL port. |
| `INBOX_GOG_ACCOUNTS` | one backend | — | Comma-separated Gmail/Workspace addresses polled via `gog`. |
| `GOG_KEYRING_PASSWORD` | with gog | — | Passphrase for gog's credential keyring, if it needs one. |
| `INBOX_GOG_BIN` | no | `gog` on PATH | Path to the gog binary. |
| `INBOX_GOG_QUERY` | no | `is:unread -in:trash -in:spam newer_than:30d` | Gmail search used to enumerate unread for gog accounts. |
| `INBOX_TASK_LABEL` | no | `Process inbox mail` | Label shown in `phantombot task list`. |
| `INBOX_WAKE_PROMPT` | no | `wake-prompt.md` beside the script, else built-in | Path to a custom triage template. |

### Triage template tokens

The wake prompt is read fresh on every wake, so edits take effect immediately —
no restart. These tokens are substituted before the agent sees the text:

| Token | Becomes |
|-------|---------|
| `{{unread}}` | total unread across all accounts |
| `{{accounts}}` | one line per account: `- addr (backend): N unread` |
| `{{account}}` | the first account address (handy for single-account setups) |
| `{{mail_helper}}` | absolute path to `inbox-mail.py` |

## inbox-mail.py reference

```bash
# --account picks the mailbox: a gog address (listed in INBOX_GOG_ACCOUNTS) uses
# the gog backend; any other address uses IMAP. Omit it for the single IMAP account.
inbox-mail.py [--account ADDR] list-unread          # [{uid, from, subject, date}, ...]
inbox-mail.py [--account ADDR] read <uid>           # {from, to, subject, date, body}
inbox-mail.py [--account ADDR] mark-seen <uid>...   # flag handled/dismissed
inbox-mail.py [--account ADDR] mark-unseen <uid>... # undo
```

For the gog backend the `uid` is the Gmail messageId; read/mark take those same
ids. All commands print JSON and exit non-zero on error.

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
| `inbox-mail.py list-unread` → `missing INBOX_EMAIL` | No IMAP account set. `phantombot env set INBOX_EMAIL ...` (and `INBOX_APP_PASSWORD`), or pass `--account` for a gog mailbox. |
| IMAP login/auth error | You used your normal password, not an **app password**, or the wrong `INBOX_IMAP_HOST`. |
| `gog ... failed` / gog auth error | The address isn't authenticated in gog (`gog auth ...`), it's missing from `INBOX_GOG_ACCOUNTS`, or `GOG_KEYRING_PASSWORD` is needed but unset. Test with `inbox-mail.py --account ADDR list-unread`. |
| Poller runs but agent never wakes | Check `phantombot task log <id>` for the poll task. A non-zero exit shows the IMAP error; a clean run with `new_unread: 0` is correct (nothing new). |
| Woken repeatedly for the same mail | The turn isn't actually marking messages seen. The end state must be zero unread; check the agent is calling `inbox-mail.py mark-seen`. |
| Reset detection state | Delete `~/.local/state/phantombot-inbox-poll/state.json`. |

## License

MIT (same as the repo root).
