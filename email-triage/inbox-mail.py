#!/usr/bin/env python3
"""Small mail helper for phantombot inbox triage wake-ups.

A thin, dependency-free CLI an agent uses to read and flag its own mailbox
during a triage turn. Supports two backends:

  - imap : pure Python stdlib (imaplib/email) — nothing to pip install.
  - gog  : Google Workspace / Gmail via the `gog` CLI (OAuth, no app password).

Subcommands (identical for both backends):
  list-unread              JSON array of unread {uid, from, subject, date}
  read <uid>               full {from, to, subject, date, body} for one message
  mark-seen <uid>...       flag messages read (use after handling/dismissing)
  mark-unseen <uid>...     clear the read flag (undo)

Which backend / mailbox is used:
  --account ADDR   the mailbox to operate on. If ADDR is listed in
                   INBOX_GOG_ACCOUNTS it uses the gog backend; otherwise imap.
                   If omitted, defaults to the single IMAP account (INBOX_EMAIL).

Configuration (environment, typically set via `phantombot env set`):
  INBOX_EMAIL          the IMAP mailbox address, e.g. you@example.com
  INBOX_APP_PASSWORD   IMAP app password for that mailbox
  INBOX_IMAP_HOST      IMAP host, default imap.gmail.com
  INBOX_IMAP_PORT      IMAP SSL port, default 993
  INBOX_GOG_ACCOUNTS   comma-separated Gmail/Workspace addresses polled via gog
  INBOX_GOG_BIN        path to the gog binary (default: gog on PATH)
  GOG_KEYRING_PASSWORD passphrase for gog's credential keyring (gog backend)

For the gog backend, the `uid` returned by list-unread is the Gmail *messageId*;
read / mark-seen / mark-unseen accept those same ids.
"""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import re
import shutil
import subprocess
import sys
from email.header import decode_header, make_header
from email.message import Message

DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993

# Gmail search used to enumerate unread mail for the gog backend. Bounded so a
# brand-new account with a huge unread history can't return an unbounded set.
DEFAULT_GOG_QUERY = "is:unread -in:trash -in:spam newer_than:30d"
GOG_MAX = 200


def load_env_files() -> None:
    """Populate INBOX_*/GOG_* vars from ~/.env and ~/.config/phantombot/.env.

    phantombot stores credentials in ~/.env (mode 0600); a plain shell does not
    export them, so running this script by hand (as the README's verify step
    does) would otherwise fail with "missing INBOX_EMAIL". Load them here,
    without overwriting anything already in the environment — an explicit env
    var or a phantombot --secret injection always wins.
    """
    wanted = ("INBOX_", "GOG_KEYRING_PASSWORD")
    for path in (
        os.path.expanduser("~/.env"),
        os.path.expanduser("~/.config/phantombot/.env"),
    ):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key = key.strip()
            if not key.startswith(wanted) or key in os.environ:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ[key] = value


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing {name}")
    return value


def gog_accounts() -> set[str]:
    raw = os.environ.get("INBOX_GOG_ACCOUNTS") or ""
    return {a.strip() for a in raw.split(",") if a.strip()}


def resolve_backend(account_arg: str | None) -> tuple[str, str]:
    """Return (address, backend) for the requested account.

    --account in INBOX_GOG_ACCOUNTS -> gog; any other --account -> imap;
    no --account -> the single configured IMAP account (back-compat).
    """
    if account_arg:
        if account_arg in gog_accounts():
            return account_arg, "gog"
        return account_arg, "imap"
    return require_env("INBOX_EMAIL"), "imap"


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #

def decode(value: str | None) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def text_from_message(message: Message) -> str:
    if message.is_multipart():
        html = ""
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disposition = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            content_type = part.get_content_type()
            if content_type == "text/plain":
                return text.strip()
            if content_type == "text/html" and not html:
                html = text
        if html:
            html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
            html = re.sub(r"(?s)<[^>]+>", " ", html)
            return re.sub(r"[ \t\r\f\v]+", " ", html).strip()
        return ""

    payload = message.get_payload(decode=True)
    if not payload:
        return ""
    charset = message.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace").strip()


# --------------------------------------------------------------------------- #
# imap backend
# --------------------------------------------------------------------------- #

def imap_connect(account: str) -> imaplib.IMAP4_SSL:
    host = os.environ.get("INBOX_IMAP_HOST", DEFAULT_IMAP_HOST)
    port = int(os.environ.get("INBOX_IMAP_PORT", DEFAULT_IMAP_PORT))
    mailbox = imaplib.IMAP4_SSL(host, port, timeout=30)
    mailbox.login(account, require_env("INBOX_APP_PASSWORD"))
    mailbox.select("INBOX")
    return mailbox


def imap_unread(mailbox: imaplib.IMAP4_SSL) -> list[str]:
    typ, data = mailbox.uid("search", None, "UNSEEN")
    if typ != "OK":
        raise RuntimeError(f"UNSEEN search returned {typ}")
    return [uid.decode("ascii", errors="replace") for uid in data[0].split() if uid]


