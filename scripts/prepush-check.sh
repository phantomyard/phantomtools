#!/usr/bin/env bash
# prepush-check.sh — run the same checks as the phantomyard CI *before* pushing.
#
# This mirrors .github/workflows/ci.yml + phantombridge-ci.yml so a local push
# does not surprise us with a red pipeline. It only runs the checks for the
# tools touched by the commits being pushed.
#
# Usage:
#   scripts/prepush-check.sh [commit-range]   # e.g. HEAD~3..HEAD, or origin/main..HEAD
#   scripts/prepush-check.sh                  # defaults to HEAD~1..HEAD
#
# Exit code is non-zero if any check fails, so it can be wired into a git
# pre-push hook (see .githooks/pre-push). Skip deliberately with:
#   git push --no-verify
#
# bandit/mypy are optional locally (the CI runs them anyway) — they print a
# warning instead of blocking when their tool isn't installed in the venv.

set -u
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

RANGE="${1:-HEAD~1..HEAD}"

# ---------------------------------------------------------------------------
# Which tools did these commits touch?
# ---------------------------------------------------------------------------
changed() { git diff --name-only "$RANGE" -- "$1" | grep -q .; }

CHECK_PHANTOMDOCS=0; CHECK_PHANTOMORG=0; CHECK_PHANTOMMEET=0; CHECK_PHANTOMBRIDGE=0
if changed "phantomdocs/";    then CHECK_PHANTOMDOCS=1;    fi
if changed "phantomorg/";     then CHECK_PHANTOMORG=1;     fi
if changed "phantommeet/";    then CHECK_PHANTOMMEET=1;    fi
if changed "phantombridge/";  then CHECK_PHANTOMBRIDGE=1;  fi

if [ "$CHECK_PHANTOMDOCS$CHECK_PHANTOMORG$CHECK_PHANTOMMEET$CHECK_PHANTOMBRIDGE" = "0000" ]; then
  echo "prepush-check: no tool changed in $RANGE — nothing to check."
  exit 0
fi

fail=0
note_ok()   { echo "  ✓ $1"; }
note_fail() { echo "  ✗ $1"; fail=1; }
warn_opt()  { echo "  ⚠ $1 no instalado (CI lo correrá): $2 -m pip install $1"; }

# Pick the interpreter for a tool dir: its .venv if present, else system python3.
py_for() { local d="$1"; if [ -x "$d/.venv/bin/python" ]; then echo "$d/.venv/bin/python"; else echo "python3"; fi; }

# ---------------------------------------------------------------------------
# phantomdocs — ruff check + ruff format --check + bandit + pytest
# ---------------------------------------------------------------------------
if [ "$CHECK_PHANTOMDOCS" = "1" ]; then
  echo "== phantomdocs =="
  cd "$REPO_ROOT/phantomdocs"
  PY=$(py_for ".")
  "$PY" -m ruff check src tests          && note_ok "ruff check"   || note_fail "ruff check"
  "$PY" -m ruff format --check src tests && note_ok "ruff format" || note_fail "ruff format"
  if "$PY" -m bandit --version >/dev/null 2>&1; then
    "$PY" -m bandit -r src -q            && note_ok "bandit"      || note_fail "bandit"
  else
    warn_opt bandit "$PY"
  fi
  "$PY" -m pytest -q                     && note_ok "pytest"      || note_fail "pytest"
fi

# ---------------------------------------------------------------------------
# phantomorg — ruff check + ruff format --check + bandit + mypy + pytest
# ---------------------------------------------------------------------------
if [ "$CHECK_PHANTOMORG" = "1" ]; then
  echo "== phantomorg =="
  cd "$REPO_ROOT/phantomorg"
  PY=$(py_for ".")
  "$PY" -m ruff check phantomorg tests          && note_ok "ruff check"   || note_fail "ruff check"
  "$PY" -m ruff format --check phantomorg tests && note_ok "ruff format" || note_fail "ruff format"
  if "$PY" -m bandit --version >/dev/null 2>&1; then
    "$PY" -m bandit -r phantomorg -q            && note_ok "bandit"      || note_fail "bandit"
  else
    warn_opt bandit "$PY"
  fi
  if "$PY" -m mypy --version >/dev/null 2>&1; then
    "$PY" -m mypy phantomorg                    && note_ok "mypy"        || note_fail "mypy"
  else
    warn_opt mypy "$PY"
  fi
  "$PY" -m pytest -q                            && note_ok "pytest"      || note_fail "pytest"
fi

# ---------------------------------------------------------------------------
# phantommeet — ruff check + ruff format --check + bandit + pytest
# ---------------------------------------------------------------------------
if [ "$CHECK_PHANTOMMEET" = "1" ]; then
  echo "== phantommeet =="
  cd "$REPO_ROOT/phantommeet"
  PY=$(py_for ".")
  "$PY" -m ruff check src tests          && note_ok "ruff check"   || note_fail "ruff check"
  "$PY" -m ruff format --check src tests && note_ok "ruff format" || note_fail "ruff format"
  if "$PY" -m bandit --version >/dev/null 2>&1; then
    "$PY" -m bandit -r src -q            && note_ok "bandit"      || note_fail "bandit"
  else
    warn_opt bandit "$PY"
  fi
  "$PY" -m pytest -q                     && note_ok "pytest"      || note_fail "pytest"
fi

# ---------------------------------------------------------------------------
# phantombridge — node --check + npm test
# ---------------------------------------------------------------------------
if [ "$CHECK_PHANTOMBRIDGE" = "1" ]; then
  echo "== phantombridge =="
  cd "$REPO_ROOT/phantombridge"
  for f in bridge.js org-routing.js secrets.js mcp-bridge.mjs test-*.js; do
    node --check "$f" && note_ok "node --check $f" || note_fail "node --check $f"
  done
  npm test && note_ok "npm test" || note_fail "npm test"
fi

echo
if [ "$fail" = "0" ]; then
  echo "prepush-check: OK — listo para pushear."
else
  echo "prepush-check: FALLÓ. Arregla los checks (ruff check --fix && ruff format ayudan) o usa git push --no-verify SOLO si sabes que el fallo es falso."
fi
exit "$fail"
