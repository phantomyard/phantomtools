#!/usr/bin/env bash
# Install the repo git hooks (pre-push → CI-equivalent checks).
# Run from the repo root. Idempotent.
set -e
cd "$(dirname "$0")/.."
git config core.hooksPath .githooks
echo "core.hooksPath = $(git config core.hooksPath)"
echo "Hooks instalados. Para saltar el pre-push: git push --no-verify"
