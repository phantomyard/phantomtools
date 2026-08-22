#!/usr/bin/env bash
#
# install.sh — symlink PhantomDocs' CLI into your PATH.
#
# Usage:
#   ./install.sh                       # symlink into ~/.local/bin
#   PREFIX=/usr/local ./install.sh     # symlink into /usr/local/bin (may need sudo)
#
# The repo stays the single source of truth: the symlinks point at bin/pd and
# bin/phantomdocs in this checkout, so editing the repo takes effect on the
# next run — no pip reinstall needed. Re-run install.sh after moving the repo.
#
# Dependencies: python3 with PyYAML and click. If they are missing, install
# them with:
#   python3 -m pip install --user pyyaml click
# or create a self-contained venv in the repo (the wrappers pick it up):
#   python3 -m venv .venv && .venv/bin/pip install .
#
set -euo pipefail

# Portable realpath: GNU readlink -f does not exist on macOS (BSD readlink).
# python3 is a hard dependency of the tool, so use it to resolve paths.
realpath() {
    python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src_pd="$here/bin/pd"
src_phantomdocs="$here/bin/phantomdocs"

bindir="${PREFIX:-$HOME/.local}/bin"
mkdir -p "$bindir"

install_symlink() {
    local src="$1"
    local target="$2"
    if [[ -L "$target" ]]; then
        current="$(realpath "$target" 2>/dev/null || true)"
        src_real="$(realpath "$src" 2>/dev/null || echo "$src")"
        if [[ "$current" == "$src_real" || "$current" == "$src" ]]; then
            rm -f "$target"
        elif [[ -z "$current" ]]; then
            rm -f "$target"  # dangling symlink, safe to replace
        else
            echo "install: refusing to overwrite $target — it links to $current, not this repo. Remove it manually if intended." >&2
            exit 1
        fi
    elif [[ -e "$target" ]]; then
        echo "install: refusing to overwrite $target — it's a regular file, not our symlink. It may hold in-place edits; remove it manually if intended." >&2
        exit 1
    fi
    ln -s "$src" "$target"
    echo "installed: $target -> $src"
}

install_symlink "$src_pd" "$bindir/pd"
install_symlink "$src_phantomdocs" "$bindir/phantomdocs"

case ":$PATH:" in
    *":$bindir:"*) ;;
    *) echo "note: $bindir is not in your PATH — add it:"
       echo "      export PATH=\"$bindir:\$PATH\"" ;;
esac

echo
echo "next steps:"
echo "  - smoke test:  pd --version   (and: pd --help)"
echo "  - the symlinks point into this checkout — don't move or delete the repo"
echo "    without re-running ./install.sh"
