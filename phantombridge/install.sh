#!/usr/bin/env bash
#
# install.sh — symlink phantombridge into your PATH.
#
# Usage:
#   ./install.sh                          # symlink into ~/.local/bin
#   PREFIX=/usr/local ./install.sh        # symlink into /usr/local/bin (may need sudo)
#   curl -fsSL <url> | bash               # standalone: fetches the repo, then installs
#
# Requires: node 18+ and npm in PATH.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/bin/phantombridge"

prefix="${PREFIX:-$HOME/.local}"
bindir="$prefix/bin"

# --- standalone install -------------------------------- #
# When fetched via `curl | bash` there is no checkout: $here is a temp dir
# with only this script. Clone the repo, then re-run from the checkout.
if [[ ! -e "$here/bin/phantombridge" ]]; then
    if ! command -v git >/dev/null 2>&1; then
        echo "install: git required for standalone install" >&2
        exit 1
    fi
    tmp="$(mktemp -d)"
    echo "install: fetching phantomtools repo..."
    git clone --depth 1 https://github.com/phantomyard/phantomtools.git "$tmp/phantomtools" >/dev/null 2>&1
    here="$tmp/phantomtools/phantombridge"
    src="$here/bin/phantombridge"
    trap 'rm -rf "$tmp"' EXIT
fi

if [[ ! -x "$src" ]]; then
    echo "install: $src not found or not executable" >&2
    exit 1
fi

if ! command -v node >/dev/null 2>&1; then
    echo "install: node not found in PATH (required)" >&2
    exit 1
fi

# Make sure the wrapper is executable (a clone may not preserve the bit).
chmod +x "$src" 2>/dev/null || true

# --- install npm deps (the bridge is Node) -------------- #
# The bridge needs its node_modules. Install into the checkout so the symlinked
# launcher (bin/phantombridge) resolves bridge.js + node_modules from it at
# runtime. Skip if already present.
if [[ ! -d "$here/node_modules" ]]; then
    echo "install: installing npm dependencies (npm install)..."
    (cd "$here" && npm install --no-audit --no-fund)
fi

mkdir -p "$bindir"

# Don't blindly `ln -sf`: that silently clobbers a regular file someone may
# have edited in place, losing the change. Reclaim only a symlink that already
# points at our source (or a dangling one); refuse a foreign symlink or a real
# file and point at report-drift so any in-place edit can be folded back in.
target="$bindir/phantombridge"
if [[ -L "$target" ]]; then
    current="$(readlink -f "$target" 2>/dev/null || true)"
    src_real="$(readlink -f "$src" 2>/dev/null || echo "$src")"
    if [[ "$current" == "$src_real" || "$current" == "$src" ]]; then
        rm -f "$target"
    elif [[ -z "$current" ]]; then
        rm -f "$target"  # dangling symlink, safe to replace
    else
        echo "install: refusing to overwrite $target — it links to $current, not this repo. Remove it manually if intended." >&2
        exit 1
    fi
elif [[ -e "$target" ]]; then
    echo "install: refusing to overwrite $target — it's a regular file, not our symlink. It may hold in-place edits; check with: github-app-auth report-drift phantombridge/phantombridge — then remove it manually if intended." >&2
    exit 1
fi
ln -s "$src" "$target"

echo "installed: $target -> $src"

case ":$PATH:" in
    *":$bindir:"*) ;;
    *) echo "note: $bindir is not in your PATH — add it:"
       echo "      export PATH=\"$bindir:\$PATH\"" ;;
esac

# --- config seed (optional) ----------------------------- #
# Create config.json from the example on first install so the bridge can boot
# after the operator fills in XMPP/Nostr credentials. Never overwrite.
# Secrets are REFERENCES (vault:NAME / env:VAR), never values: the bridge
# owns no plaintext secret files. Store each secret once with
#   phantombot vault set bridge-nsec <nsec>   (and bridge-relay-nsec,
#                                              bridge-xmpp-password,
#                                              bridge-admin-token)
if [[ ! -e "$here/config.json" && -e "$here/config.example.json" ]]; then
    cp "$here/config.example.json" "$here/config.json"
    chmod 600 "$here/config.json"
    echo "created $here/config.json (0600) — secrets are vault:/env: references (no secret files)"
fi

echo
echo "next steps:"
echo "  - store each secret once in the phantombot vault:"
echo "      phantombot vault set bridge-nsec <nsec>"
echo "      phantombot vault set bridge-relay-nsec <nsec>"
echo "      phantombot vault set bridge-xmpp-password <password>"
echo "      phantombot vault set bridge-admin-token <token>"
echo "    (or inject them as env vars and use \"env:VAR\" references in config.json)"
echo "  - if you use an org.yaml (norma v1.6), place it next to config.json — the bridge derives agents + DM routing from it"
echo "  - smoke test:  phantombridge --version"
echo "  - run the bridge in the foreground:  phantombridge  (or under systemd / your supervisor)"
echo
echo "see: phantombridge --help"
