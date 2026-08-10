#!/bin/bash
# Tests for bin/git — the PATH wrapper.
#
# Focus: the precedence order between real git over HTTPS and the API-based
# -as-app fallback, and the guarantee that no ambient credential helper can
# answer for github.com.
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WRAPPER_SRC="$REPO_ROOT/github-app-auth/bin/git"

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

BIN_DIR="$TMP_DIR/bin"
REAL_GIT_DIR="$TMP_DIR/real-git"
mkdir -p "$BIN_DIR" "$REAL_GIT_DIR"

FAKE_GIT="$REAL_GIT_DIR/git"
GIT_ARGS_LOG="$TMP_DIR/git-args.log"
HELPER_LOG="$TMP_DIR/helper.log"

cp "$WRAPPER_SRC" "$BIN_DIR/git"
chmod +x "$BIN_DIR/git"

export PATH="$BIN_DIR:$REAL_GIT_DIR:$PATH"
export GITHUB_APP_REAL_GIT="$FAKE_GIT"
# The wrapper also probes ~/.github_env; keep tests hermetic.
export HOME="$TMP_DIR/home"
mkdir -p "$HOME"

pass=0
fail=0

ok() { echo "  SUCCESS: $1"; pass=$((pass + 1)); }
ko() { echo "  FAILURE: $1"; fail=$((fail + 1)); }

# --- fixtures ---------------------------------------------------------------

# A fake "real git". Answers `remote get-url` with a GitHub HTTPS URL, logs
# every invocation, and behaves for fetch/pull/push according to $MODE:
#   ok       — succeed
#   auth     — fail with an authentication error
#   rejected — fail with a non-fast-forward (NOT an auth problem)
write_fake_git() {
    cat > "$FAKE_GIT" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$GIT_ARGS_LOG"
for arg in "$@"; do
    if [[ "$arg" == "get-url" ]]; then
        echo "https://github.com/owner/repo.git"
        exit 0
    fi
done
case "${MODE:-ok}" in
    auth)
        echo "remote: Invalid username or password." >&2
        echo "fatal: Authentication failed for 'https://github.com/owner/repo.git/'" >&2
        exit 128
        ;;
    rejected)
        echo "REAL GIT STDOUT"
        echo " ! [rejected]        main -> main (non-fast-forward)" >&2
        exit 1
        ;;
    *)
        echo "REAL GIT CALLED WITH: $*"
        exit 0
        ;;
esac
EOF
    chmod +x "$FAKE_GIT"
}

write_as_app_helpers() {
    for sub in fetch pull push; do
        cat > "$BIN_DIR/git-$sub-as-app" <<EOF
#!/bin/bash
printf 'git-$sub-as-app %s\n' "\$*" >> "$HELPER_LOG"
echo "AS-APP CALLED (REAL_GIT is \$REAL_GIT)"
EOF
        chmod +x "$BIN_DIR/git-$sub-as-app"
    done
}

reset_logs() { : > "$GIT_ARGS_LOG"; : > "$HELPER_LOG"; }

write_fake_git
write_as_app_helpers
export GIT_ARGS_LOG

# --- 1. non-intercepted subcommands fall through ----------------------------

echo "Testing wrapper fallthrough..."
reset_logs
OUTPUT=$("$BIN_DIR/git" status)
if [[ "$OUTPUT" == *"REAL GIT CALLED WITH: status"* ]]; then
    ok "wrapper fell through to real git"
else
    ko "wrapper output: $OUTPUT"
fi

# --- 2. no ghs_ token: never touch the App path -----------------------------

echo "Testing no-token passthrough..."
reset_logs
OUTPUT=$(env -u GITHUB_TOKEN "$BIN_DIR/git" push origin main 2>&1)
if [[ -s "$HELPER_LOG" ]]; then
    ko "as-app helper ran without a ghs_ token"
elif [[ "$OUTPUT" == *"REAL GIT CALLED WITH: push origin main"* ]]; then
    ok "no ghs_ token — plain real git, no credential pinning"
else
    ko "wrapper output: $OUTPUT"
fi

export GITHUB_TOKEN="ghs_dummy_token"

# --- 3. real git first: success means the API route is never used -----------

echo "Testing real-git-first on push..."
reset_logs
MODE=ok OUTPUT=$("$BIN_DIR/git" push origin main 2>&1)
if [[ -s "$HELPER_LOG" ]]; then
    ko "as-app helper ran even though real git succeeded: $(cat "$HELPER_LOG")"
elif grep -q -- "push origin main" "$GIT_ARGS_LOG"; then
    ok "real git handled the push; API route untouched"
else
    ko "real git was not called with the push; log: $(cat "$GIT_ARGS_LOG")"
fi

# --- 4. the ambient credential helper cannot answer -------------------------
# This is the regression that matters: without the reset, an osxkeychain /
# manager-core entry holding a user PAT would authenticate the push as the
# wrong identity, succeed, and the App fallback would never fire.

