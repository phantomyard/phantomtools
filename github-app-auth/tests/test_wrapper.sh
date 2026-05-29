#!/bin/bash
set -e

# Setup a fake environment
TMP_DIR=$(mktemp -d)
trap "rm -rf $TMP_DIR" EXIT

BIN_DIR="$TMP_DIR/bin"
REAL_GIT_DIR="$TMP_DIR/real-git"
mkdir -p "$BIN_DIR" "$REAL_GIT_DIR"

# 1. Create a "real" git that just echoes "REAL GIT CALLED"
cat > "$REAL_GIT_DIR/git" <<EOF
#!/bin/bash
echo "REAL GIT CALLED"
EOF
chmod +x "$REAL_GIT_DIR/git"

# 2. Copy our wrapper to BIN_DIR/git
cp github-app-auth/bin/git "$BIN_DIR/git"

# 3. Create the Python helpers (mocked) in BIN_DIR so the wrapper finds them
touch "$BIN_DIR/git-fetch-as-app" "$BIN_DIR/git-push-as-app" "$BIN_DIR/git-pull-as-app"
chmod +x "$BIN_DIR/git-fetch-as-app" "$BIN_DIR/git-push-as-app" "$BIN_DIR/git-pull-as-app"

# 4. Test: Call the wrapper with a command that should fall through to real git
# We need to make sure REAL_GIT is NOT set initially, or set to our fake real git.
export PATH="$BIN_DIR:$REAL_GIT_DIR:$PATH"
export GITHUB_APP_REAL_GIT="$REAL_GIT_DIR/git"

echo "Testing wrapper fallthrough..."
OUTPUT=$("$BIN_DIR/git" status)
if [[ "$OUTPUT" == "REAL GIT CALLED" ]]; then
    echo "SUCCESS: Wrapper fell through to real git"
else
    echo "FAILURE: Wrapper output: $OUTPUT"
    exit 1
fi

# 5. Test: Check if REAL_GIT is exported for children
cat > "$BIN_DIR/git-push-as-app" <<EOF
#!/bin/bash
echo "REAL_GIT is \$REAL_GIT"
EOF

echo "Testing REAL_GIT export..."
export GITHUB_TOKEN="ghs_dummy_token"
# We need a github remote to trigger the wrapper to call git-push-as-app
# Mock a git config call within the wrapper? 
# Actually, let's just test if the wrapper exports it when it would call a helper.
# The wrapper checks 'git remote get-url origin'
# Let's mock 'git remote' to return a github URL.

cat > "$REAL_GIT_DIR/git" <<EOF
#!/bin/bash
if [[ "\$*" == "remote get-url origin" ]]; then
    echo "https://github.com/owner/repo.git"
else
    echo "REAL GIT CALLED WITH: \$*"
fi
EOF

OUTPUT=$("$BIN_DIR/git" push origin main 2>&1)
if [[ "$OUTPUT" == *"REAL_GIT is $REAL_GIT_DIR/git"* ]]; then
    echo "SUCCESS: REAL_GIT was exported to child"
else
    echo "FAILURE: Wrapper output: $OUTPUT"
    exit 1
fi
