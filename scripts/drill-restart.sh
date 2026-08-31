#!/usr/bin/env bash
# Restart drill: freeze the supervisor loop, prove the container runtime
# notices and restarts us, and prove the restart re-runs the startup safe-state
# assertion.
#
# This is the inverse of the fail-safe drills in
# services/hardware_io/tests/test_drills.py. Those prove the supervisor drives
# actuators safe while it is running. This proves something takes over when the
# supervisor itself stops running — which the in-process tests cannot, because a
# frozen process cannot assert anything about itself.
#
# Needs the real runtime and the real image, so it runs on the Pi, not in CI.
#
#   ./scripts/drill-restart.sh            # local (on the Pi)
#   ./scripts/drill-restart.sh reef       # over ssh
#
# What replaced what, when the host systemd units were deleted on 2026-08-13:
# systemd used to tell you why it killed the unit ("Watchdog timeout" in the
# journal). Docker does not, so the process says it on the way out instead —
# LivenessGuard exits 70 (LIVENESS_EXIT_CODE, EX_SOFTWARE), and this script
# reads that off the `die` event. `restart: unless-stopped` acts on process
# exit, never on an unhealthy healthcheck; that distinction is the whole reason
# LivenessGuard exists, and this drill is what proves the chain end to end.

set -uo pipefail

SERVICE=hardware-io
REMOTE="${1:-}"
REPO=/home/david/bellasreef

COMPOSE_BASE="docker compose -f hub/deploy/compose.yaml --env-file hub/deploy/.env"
COMPOSE_ARMED="docker compose -f hub/deploy/compose.yaml -f deploy/compose.drill.yaml --env-file hub/deploy/.env"

if [[ -n "$REMOTE" ]]; then
    run() { ssh -o ControlPath=none "$REMOTE" "cd $REPO && $1"; }
else
    cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
    run() { bash -c "$1"; }
fi

# Dump the evidence BEFORE the trap disarms, because disarming recreates the
# container and takes its logs with it. Learned the hard way on 2026-08-14: the
# first failing run destroyed the only record of why it failed.
fail() {
    echo "DRILL FAILED: $*" >&2
    if [[ -n "${CID:-}" ]]; then
        echo "── evidence (logs are about to be destroyed by the recreate) ──" >&2
        run "docker logs --tail 25 $CID 2>&1" >&2 || true
        run "docker inspect -f 'Status={{.State.Status}} ExitCode={{.State.ExitCode}} RestartCount={{.RestartCount}} RestartPolicy={{.HostConfig.RestartPolicy.Name}}' $CID" >&2 || true
        # Epochs are evaluated on the Pi, not here: the window has to be in the
        # docker daemon's clock, not the dev machine's.
        run "docker events --since ${t0:-$T_START} --until \$(date +%s) --filter container=$CID --format 'event={{.Action}} exitCode={{.Actor.Attributes.exitCode}}'" >&2 || true
    fi
    exit 1
}

inspect() { run "docker inspect -f '$1' $CID"; }

# Recreate hardware-io from compose.yaml alone, dropping the drill env. Runs on
# every exit path — a drill that leaves the freeze trigger armed on the hub has
# broken the thing it was checking.
disarm() {
    echo
    echo "── disarming ──"
    run "$COMPOSE_BASE up -d --force-recreate $SERVICE" >/dev/null 2>&1 \
        || echo "  WARNING: could not recreate $SERVICE unarmed — check it by hand" >&2

    local cid env_after
    cid=$(run "$COMPOSE_BASE ps -q $SERVICE" 2>/dev/null | tail -1)
    if [[ -z "$cid" ]]; then
        echo "  WARNING: $SERVICE has no container — bring it up by hand" >&2
        return
    fi
    env_after=$(run "docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' $cid" 2>/dev/null \
        | grep -c BELLASREEF_ENABLE_FREEZE_DRILL || true)
    if [[ "${env_after//[!0-9]/}" == "0" ]]; then
        echo "  disarmed: freeze trigger is not set on the running container"
    else
        echo "  WARNING: freeze trigger still present — disarm by hand" >&2
    fi
}

wait_healthy() {
    local cid="$1" deadline=$((SECONDS + 90)) status
    while (( SECONDS < deadline )); do
        status=$(run "docker inspect -f '{{.State.Health.Status}}' $cid" 2>/dev/null || true)
        [[ "$status" == "healthy" ]] && return 0
        sleep 3
    done
    return 1
}

echo "── preflight ──"

# Read on the Pi: every docker --since/--until below is in the daemon's clock.
T_START=$(run "date +%s")

running=$(run "$COMPOSE_BASE ps --format '{{.Service}} {{.State}}' | grep '^$SERVICE '" || true)
[[ "$running" == *running* ]] || fail "$SERVICE is not running (got '${running:-nothing}')"
echo "  $SERVICE is running"

echo "── arming the freeze drill ──"

