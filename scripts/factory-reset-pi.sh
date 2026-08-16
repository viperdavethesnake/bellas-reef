#!/usr/bin/env bash
# Factory-reset the hub: the 2026-08-14 manual wipe as one audited command.
#
# Destroys: bellasreef_postgres-data, bellasreef_vm-data, bellasreef_nats-data
# - all pairings, all devices, all telemetry history, the audit log.
# Keeps: /backups (a fresh pre-reset backup is mandatory and taken first),
# the git checkout, the images, /boot/firmware config.
#
# Sanctioned exception (spec 2026-08-15) to the deployment-discipline rule
# that spine data services are never recreated by a deploy — see CLAUDE.md,
# "Deployment discipline". Everywhere else, postgres/nats/victoria-metrics
# keep restart: unless-stopped and are never force-recreated; this script
# is the one deliberate, typed-confirmation exception.
#
# Acceptance is manual, on the hub; the dry run IS the 2026-08-14 transcript.
#
# Usage: ./scripts/factory-reset-pi.sh
#
# Takes no other flags by design — this script performs exactly one
# operation on exactly one hub, named by BELLASREEF_PI_HOST (default
# bellasreef.local). -h/--help prints usage and exits before anything runs;
# any other argument is rejected the same way, before anything runs.
set -uo pipefail

