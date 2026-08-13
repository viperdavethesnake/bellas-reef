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
# There are two ends to that, and for a long time this script only checked one.
# Telemetry proved the tank was being watched and proved nothing about anyone
# being able to look: no request ever reached the API, so discovery or pairing
# could have been broken on the hub through any number of green deploys. The
# second verification leg below closes that.
#
# Usage:  ./scripts/deploy-pi.sh [--host H] [--no-verify] [--no-verify-ci]

set -uo pipefail

PI_HOST="${BELLASREEF_PI_HOST:-bellasreef.local}"
PI_DIR="/home/david/bellasreef"
API_PORT=8000
VERIFY=1
SKIP_CI_CHECK=0
UNITS=(bellasreef-hardware-io bellasreef-control-engine bellasreef-api)
AVAHI_RECORD="deploy/avahi/bellasreef.service"
AVAHI_INSTALLED="/etc/avahi/services/bellasreef.service"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) PI_HOST="$2"; shift 2 ;;
        --no-verify) VERIFY=0; shift ;;
        --no-verify-ci) SKIP_CI_CHECK=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

cd "$(git rev-parse --show-toplevel)"

die() { printf '\033[31mdeploy: %s\033[0m\n' "$1" >&2; exit 1; }
step() { printf '\033[1m▶ %s\033[0m\n' "$1"; }
warn() { printf '\033[33m  %s\033[0m\n' "$1"; }

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

step "reading the contract version this commit ships"
# Derived exactly the way the API derives it — importlib.metadata against the
# installed bellasreef-contracts package — so the number checked against /info
# later is the number the deployed code should be reporting, not a literal
# somebody kept up to date by hand. That literal is precisely how the avahi TXT
# record ended up advertising 2.0.0 against a 3.3.0 package.
#
# Read from this workspace rather than from the Pi on purpose. Both resolve from
# the same uv.lock at the same commit, so they agree — and asking the Pi would
# compare the hub against itself, which is exactly the tautology that lets a
# unit that never restarted look correct.
CONTRACTS="$(uv run python -c 'from importlib.metadata import version
print(version("bellasreef-contracts"))' 2>/dev/null)"
[[ -n "$CONTRACTS" ]] || die "could not read the bellasreef-contracts version from this workspace. Run 'uv sync' — the deploy has no way to tell what contract the hub is supposed to be serving without it."
echo "  contracts ${CONTRACTS}"

step "checking CI is green for this commit"
# The stop condition is CI green AND deployed. Without this check the script
# shipped a red commit to the tank — which it did, once, minutes after the rule
# was written down. Reading a CI result and acting on it are two different
# things; an exit code is the control.
#
# A missing or unreachable answer warns rather than blocks: GitHub being down
# must not stop a deploy during an incident, but it must say so out loud.
if [[ $SKIP_CI_CHECK -eq 1 ]]; then
    printf '\033[33m  skipped at your request\033[0m\n'
elif ! command -v gh >/dev/null 2>&1; then
    printf '\033[33m  gh not available — deploying without checking CI\033[0m\n'
else
    conclusion="$(gh run list --commit "$SHA" --limit 1 --json conclusion -q '.[0].conclusion' 2>/dev/null || true)"
    case "$conclusion" in
        success)
            echo "  CI success"
            ;;
        "")
            printf '\033[33m  no CI run found for %s yet — deploying unverified\033[0m\n' "${SHA:0:8}"
            ;;
        *)
            die "CI for ${SHA:0:8} is '${conclusion}'. Fix it before it reaches the tank, or pass --no-verify-ci to ship a known-red build deliberately."
            ;;
    esac
fi

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

step "installing the discovery record"
# Step 1 of every client's journey is a Bonjour browse for _bellasreef._tcp, and
# the TXT record tells the client what contract version this hub speaks. That
# record was hardcoded in the repo, three minor versions stale, installed by a
# `cp` in a setup document, and never touched by a deploy again — derived from
# nothing, verified by nothing, and the first thing a phone reads.
#
# So the version is substituted here from the package rather than trusted from
# the file, and the file is reinstalled on every deploy like any other artefact.
# Rendered locally and piped: no half-written file on the host, and nothing left
# in /tmp to be read by the next thing that looks there.
sed -E "s|(<txt-record>contracts=)[^<]*|\1${CONTRACTS}|" "$AVAHI_RECORD" \
    | ssh "$PI_HOST" "sudo mkdir -p $(dirname "$AVAHI_INSTALLED") && sudo tee ${AVAHI_INSTALLED} >/dev/null && sudo chmod 0644 ${AVAHI_INSTALLED}" \
    || die "could not install the avahi service record on ${PI_HOST}"

if ! ssh "$PI_HOST" "sudo systemctl reload avahi-daemon" 2>/dev/null; then
    warn "avahi-daemon would not reload — the record is installed but not being advertised yet"
fi

advertised="$(ssh "$PI_HOST" "sed -n 's|.*<txt-record>contracts=\\([^<]*\\)</txt-record>.*|\\1|p' ${AVAHI_INSTALLED}" 2>/dev/null)"
[[ "$advertised" == "$CONTRACTS" ]] \
    || die "the hub advertises contracts=${advertised:-<none>} but this build ships ${CONTRACTS}. A client reads that record to decide whether it can talk to this hub at all."
echo "  advertising contracts=${advertised}"

