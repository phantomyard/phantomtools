#!/usr/bin/env bash
#
# install.sh — symlink bot-inbox into your PATH.
#
# Usage:
#   ./install.sh                 # symlink into ~/.local/bin
#   PREFIX=/usr/local ./install.sh   # symlink into /usr/local/bin (may need sudo)
#
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/bin/bot-inbox"

prefix="${PREFIX:-$HOME/.local}"
bindir="$prefix/bin"

if [[ ! -x "$src" ]]; then
    echo "install: $src not found or not executable" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "install: python3 not found in PATH (required)" >&2
    exit 1
fi

mkdir -p "$bindir"
ln -sf "$src" "$bindir/bot-inbox"

echo "installed: $bindir/bot-inbox -> $src"

case ":$PATH:" in
    *":$bindir:"*) ;;
    *) echo "note: $bindir is not in your PATH — add it:"
       echo "      export PATH=\"$bindir:\$PATH\"" ;;
esac

root="${BOT_INBOX_ROOT:-/mnt/shared-data/bots/inbox}"

# Create the shared inbox root if it doesn't exist yet. The env var stays the
# single source of truth for *where* the inbox lives; install.sh only ensures
# the directory is present so the first send/list doesn't fail. group-writable
# (2775) so multiple bots sharing the host can drop into each other's inboxes.
if [[ -d "$root" ]]; then
    echo "inbox root exists: $root"
else
    if mkdir -p "$root" 2>/dev/null; then
        chmod 2775 "$root" 2>/dev/null || true
        echo "created inbox root: $root"
    else
        echo "note: could not create inbox root: $root" >&2
        echo "      create it manually (may need sudo / different owner):" >&2
        echo "      sudo mkdir -p '$root' && sudo chmod 2775 '$root'" >&2
    fi
fi

echo
echo "next steps:"
echo "  - under phantombot, \$PHANTOMBOT_PERSONA is set per-turn — --from is optional"
echo "    (outside phantombot, export PHANTOMBOT_PERSONA or always pass --from)"
if [[ "$root" == "/mnt/shared-data/bots/inbox" ]]; then
    echo "  - set BOT_INBOX_ROOT in ~/.env if the inbox lives elsewhere"
fi
echo "  - smoke test:  bot-inbox --from \$PHANTOMBOT_PERSONA list"