# Found live (2026-08-15): `--help` used to fall through the bottom of this
# case entirely — there was no case, no parsing at all, so ANY argument,
# `--help` included, dropped straight into step 1 and fired the backup. This
# has to be the first thing the script does: no ssh, no backup, nothing above
# it, or a mistyped flag is one more way to trigger the exact destructive run
# it looks like it's asking to avoid.
usage() {
    cat <<USAGE
Usage: ./scripts/factory-reset-pi.sh

Factory-resets the hub named by \$BELLASREEF_PI_HOST (default: bellasreef.local).

DESTROYS on that hub:
  - docker volumes: bellasreef_postgres-data bellasreef_vm-data bellasreef_nats-data
  - every pairing, every device, the audit log, ALL telemetry history

A pre-reset backup is mandatory and is taken first, before anything else runs.
Destruction itself additionally requires typing 'factory-reset' at a
confirmation prompt once the backup has completed.

Takes no other flags by design — this script performs exactly one operation
on exactly one hub.

  -h, --help   print this message and exit
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 1 ;;
    esac
done

# Same variable name and default as deploy-pi.sh (BELLASREEF_PI_HOST /
# bellasreef.local) — one hostname convention for both operator scripts.
PI_HOST="${BELLASREEF_PI_HOST:-bellasreef.local}"
PI_DIR="/home/david/bellasreef"
DEPLOY_DIR="${PI_DIR}/deploy"
COMPOSE_FILE="${DEPLOY_DIR}/compose.yaml"
COMPOSE_ENV="${DEPLOY_DIR}/.env"
API_PORT=8000

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="/backups/bellasreef-pre-factory-${STAMP}.tar.gz"

die() { printf '\033[31mfactory-reset: %s\033[0m\n' "$1" >&2; exit 1; }
step() { printf '\033[1m▶ %s\033[0m\n' "$1"; }
warn() { printf '\033[33m  %s\033[0m\n' "$1"; }

# Unlike deploy-pi.sh's compose(), no BELLASREEF_TAG override: these calls
# run against whatever is already deployed (read from deploy/.env via
# --env-file), never a new SHA being rolled out — that part is entirely
# deploy-pi.sh's job, invoked in step 4 below.
compose() {
    # -n: never read the script's stdin. The confirmation prompt below reads
    # it, and the first run of this script (2026-08-15) proved that an ssh
    # hop before the prompt silently swallows piped/typed input — the
    # operator's "factory-reset" was eaten by the backup leg and the run
    # aborted "not confirmed" through no fault of theirs.
    ssh -n "$PI_HOST" "docker compose -f ${COMPOSE_FILE} --env-file ${COMPOSE_ENV} $*"
}

# ------------------------------------------------------------- 1. backup
# Mandatory, no skip flag, and runs before the confirmation prompt below —
# by spec. An aborted run (operator declines to confirm) still leaves this
# backup on disk; that is a harmless additive side effect, not the
# destructive state the confirmation gate protects against.
step "taking pre-reset backup to ${BACKUP} on ${PI_HOST}"
compose "exec -T api bellasreef backup --out '${BACKUP}'" \
    || die "backup failed; aborting with nothing touched"

# ------------------------------------------------------------- 2. consent
cat <<DOOM

About to DESTROY on ${PI_HOST}:
  - docker volumes: bellasreef_postgres-data bellasreef_vm-data bellasreef_nats-data
  - every pairing, every device, the audit log, ALL telemetry history

Pre-reset backup: ${BACKUP} (on the hub)
DOOM
# The word must be typed by a person — at the prompt, or written into the
# command line itself (`echo factory-reset | ...`) where no interactive
# stdin exists. Both satisfy the spec's intent: a human writes the word,
# verbatim, with the destruction notice on screen. ssh -n above is what
# makes the piped form work at all — without it the backup leg eats stdin.
read -r -p "Type 'factory-reset' to proceed: " confirm || confirm=""
[[ "$confirm" == "factory-reset" ]] || die "not confirmed; nothing touched"

# ------------------------------------------------------- 3. stop, down, wipe
# Order matters: a stopped-but-not-removed container still holds a reference
# to its volume (measured 2026-08-14), so the boot unit has to stop and the
# stack has to come down before the volumes will actually release.
step "stopping bellasreef.service on ${PI_HOST}"
ssh "$PI_HOST" "sudo systemctl stop bellasreef.service" \
    || die "could not stop bellasreef.service on ${PI_HOST}; nothing removed"

step "bringing the stack down"
compose "down" \
    || die "compose down failed on ${PI_HOST}; volumes not touched — check container state before retrying"

step "removing the three data volumes"
ssh "$PI_HOST" "docker volume rm bellasreef_postgres-data bellasreef_vm-data bellasreef_nats-data" \
    || die "volume removal failed on ${PI_HOST} — the stack is down but one or more volumes may remain; check 'docker volume ls' on ${PI_HOST} before retrying rather than re-running blind"

# --------------------------------------------------------- 4. redeploy clean
# --no-verify is correct here by construction, not a shortcut: the deploy
# telemetry gate cannot pass on an empty registry — no devices means no
# readings means nothing on the wire to verify (2026-08-12, 2026-08-14 both
# hit this).
#
# --no-setup-code is what fixed the second live defect (2026-08-15): without
# it, deploy-pi.sh's own --no-verify path prints a setup code itself once
# containers are up, and step 6 below then MINTS A NEW ONE, rotating that
# first one out — two different codes on one screen, the first dead on
# arrival. Passing --no-setup-code here silences deploy-pi.sh's print so
# step 6's code is the only one this run ever shows.
step "redeploying from zero"
"$(dirname "$0")/deploy-pi.sh" --host "$PI_HOST" --no-verify --no-setup-code \
    || die "redeploy failed after the wipe — the hub has NO data volumes and is not confirmed running. Do not treat this as a completed reset; investigate deploy-pi.sh's output above before retrying."

# ------------------------------------------------------ 5. verify fresh state
step "verifying factory-fresh state"
# Direct curl to PI_HOST:API_PORT, same mechanism deploy-pi.sh's own auth-leg
# check uses — one way to ask the hub about itself, not a second one over
# ssh+localhost. Retried briefly: uvicorn has just come up in a fresh
# container.
DEADLINE=$(($(date +%s) + 30))
info_body=""
while :; do
    info_body="$(curl -sS --max-time 10 "http://${PI_HOST}:${API_PORT}/api/v1/info" 2>/dev/null || true)"
    [[ -n "$info_body" ]] && break
    [[ "$(date +%s)" -ge $DEADLINE ]] && break
    sleep 3
done
[[ -n "$info_body" ]] || die "GET /api/v1/info on ${PI_HOST}:${API_PORT} did not answer after the redeploy — cannot confirm factory-fresh state"

paired="$(sed -n 's/.*"paired_client_count":\([0-9]*\).*/\1/p' <<<"$info_body")"
# [a-z]* rather than \(true\|false\): \| alternation is a GNU sed extension.
# This runs on this Mac's BSD /usr/bin/sed, where \| is literal and the GNU
# form matches nothing — verified against both true and false bodies (see
# the fix report in the sdd task file).
setup_mode="$(sed -n 's/.*"setup_mode":\([a-z]*\).*/\1/p' <<<"$info_body")"
[[ "$paired" == "0" ]] \
    || die "post-reset /info reports paired_client_count=${paired:-<absent>} — expected 0. The reset did not clear pairings."
[[ "$setup_mode" == "true" ]] \
    || die "post-reset /info reports setup_mode=${setup_mode:-<absent>} — expected true. The reset did not reopen setup mode."
echo "  0 paired clients, setup mode open"

# devices/audit_log counts — asserted, not just printed: a nonzero count here
# is "the reset left N devices behind", a real finding, not a footnote. -U/-d
# reference $POSTGRES_USER/$POSTGRES_DB inside the postgres container's own
# shell (single-quoted through both the ssh hop and docker compose exec, so
# neither this script nor the Pi's login shell ever needs to know the real
# values — the container already has them from deploy/.env via compose's
# environment: block).
#
# Transport failures (can't reach the container at all) warn, since the wipe
# and redeploy have already both succeeded by this point and a connectivity
# hiccup here isn't itself evidence of a bad reset; a value that comes back
# and is wrong is a real finding and dies.
step "confirming devices and audit log are empty"
if psql_out="$(ssh "$PI_HOST" "cd ${DEPLOY_DIR} && docker compose -f ${COMPOSE_FILE} --env-file ${COMPOSE_ENV} exec -T postgres sh -c 'psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -tAc \"SELECT count(*) FROM devices; SELECT count(*) FROM audit_log;\"'" 2>&1)"; then
    device_count="$(sed -n '1p' <<<"$psql_out")"
    audit_count="$(sed -n '2p' <<<"$psql_out")"
    [[ "$device_count" == "0" ]] \
        || die "the reset left ${device_count:-<unreadable>} device(s) behind in postgres on ${PI_HOST} — the wipe did not clear the devices table"
    [[ "$audit_count" == "0" ]] \
        || die "the reset left ${audit_count:-<unreadable>} audit_log row(s) behind in postgres on ${PI_HOST} — the wipe did not clear the audit log"
    echo "  0 devices, 0 audit_log rows"
else
    warn "could not query devices/audit_log counts on ${PI_HOST} (transport failure: ${psql_out}) — check manually with 'docker compose exec -T postgres psql -U \$POSTGRES_USER -d \$POSTGRES_DB -c \"SELECT count(*) FROM devices;\"'"
fi

# alembic at head — the redeploy already ran 'alembic upgrade head' as part
# of deploy-pi.sh's migration step; this confirms that landed rather than
# trusting it silently. 'alembic current' prints the revision id with a
# literal "(head)" suffix when the database is at the newest revision — the
# standard alembic CLI convention, not project-specific behavior.
step "confirming alembic is at head"
if alembic_out="$(ssh "$PI_HOST" "cd ${DEPLOY_DIR} && docker compose -f ${COMPOSE_FILE} --env-file ${COMPOSE_ENV} exec -T api sh -c 'cd /app/db && alembic current' 2>&1")"; then
    [[ "$alembic_out" == *"(head)"* ]] \
        || die "alembic current on ${PI_HOST} reports '${alembic_out}' — not at head after the redeploy's migration step"
    echo "  alembic at head"
else
    warn "could not check the alembic revision on ${PI_HOST} (transport failure: ${alembic_out}) — check manually with 'docker compose exec -T api sh -c \"cd /app/db && alembic current\"'"
fi

# All six JetStream streams — hardware-io's provision() (spine.py) logs one
# structured "stream created" line per entry in STREAMS (BR_CMD, BR_STATE,
# BR_REGISTRY, BR_CAPABILITY, BR_ASSIGNMENT, BR_AUDIT) at startup, against a
# freshly wiped nats-data volume this always reads "created", never
# "updated". Logs are JSON with compact separators (bellasreef_service.
# logging.JsonFormatter), so the substring match is exact. `|| true` runs on
# the REMOTE side deliberately: grep -c still prints "0" on no match but
# exits 1, so ssh's own exit status would be nonzero and a local
# `|| echo 0` fallback would fire on top of grep's own "0" — doubling the
# output to "0\n0". Neutralizing the exit status remotely, before it crosses
# the ssh boundary, is what keeps this to one line. Retried up to 60s:
# hardware-io needs a few seconds after container start to connect and
# provision.
step "confirming all six JetStream streams were recreated"
STREAM_DEADLINE=$(($(date +%s) + 60))
stream_count=0
while :; do
    stream_count="$(ssh "$PI_HOST" "docker logs bellasreef-hardware-io-1 2>&1 | grep -c '\"msg\":\"stream created\"' || true" 2>/dev/null)"
    stream_count="${stream_count:-0}"
    [[ "$stream_count" -ge 6 ]] && break
    [[ "$(date +%s)" -ge $STREAM_DEADLINE ]] && break
    sleep 3
done
[[ "$stream_count" -ge 6 ]] \
    || die "hardware-io logged only ${stream_count} of 6 expected 'stream created' lines (BR_CMD, BR_STATE, BR_REGISTRY, BR_CAPABILITY, BR_ASSIGNMENT, BR_AUDIT) within 60s of the redeploy — JetStream provisioning did not complete"
echo "  all 6 JetStream streams recreated"

# Capabilities announced — same log-substring approach, same remote-side
# `|| true`, same bounded retry; a timeout here is now a real finding (die),
# not narrated as "may still be discovering".
step "checking hardware-io capability announcements"
CAP_DEADLINE=$(($(date +%s) + 60))
announced=0
while :; do
    announced="$(ssh "$PI_HOST" "docker logs bellasreef-hardware-io-1 2>&1 | grep -c '\"msg\":\"capability announced\"' || true" 2>/dev/null)"
    announced="${announced:-0}"
    [[ "$announced" -ge 1 ]] && break
    [[ "$(date +%s)" -ge $CAP_DEADLINE ]] && break
    sleep 3
done
[[ "$announced" -ge 1 ]] \
    || die "hardware-io logged no capability announcements within 60s of the redeploy — discovery did not run or found nothing"
echo "  hardware-io announced ${announced} capability line(s)"

# --------------------------------------------------- 6. mint the final code
# NOT a reprint. `bellasreef setup-code` ROTATES: it mints a new code and
# stores only the hash. Reprinting is impossible by design — there is no
# plaintext anywhere to reprint from (security.py, hash_setup_code).
#
# Step 4 passed deploy-pi.sh --no-setup-code specifically so that this is the
# ONLY code this run prints — no earlier one exists to be rotated out or
# scrolled past. This is the last thing on screen, and now it is also the
# first and only code shown.
step "minting the final setup code"
ssh "$PI_HOST" "cd ${DEPLOY_DIR} && docker compose -f ${COMPOSE_FILE} --env-file ${COMPOSE_ENV} exec -T api bellasreef setup-code" \
    || warn "could not mint the final setup code on ${PI_HOST} — mint one with 'docker compose exec -T api bellasreef setup-code'"
echo "Use the code directly above."
echo "Reminder: adopt devices in the app before the deploy telemetry gate can pass again."
