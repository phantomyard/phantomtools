#!/usr/bin/env bash
#
# install.sh — symlink phantombridge's CLI into your PATH.
#
# Two ways to run it:
#
#   1. From inside a checkout (the repo stays the single source of truth):
#        ./install.sh
#
#   2. Standalone, piped straight from the repo (the one-line install):
#        curl -fsSL https://raw.githubusercontent.com/phantomyard/phantomtools/main/phantombridge/install.sh | bash
#      When run this way the script is not inside a working tree, so it
#      clones the repo first and symlinks into that clone.
#
# Usage:
#   ./install.sh                       # symlink into ~/.local/bin
#   PREFIX=/usr/local ./install.sh     # symlink into /usr/local/bin (may need sudo)
#
# The symlink points at bin/phantombridge in the repo, so editing the repo
# takes effect on the next run — no npm reinstall needed. Re-run install.sh
# after moving the repo.
#
# Environment (all optional):
#   PREFIX                  install prefix (default $HOME/.local)
#   PHANTOMBRIDGE_REPO_URL  git URL to clone in standalone mode
#                           (default https://github.com/phantomyard/phantomtools.git)
#   PHANTOMBRIDGE_REPO_DIR  where to keep the clone in standalone mode
#                           (default $HOME/.local/share/phantombridge)
#
# Dependencies: node >= 18 with npm. After installing, run:
#   npm install            # inside the checkout (or the standalone clone)
# and copy config.example.json to config.json with your real values.
#
set -euo pipefail

REPO_URL="${PHANTOMBRIDGE_REPO_URL:-https://github.com/phantomyard/phantomtools.git}"
REPO_DIR="${PHANTOMBRIDGE_REPO_DIR:-$HOME/.local/share/phantombridge}"
PREFIX="${PREFIX:-$HOME/.local}"
BIN_DIR="$PREFIX/bin"

# Detect whether we are inside a checkout (i.e. ./bridge.js exists next to us).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/bridge.js" ] && [ -f "$SCRIPT_DIR/package.json" ]; then
  REPO_DIR="$SCRIPT_DIR"
  STANDALONE=0
  echo "Using local checkout: $REPO_DIR"
else
  STANDALONE=1
  echo "Not inside a checkout; cloning $REPO_URL ..."
fi

if [ "$STANDALONE" = "1" ]; then
  if [ ! -f "$REPO_DIR/phantombridge/package.json" ]; then
    mkdir -p "$(dirname "$REPO_DIR")"
    git clone --depth 1 "$REPO_URL" "$REPO_DIR"
  else
    echo "Checkout already present at $REPO_DIR (not re-cloning)."
  fi
  REPO_DIR="$REPO_DIR/phantombridge"
fi

# Node sanity check.
if ! command -v node >/dev/null 2>&1; then
  echo "error: node not found in PATH. Install Node.js >= 18 first." >&2
  exit 1
fi

mkdir -p "$BIN_DIR"
ln -sfn "$REPO_DIR/bin/phantombridge" "$BIN_DIR/phantombridge"
echo "Installed: $BIN_DIR/phantombridge -> $REPO_DIR/bin/phantombridge"
echo
echo "Next steps:"
echo "  1. cd $REPO_DIR && npm install"
echo "  2. cp config.example.json config.json and fill in your values"
echo "     (xmpp password, nostr nsec, agent pubkeys, permissions)"
echo "  3. Run: phantombridge"
