#!/usr/bin/env bash
# =============================================================================
# github-app-auth uninstaller
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$SCRIPT_DIR/bin"
LOCAL_BIN="$HOME/.local/bin"

info() { echo "→ $*"; }

# --- Remove symlinks ---
info "Removing symlinks from $LOCAL_BIN..."
for bin in "$BIN_DIR"/*; do
    name=$(basename "$bin")
    target="$LOCAL_BIN/$name"
    if [[ -L "$target" && "$(readlink -f "$target")" == "$(readlink -f "$bin")" ]]; then
        rm -f "$target"
        info "  Removed $name"
    fi
done

# --- Stop and remove systemd timer ---
info "Removing systemd timer..."
systemctl --user stop github-app-auth-refresh.timer 2>/dev/null || true
systemctl --user disable github-app-auth-refresh.timer 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/github-app-auth-refresh.timer"
rm -f "$HOME/.config/systemd/user/github-app-auth-refresh.service"
systemctl --user daemon-reload

# --- Unconfigure git ---
info "Removing git credential helper..."
current_helper=$(git config --global credential.helper 2>/dev/null || true)
if [[ "$current_helper" == *"git-credential-github-app"* ]]; then
    git config --global --unset credential.helper 2>/dev/null || true
fi

# --- Clean up token file ---
rm -f "$HOME/.github_env"

echo ""
echo "✓ github-app-auth uninstalled."
echo "  ~/.env entries (GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY_PATH) were NOT removed."
