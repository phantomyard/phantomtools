#!/usr/bin/env bash
#
# install.sh — symlink PhantomForge's CLI into your PATH.
#
# Two ways to run it:
#
#   1. From inside a checkout (the repo stays the single source of truth):
#        ./install.sh
#
#   2. Standalone, piped straight from the repo (the one-line install):
#        curl -fsSL https://raw.githubusercontent.com/phantomyard/phantomtools/main/phantomforge/install.sh | bash
#      When run this way the script is not inside a working tree, so it
#      clones the repo first and symlinks into that clone.
#
# Usage:
#   ./install.sh                       # symlink into ~/.local/bin
#   PREFIX=/usr/local ./install.sh     # symlink into /usr/local/bin (may need sudo)
#
# The symlinks point at bin/pf and bin/phantomforge in the repo, so editing
# the repo takes effect on the next run — no pip reinstall needed. Re-run
# install.sh after moving the repo.
#
# Environment (all optional):
#   PREFIX                 install prefix (default $HOME/.local)
#   PHANTOMFORGE_REPO_URL  git URL to clone in standalone mode
#                          (default https://github.com/phantomyard/phantomtools.git)
#   PHANTOMFORGE_REPO_DIR  where to keep the clone in standalone mode
#                          (default $HOME/.local/share/phantomforge)
#
# Dependencies: python3 with PyYAML, Jinja2 and click. If they are missing,
# install them with:
#   python3 -m pip install --user pyyaml jinja2 click
# or create a self-contained venv in the repo (the wrappers pick it up):
#   python3 -m venv .venv && .venv/bin/pip install .
#
set -euo pipefail

REPO_URL="${PHANTOMFORGE_REPO_URL:-https://github.com/phantomyard/phantomtools.git}"
REPO_DIR="${PHANTOMFORGE_REPO_DIR:-$HOME/.local/share/phantomforge}"

# Where is this script? When piped via `curl | bash`, BASH_SOURCE is not
# defined, so `script_dir` ends up empty and we fall through to the
# standalone clone below.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd || true)"

if [[ -z "$script_dir" || ! -x "$script_dir/bin/pf" ]]; then
    # Not running from a checkout — clone the repo so the symlinks have a
    # single source of truth to point at. Safe to run standalone.
    if ! command -v git >/dev/null 2>&1; then
        echo "install: git is required to fetch the repo (not found in PATH)" >&2
        exit 1
    fi
    echo "install: not a checkout — fetching $REPO_URL"
    if [[ -d "$REPO_DIR/.git" ]]; then
        echo "install: updating existing clone at $REPO_DIR"
        git -C "$REPO_DIR" pull --ff-only >/dev/null
    else
        mkdir -p "$(dirname "$REPO_DIR")"
        git clone --depth 1 "$REPO_URL" "$REPO_DIR"
    fi
    here="$REPO_DIR/phantomforge"
else
    here="$script_dir"
fi

# Portable realpath: GNU readlink -f does not exist on macOS (BSD readlink).
# python3 is a hard dependency of the tool, so use it to resolve paths.
realpath() {
    python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

src_pf="$here/bin/pf"
src_phantomforge="$here/bin/phantomforge"

prefix="${PREFIX:-$HOME/.local}"
bindir="$prefix/bin"

if [[ ! -x "$src_pf" ]]; then
    echo "install: $src_pf not found or not executable" >&2
    exit 1
fi
if [[ ! -x "$src_phantomforge" ]]; then
    echo "install: $src_phantomforge not found or not executable" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "install: python3 not found in PATH (required)" >&2
    exit 1
fi

# Check the runtime dependencies up front; report what's missing without
# blocking the symlink install (the wrappers fail with a clear hint otherwise).
if ! python3 -c "import yaml, jinja2, click" >/dev/null 2>&1; then
    echo "install: warning: PyYAML, Jinja2 and/or click not importable by python3." >&2
    echo "         install them with:  python3 -m pip install --user pyyaml jinja2 click" >&2
    echo "         or use a repo venv: python3 -m venv \"$here/.venv\" && \"$here/.venv/bin/pip\" install ." >&2
fi

mkdir -p "$bindir"

# Don't blindly `ln -sf`: that silently clobbers a regular file someone may
# have edited in place, losing the change. Reclaim only a symlink that already
# points at our source (or a dangling one); refuse a foreign symlink or a real
# file so any in-place edit is preserved.
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

install_symlink "$src_pf" "$bindir/pf"
install_symlink "$src_phantomforge" "$bindir/phantomforge"

case ":$PATH:" in
    *":$bindir:"*) ;;
    *) echo "note: $bindir is not in your PATH — add it:"
       echo "      export PATH=\"$bindir:\$PATH\"" ;;
esac

echo
echo "next steps:"
echo "  - smoke test:  pf --version   (and: pf --help)"
echo "  - the symlinks point into $here — don't move or delete the repo"
echo "    without re-running ./install.sh"
