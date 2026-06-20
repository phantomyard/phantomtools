#!/bin/bash
set -e

# Setup a fake environment
TMP_DIR=$(mktemp -d)
trap "rm -rf $TMP_DIR" EXIT

REAL_GIT_DIR="$TMP_DIR/real-git"
mkdir -p "$REAL_GIT_DIR"

# A fake "real" git that echoes exactly the args it was exec'd with, so we can
# assert on the command the wrapper built (clone URL, -c extraheader, etc.).
cat > "$REAL_GIT_DIR/git" <<'EOF'
#!/bin/bash
echo "REAL GIT: $*"
EOF
chmod +x "$REAL_GIT_DIR/git"

# Run the wrapper straight from the repo so it imports its sibling ghapplib.py.
WRAPPER="github-app-auth/bin/git-clone-as-app"

export GITHUB_APP_REAL_GIT="$REAL_GIT_DIR/git"
export HOME="$TMP_DIR"          # wrapper reads our fake ~/.github_env
unset GITHUB_TOKEN

pass() { echo "SUCCESS: $1"; }
fail() { echo "FAILURE: $1"; echo "  output: $2"; exit 1; }

write_token() {
    echo 'export GITHUB_TOKEN="ghs_dummy_install_token"' > "$TMP_DIR/.github_env"
    chmod 600 "$TMP_DIR/.github_env"   # match the 0600 refresh-github-env.sh writes
}

# 1. No token anywhere → clean error, exit 1, no git call.
rm -f "$TMP_DIR/.github_env"
set +e
OUT=$(python3 "$WRAPPER" owner/repo 2>&1); RC=$?
set -e
[[ $RC -eq 1 ]] || fail "no-token must exit 1" "rc=$RC $OUT"
[[ "$OUT" == *"no GitHub token"* ]] || fail "no-token error message" "$OUT"
[[ "$OUT" != *"REAL GIT:"* ]] || fail "no-token must not call git" "$OUT"
pass "no token → clean error, no clone"

# 2. owner/repo shorthand → expanded to a clean HTTPS URL with auth header.
write_token
OUT=$(python3 "$WRAPPER" owner/repo 2>&1)
[[ "$OUT" == *"REAL GIT:"* ]] || fail "shorthand should call git" "$OUT"
[[ "$OUT" == *"clone https://github.com/owner/repo.git"* ]] \
    || fail "shorthand should expand to clean HTTPS url" "$OUT"
[[ "$OUT" == *"extraheader=AUTHORIZATION: basic "* ]] \
    || fail "shorthand should inject auth via -c extraheader" "$OUT"
pass "shorthand expanded + auth via extraheader"

# 3. Token must NOT be embedded in the clone URL (would persist in .git/config).
[[ "$OUT" != *"ghs_dummy_install_token@"* ]] \
    || fail "raw token must not appear in the clone URL" "$OUT"
[[ "$OUT" != *"x-access-token:ghs_"* ]] \
    || fail "raw token must not appear on the command line" "$OUT"
pass "token kept out of the positional URL"

# 4. Full HTTPS GitHub URL → preserved, auth injected.
OUT=$(python3 "$WRAPPER" https://github.com/o/r.git target-dir 2>&1)
[[ "$OUT" == *"clone https://github.com/o/r.git target-dir"* ]] \
    || fail "https url + dir preserved" "$OUT"
[[ "$OUT" == *"extraheader=AUTHORIZATION: basic "* ]] \
    || fail "https url should inject auth" "$OUT"
pass "https github url preserved + auth injected"

# 5. SSH URL → pass through untouched, NO token injection.
OUT=$(python3 "$WRAPPER" git@github.com:o/r.git 2>&1)
[[ "$OUT" == *"clone git@github.com:o/r.git"* ]] || fail "ssh should pass through" "$OUT"
[[ "$OUT" != *"extraheader"* ]] || fail "ssh must not inject auth" "$OUT"
pass "ssh url passes through, no auth"

# 6. Non-GitHub HTTPS URL → not ours to auth, pass through untouched.
OUT=$(python3 "$WRAPPER" https://gitlab.com/o/r.git 2>&1)
[[ "$OUT" == *"clone https://gitlab.com/o/r.git"* ]] || fail "non-github pass through" "$OUT"
[[ "$OUT" != *"extraheader"* ]] || fail "non-github must not inject auth" "$OUT"
pass "non-github url passes through, no auth"

# 7. Flags before the source → source still found and expanded.
OUT=$(python3 "$WRAPPER" -b main --depth 1 owner/repo 2>&1)
[[ "$OUT" == *"clone -b main --depth 1 https://github.com/owner/repo.git"* ]] \
    || fail "flags before source should be preserved, source expanded" "$OUT"
pass "flags before source handled"

# 8. --help → prints usage, no git call.
OUT=$(python3 "$WRAPPER" --help 2>&1)
[[ "$OUT" == *"git-clone-as-app"* ]] || fail "help should print usage" "$OUT"
[[ "$OUT" != *"REAL GIT:"* ]] || fail "help must not call git" "$OUT"
pass "--help prints usage"

echo "ALL CLONE WRAPPER TESTS PASSED"
