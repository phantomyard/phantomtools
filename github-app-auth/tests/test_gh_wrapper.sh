#!/bin/bash
set -e

# Setup a fake environment
TMP_DIR=$(mktemp -d)
trap "rm -rf $TMP_DIR" EXIT

BIN_DIR="$TMP_DIR/bin"
REAL_GH_DIR="$TMP_DIR/real-gh"
mkdir -p "$BIN_DIR" "$REAL_GH_DIR"

# A fake "real" gh that echoes the args it got and whether a token was injected.
cat > "$REAL_GH_DIR/gh" <<'EOF'
#!/bin/bash
echo "REAL GH CALLED: $*"
echo "GH_TOKEN=${GH_TOKEN:-<unset>}"
EOF
chmod +x "$REAL_GH_DIR/gh"

# Our wrapper + sibling create-pr-as-app stub
cp github-app-auth/bin/gh "$BIN_DIR/gh"
chmod +x "$BIN_DIR/gh"
cat > "$BIN_DIR/create-pr-as-app" <<'EOF'
#!/bin/bash
echo "CREATE-PR-AS-APP CALLED: $*"
EOF
chmod +x "$BIN_DIR/create-pr-as-app"

export PATH="$BIN_DIR:$REAL_GH_DIR:$PATH"
export GITHUB_APP_REAL_GH="$REAL_GH_DIR/gh"
export HOME="$TMP_DIR"   # so the wrapper reads our fake ~/.github_env (none yet)

pass() { echo "SUCCESS: $1"; }
fail() { echo "FAILURE: $1"; echo "  output: $2"; exit 1; }

# 1. No App token anywhere → pass through untouched, no GH_TOKEN injected.
unset GITHUB_TOKEN GH_TOKEN
OUT=$("$BIN_DIR/gh" api user 2>&1)
[[ "$OUT" == *"REAL GH CALLED: api user"* ]] || fail "no-token fallthrough" "$OUT"
[[ "$OUT" == *"GH_TOKEN=<unset>"* ]] || fail "no-token must not inject GH_TOKEN" "$OUT"
pass "no App token → real gh, no injection"

# 2. App token in ~/.github_env → injected as GH_TOKEN for `gh api`.
echo 'export GITHUB_TOKEN="ghs_dummy_install_token"' > "$TMP_DIR/.github_env"
chmod 600 "$TMP_DIR/.github_env"   # match the 0600 refresh-github-env.sh writes
OUT=$("$BIN_DIR/gh" api repos/o/r 2>&1)
[[ "$OUT" == *"REAL GH CALLED: api repos/o/r"* ]] || fail "api with token" "$OUT"
[[ "$OUT" == *"GH_TOKEN=ghs_dummy_install_token"* ]] || fail "App token must be injected" "$OUT"
pass "App token from ~/.github_env injected as GH_TOKEN"

# 3. A PAT (not ghs_*) must NOT be hijacked — real gh keeps its own auth.
echo 'export GITHUB_TOKEN="ghp_a_personal_token"' > "$TMP_DIR/.github_env"
chmod 600 "$TMP_DIR/.github_env"
unset GITHUB_TOKEN GH_TOKEN
OUT=$("$BIN_DIR/gh" api user 2>&1)
[[ "$OUT" == *"GH_TOKEN=<unset>"* ]] || fail "PAT must not be injected as GH_TOKEN" "$OUT"
pass "PAT is left alone (no override)"

# 4. `gh pr create` under an App token → redirected, real gh NOT called.
echo 'export GITHUB_TOKEN="ghs_dummy_install_token"' > "$TMP_DIR/.github_env"
chmod 600 "$TMP_DIR/.github_env"
set +e
OUT=$("$BIN_DIR/gh" pr create --title x 2>&1); RC=$?
set -e
[[ $RC -ne 0 ]] || fail "pr create should exit non-zero" "$OUT"
[[ "$OUT" == *"create-pr-as-app"* ]] || fail "pr create should redirect" "$OUT"
[[ "$OUT" != *"REAL GH CALLED"* ]] || fail "pr create must not reach real gh" "$OUT"
pass "gh pr create redirected to create-pr-as-app"