echo "Testing credential pinning..."
if grep -q -- "-c credential.helper= " "$GIT_ARGS_LOG"; then
    ok "inherited credential helpers are cleared"
else
    ko "credential.helper was not reset; log: $(cat "$GIT_ARGS_LOG")"
fi
if grep -q -- "credential.https://github.com.helper=\!.*git-credential-github-app" "$GIT_ARGS_LOG"; then
    ok "App credential helper is pinned for github.com"
else
    ko "App helper not pinned; log: $(cat "$GIT_ARGS_LOG")"
fi

# --- 5. auth failure falls back to the API route ----------------------------

echo "Testing fallback on auth failure..."
reset_logs
OUTPUT=$(MODE=auth "$BIN_DIR/git" push origin main 2>&1)
if grep -q "git-push-as-app origin main" "$HELPER_LOG"; then
    ok "auth failure fell back to git-push-as-app"
else
    ko "no fallback; output: $OUTPUT"
fi
if [[ "$OUTPUT" == *"re-creates commits"* ]]; then
    ok "fallback warns that the API route re-creates commits"
else
    ko "no re-created-commits warning; output: $OUTPUT"
fi

# --- 6. non-auth failure does NOT fall back ---------------------------------

echo "Testing no fallback on non-auth failure..."
reset_logs
set +e
OUTPUT=$(MODE=rejected "$BIN_DIR/git" push origin main 2>&1)
RC=$?
set -e
if [[ -s "$HELPER_LOG" ]]; then
    ko "as-app helper ran on a non-fast-forward rejection"
elif [[ "$RC" -eq 1 ]]; then
    ok "non-auth failure propagated real git's exit code"
else
    ko "expected exit 1, got $RC; output: $OUTPUT"
fi
if [[ "$OUTPUT" == *"non-fast-forward"* && "$OUTPUT" == *"REAL GIT STDOUT"* ]]; then
    ok "both stdout and stderr from real git reach the user"
else
    ko "real git output was swallowed: $OUTPUT"
fi

# --- 7. GITHUB_APP_FORCE_API=1 skips real git -------------------------------

echo "Testing GITHUB_APP_FORCE_API..."
reset_logs
OUTPUT=$(GITHUB_APP_FORCE_API=1 MODE=ok "$BIN_DIR/git" push origin main 2>&1)
if ! grep -q "git-push-as-app origin main" "$HELPER_LOG"; then
    ko "FORCE_API did not reach the API route; output: $OUTPUT"
elif grep -q -- "^push origin main" "$GIT_ARGS_LOG"; then
    ko "FORCE_API still attempted a real-git push; log: $(cat "$GIT_ARGS_LOG")"
else
    ok "FORCE_API=1 goes straight to the API route"
fi

# --- 8. GITHUB_APP_NO_API=1 refuses to fall back ----------------------------

echo "Testing GITHUB_APP_NO_API..."
reset_logs
set +e
OUTPUT=$(GITHUB_APP_NO_API=1 MODE=auth "$BIN_DIR/git" push origin main 2>&1)
RC=$?
set -e
if [[ -s "$HELPER_LOG" ]]; then
    ko "NO_API still fell back to the API route"
elif [[ "$RC" -eq 128 ]]; then
    ok "NO_API=1 surfaces the auth failure instead of falling back"
else
    ko "expected exit 128, got $RC; output: $OUTPUT"
fi

# --- 9. fetch and pull follow the same order --------------------------------

for sub in fetch pull; do
    echo "Testing $sub precedence..."
    reset_logs
    MODE=ok "$BIN_DIR/git" "$sub" origin >/dev/null 2>&1
    if [[ -s "$HELPER_LOG" ]]; then
        ko "$sub used the API route while real git worked"
    elif grep -q -- "$sub origin" "$GIT_ARGS_LOG"; then
        ok "$sub tried real git first"
    else
        ko "$sub did not reach real git; log: $(cat "$GIT_ARGS_LOG")"
    fi

    reset_logs
    MODE=auth "$BIN_DIR/git" "$sub" origin >/dev/null 2>&1 || true
    if grep -q "git-$sub-as-app origin" "$HELPER_LOG"; then
        ok "$sub fell back on auth failure"
    else
        ko "$sub did not fall back on auth failure"
    fi
done

# --- 10. REAL_GIT is exported to the -as-app children -----------------------

echo "Testing REAL_GIT export..."
reset_logs
OUTPUT=$(GITHUB_APP_FORCE_API=1 "$BIN_DIR/git" push origin main 2>&1)
if [[ "$OUTPUT" == *"REAL_GIT is $FAKE_GIT"* ]]; then
    ok "REAL_GIT was exported to the child"
else
    ko "wrapper output: $OUTPUT"
fi

# --- summary ----------------------------------------------------------------

echo
echo "$pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
