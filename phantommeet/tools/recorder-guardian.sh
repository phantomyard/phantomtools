#!/usr/bin/env bash
#
# PhantomMeet — recorder guardian (v1)
#
# Companion health watch for the Jitsi/Jibri recorder on a meeting host. It is
# meant to run from a systemd timer + oneshot. It never wraps, gates or orders
# the recorder: it only observes and, when the recorder is alive but unable to
# record, asks systemd to restart the recorder units. If this script is broken
# or stopped, the recorder is completely unaffected.
#
# What it covers:
#   - process dead          -> already covered by systemd (Restart=always +
#                              StartLimitIntervalSec=0). Not this script's job.
#   - alive but dumb        -> the health endpoint reports a bad state, or does
#                              not answer at all -> this script acts.
#   - recording in progress -> NEVER act (a recording is never interrupted).
#
# Design (see docs/SPEC.md §12):
#   - ZERO state owned: no counter file, no database. "Two consecutive bad
#     readings" is decided inside one run: read, wait GUARDIAN_RECHECK, read
#     again; act only if both readings are bad.
#   - Loop brake independent of our state: ask systemd when the recorder's main
#     process started; a process younger than GUARDIAN_GRACE is left alone, so
#     a persistently broken recorder is restarted at most once per grace window.
#   - The action is narrow: restart exactly the four recorder units.
#   - Silent when healthy; loud only on problems and actions.
#
# Environment knobs (all optional):
#   GUARDIAN_HEALTH_URL  health endpoint (default the recorder's local API)
#   GUARDIAN_RECHECK     seconds between the two readings in a run (default 10)
#   GUARDIAN_GRACE       seconds after the recorder's main process start during
#                        which it is left alone (default 180)
#   GUARDIAN_ACTION      command that restarts the recorder (default: restart
#                        the four units). Empty -> log only, never act.
#   GUARDIAN_OBSERVE     1 -> never execute the action, only log what it would do
#   GUARDIAN_UNIT        unit whose main-process start time gates the action
#                        (default jibri)
#
# Exit codes: 0 on healthy (silent) and after acting; non-zero only when the
# script cannot run at all (e.g. python3 missing) — and then it does not touch
# the recorder.

set -u

HEALTH_URL="${GUARDIAN_HEALTH_URL:-http://127.0.0.1:2222/jibri/api/v1.0/health}"
RECHECK="${GUARDIAN_RECHECK:-10}"
GRACE="${GUARDIAN_GRACE:-180}"
# `-` (not `:-`): an unset GUARDIAN_ACTION means "use the narrow default"; an
# explicitly EMPTY GUARDIAN_ACTION means "log only, never act".
ACTION="${GUARDIAN_ACTION-systemctl restart jibri jibri-xorg jibri-icewm pulseaudio-jibri}"
OBSERVE="${GUARDIAN_OBSERVE:-0}"
UNIT="${GUARDIAN_UNIT:-jibri}"

log() { printf '[recorder-guardian] %s %s\n' "$(date -Is)" "$*" >&2; }

# The classifier only needs python3; check it before doing anything so that a
# missing interpreter is a clean, non-recorder-touching failure.
if ! command -v python3 >/dev/null 2>&1; then
  printf '[recorder-guardian] python3 not found: cannot read the recorder health; not touching the recorder\n' >&2
  exit 3
fi

# $1 = health URL -> prints exactly one of: ok | busy | act | unknown
verdict() {
  python3 - "$1" <<'PY'
import json
import sys
import urllib.request

url = sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=5) as response:
        raw = response.read().decode("utf-8", "replace")
except Exception:
    print("unknown")
    raise SystemExit(0)

try:
    data = json.loads(raw)
except Exception:
    print("unknown")
    raise SystemExit(0)

status = data.get("status") if isinstance(data, dict) else None
if not isinstance(status, dict):
    print("unknown")
    raise SystemExit(0)

# A recording in progress is never interrupted, whatever the health says.
if str(status.get("busyStatus", "")).upper() == "BUSY":
    print("busy")
    raise SystemExit(0)

health = status.get("health")
if isinstance(health, dict):
    health_status = str(health.get("healthStatus", "")).upper()
    if health_status and health_status != "HEALTHY":
        print("act")
        raise SystemExit(0)
    # HEALTHY, or a health object without a usable healthStatus.
    print("ok")
    raise SystemExit(0)

# A parseable status object WITHOUT a health key is treated as OK: a Jitsi
# version changing the payload shape must never cause a restart loop.
print("ok")
PY
}

# True (0) when the recorder's main process started less than GRACE seconds
# ago. If the start time cannot be read (unit absent/inactive, no systemctl),
# the recorder is not "recently started" and the action is allowed.
in_grace() {
  command -v systemctl >/dev/null 2>&1 || return 1
  local started start_epoch age now
  started="$(systemctl show "$UNIT" -p ExecMainStartTimestamp --value 2>/dev/null || true)"
  [ -n "$started" ] || return 1
  start_epoch="$(date -d "$started" +%s 2>/dev/null || true)"
  [ -n "$start_epoch" ] || return 1
  now="$(date +%s)"
  age=$(( now - start_epoch ))
  [ "$age" -lt "$GRACE" ]
}

# ---- Reading 1 ------------------------------------------------------------
first="$(verdict "$HEALTH_URL" 2>/dev/null || true)"
[ -n "$first" ] || first="unknown"
case "$first" in
  ok|busy) exit 0 ;;
esac

# ---- Reading 2 (after the recheck delay) ----------------------------------
sleep "$RECHECK"
second="$(verdict "$HEALTH_URL" 2>/dev/null || true)"
[ -n "$second" ] || second="unknown"
case "$second" in
  # One bad reading is a transient hiccup, not a failure.
  ok|busy) exit 0 ;;
esac

# ---- Two consecutive bad readings: apply the brake, then act --------------
if in_grace; then
  log "recorder unhealthy ($first then $second) but its main process started less than ${GRACE}s ago: holding off"
  exit 0
fi

if [ "$OBSERVE" = "1" ] || [ -z "$ACTION" ]; then
  log "recorder unhealthy ($first then $second): would restart the recorder [observe/log-only]"
  exit 0
fi

log "recorder unhealthy ($first then $second): restarting the recorder: $ACTION"
if sh -c "$ACTION"; then
  log "restart command succeeded"
else
  rc=$?
  log "restart command FAILED (rc=$rc)"
fi
exit 0
