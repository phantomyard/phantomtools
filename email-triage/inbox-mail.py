#!/usr/bin/env python3
"""Small IMAP helper for phantombot inbox triage wake-ups.

A thin, dependency-free CLI an agent uses to read and flag its own mailbox
during a triage turn. Pure Python stdlib (imaplib/email) — nothing to pip
install.

Subcommands:
  list-unread              JSON array of unread {uid, from, subject, date}
  read <uid>               full {from, to, subject, date, body} for one message
  mark-seen <uid>...       flag messages read (use after handling/dismissing)
  mark-unseen <uid>...     clear the read flag (undo)

Configuration (environment, typically set via `phantombot env set`):
  INBOX_EMAIL          mailbox address, e.g. you@example.com   (required)
  INBOX_APP_PASSWORD   IMAP app password for that mailbox       (required)
  INBOX_IMAP_HOST      IMAP host, default imap.gmail.com
  INBOX_IMAP_PORT      IMAP SSL port, default 993
"""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import os
import re
import sys
from email.header import decode_header, make_header
from email.message import Message

DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993


def load_env_files() -> None:
    """Populate INBOX_* vars from ~/.env and ~/.config/phantombot/.env.

    phantombot stores credentials in ~/.env (mode 0600); a plain shell does not
    export them, so running this script by hand (as the README's verify step
    does) would otherwise fail with "missing INBOX_EMAIL". Load them here,
    without overwriting anything already in the environment — an explicit env
    var or a phantombot --secret injection always wins.
    """
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
            if not key.startswith("INBOX_") or key in os.environ:
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


def connect() -> imaplib.IMAP4_SSL:
    host = os.environ.get("INBOX_IMAP_HOST", DEFAULT_IMAP_HOST)
    port = int(os.environ.get("INBOX_IMAP_PORT", DEFAULT_IMAP_PORT))
    mailbox = imaplib.IMAP4_SSL(host, port, timeout=30)
    mailbox.login(require_env("INBOX_EMAIL"), require_env("INBOX_APP_PASSWORD"))
    mailbox.select("INBOX")
    return mailbox


def unread(mailbox: imaplib.IMAP4_SSL) -> list[str]:
    typ, data = mailbox.uid("search", None, "UNSEEN")
    if typ != "OK":
        raise RuntimeError(f"UNSEEN search returned {typ}")
    return [uid.decode("ascii", errors="replace") for uid in data[0].split() if uid]


def fetch(mailbox: imaplib.IMAP4_SSL, uid: str) -> Message:
    typ, data = mailbox.uid("fetch", uid, "(BODY.PEEK[])")
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        raise RuntimeError(f"fetch {uid} returned {typ}")
    return email.message_from_bytes(data[0][1])


def cmd_list(args: argparse.Namespace) -> int:
    with connect() as mailbox:
        rows = []
        for uid in unread(mailbox):
            message = fetch(mailbox, uid)
            rows.append(
                {
                    "uid": uid,
                    "from": decode(message.get("From")),
                    "subject": decode(message.get("Subject")),
                    "date": decode(message.get("Date")),
                }
            )
    print(json.dumps(rows, indent=2, ensure_ascii=False))
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    with connect() as mailbox:
        message = fetch(mailbox, args.uid)
    output = {
        "uid": args.uid,
        "from": decode(message.get("From")),
        "to": decode(message.get("To")),
        "subject": decode(message.get("Subject")),
        "date": decode(message.get("Date")),
        "body": text_from_message(message),
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0


def cmd_mark_seen(args: argparse.Namespace) -> int:
    with connect() as mailbox:
        for uid in args.uids:
            typ, _ = mailbox.uid("store", uid, "+FLAGS", "(\\Seen)")
            if typ != "OK":
                raise RuntimeError(f"mark-seen {uid} returned {typ}")
    print(json.dumps({"marked_seen": args.uids}))
    return 0


def cmd_mark_unseen(args: argparse.Namespace) -> int:
    with connect() as mailbox:
        for uid in args.uids:
            typ, _ = mailbox.uid("store", uid, "-FLAGS", "(\\Seen)")
            if typ != "OK":
                raise RuntimeError(f"mark-unseen {uid} returned {typ}")
    print(json.dumps({"marked_unseen": args.uids}))
    return 0


def main() -> int:
    load_env_files()
    parser = argparse.ArgumentParser(description="IMAP helper for phantombot inbox triage")
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
