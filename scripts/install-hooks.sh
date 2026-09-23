#!/usr/bin/env bash
# Install the repo git hooks (pre-push → CI-equivalent checks).
# Run from the repo root. Idempotent.
set -e
cd "$(dirname "$0")/.."
git config core.hooksPath .githooks
echo "core.hooksPath = $(git config core.hooksPath)"
echo "Hooks installed. To skip the pre-push hook: git push --no-verify"
