#!/usr/bin/env bash
# Restart drill: freeze the supervisor loop, prove systemd notices and restarts
# us, and prove the restart re-runs the startup safe-state assertion.
#
# This is the inverse of the fail-safe drills in
# services/hardware_io/tests/test_drills.py. Those prove the supervisor drives
# actuators safe while it is running. This proves something takes over when the
# supervisor itself stops running — which the in-process tests cannot, because a
# frozen process cannot assert anything about itself.
#
# Needs systemd and a deployed checkout, so it runs on the Pi, not in CI.
#
#   ./scripts/drill-restart.sh            # local (on the Pi)
#   ./scripts/drill-restart.sh reef       # over ssh

set -uo pipefail

UNIT=bellasreef-hardware-io
REMOTE="${1:-}"

run() {
    if [[ -n "$REMOTE" ]]; then ssh -o ControlPath=none "$REMOTE" "$1"; else bash -c "$1"; fi
}

prop() { run "systemctl show $UNIT -p $1 --value"; }

fail() { echo "DRILL FAILED: $*" >&2; exit 1; }

echo "── drill: freeze the supervisor loop ──"

active=$(run "systemctl is-active $UNIT" || true)
[[ "$active" == "active" ]] || fail "$UNIT is not active (got '$active')"

# The freeze trigger is opt-in; a permanently armed "hang yourself" signal in
# production would be a liability.
env_ok=$(run "systemctl show $UNIT -p Environment --value | grep -c BELLASREEF_ENABLE_FREEZE_DRILL=1" || true)
[[ "$env_ok" == "1" ]] || fail "BELLASREEF_ENABLE_FREEZE_DRILL=1 is not set on the unit"

pid_before=$(prop MainPID)
restarts_before=$(prop NRestarts)
watchdog=$(prop WatchdogUSec)
echo "  before:   MainPID=$pid_before NRestarts=$restarts_before WatchdogSec=$watchdog"

echo "  freezing…"
run "sudo kill -USR1 $pid_before" || fail "could not signal $pid_before"

deadline=$((SECONDS + 60))
while (( SECONDS < deadline )); do
    sleep 3
    if [[ "$(prop NRestarts)" != "$restarts_before" ]]; then break; fi
done

restarts_after=$(prop NRestarts)
pid_after=$(prop MainPID)
[[ "$restarts_after" != "$restarts_before" ]] || fail "systemd never restarted the unit"
[[ "$pid_after" != "$pid_before" ]] || fail "MainPID unchanged; process was not replaced"
echo "  restarted: MainPID=$pid_after NRestarts=$restarts_after"

echo "── assertions ──"

# 1. systemd attributed the kill to the watchdog, not something incidental.
if run "sudo journalctl -u $UNIT --since '-2min' --no-pager -o cat | grep -q 'Watchdog timeout'"; then
    echo "  PASS  systemd reported a watchdog timeout"
else
    fail "no watchdog timeout in the journal — the restart had another cause"
fi

# 2. The restarted process re-ran the startup safe-state assertion, and the
#    actuator that came up energised actually reached safe state. A restart that
#    skipped this would leave hardware in whatever state the crash left it.
last=$(run "sudo journalctl -u $UNIT --since '-2min' --no-pager -o cat | grep safe_state_asserted | tail -1")
echo "  last assertion: $last"
grep -q '"drill_actuator_safe":true' <<<"$last" \
    || fail "restart did not drive the drill actuator to safe state"
echo "  PASS  restart re-ran the startup safe-state assertion"

# 3. It is actually serving again.
if run "curl -fsS localhost:9101/healthz | grep -q '\"healthy\": true'"; then
    echo "  PASS  health endpoint green after restart"
else
    fail "service is not healthy after restart"
fi

echo
echo "DRILL PASSED"
