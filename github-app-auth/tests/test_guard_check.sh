#!/bin/bash
# Tests for `git-push-as-app --guard-check` — the check-only mode the git
# wrapper calls before letting a force push out over HTTPS.
#
# The exit code is the entire contract (0 allowed / 1 refused / 2 undecidable),
# so it is what these tests pin. Everything here is offline: the cases that
# would need the GitHub API are the ones that must short-circuit before it.
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PUSH_AS_APP="$REPO_ROOT/github-app-auth/bin/git-push-as-app"

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

export HOME="$TMP_DIR/home"          # no ~/.github_env, no App config
mkdir -p "$HOME"
export GITHUB_APP_REAL_GIT=/usr/bin/git
unset GITHUB_TOKEN GITHUB_APP_ALLOW_FORCE_DEFAULT

pass=0
fail=0
ok() { echo "  SUCCESS: $1"; pass=$((pass + 1)); }
ko() { echo "  FAILURE: $1"; fail=$((fail + 1)); }

make_repo() {
    local dir="$1" url="$2"
    mkdir -p "$dir"
    /usr/bin/git -C "$dir" init -q
    /usr/bin/git -C "$dir" remote add origin "$url"
    echo "$dir"
}

GH_REPO=$(make_repo "$TMP_DIR/gh" "https://github.com/owner/repo.git")
OTHER_REPO=$(make_repo "$TMP_DIR/other" "https://gitlab.com/owner/repo.git")

run_guard() {
    local dir="$1"; shift
    (cd "$dir" && "$PUSH_AS_APP" --guard-check "$@" 2>&1)
}
guard_rc() {
    local dir="$1"; shift
    set +e
    (cd "$dir" && "$PUSH_AS_APP" --guard-check "$@" >/dev/null 2>&1)
    local rc=$?
    set -e
    echo "$rc"
}

# --- 1. no token: cannot check, so refuse -----------------------------------
# Fail-closed matters more here than anywhere else — the alternative is a
# silent history rewrite because a token happened to be missing.

echo "Testing undecidable guard..."
RC=$(guard_rc "$GH_REPO" --force origin main)
OUTPUT=$(run_guard "$GH_REPO" --force origin main || true)
if [[ "$RC" -eq 2 ]]; then
    ok "no token → exit 2 (undecidable, caller aborts)"
else
    ko "expected exit 2, got $RC; output: $OUTPUT"
fi
if [[ "$OUTPUT" == *"GITHUB_APP_ALLOW_FORCE_DEFAULT=1"* ]]; then
    ok "refusal names the override"
else
    ko "no override hint in: $OUTPUT"
fi

# --- 2. the override short-circuits everything ------------------------------
# Including the API call, so it still works when GitHub is unreachable.

echo "Testing override..."
RC=$(cd "$GH_REPO" && GITHUB_APP_ALLOW_FORCE_DEFAULT=1 bash -c \
        '"$0" --guard-check --force origin main >/dev/null 2>&1; echo $?' "$PUSH_AS_APP")
if [[ "$RC" -eq 0 ]]; then
    ok "GITHUB_APP_ALLOW_FORCE_DEFAULT=1 allows the push without any lookup"
else
    ko "expected exit 0 with the override, got $RC"
fi

# --- 3. not a GitHub remote: not ours to guard ------------------------------

echo "Testing non-GitHub remote..."
RC=$(guard_rc "$OTHER_REPO" --force origin main)
if [[ "$RC" -eq 0 ]]; then
    ok "non-GitHub remote passes through"
else
    ko "expected exit 0 for a gitlab remote, got $RC"
fi

# --- 4. unknown remote: let git report it -----------------------------------

echo "Testing unknown remote..."
RC=$(guard_rc "$GH_REPO" --force upstream main)
if [[ "$RC" -eq 0 ]]; then
    ok "unknown remote passes through (git will complain in its own words)"
else
    ko "expected exit 0 for an unknown remote, got $RC"
fi

# --- 5. check-only really is check-only -------------------------------------
# A guard check must never create a ref, a commit, or an object.

echo "Testing that nothing is written..."
BEFORE=$(/usr/bin/git -C "$GH_REPO" rev-list --all --count 2>/dev/null || echo 0)
guard_rc "$GH_REPO" --force origin main >/dev/null
AFTER=$(/usr/bin/git -C "$GH_REPO" rev-list --all --count 2>/dev/null || echo 0)
if [[ "$BEFORE" == "$AFTER" ]]; then
    ok "guard check left the repository untouched"
else
    ko "guard check changed the repo ($BEFORE → $AFTER commits)"
fi

echo
echo "$pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
