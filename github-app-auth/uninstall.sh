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
# URL-scoped helper (current install layout): unset only if it still points
# at our binary — never touch unrelated helpers.
scoped_helper=$(git config --global credential.https://github.com.helper 2>/dev/null || true)
if [[ "$scoped_helper" == *"git-credential-github-app"* ]]; then
    git config --global --unset credential.https://github.com.helper 2>/dev/null || true
fi
# Legacy global helper (older installs wrote here): remove just the entry that
# pointed at our binary so we don't strip the user's osxkeychain/manager-core/…
if git config --global --get-all credential.helper 2>/dev/null \
        | grep -q "git-credential-github-app"; then
    git config --global --unset-all credential.helper '.*git-credential-github-app.*' \
        2>/dev/null || true
fi

# --- Clean up token file ---
rm -f "$HOME/.github_env"

echo ""
echo "✓ github-app-auth uninstalled."
echo "  ~/.env entries (GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY_PATH) were NOT removed."