# 5. Escape hatch lets pr create through to real gh.
OUT=$(GITHUB_APP_GH_ALLOW_PR_CREATE=1 "$BIN_DIR/gh" pr create --title x 2>&1)
[[ "$OUT" == *"REAL GH CALLED: pr create"* ]] || fail "escape hatch should pass through" "$OUT"
pass "GITHUB_APP_GH_ALLOW_PR_CREATE=1 passes pr create through"

# 6. Other `gh pr` verbs (list/view) still work and get the token.
OUT=$("$BIN_DIR/gh" pr list 2>&1)
[[ "$OUT" == *"REAL GH CALLED: pr list"* ]] || fail "pr list should pass through" "$OUT"
[[ "$OUT" == *"GH_TOKEN=ghs_dummy_install_token"* ]] || fail "pr list should get token" "$OUT"
pass "gh pr list passes through with token"

# 7. `gh auth ...` passes through untouched and is NOT given the App token
#    (so interactive login still works on a human's machine).
OUT=$("$BIN_DIR/gh" auth status 2>&1)
[[ "$OUT" == *"REAL GH CALLED: auth status"* ]] || fail "auth should pass through" "$OUT"
[[ "$OUT" == *"GH_TOKEN=<unset>"* ]] || fail "auth must not get App token" "$OUT"
pass "gh auth passes through without token injection"

# 8. Without the GITHUB_APP_REAL_GH override, the wrapper must discover the real
#    gh by walking $PATH (skipping itself) — exercises resolve_real_gh's
#    PATH-discovery branch, the fiddliest part of the shim.
echo 'export GITHUB_TOKEN="ghs_dummy_install_token"' > "$TMP_DIR/.github_env"
chmod 600 "$TMP_DIR/.github_env"
OUT=$(unset GITHUB_APP_REAL_GH; "$BIN_DIR/gh" api user 2>&1)
[[ "$OUT" == *"REAL GH CALLED: api user"* ]] || fail "PATH discovery should find real gh" "$OUT"
[[ "$OUT" == *"GH_TOKEN=ghs_dummy_install_token"* ]] || fail "PATH-discovered gh should get token" "$OUT"
pass "real gh discovered via \$PATH when no override is set"

# 9. `gh auth` with an App token already in the env (a bot process, or a shell
#    exporting GITHUB_TOKEN=ghs_*) must have it STRIPPED before reaching real
#    gh — otherwise `gh auth login` refuses and `auth status` reports the App
#    instead of the human keyring.
echo 'export GITHUB_TOKEN="ghs_dummy_install_token"' > "$TMP_DIR/.github_env"
chmod 600 "$TMP_DIR/.github_env"
OUT=$(GH_TOKEN=ghs_dummy_install_token GITHUB_TOKEN=ghs_dummy_install_token \
    "$BIN_DIR/gh" auth status 2>&1)
[[ "$OUT" == *"REAL GH CALLED: auth status"* ]] || fail "auth should pass through" "$OUT"
[[ "$OUT" == *"GH_TOKEN=<unset>"* ]] || fail "auth must strip App-shaped GH_TOKEN" "$OUT"
pass "gh auth strips App-shaped env tokens before real gh"

# 10. A human's own PAT in the env must survive `gh auth` (not ours to strip),
#     so `gh auth status` still sees it.
OUT=$(GH_TOKEN=ghp_a_personal_token "$BIN_DIR/gh" auth status 2>&1)
[[ "$OUT" == *"GH_TOKEN=ghp_a_personal_token"* ]] || fail "auth must keep a human PAT" "$OUT"
pass "gh auth leaves a human PAT in the env untouched"

echo "ALL GH WRAPPER TESTS PASSED"
