#!/usr/bin/env bash
# =============================================================================
# github-app-auth installer
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$SCRIPT_DIR/bin"
SYSTEMD_DIR="$SCRIPT_DIR/systemd"
LOCAL_BIN="$HOME/.local/bin"
USER_SYSTEMD="$HOME/.config/systemd/user"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

die() { echo -e "${RED}ERROR:${NC} $*" >&2; exit 1; }
info() { echo -e "${GREEN}→${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }

# --- Check prerequisites ---
info "Checking prerequisites..."

if ! command -v openssl &>/dev/null; then die "openssl is required"; fi
if ! command -v python3 &>/dev/null; then die "python3 is required"; fi
if ! command -v curl &>/dev/null; then die "curl is required"; fi

if [[ ! -f "$HOME/.env" ]]; then
    die "~/.env not found. Please create it with GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY_PATH."
fi

# shellcheck source=/dev/null
set -a; source "$HOME/.env"; set +a

if [[ -z "${GITHUB_APP_ID:-}" ]]; then
    die "GITHUB_APP_ID not set in ~/.env"
fi
if [[ -z "${GITHUB_APP_PRIVATE_KEY_PATH:-}" ]]; then
    die "GITHUB_APP_PRIVATE_KEY_PATH not set in ~/.env"
fi
if [[ ! -f "$GITHUB_APP_PRIVATE_KEY_PATH" ]]; then
    die "Private key not found at $GITHUB_APP_PRIVATE_KEY_PATH"
fi

# --- Symlink binaries ---
info "Installing binaries to $LOCAL_BIN..."
mkdir -p "$LOCAL_BIN"

for bin in "$BIN_DIR"/*; do
    name=$(basename "$bin")
    target="$LOCAL_BIN/$name"
    if [[ -L "$target" || -f "$target" ]]; then
        rm -f "$target"
    fi
    ln -s "$bin" "$target"
    info "  $name → $target"
done

# --- Remove old conflicting timers/services ---
info "Removing old conflicting systemd units..."
OLD_UNITS=(
    "phantombot-refresh-github-env.timer"
    "phantombot-refresh-github-env.service"
    "phantombot-refresh-github-token.timer"
    "phantombot-refresh-github-token.service"
)

for unit in "${OLD_UNITS[@]}"; do
    if systemctl --user list-unit-files "$unit" &>/dev/null; then
        systemctl --user stop "$unit" 2>/dev/null || true
        systemctl --user disable "$unit" 2>/dev/null || true
        rm -f "$USER_SYSTEMD/$unit"
        info "  Removed old unit: $unit"
    fi
done

# --- Install new systemd timer ---
info "Installing systemd timer..."
mkdir -p "$USER_SYSTEMD"

cp "$SYSTEMD_DIR/github-app-auth-refresh.timer" "$USER_SYSTEMD/"
cp "$SYSTEMD_DIR/github-app-auth-refresh.service" "$USER_SYSTEMD/"

systemctl --user daemon-reload
systemctl --user enable github-app-auth-refresh.timer
systemctl --user start github-app-auth-refresh.timer

info "  Timer installed: github-app-auth-refresh.timer"

# --- Configure git ---
info "Configuring git..."
git config --global credential.helper "!$LOCAL_BIN/git-credential-github-app"
info "  git credential.helper set"

# --- Test token generation ---
info "Testing token generation..."
if ! "$LOCAL_BIN/refresh-github-env.sh" >/dev/null 2>&1; then
    die "Token generation failed. Check your App ID, private key, and network."
fi
info "  Token generation OK"

# --- Summary ---
echo ""
echo -e "${GREEN}✓ github-app-auth installed successfully!${NC}"
echo ""
echo "Binaries:      $LOCAL_BIN/"
echo "Systemd timer: github-app-auth-refresh.timer (every 50 min)"
echo "Token file:    ~/.github_env"
echo ""
echo "Next steps:"
echo "  - Ensure your GitHub App is installed on the repos you need"
echo "  - Run 'git push', 'git pull', 'git fetch' — the wrapper handles GitHub repos automatically"
echo ""
echo "To uninstall:"
echo "  $SCRIPT_DIR/uninstall.sh"
