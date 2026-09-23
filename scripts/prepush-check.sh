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
warn_opt()  { echo "  ⚠ $1 not installed (CI will run it): $2 -m pip install $1"; }

# Pick the interpreter for a tool dir: its .venv if present, else system python3.
# Returned absolute: callers resolve it while standing in the tool dir, but the
# smoke tests then cd to a throwaway directory.
py_for() { local d="$1"; if [ -x "$d/.venv/bin/python" ]; then echo "$(cd "$d/.venv/bin" && pwd)/python"; else echo "python3"; fi; }

# Resolve a CLI entry point: the tool's .venv bin/<name> if present, else "".
# Absolute for the same reason: a relative path resolves against the caller's cwd.
cli_for() { local d="$1" name="$2"; if [ -x "$d/.venv/bin/$name" ]; then echo "$(cd "$d/.venv/bin" && pwd)/$name"; else echo ""; fi; }

# Smoke tests (mirror the CI's packaged-install smoke step). Each runs in a
# throwaway temp dir; success is silent, failure prints the tail of the log.

smoke_pd() {  # $1 = pd binary (absolute)
  local pd="$1" d out rc; d=$(mktemp -d)
  trap 'rm -rf "$d"' RETURN
  out=$(
    exec 2>&1
    cd "$d" || exit 90
    cp "$REPO_ROOT/phantomdocs/tests/fixtures/smoke-org.yaml" org.yaml
    sed "s/^  - id: marco/  - id: $(id -un)/" org.yaml > org.yaml.tmp && mv org.yaml.tmp org.yaml
    "$pd" init --org smoke --root . >/dev/null || exit 1
    printf '# hello\n' > hello.md
    "$pd" add hello.md --slug hello.md --category 1 --owners ceo --root . --org-yaml org.yaml >/dev/null || exit 2
    "$pd" verify --root . | grep -q "verified 1 node" || exit 3
  )
  rc=$?
  [ "$rc" = "0" ] || printf '%s\n' "$out" | tail -5
  return "$rc"
}

smoke_po() {  # $1 = po binary (absolute)
  local po="$1" d out rc; d=$(mktemp -d)
  trap 'rm -rf "$d"' RETURN
  out=$(
    exec 2>&1
    cd "$d" || exit 90
    "$po" new-org --id smoke --name "Smoke Org" --sector ngo --lang en --template ngo >/dev/null || exit 1
    "$po" add-department --org organizations/smoke/org.yaml --id ops --name Operations --access-policy level-2 >/dev/null || exit 2
    "$po" add-role --org organizations/smoke/org.yaml --id lead --name "Team Lead" --department ops --access-level level-2 >/dev/null || exit 3
    "$po" add-actor --org organizations/smoke/org.yaml --id maria --role lead --tool email >/dev/null || exit 4
    "$po" validate --org organizations/smoke/org.yaml >/dev/null || exit 5
    "$po" build --org organizations/smoke/org.yaml --out "$d/dist" >/dev/null || exit 6
    find "$d/dist" -name SOUL.md | grep -q . || exit 7
  )
  rc=$?
  [ "$rc" = "0" ] || printf '%s\n' "$out" | tail -5
  return "$rc"
}

smoke_pm() {  # $1 = pm binary (absolute)
  local pm="$1" d out rc; d=$(mktemp -d)
  trap 'rm -rf "$d"' RETURN
  out=$(
    exec 2>&1
    cd "$d" || exit 90
    "$pm" derive-manifest \
      --org "$REPO_ROOT/phantommeet/tests/fixtures/org.smoke.yaml" \
      --base "$REPO_ROOT/phantommeet/tests/fixtures/base.smoke.yaml" \
      --out "$d/smoke-derived.yaml" >/dev/null || exit 1
    "$pm" validate --manifest "$d/smoke-derived.yaml" | grep -q "OK" || exit 2
  )
  rc=$?
  [ "$rc" = "0" ] || printf '%s\n' "$out" | tail -5
  return "$rc"
}

# ---------------------------------------------------------------------------
# phantomdocs — ruff check + ruff format --check + bandit + pytest + smoke
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

  PD=$(cli_for "." pd)
  if [ -n "$PD" ]; then
    if smoke_pd "$PD"; then note_ok "smoke (init+add+verify)"; else note_fail "smoke (init+add+verify)"; fi
  else
    echo "  ⚠ pd not installed (CI will run it): pip install -e ."
  fi
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

  PO=$(cli_for "." po)
  if [ -n "$PO" ]; then
    if smoke_po "$PO"; then note_ok "smoke (new-org+build)"; else note_fail "smoke (new-org+build)"; fi
  else
    echo "  ⚠ po not installed (CI will run it): pip install -e ."
  fi
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

  PM=$(cli_for "." pm)
  if [ -n "$PM" ]; then
    if smoke_pm "$PM"; then note_ok "smoke (derive+validate)"; else note_fail "smoke (derive+validate)"; fi
  else
    echo "  ⚠ pm not installed (CI will run it): pip install -e ."
  fi
fi

# ---------------------------------------------------------------------------
# phantombridge — node --check + npm test
# ---------------------------------------------------------------------------
if [ "$CHECK_PHANTOMBRIDGE" = "1" ]; then
  echo "== phantombridge =="
  cd "$REPO_ROOT/phantombridge"
  # Same files as the phantombridge CI. Literal names stay in the list even
  # when the file is missing, so we report it instead of letting node --check
  # fail on a path that a rename removed; the test-*.js glob expands to
  # whatever exists (nullglob: nothing at all when there are no test files).
  shopt -s nullglob
  bridge_files=(bridge.js org-routing.js secrets.js mcp-bridge.mjs test-*.js)
  shopt -u nullglob
  for f in "${bridge_files[@]}"; do
    if [ ! -f "$f" ]; then
      echo "  ⚠ $f missing — skipped (CI lists it explicitly)"
      continue
    fi
    node --check "$f" && note_ok "node --check $f" || note_fail "node --check $f"
  done
  npm test && note_ok "npm test" || note_fail "npm test"
fi

echo
if [ "$fail" = "0" ]; then
  echo "prepush-check: OK — ready to push."
else
  echo "prepush-check: FAILED. Fix the checks (ruff check --fix && ruff format help) or use git push --no-verify ONLY when you know the failure is a false positive."
fi
exit "$fail"
