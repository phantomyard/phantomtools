#!/usr/bin/env bash
# =============================================================================
# GitHub App Token Generator
# Reads config from environment / ~/.env
# Sets: GITHUB_TOKEN, GITHUB_INSTALLATION_ID, GITHUB_JWT
#
# Required env:
#   GITHUB_APP_ID
#   GITHUB_APP_PRIVATE_KEY_PATH
#
# Requirements: openssl, python3, curl
# =============================================================================
set -euo pipefail

: "${GITHUB_APP_ID:?GITHUB_APP_ID is not set}"
: "${GITHUB_APP_PRIVATE_KEY_PATH:?GITHUB_APP_PRIVATE_KEY_PATH is not set}"

if [[ ! -f "$GITHUB_APP_PRIVATE_KEY_PATH" ]]; then
    echo "ERROR: Private key not found at $GITHUB_APP_PRIVATE_KEY_PATH" >&2
    return 1
fi

# --- Step 1: Build JWT (RS256) using only openssl + python3 stdlib ---
now=$(date +%s)
iat=$((now - 60))
exp=$((now + 600))  # 10 min JWT lifetime

header='{"alg":"RS256","typ":"JWT"}'
payload="{\"iat\":$iat,\"exp\":$exp,\"iss\":$GITHUB_APP_ID}"

b64enc() { python3 -c "import base64,sys; print(base64.urlsafe_b64encode(sys.stdin.buffer.read()).decode().rstrip('='))"; }

b64_header=$(printf '%s' "$header" | b64enc)
b64_payload=$(printf '%s' "$payload" | b64enc)
signing_input="${b64_header}.${b64_payload}"

signature=$(printf '%s' "$signing_input" | openssl dgst -sha256 -sign "$GITHUB_APP_PRIVATE_KEY_PATH" | b64enc)

JWT="${signing_input}.${signature}"

# --- Step 2: Find installation ID ---
installations=$(curl -sS -H "Authorization: Bearer $JWT" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/app/installations")

# Try jq first, fallback to python3
if command -v jq >/dev/null 2>&1; then
    INSTALL_ID=$(echo "$installations" | jq -r '.[0].id // empty')
else
    INSTALL_ID=$(python3 -c "import sys,json; d=json.load(sys.stdin); print(d[0]['id'] if d else '')" <<<"$installations")
fi

if [[ -z "$INSTALL_ID" ]]; then
    echo "ERROR: No GitHub App installation found" >&2
    echo "Response: $installations" >&2
    return 1
fi

# --- Step 3: Exchange JWT for installation access token ---
token_resp=$(curl -sS -X POST \
    -H "Authorization: Bearer $JWT" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/app/installations/$INSTALL_ID/access_tokens")

if command -v jq >/dev/null 2>&1; then
    TOKEN=$(echo "$token_resp" | jq -r '.token // empty')
    EXPIRES=$(echo "$token_resp" | jq -r '.expires_at // empty')
else
    TOKEN=$(python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('token',''))" <<<"$token_resp")
    EXPIRES=$(python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('expires_at',''))" <<<"$token_resp")
fi

if [[ -z "$TOKEN" ]]; then
    echo "ERROR: Failed to get installation token" >&2
    echo "Response: $token_resp" >&2
    return 1
fi

export GITHUB_TOKEN="$TOKEN"
export GITHUB_INSTALLATION_ID="$INSTALL_ID"
export GITHUB_JWT="$JWT"
# Persist the expiry so consumers (ghapplib, doctor) can detect a stale token
# proactively instead of only discovering it via a 401. May be empty if GitHub
# omitted expires_at; downstream readers treat "unknown" as "can't tell".
export GITHUB_TOKEN_EXPIRES_AT="$EXPIRES"

echo "GitHub token acquired (expires: $EXPIRES)" >&2
