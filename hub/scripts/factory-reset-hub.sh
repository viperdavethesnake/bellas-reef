#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
#
# Factory-reset this hub. Runs ON the hub, from the bellasreef-hub clone.
#
# Destroys: the docker volumes bellasreef_postgres-data, bellasreef_vm-data,
# bellasreef_nats-data — every pairing, every device, the audit log, all
# telemetry history. Keeps: the pre-reset backup (mandatory, taken first),
# this checkout, deploy/.env, the images, /boot/firmware.
#
# Sanctioned exception to "spine data services are never recreated" (see the
# hub docs): one deliberate, typed-confirmation wipe. Order matters: a
# stopped-but-not-removed container still pins its volume (measured
# 2026-08-14), so the unit stops and the stack comes down before the volumes
# will release. The redeploy goes through `systemctl start bellasreef.service`
# so a reset also proves the power-cut path.
#
# Usage: ./scripts/factory-reset-hub.sh            (prompts for the word)
#        echo factory-reset | ./scripts/factory-reset-hub.sh
#
# IH_ROOT / FR_*_SECS are test seams, empty/default in production.
set -uo pipefail

usage() {
    cat <<USAGE
Usage: ./scripts/factory-reset-hub.sh

Factory-resets THIS hub.

DESTROYS:
  - docker volumes: bellasreef_postgres-data bellasreef_vm-data bellasreef_nats-data
  - every pairing, every device, the audit log, ALL telemetry history

A pre-reset backup is mandatory and is taken first. Destruction additionally
requires typing 'factory-reset' at a prompt once the backup has completed.
Takes no other flags by design.

  -h, --help   print this message and exit
USAGE
}
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 1 ;;
    esac
done

IH_ROOT="${IH_ROOT:-}"
FR_API_DEADLINE_SECS="${FR_API_DEADLINE_SECS:-30}"
FR_STREAM_DEADLINE_SECS="${FR_STREAM_DEADLINE_SECS:-60}"
FR_POLL_SECS="${FR_POLL_SECS:-3}"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${REPO_DIR}/deploy/compose.yaml"
ENV_FILE="${IH_ROOT}${REPO_DIR}/deploy/.env"
UNIT="${IH_ROOT}/etc/systemd/system/bellasreef.service"
API_INFO_URL="http://127.0.0.1:8000/api/v1/info"
VOLUMES=(bellasreef_postgres-data bellasreef_vm-data bellasreef_nats-data)

die()  { printf '\033[31mfactory-reset: %s\033[0m\n' "$1" >&2; exit 1; }
step() { printf '\033[1m▶ %s\033[0m\n' "$1"; }
warn() { printf '\033[33m  %s\033[0m\n' "$1"; }

