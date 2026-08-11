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
GUARD_LOG="$TMP_DIR/guard.log"

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
if [[ "\$1" == "--guard-check" ]]; then
    printf 'guard-check %s\n' "\$*" >> "$GUARD_LOG"
    exit \${GUARD_RC:-0}
fi
printf 'git-$sub-as-app %s\n' "\$*" >> "$HELPER_LOG"
echo "AS-APP CALLED (REAL_GIT is \$REAL_GIT)"
EOF
        chmod +x "$BIN_DIR/git-$sub-as-app"
    done
}

reset_logs() { : > "$GIT_ARGS_LOG"; : > "$HELPER_LOG"; : > "$GUARD_LOG"; }

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

# --- 11. the default-branch force guard survives the HTTPS route ------------
# The regression this suite exists for: the guard lives in git-push-as-app, so
# making plain git the primary path let `git push --force origin main` bypass
# it completely. The wrapper must consult the guard BEFORE it pushes.

echo "Testing force-push guard..."
reset_logs
set +e
OUTPUT=$(GUARD_RC=1 MODE=ok "$BIN_DIR/git" push --force origin main 2>&1)
RC=$?
set -e
if [[ ! -s "$GUARD_LOG" ]]; then
    ko "guard was never consulted on a force push"
elif grep -q -- "^push --force origin main" "$GIT_ARGS_LOG"; then
    ko "refused push still reached real git; log: $(cat "$GIT_ARGS_LOG")"
elif [[ "$RC" -ne 1 ]]; then
    ko "expected exit 1 from a refused force push, got $RC"
else
    ok "force push to the default branch is blocked before it leaves"
fi

reset_logs
OUTPUT=$(GUARD_RC=0 MODE=ok "$BIN_DIR/git" push --force origin feat/x 2>&1)
if [[ ! -s "$GUARD_LOG" ]]; then
    ko "guard was not consulted for a feature-branch force push"
elif grep -q -- "push --force origin feat/x" "$GIT_ARGS_LOG"; then
    ok "allowed force push proceeds over HTTPS"
else
    ko "allowed force push never reached real git; log: $(cat "$GIT_ARGS_LOG")"
fi

# An undecidable guard (no token, API unreachable) must fail closed: a force
# push is destructive, so "I could not check" cannot mean "go ahead".
reset_logs
set +e
OUTPUT=$(GUARD_RC=2 MODE=ok "$BIN_DIR/git" push --force origin main 2>&1)
RC=$?
set -e
if grep -q -- "^push --force" "$GIT_ARGS_LOG"; then
    ko "push proceeded although the guard could not decide"
elif [[ "$RC" -eq 2 ]]; then
    ok "an undecidable guard fails closed"
else
    ko "expected exit 2, got $RC; output: $OUTPUT"
fi

# Ordinary pushes must not pay for the guard's API call.
reset_logs
MODE=ok "$BIN_DIR/git" push origin main >/dev/null 2>&1
if [[ -s "$GUARD_LOG" ]]; then
    ko "guard ran on a non-force push: $(cat "$GUARD_LOG")"
else
    ok "non-force push skips the guard entirely"
fi

# --- 12. bash and python agree on what "force" means ------------------------
# is_force_push() in bin/git and is_force_arg() in ghapplib.py are separate
# implementations of the same rule; drift means a spelling of --force that the
# wrapper waves through. Both are run against tests/force_cases.txt.

echo "Testing force detection against the shared case list..."
CASES_FILE="$REPO_ROOT/github-app-auth/tests/force_cases.txt"
mismatch=""
while IFS=$'\t' read -r expect argv; do
    [[ -z "${expect// }" || "$expect" == \#* ]] && continue
    reset_logs
    # shellcheck disable=SC2086
    GUARD_RC=0 MODE=ok "$BIN_DIR/git" push $argv >/dev/null 2>&1 || true
    consulted="plain"
    [[ -s "$GUARD_LOG" ]] && consulted="force"
    if [[ "$consulted" != "$expect" ]]; then
        mismatch+="  '$argv' → wrapper says $consulted, expected $expect"$'\n'
    fi
done < "$CASES_FILE"
if [[ -z "$mismatch" ]]; then
    ok "bash force detection matches the shared case list"
else
    ko "force detection drifted:"$'\n'"$mismatch"
fi

# --- summary ----------------------------------------------------------------

echo
echo "$pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