# Recreates the container: the env flag is read once, at process start.
trap disarm EXIT
run "$COMPOSE_ARMED up -d --force-recreate $SERVICE" >/dev/null 2>&1 \
    || fail "could not recreate $SERVICE with the drill overlay"

CID=$(run "$COMPOSE_ARMED ps -q $SERVICE")
[[ -n "$CID" ]] || fail "no container id for $SERVICE after recreate"

wait_healthy "$CID" || fail "$SERVICE never became healthy after arming"

armed=$(inspect '{{range .Config.Env}}{{println .}}{{end}}' | grep -c 'BELLASREEF_ENABLE_FREEZE_DRILL=1' || true)
[[ "${armed//[!0-9]/}" == "1" ]] || fail "BELLASREEF_ENABLE_FREEZE_DRILL=1 is not set on the container"

pid_before=$(inspect '{{.State.Pid}}')
restarts_before=$(inspect '{{.RestartCount}}')
timeout_s=$(inspect '{{range .Config.Env}}{{println .}}{{end}}' \
    | sed -n 's/^BELLASREEF_LIVENESS_TIMEOUT_S=//p')
echo "  armed:    container=${CID:0:12} Pid=$pid_before RestartCount=$restarts_before LivenessTimeout=${timeout_s:-default}s"

# Epoch, not a formatted stamp: both `docker events --since` and
# `docker logs --since` take it, and it sidesteps the container's timezone.
t0=$(run "date +%s")

# Signalled from INSIDE the container, not with `docker kill --signal=USR1`.
# Measured on the Pi 2026-08-14, docker 29.7.2: `docker kill` marks the
# container manually-stopped, and `unless-stopped` then declines to restart it.
# The guard still fired and the process still exited 70 — the container simply
# stayed dead, RestartCount=0, indefinitely.
#
# That is an artefact of the daemon's kill API, NOT of the recovery path: a
# genuine stall exits the process without anyone calling `docker kill`, and
# that case was verified to restart normally (Pid changed, RestartCount 0->1,
# ~15 s). Signalling PID 1 from inside reproduces the real failure faithfully.
# Using python rather than `kill` because the slim image ships no shell utils.
echo "  freezing…"
run "docker exec $CID python -c 'import os,signal; os.kill(1, signal.SIGUSR1)'" >/dev/null \
    || fail "could not signal PID 1 inside $CID"

deadline=$((SECONDS + 90))
while (( SECONDS < deadline )); do
    sleep 3
    [[ "$(inspect '{{.RestartCount}}')" != "$restarts_before" ]] && break
done

restarts_after=$(inspect '{{.RestartCount}}')
pid_after=$(inspect '{{.State.Pid}}')
[[ "$restarts_after" != "$restarts_before" ]] || fail "the runtime never restarted the container"
[[ "$pid_after" != "$pid_before" ]] || fail "Pid unchanged; the process was not replaced"
echo "  restarted: Pid=$pid_after RestartCount=$restarts_after"

echo "── assertions ──"

# 1. The runtime killed us because the loop stalled, not for some other reason.
#    Exit 70 is LIVENESS_EXIT_CODE; 137 would be a SIGKILL/OOM and 0 a clean
#    stop, so this distinguishes a stall from every other way the process ends.
t1=$(run "date +%s")
codes=$(run "docker events --since $t0 --until $t1 --filter container=$CID --filter event=die --format '{{.Actor.Attributes.exitCode}}'" || true)
if grep -qx 70 <<<"$codes"; then
    echo "  PASS  died with exit 70 — the liveness guard fired"
else
    fail "no exit-70 die event (saw: ${codes:-none}); the restart had another cause"
fi

# 2. The guard said so on the way out. Belt and braces with the exit code: the
#    event says a stall killed it, this says which code decided that.
if run "docker logs --since $t0 $CID 2>&1 | grep -q 'supervisor loop stalled'"; then
    echo "  PASS  guard logged the stall before terminating"
else
    fail "no stall log line from the liveness guard"
fi

# 3. The restarted process re-ran the startup safe-state assertion, and the
#    actuator that came up energised actually reached safe state. A restart that
#    skipped this would leave hardware in whatever state the crash left it.
last=$(run "docker logs --since $t0 $CID 2>&1 | grep safe_state_asserted | tail -1")
echo "  last assertion: $last"
grep -q '"drill_actuator_safe": *true' <<<"$last" \
    || fail "restart did not drive the drill actuator to safe state"
echo "  PASS  restart re-ran the startup safe-state assertion"

# 4. It is actually serving again. The metrics port is deliberately unpublished
#    (compose.yaml), so this reads the container's own healthcheck rather than
#    curling a host port that does not exist.
if wait_healthy "$CID"; then
    echo "  PASS  healthcheck green after restart"
else
    fail "$SERVICE is not healthy after restart"
fi

echo
echo "DRILL PASSED"