# </dev/null on every compose call: the confirmation prompt below is the only
# thing allowed to read stdin, and a docker exec that inherits it eats the
# word before the prompt sees it (the 2026-08-15 failure, over ssh then).
compose() { docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@" </dev/null; }

# ------------------------------------------------------------ 0. installed?
[[ -f "$UNIT" && -s "$ENV_FILE" ]] \
    || die "nothing to reset: no bellasreef.service or no deploy/.env on this machine — run scripts/install-hub.sh"
backup_dir="$(sed -n 's/^BELLASREEF_BACKUP_DIR=//p' "$ENV_FILE" | head -1)"

# ------------------------------------------------------------- 1. backup
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_NAME="bellasreef-pre-factory-${STAMP}.tar.gz"
step "taking pre-reset backup to ${backup_dir:-/backups}/${BACKUP_NAME}"
compose exec -T api bellasreef backup --out "/backups/${BACKUP_NAME}" \
    || die "backup failed; aborting with nothing touched"

# ------------------------------------------------------------- 2. consent
cat <<DOOM

About to DESTROY on this hub:
  - docker volumes: ${VOLUMES[*]}
  - every pairing, every device, the audit log, ALL telemetry history

Pre-reset backup: ${backup_dir:-/backups}/${BACKUP_NAME}
DOOM
read -r -p "Type 'factory-reset' to proceed: " confirm || confirm=""
[[ "$confirm" == "factory-reset" ]] || die "not confirmed; nothing touched"

# ------------------------------------------------------- 3. stop, down, wipe
step "stopping bellasreef.service"
sudo systemctl stop bellasreef.service || die "could not stop bellasreef.service; nothing removed"
step "bringing the stack down"
compose down || die "compose down failed; volumes not touched — check container state before retrying"
step "removing the three data volumes"
existing="$(docker volume ls -q 2>/dev/null)"
for v in "${VOLUMES[@]}"; do
    grep -qx "$v" <<<"$existing" || die "volume ${v} not found — a project-name mismatch, not a clean removal; check 'docker volume ls' before retrying"
done
docker volume rm "${VOLUMES[@]}" \
    || die "volume removal failed — the stack is down but one or more volumes may remain; check 'docker volume ls' before retrying"

# --------------------------------------------------------- 4. redeploy clean
step "migrating the empty database"
compose run --rm api sh -c 'cd /app/db && alembic upgrade head' \
    || die "migrations failed after the wipe. The hub has NO data volumes and is not running. Recover by hand: docker compose -f deploy/compose.yaml --env-file deploy/.env run --rm api sh -c 'cd /app/db && alembic upgrade head', then sudo systemctl start bellasreef.service"
step "starting bellasreef.service (the boot unit brings the stack up)"
sudo systemctl start bellasreef.service \
    || die "bellasreef.service did not start after the wipe. The hub is not confirmed running. Recover with: sudo systemctl start bellasreef.service (then re-run this script's checks by hand)"

# ------------------------------------------------------ 5. verify fresh state
step "verifying factory-fresh state"
deadline=$(( $(date +%s) + FR_API_DEADLINE_SECS ))
info=""
while :; do
    info="$(curl -fsS --max-time 10 "$API_INFO_URL" 2>/dev/null || true)"
    [[ -n "$info" ]] && break
    (( $(date +%s) >= deadline )) && break
    sleep "$FR_POLL_SECS"
done
[[ -n "$info" ]] || die "GET ${API_INFO_URL} did not answer within ${FR_API_DEADLINE_SECS}s — cannot confirm factory-fresh state"
paired="$(sed -n 's/.*"paired_client_count":\([0-9]*\).*/\1/p' <<<"$info")"
setup_mode="$(sed -n 's/.*"setup_mode":\([a-z]*\).*/\1/p' <<<"$info")"
[[ "$paired" == "0" ]] || die "post-reset /info reports paired_client_count=${paired:-<absent>} — expected 0; the reset did not clear pairings"
[[ "$setup_mode" == "true" ]] || die "post-reset /info reports setup_mode=${setup_mode:-<absent>} — expected true; setup mode did not reopen"
echo "  0 paired clients, setup mode open"

step "confirming devices and audit log are empty"
# shellcheck disable=SC2016 # single-quoted on purpose: $POSTGRES_USER/$POSTGRES_DB
# expand inside the postgres container's own sh, not in this shell.
if psql_out="$(compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT count(*) FROM devices; SELECT count(*) FROM audit_log;"' 2>&1)"; then
    device_count="$(sed -n '1p' <<<"$psql_out")"
    audit_count="$(sed -n '2p' <<<"$psql_out")"
    [[ "$device_count" == "0" ]] || die "the reset left ${device_count:-<unreadable>} device(s) behind"
    [[ "$audit_count" == "0" ]] || die "the reset left ${audit_count:-<unreadable>} audit_log row(s) behind"
    echo "  0 devices, 0 audit_log rows"
else
    die "could not query devices/audit_log after the redeploy (${psql_out}) — the wipe and redeploy ran, but this hub is NOT confirmed factory-fresh; check manually with: docker compose -f deploy/compose.yaml --env-file deploy/.env exec -T postgres psql -U \$POSTGRES_USER -d \$POSTGRES_DB -c 'SELECT count(*) FROM devices;'"
fi

step "confirming alembic is at head"
if alembic_out="$(compose exec -T api sh -c 'cd /app/db && alembic current' 2>&1)"; then
    [[ "$alembic_out" == *"(head)"* ]] || die "alembic current reports '${alembic_out}' — not at head after the redeploy"
    echo "  alembic at head"
else
    die "could not read the alembic revision after the redeploy (${alembic_out}) — not confirmed at head; check manually with: docker compose -f deploy/compose.yaml --env-file deploy/.env exec -T api sh -c 'cd /app/db && alembic current'"
fi

# Eight JetStream streams (BR_CMD, BR_STATE, BR_REGISTRY, BR_CAPABILITY,
# BR_CHIP, BR_HOST, BR_ASSIGNMENT, BR_AUDIT) are provisioned by hardware-io at
# start; against a wiped nats-data volume every one logs "stream created".
step "confirming all eight JetStream streams were recreated"
deadline=$(( $(date +%s) + FR_STREAM_DEADLINE_SECS ))
stream_count=0
while :; do
    stream_count="$(docker logs bellasreef-hardware-io-1 2>&1 | grep -c '"msg":"stream created"' || true)"
    stream_count="${stream_count:-0}"
    (( stream_count >= 8 )) && break
    (( $(date +%s) >= deadline )) && break
    sleep "$FR_POLL_SECS"
done
(( stream_count >= 8 )) || die "hardware-io logged ${stream_count} of 8 expected 'stream created' lines within ${FR_STREAM_DEADLINE_SECS}s — JetStream provisioning did not complete"
echo "  all 8 JetStream streams recreated"

# Watch item (2026-08-30): a hub with no hardware attached may legitimately
# announce nothing under the inventory-only ruling. Kept strict until coco
# shows otherwise; if it does, this becomes "announced N (0 is fine with no
# hardware)" rather than a die.
step "checking hardware-io capability announcements"
announced="$(docker logs bellasreef-hardware-io-1 2>&1 | grep -c '"msg":"capability announced"' || true)"
(( ${announced:-0} >= 1 )) || die "hardware-io logged no capability announcements — discovery did not run"
echo "  hardware-io announced ${announced} capability line(s)"

# --------------------------------------------------- 6. mint the final code
# `bellasreef setup-code` ROTATES (stores only a hash); this is the one code
# this run prints and the last thing on screen.
step "minting the setup code"
compose exec -T api bellasreef setup-code \
    || warn "could not mint the setup code — mint one with: docker compose -f deploy/compose.yaml --env-file deploy/.env exec -T api bellasreef setup-code"
echo "Use the code directly above. Then re-import devices (see scripts/install-hub.sh's hand-off)."