step "enabling services at boot"
# Idempotent, and separate from restart on purpose: `systemctl restart` on a
# disabled unit starts it now and forgets it at the next power cut, which on a
# tank is the outage you find out about from a thermometer.
ssh "$PI_HOST" "sudo systemctl enable ${UNITS[*]} bellasreef-spine.service 2>&1 | tail -1" || die "could not enable units"

step "starting the spine"
# The spine is started, never restarted, by a deploy: restarting it would
# bounce Postgres and NATS under every code push for no reason. `start` on an
# already-active oneshot with RemainAfterExit is a no-op.
ssh "$PI_HOST" "sudo systemctl start bellasreef-spine.service" || die "spine failed to start"

step "applying migrations"
# Before the restart, so the new code never meets the old schema. Caught the
# hard way: a deploy that shipped code reading `sensor_alerts.alert_class` to a
# database that had never heard of it, and control-engine crash-looped on boot.
#
# Migrations here are additive by policy, so the *old* code running during this
# window is fine. Note that the restore flow deliberately does NOT do this —
# see docs/backup-restore.md, where migrating before a restore is exactly the
# mistake that gets an archive refused.
#
# Run after the spine starts, not before: the spine brings up Postgres, and a
# fresh checkout has no database to migrate until it does.
ssh "$PI_HOST" "cd ${PI_DIR}/db && set -a && . /etc/bellasreef/api.env && set +a && uv run alembic upgrade head 2>&1 | tail -5" \
    || die "alembic upgrade failed on ${PI_HOST}"

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

step "checking the API answers and speaks the contract we just shipped"
# The other half of "verified on the wire". Everything below this comment used
# to be missing, which meant a deploy could prove the tank was monitored and
# never prove a single person could log in — discovery or pairing broken on the
# hub, three units active, green banner.
#
# /api/v1/info is the right probe for exactly the reason it is unauthenticated:
# it is the first request any client makes, before pairing, before a credential
# exists. If this does not answer, nobody is getting in, and the contract
# version it reports is what a client compares against before it will speak.
#
# Retried briefly rather than asked once: uvicorn has just been restarted, and a
# connection refused two seconds after `systemctl restart` means nothing.
AUTH_DEADLINE=$(($(date +%s) + 30))
info_body=""
info_code=""
while :; do
    probe="$(curl -sS --max-time 10 -w $'\n%{http_code}' \
        "http://${PI_HOST}:${API_PORT}/api/v1/info" 2>/dev/null || true)"
    info_code="$(tail -n1 <<<"$probe")"
    info_body="$(sed '$d' <<<"$probe")"
    [[ "$info_code" == "200" ]] && break
    [[ "$(date +%s)" -ge $AUTH_DEADLINE ]] && break
    sleep 3
done

if [[ "$info_code" != "200" ]]; then
    ssh "$PI_HOST" "sudo journalctl -u bellasreef-api -n 40 --no-pager"
    die "GET /api/v1/info on ${PI_HOST}:${API_PORT} answered '${info_code:-nothing}' after 30s. The unit is active and the front door is shut — no client can discover this hub, pair with it, or refresh a token."
fi

served_contracts="$(sed -n 's/.*"contracts_version":"\([^"]*\)".*/\1/p' <<<"$info_body")"
if [[ "$served_contracts" != "$CONTRACTS" ]]; then
    die "the API reports contracts_version='${served_contracts:-<absent>}' and this build ships ${CONTRACTS}. The unit restarted into code that is not what was just deployed, or /info is not the endpoint this reply came from — either way clients are being told the wrong contract."
fi

paired="$(sed -n 's/.*"paired_client_count":\([0-9]*\).*/\1/p' <<<"$info_body")"
echo "  /info 200 · contracts ${served_contracts} · ${paired:-?} paired client(s)"
[[ "${paired:-1}" == "0" ]] && warn "no client has ever paired — the hub is open for trust-on-first-use"

step "waiting for a fresh sample on the wire"
# Not a process check and not the metrics endpoint. A reading that reaches
# VictoriaMetrics has traversed driver -> NATS -> telemetry writer -> VM, which
# is the whole path a dark tank would break somewhere in.
#
# /api/v1/export rather than /api/v1/query: VictoriaMetrics hides the newest
# ~30s from instant queries (-search.latencyOffset), so a fresh write looks
# like a failed one through /query.
#
# Probed over ssh against localhost, not curled from the dev machine: this
# branch's compose cutover binds VictoriaMetrics to 127.0.0.1 on the Pi (see
# deploy/compose.yaml), so a probe from off-box would get connection-refused
# while telemetry is perfectly healthy. Same shape as the journalctl calls
# elsewhere in this script — run the check where the loopback port actually is.
START="$(date +%s)"
DEADLINE=$((START + 90))
FOUND=0
while [[ "$(date +%s)" -lt $DEADLINE ]]; do
    now="$(date +%s)"
    body="$(ssh "$PI_HOST" "curl -fsS --max-time 10 'http://localhost:8428/api/v1/export?match[]=bellasreef_sensor_reading&start=${START}&end=${now}'" 2>/dev/null)"
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
printf '\033[32m✓ deployed %s — API answering at contracts %s, fresh sample on the wire (%s)\033[0m\n' \
    "${SHA:0:8}" "$CONTRACTS" "${latest:-ok}"
