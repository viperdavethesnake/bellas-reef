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
set -uo pipefail

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
    ssh "$PI_HOST" "docker compose -f ${COMPOSE_FILE} --env-file ${COMPOSE_ENV} $*"
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
# hit this). deploy-pi.sh's own --no-verify path prints the setup code
# itself (Feature 1) once containers are up; step 6 below reprints it so it
# cannot scroll off screen behind steps 5's checks.
step "redeploying from zero"
"$(dirname "$0")/deploy-pi.sh" --host "$PI_HOST" --no-verify \
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
setup_mode="$(sed -n 's/.*"setup_mode":\(true\|false\).*/\1/p' <<<"$info_body")"
[[ "$paired" == "0" ]] \
    || die "post-reset /info reports paired_client_count=${paired:-<absent>} — expected 0. The reset did not clear pairings."
[[ "$setup_mode" == "true" ]] \
    || die "post-reset /info reports setup_mode=${setup_mode:-<absent>} — expected true. The reset did not reopen setup mode."
echo "  0 paired clients, setup mode open"

# devices/audit_log counts: diagnostic, not gating — the destructive action
# and the redeploy have already both succeeded by this point. -U/-d
# reference $POSTGRES_USER/$POSTGRES_DB inside the postgres container's own
# shell (single-quoted through both the ssh hop and docker compose exec, so
# neither this script nor the Pi's login shell ever needs to know the real
# values — the container already has them from deploy/.env via compose's
# environment: block).
step "confirming devices and audit log are empty"
ssh "$PI_HOST" "cd ${DEPLOY_DIR} && docker compose -f ${COMPOSE_FILE} --env-file ${COMPOSE_ENV} exec -T postgres sh -c 'psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -tAc \"SELECT count(*) FROM devices; SELECT count(*) FROM audit_log;\"'" \
    || warn "could not confirm devices/audit_log counts on ${PI_HOST} — check manually with 'docker compose exec -T postgres psql -U \$POSTGRES_USER -d \$POSTGRES_DB -c \"SELECT count(*) FROM devices;\"'"

step "checking hardware-io capability announcements"
announced="$(ssh "$PI_HOST" "docker logs bellasreef-hardware-io-1 2>&1 | grep -c 'capability announced'" 2>/dev/null || echo 0)"
echo "  hardware-io has announced ${announced} capability line(s) so far (may still be discovering)"

# ------------------------------------------------------- 6. setup code, again
# deploy-pi.sh already printed the setup code once containers came up
# (Feature 1, --no-verify path); step 5's checks above put several more
# screens of output after it, so reprint here to make it the actual last
# thing on screen.
step "setup code"
ssh "$PI_HOST" "cd ${DEPLOY_DIR} && docker compose -f ${COMPOSE_FILE} --env-file ${COMPOSE_ENV} exec -T api bellasreef setup-code" \
    || warn "could not reprint the setup code from ${PI_HOST} — check manually with 'docker compose exec -T api bellasreef setup-code'"
echo "Reminder: adopt devices in the app before the deploy telemetry gate can pass again."
