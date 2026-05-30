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

# Credentials are validated separately — install proceeds without them so
# binaries + timer can be put in place before ~/.env is filled in.
CREDS_OK=1
CREDS_REASON=""

check_credentials() {
    if [[ ! -f "$HOME/.env" ]]; then
        CREDS_OK=0
        CREDS_REASON="~/.env not found"
        return
    fi
    # shellcheck source=/dev/null
    set -a; source "$HOME/.env"; set +a
    if [[ -z "${GITHUB_APP_ID:-}" ]]; then
        CREDS_OK=0
        CREDS_REASON="GITHUB_APP_ID not set in ~/.env"
        return
    fi
    if [[ -z "${GITHUB_APP_PRIVATE_KEY_PATH:-}" ]]; then
        CREDS_OK=0
        CREDS_REASON="GITHUB_APP_PRIVATE_KEY_PATH not set in ~/.env"
        return
    fi
    if [[ ! -f "$GITHUB_APP_PRIVATE_KEY_PATH" ]]; then
        CREDS_OK=0
        CREDS_REASON="Private key not found at $GITHUB_APP_PRIVATE_KEY_PATH"
        return
    fi
}

check_credentials
if [[ $CREDS_OK -eq 0 ]]; then
    warn "Credentials incomplete: $CREDS_REASON"
    warn "Continuing with binary + timer install; token test will be skipped."
fi

# --- Symlink binaries ---
info "Installing binaries to $LOCAL_BIN..."
mkdir -p "$LOCAL_BIN"

for bin in "$BIN_DIR"/*; do
    name=$(basename "$bin")
    target="$LOCAL_BIN/$name"
    if [[ -L "$target" ]]; then
        # Only reclaim a symlink that already points into this repo's bin dir.
        # A symlink to somewhere else is someone else's tool — refuse to touch it.
        current="$(readlink -f "$target" 2>/dev/null || true)"
        if [[ "$current" == "$(readlink -f "$bin")" || "$current" == "$bin" ]]; then
            rm -f "$target"
        elif [[ -z "$current" ]]; then
            rm -f "$target"  # dangling symlink, safe to replace
        else
            die "refusing to overwrite $target — it links to $current, not this repo. Remove it manually if intended."
        fi
    elif [[ -e "$target" ]]; then
        # A real file (not a symlink) with our name — don't silently clobber it.
        die "refusing to overwrite $target — it's a regular file, not one of our symlinks. Remove it manually if intended."
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

if [[ $CREDS_OK -eq 1 ]]; then
    systemctl --user start github-app-auth-refresh.timer
    info "  Timer installed and started: github-app-auth-refresh.timer"
else
    info "  Timer installed and enabled (not started — fill ~/.env first, then:"
    info "    systemctl --user start github-app-auth-refresh.timer)"
fi

# --- Configure git ---
info "Configuring git..."

# Migration: earlier versions of this installer set credential.helper at the
# global scope, which blew away platform helpers (osxkeychain, manager-core,
# store, …) for ALL remotes. Remove only the entry that pointed at our helper.
if git config --global --get-all credential.helper 2>/dev/null \
        | grep -q "git-credential-github-app"; then
    git config --global --unset-all credential.helper '.*git-credential-github-app.*' \
        2>/dev/null || true
    info "  removed legacy global credential.helper entry"
fi

# URL-scoped: only intercept credentials for github.com. Other hosts keep
# whatever helper the user already had configured.
git config --global --replace-all \
    credential.https://github.com.helper \
    "!$LOCAL_BIN/git-credential-github-app"
info "  git credential.helper set (scoped to https://github.com)"

# --- Test token generation ---
if [[ $CREDS_OK -eq 1 ]]; then
    info "Testing token generation..."
    if ! "$LOCAL_BIN/refresh-github-env.sh" >/dev/null 2>&1; then
        die "Token generation failed. Check your App ID, private key, and network."
    fi
    info "  Token generation OK"
else
    info "Skipping token generation test (credentials incomplete)."
fi

# --- Phantombot memory capture (optional) ---
# If this host runs phantombot, leave a breadcrumb in its memory so the agent
# discovers the new capability on its next turn. Silent no-op if phantombot is
# not installed — this tool stays usable on hosts without it.
if command -v phantombot &>/dev/null; then
    info "Capturing capability hint to phantombot memory..."
    phantombot memory capture \
      "github-app-auth installed: I can read/write any repo my GitHub App is installed on. Run \`list-repos-as-app\` to discover which repos are accessible (use \`--clone-urls\` for HTTPS URLs, \`--json\` for scripting). Standard \`git clone/fetch/pull/push\` work transparently for those repos via the wrapper in ~/.local/bin/git. To open a pull request, use \`create-pr-as-app \"<title>\"\` instead of gh (needs the App's 'Pull requests: write' permission)." \
      --tag lesson >/dev/null 2>&1 \
      && info "  hint captured (will surface on next agent turn)" \
      || warn "  phantombot memory capture failed (non-fatal)"
fi

# --- Summary ---
echo ""
echo -e "${GREEN}✓ github-app-auth installed successfully!${NC}"
echo ""
echo "Binaries:      $LOCAL_BIN/"
echo "Systemd timer: github-app-auth-refresh.timer (every 50 min)"
echo "Token file:    ~/.github_env"
echo ""
if [[ $CREDS_OK -eq 1 ]]; then
    echo "Next steps:"
    echo "  - Ensure your GitHub App is installed on the repos you need"
    echo "  - Run 'git push', 'git pull', 'git fetch' — the wrapper handles GitHub repos automatically"
else
    echo -e "${YELLOW}Next steps (credentials still needed):${NC}"
    echo "  1. Add to ~/.env:"
    echo "       GITHUB_APP_ID=<your-app-id>"
    echo "       GITHUB_APP_PRIVATE_KEY_PATH=<path-to-private-key.pem>"
    echo "  2. Verify token generation:"
    echo "       $LOCAL_BIN/refresh-github-env.sh"
    echo "  3. Start the refresh timer:"
    echo "       systemctl --user start github-app-auth-refresh.timer"
fi
echo ""
echo "To uninstall:"
echo "  $SCRIPT_DIR/uninstall.sh"
