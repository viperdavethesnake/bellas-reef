#!/usr/bin/env bash
# Deploy to the hub. The only supported way to change what the Pi runs.
#
# The rule this enforces (CLAUDE.md, "Deployment discipline"): the Pi runs
# supervised systemd units, from pushed commits, only. No rsync, no editing on
# the box, no dev launchers, nothing uncommitted. If it is not pushed, it does
# not run.
#
# The last step is the one that matters. A deploy that checks "are the
# processes up" proves almost nothing here: hardware-io without
# BELLASREEF_NATS_URL starts cleanly, serves metrics, logs a healthy startup
# and publishes absolutely nothing. So this script does not report success
# until it has seen a *fresh sample land in VictoriaMetrics* — the wire, not
# the gauge.
#
# Usage:  ./scripts/deploy-pi.sh [--host bellasreef.local] [--no-verify]

set -uo pipefail

PI_HOST="${BELLASREEF_PI_HOST:-bellasreef.local}"
PI_DIR="/home/david/bellasreef"
VERIFY=1
UNITS=(bellasreef-hardware-io bellasreef-control-engine bellasreef-api)

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) PI_HOST="$2"; shift 2 ;;
        --no-verify) VERIFY=0; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

cd "$(git rev-parse --show-toplevel)"

die() { printf '\033[31mdeploy: %s\033[0m\n' "$1" >&2; exit 1; }
step() { printf '\033[1m▶ %s\033[0m\n' "$1"; }

# ---------------------------------------------------------------- preconditions

step "checking the tree is deployable"
[[ -z "$(git status --porcelain)" ]] || die "working tree is dirty. Commit or stash first — the Pi runs pushed commits, so anything uncommitted would silently not be deployed."

SHA="$(git rev-parse HEAD)"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"

git fetch -q origin || die "could not reach origin"
if ! git merge-base --is-ancestor "$SHA" "origin/${BRANCH}" 2>/dev/null; then
    die "HEAD (${SHA:0:8}) is not on origin/${BRANCH}. Push first."
fi
echo "  ${SHA:0:8} on origin/${BRANCH}"

# ---------------------------------------------------------------------- deploy

step "resetting ${PI_HOST}:${PI_DIR} to ${SHA:0:8}"
ssh "$PI_HOST" "cd ${PI_DIR} && git fetch -q origin && git reset -q --hard ${SHA} && git clean -qfd -e .venv -e deploy/.env && git log --oneline -1" \
    || die "could not update the checkout on ${PI_HOST}"

step "syncing dependencies"
ssh "$PI_HOST" "cd ${PI_DIR} && uv sync --frozen 2>&1 | tail -3" \
    || die "uv sync failed on ${PI_HOST}"

step "installing unit files"
ssh "$PI_HOST" "sudo install -m 0644 ${PI_DIR}/deploy/systemd/*.service /etc/systemd/system/ && sudo systemctl daemon-reload" \
    || die "could not install units"

step "restarting services"
# Restarted oldest-dependency-first. hardware-io provisions the streams the
# others bind to, and the API's registry consumer retries until the stream
# exists, so this ordering is a courtesy rather than a requirement.
ssh "$PI_HOST" "sudo systemctl restart ${UNITS[*]}" || die "restart failed"

sleep 3
step "unit status"
for unit in "${UNITS[@]}"; do
    state="$(ssh "$PI_HOST" "systemctl is-active ${unit}" 2>/dev/null)"
    printf '  %-32s %s\n' "$unit" "$state"
    [[ "$state" == "active" ]] || {
        ssh "$PI_HOST" "sudo journalctl -u ${unit} -n 30 --no-pager"
        die "${unit} is ${state}"
    }
done

# ----------------------------------------------------------------- verification

if [[ $VERIFY -eq 0 ]]; then
    printf '\033[33mdeploy: skipping wire verification at your request\033[0m\n'
    exit 0
fi

step "waiting for a fresh sample on the wire"
# Not a process check and not the metrics endpoint. A reading that reaches
# VictoriaMetrics has traversed driver -> NATS -> telemetry writer -> VM, which
# is the whole path a dark tank would break somewhere in.
#
# /api/v1/export rather than /api/v1/query: VictoriaMetrics hides the newest
# ~30s from instant queries (-search.latencyOffset), so a fresh write looks
# like a failed one through /query.
START="$(date +%s)"
DEADLINE=$((START + 90))
FOUND=0
while [[ "$(date +%s)" -lt $DEADLINE ]]; do
    now="$(date +%s)"
    body="$(curl -fsS --max-time 10 \
        "http://${PI_HOST}:8428/api/v1/export?match[]=bellasreef_sensor_reading&start=${START}&end=${now}" 2>/dev/null)"
    if [[ -n "$body" ]] && grep -q '"values"' <<<"$body"; then
        FOUND=1
        break
    fi
    sleep 5
done

if [[ $FOUND -eq 0 ]]; then
    ssh "$PI_HOST" "sudo journalctl -u bellasreef-hardware-io -n 40 --no-pager"
    die "no sensor sample reached VictoriaMetrics within 90s. The processes are up and the tank is not being monitored — this is exactly the state a process check would have called a successful deploy."
fi

latest="$(sed -n 's/.*"values":\[\([^,]*\).*/\1/p' <<<"$body" | tail -1)"
printf '\033[32m✓ deployed %s — fresh sample on the wire (%s)\033[0m\n' "${SHA:0:8}" "${latest:-ok}"