def imap_fetch(mailbox: imaplib.IMAP4_SSL, uid: str) -> Message:
    typ, data = mailbox.uid("fetch", uid, "(BODY.PEEK[])")
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        raise RuntimeError(f"fetch {uid} returned {typ}")
    return email.message_from_bytes(data[0][1])


def imap_list(account: str) -> list[dict]:
    with imap_connect(account) as mailbox:
        rows = []
        for uid in imap_unread(mailbox):
            message = imap_fetch(mailbox, uid)
            rows.append({
                "uid": uid,
                "from": decode(message.get("From")),
                "subject": decode(message.get("Subject")),
                "date": decode(message.get("Date")),
            })
    return rows


def imap_read(account: str, uid: str) -> dict:
    with imap_connect(account) as mailbox:
        message = imap_fetch(mailbox, uid)
    return {
        "uid": uid,
        "from": decode(message.get("From")),
        "to": decode(message.get("To")),
        "subject": decode(message.get("Subject")),
        "date": decode(message.get("Date")),
        "body": text_from_message(message),
    }


def imap_store(account: str, uids: list[str], seen: bool) -> None:
    # IMAP tracks read state with the \Seen flag: ADD it to mark read.
    op = "+FLAGS" if seen else "-FLAGS"
    with imap_connect(account) as mailbox:
        for uid in uids:
            typ, _ = mailbox.uid("store", uid, op, "(\\Seen)")
            if typ != "OK":
                raise RuntimeError(f"store {uid} returned {typ}")


# --------------------------------------------------------------------------- #
# gog backend
# --------------------------------------------------------------------------- #

def gog_bin() -> str:
    return os.environ.get("INBOX_GOG_BIN") or shutil.which("gog") or "/usr/local/bin/gog"


def gog_run(account: str, args: list[str], parse_json: bool = True):
    cmd = [gog_bin(), "--account", account]
    if parse_json:
        cmd.append("--json")
    cmd += args
    result = subprocess.run(
        cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=120, env=os.environ.copy(),
    )
    if result.returncode != 0:
        raise RuntimeError(f"gog {' '.join(args)} failed (exit {result.returncode}): {result.stderr.strip()}")
    if not parse_json:
        return result.stdout
    return json.loads(result.stdout or "{}")


def gog_list(account: str) -> list[dict]:
    query = os.environ.get("INBOX_GOG_QUERY", DEFAULT_GOG_QUERY)
    data = gog_run(account, ["gmail", "messages", "search", query, "--max", str(GOG_MAX)])
    rows = []
    for msg in data.get("messages") or []:
        if not msg.get("id"):
            continue
        rows.append({
            "uid": msg["id"],
            "from": msg.get("from", ""),
            "subject": msg.get("subject", ""),
            "date": msg.get("date", ""),
        })
    return rows


def gog_read(account: str, uid: str) -> dict:
    data = gog_run(account, ["gmail", "get", uid])
    headers = data.get("headers") or {}
    return {
        "uid": uid,
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "body": data.get("body", ""),
    }


def gog_store(account: str, uids: list[str], seen: bool) -> None:
    # Gmail tracks read state with the UNREAD label: REMOVE it to mark read.
    flag = "--remove" if seen else "--add"
    gog_run(account, ["gmail", "batch", "modify", *uids, flag, "UNREAD", "--force"], parse_json=False)


# --------------------------------------------------------------------------- #
# command dispatch
# --------------------------------------------------------------------------- #

def cmd_list(args: argparse.Namespace) -> int:
    account, backend = resolve_backend(args.account)
    rows = gog_list(account) if backend == "gog" else imap_list(account)
    print(json.dumps(rows, indent=2, ensure_ascii=False))
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    account, backend = resolve_backend(args.account)
    out = gog_read(account, args.uid) if backend == "gog" else imap_read(account, args.uid)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_mark_seen(args: argparse.Namespace) -> int:
    account, backend = resolve_backend(args.account)
    store = gog_store if backend == "gog" else imap_store
    store(account, args.uids, seen=True)
    print(json.dumps({"marked_seen": args.uids}))
    return 0


def cmd_mark_unseen(args: argparse.Namespace) -> int:
    account, backend = resolve_backend(args.account)
    store = gog_store if backend == "gog" else imap_store
    store(account, args.uids, seen=False)
    print(json.dumps({"marked_unseen": args.uids}))
    return 0


def main() -> int:
    load_env_files()
    parser = argparse.ArgumentParser(description="Mail helper for phantombot inbox triage")
    parser.add_argument("--account", help="mailbox to operate on (gog if in INBOX_GOG_ACCOUNTS, else imap)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-unread").set_defaults(fn=cmd_list)
    read = sub.add_parser("read")
    read.add_argument("uid")
    read.set_defaults(fn=cmd_read)
    mark_seen = sub.add_parser("mark-seen")
    mark_seen.add_argument("uids", nargs="+")
    mark_seen.set_defaults(fn=cmd_mark_seen)
    mark_unseen = sub.add_parser("mark-unseen")
    mark_unseen.add_argument("uids", nargs="+")
    mark_unseen.set_defaults(fn=cmd_mark_unseen)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"inbox-mail.py: {exc}", file=sys.stderr)
        raise SystemExit(1)
