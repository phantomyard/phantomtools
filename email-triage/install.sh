#!/usr/bin/env bash
#
# email-triage installer for a phantombot persona.
#
# Copies the poller + mail helper into your local bin, drops an editable
# wake-prompt next to them, creates the state dir, and registers the recurring
# command-backed phantombot task that drives the whole thing.
#
# Idempotent: safe to re-run after editing. It will not create a second task if
# one with the same label already exists.
#
# Usage:
#   ./install.sh                 # install to ~/.local/bin, poll every 15m
#   INSTALL_DIR=~/bin ./install.sh
#   POLL_INTERVAL=10m ./install.sh
#
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/bin}"
POLL_INTERVAL="${POLL_INTERVAL:-15m}"

PHANTOMBOT="$(command -v phantombot || echo "$HOME/.local/bin/phantombot")"
if [[ ! -x "$PHANTOMBOT" ]]; then
  echo "error: phantombot not found (looked for it on PATH and at ~/.local/bin/phantombot)." >&2
  echo "email-triage is a phantombot add-on and needs it installed first." >&2
  exit 1
fi

echo "==> Installing scripts to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
install -m 0755 "$SRC_DIR/inbox-poll.py" "$INSTALL_DIR/inbox-poll.py"
install -m 0755 "$SRC_DIR/inbox-mail.py" "$INSTALL_DIR/inbox-mail.py"

WAKE_PROMPT="$INSTALL_DIR/wake-prompt.md"
if [[ -e "$WAKE_PROMPT" ]]; then
  echo "==> Keeping your existing $WAKE_PROMPT (edit it to tailor triage behaviour)"
else
  echo "==> Creating editable $WAKE_PROMPT from the example template"
  cp "$SRC_DIR/wake-prompt.example.md" "$WAKE_PROMPT"
fi

echo "==> Creating state dir"
mkdir -p "$HOME/.local/state/phantombot-inbox-poll"

# Credentials the command-backed task must be able to see at fire time. The task
# runs with a minimal env, so each must be exposed explicitly via --secret.
SECRETS=(INBOX_EMAIL INBOX_APP_PASSWORD INBOX_IMAP_HOST INBOX_IMAP_PORT INBOX_TASK_LABEL INBOX_WAKE_PROMPT)

if [[ -z "${INBOX_EMAIL:-}" ]] && ! "$PHANTOMBOT" env list 2>/dev/null | grep -q '^INBOX_EMAIL$'; then
  echo
  echo "!! INBOX_EMAIL is not set yet. Before the poller can work, run:"
  echo "     phantombot env set INBOX_EMAIL        \"you@example.com\""
  echo "     phantombot env set INBOX_APP_PASSWORD \"your-imap-app-password\""
  echo "   (see .env.example for the optional vars)."
  echo
fi

LABEL="${INBOX_TASK_LABEL:-Process inbox mail}"
if "$PHANTOMBOT" task list 2>/dev/null | grep -qF "$LABEL"; then
  echo "==> A task labelled \"$LABEL\" already exists — not creating a duplicate."
  echo "    (cancel it with \`phantombot task cancel <id>\` first if you want to re-register.)"
else
  echo "==> Registering recurring poll task (every $POLL_INTERVAL)"
  SECRET_FLAGS=()
  for s in "${SECRETS[@]}"; do SECRET_FLAGS+=(--secret "$s"); done
  "$PHANTOMBOT" task add \
    "Poll my inbox for new mail and wake a triage turn when there is any. Audit context only; the real work runs via --command." \
    "$LABEL" \
    --every "$POLL_INTERVAL" \
    --command "$INSTALL_DIR/inbox-poll.py" \
    "${SECRET_FLAGS[@]}"
fi

echo
echo "Done. Quick checks:"
echo "  $INSTALL_DIR/inbox-mail.py list-unread     # verify IMAP creds work"
echo "  phantombot task list                       # confirm the poll task is scheduled"
