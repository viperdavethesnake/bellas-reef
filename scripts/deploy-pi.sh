#!/usr/bin/env bash
# Deploy to the hub. The only supported way to change what the Pi runs.
#
# The rule this enforces (CLAUDE.md, "Deployment discipline"): the Pi runs
# supervised containers, from pushed commits, only. No rsync, no editing on
# the box, no dev launchers, nothing uncommitted. If it is not pushed and
# built by CI, it does not run.
#
# Containers-only topology: hardware-io, control-engine and api are images
# CI publishes to ghcr.io, tagged with the commit SHA. This script pulls that
# tag, records the manifest digest it resolved to, migrates, and recreates
# the three app containers — it never builds on the Pi and never touches the
# spine's data services (postgres, nats, victoria-metrics keep
# restart: unless-stopped and are only ever started, never recreated, by a
# deploy).
#
# The last step is the one that matters. A deploy that checks "are the
# containers up" proves almost nothing here: hardware-io without
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
DEPLOY_DIR="${PI_DIR}/deploy"
COMPOSE_FILE="${DEPLOY_DIR}/compose.yaml"
COMPOSE_ENV="${DEPLOY_DIR}/.env"
API_PORT=8000
VERIFY=1
SKIP_CI_CHECK=0
SERVICES=(hardware-io control-engine api)
IMAGE_PREFIX="ghcr.io/viperdavethesnake/bellasreef"
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

# A compose invocation against the Pi's project, with an explicit -f/--env-file
# rather than relying on cwd — matches how bellasreef.service itself invokes
# compose, and works the same whether or not the ssh session's default
# directory is the clone.
#
# BELLASREEF_TAG is exported into every call, not just read from
# deploy/.env: compose prefers a shell-environment value over the .env file,
# so this is what makes `pull` and the migration `run` resolve the image
# refs to the SHA being deployed right now, before the .env rewrite step
# further down persists that same tag for the next boot. Without this, pull
# and migrate would silently target whatever tag the *previous* deploy left
# in .env.
compose() {
    ssh "$PI_HOST" "BELLASREEF_TAG=${SHA} docker compose -f ${COMPOSE_FILE} --env-file ${COMPOSE_ENV} $*"
}

# New-owner bootstrap (spec 2026-08-15): if nobody has ever paired, every
# deploy rotates and prints the setup code as its final output — harmless
# before the first pair, impossible after it. Reuses the same direct-curl
# mechanism the auth-leg check below uses (PI_HOST:API_PORT, not an
# ssh+localhost hop), so there is exactly one way this script asks the hub
# about itself. Retried briefly rather than asked once: called from the
# --no-verify path too (factory-reset-pi.sh drives that one), right after
# `up -d --wait`, before uvicorn has necessarily accepted its first
# connection — api has no compose healthcheck for --wait to key off.
print_setup_code_if_open() {
    local deadline body setup_mode
    deadline=$(($(date +%s) + 20))
    body=""
    while :; do
        body="$(curl -sS --max-time 10 "http://${PI_HOST}:${API_PORT}/api/v1/info" 2>/dev/null || true)"
        [[ -n "$body" ]] && break
        [[ "$(date +%s)" -ge $deadline ]] && break
        sleep 2
    done
    # [a-z]* rather than \(true\|false\): \| alternation is a GNU sed
    # extension. This runs on this Mac's BSD /usr/bin/sed, where \| is
    # literal and the GNU form matches nothing — verified against both
    # true and false bodies (see the fix report in the sdd task file).
    setup_mode="$(sed -n 's/.*"setup_mode":\([a-z]*\).*/\1/p' <<<"$body")"
    if [[ "$setup_mode" == "true" ]]; then
        echo
        compose "exec -T api bellasreef setup-code" \
            || warn "could not read the setup code from ${PI_HOST} — check manually with 'docker compose exec -T api bellasreef setup-code'"
    fi
}

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
# It is also, now, the thing that proves the images this script is about to
# pull actually exist: the publish job only runs on a green push to main.
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

step "checking the ghcr.io pull credential"
# The credential is host state (CLAUDE.md, Deployment discipline) — this
# script never supplies one. The check is a heuristic (it greps the default
# config.json auths map, which is where a plain `docker login` without a
# credential helper stores the token) rather than a guarantee: a Pi configured
# with a credsStore would fail this grep even when logged in. Either way the
# real answer comes from the pull attempt below, which is where the actual
# failure message lives.
if ! ssh "$PI_HOST" "grep -q '\"ghcr.io\"' ~/.docker/config.json 2>/dev/null"; then
    warn "no ghcr.io entry found in ~/.docker/config.json — if 'docker login ghcr.io' hasn't been run on ${PI_HOST}, the pull below will fail"
fi

step "pulling images tagged ${SHA:0:8}"
compose "pull ${SERVICES[*]}" || die "image pull failed on ${PI_HOST}. If this is an auth error: the pull credential is host state — run 'docker login ghcr.io' on ${PI_HOST} first (a PAT with read:packages scope, or 'gh auth token' from a machine authorized for this repo's packages if its scopes include read:packages). Otherwise check that CI actually published ${SHA:0:8} — see the publish job in .github/workflows/ci.yaml."

step "recording pulled image digests"
# Every spine image in compose.yaml is pinned by manifest digest already; this
# is the same audit trail for the app images CI just published, read back from
# the registry rather than trusted from a tag.
for svc in "${SERVICES[@]}"; do
    digest="$(ssh "$PI_HOST" "docker inspect --format '{{index .RepoDigests 0}}' ${IMAGE_PREFIX}-${svc}:${SHA} 2>/dev/null")"
    # A missing digest means the pull did not produce an inspectable image —
    # fatal, not a line item, for a trail whose entire point is pinning by
    # digest rather than trusting a tag.
    [[ -n "$digest" ]] || die "no digest found for ${IMAGE_PREFIX}-${svc}:${SHA} on ${PI_HOST}. The pull did not produce an inspectable image, so there is nothing to pin — check the pull step's output above."
    printf '  %-16s %s\n' "$svc" "$digest"
done

step "applying migrations"
# Containerized equivalent of the old venv invocation
# (cd db && uv run alembic upgrade head): the api image ships alembic and the
# db package at /app/db, alembic.ini's script_location is relative, so the
# working directory has to move with it. BELLASREEF_DATABASE_URL is already in
# the api service's compose environment (from deploy/.env), so there is
# nothing extra to source here.
#
# Before the app containers restart, same as before — the new code must never
# meet the old schema. `docker compose run` starts any unmet depends_on
# (nats, postgres) on its own, so this also brings the spine up on a Pi where
# it was not already running.
#
# Not routed through compose(): `| tail -5` for readable output would
# otherwise swallow alembic's real exit status behind tail's, so `|| die`
# could never fire and a migration failure would silently let the deploy
# continue into new code against an old schema. `set -o pipefail` on the
# remote shell is what makes the pipeline's exit status alembic's again.
ssh "$PI_HOST" "set -o pipefail && BELLASREEF_TAG=${SHA} docker compose -f ${COMPOSE_FILE} --env-file ${COMPOSE_ENV} run --rm api sh -c 'cd /app/db && alembic upgrade head' 2>&1 | tail -5" \
    || die "alembic upgrade failed on ${PI_HOST}"

step "pointing the boot unit at ${SHA:0:8}"
# Written to deploy/.env, not exported ad hoc: bellasreef.service has no shell
# around it to set BELLASREEF_TAG, so the tag a reboot picks up is whatever
# this line says. sed -i on a line that already matches the key; appended only
# if the key is missing, never duplicated.
ssh "$PI_HOST" "grep -q '^BELLASREEF_TAG=' ${COMPOSE_ENV} && sed -i 's/^BELLASREEF_TAG=.*/BELLASREEF_TAG=${SHA}/' ${COMPOSE_ENV} || echo 'BELLASREEF_TAG=${SHA}' >> ${COMPOSE_ENV}" \
    || die "could not update BELLASREEF_TAG in ${COMPOSE_ENV}"

step "installing unit files"
# Globs deploy/systemd/*.service, which today is bellasreef.service alone —
# the app units it once installed alongside the spine unit are deleted from
# the repo, so there is nothing left to install beyond it.
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

step "stopping any legacy host units"
# The first cutover to containers can find the old host units still active:
# the host api holds 0.0.0.0:8000 and the host hardware-io holds the BR_CMD
# durable (a JetStream workqueue permits exactly one consumer per filter
# subject), so the container versions of both collide with their host
# counterparts by construction if this isn't done first. Idempotent —
# `2>/dev/null || true` makes "unit not found" harmless once the units no
# longer exist on a host — and kept here for the cutover plus any straggler
# host that never got the memo.
ssh "$PI_HOST" "sudo systemctl stop bellasreef-hardware-io bellasreef-control-engine bellasreef-api 2>/dev/null || true"

step "starting the app containers"
# Data services are untouched unless their pinned digests changed: `up -d`
# only recreates a service whose definition actually changed, and the app
# images are the only thing this deploy pulled.
compose "up -d --wait ${SERVICES[*]}" || die "compose up failed on ${PI_HOST}"

step "enabling the boot unit"
ssh "$PI_HOST" "sudo systemctl enable bellasreef.service 2>&1 | tail -1" || die "could not enable bellasreef.service"

step "starting the boot unit"
# Started, never restarted: see bellasreef.service's own comment. `start` on
# an already-active oneshot with RemainAfterExit is a no-op — this line exists
# for boot persistence, not to reconcile a deploy that already ran the
# targeted `up -d --wait` above.
ssh "$PI_HOST" "sudo systemctl start bellasreef.service" || die "bellasreef.service failed to start"

step "container status"
for svc in "${SERVICES[@]}"; do
    cid="$(ssh "$PI_HOST" "docker compose -f ${COMPOSE_FILE} --env-file ${COMPOSE_ENV} ps -q ${svc}" 2>/dev/null)"
    state=""
    [[ -n "$cid" ]] && state="$(ssh "$PI_HOST" "docker inspect -f '{{.State.Status}}' ${cid}" 2>/dev/null)"
    printf '  %-16s %s\n' "$svc" "${state:-not running}"
    [[ "$state" == "running" ]] || {
        compose "logs --tail=30 ${svc}"
        die "${svc} is ${state:-not running}"
    }
done

# ----------------------------------------------------------------- verification

if [[ $VERIFY -eq 0 ]]; then
    printf '\033[33mdeploy: skipping wire verification at your request\033[0m\n'
    print_setup_code_if_open
    exit 0
fi

step "checking the API answers and speaks the contract we just shipped"
# The other half of "verified on the wire". Everything below this comment used
# to be missing, which meant a deploy could prove the tank was monitored and
# never prove a single person could log in — discovery or pairing broken on the
# hub, three containers active, green banner.
#
# /api/v1/info is the right probe for exactly the reason it is unauthenticated:
# it is the first request any client makes, before pairing, before a credential
# exists. If this does not answer, nobody is getting in, and the contract
# version it reports is what a client compares against before it will speak.
#
# Retried briefly rather than asked once: uvicorn has just come up in a fresh
# container, and a connection refused two seconds after `up -d` means nothing.
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
    compose "logs --tail=40 api"
    die "GET /api/v1/info on ${PI_HOST}:${API_PORT} answered '${info_code:-nothing}' after 30s. The container is running and the front door is shut — no client can discover this hub, pair with it, or refresh a token."
fi

served_contracts="$(sed -n 's/.*"contracts_version":"\([^"]*\)".*/\1/p' <<<"$info_body")"
if [[ "$served_contracts" != "$CONTRACTS" ]]; then
    die "the API reports contracts_version='${served_contracts:-<absent>}' and this build ships ${CONTRACTS}. The container started into code that is not what was just deployed, or /info is not the endpoint this reply came from — either way clients are being told the wrong contract."
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
# Probed over ssh against localhost, not curled from the dev machine: the
# compose stack binds VictoriaMetrics to 127.0.0.1 on the Pi (see
# deploy/compose.yaml), so a probe from off-box would get connection-refused
# while telemetry is perfectly healthy. Same shape as the `compose logs` calls
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
    compose "logs --tail=40 hardware-io"
    die "no sensor sample reached VictoriaMetrics within 90s. The containers are up and the tank is not being monitored — this is exactly the state a process check would have called a successful deploy."
fi

latest="$(sed -n 's/.*"values":\[\([^,]*\).*/\1/p' <<<"$body" | tail -1)"

printf '\033[32m✓ deployed %s — API answering at contracts %s, fresh sample on the wire (%s)\033[0m\n' \
    "${SHA:0:8}" "$CONTRACTS" "${latest:-ok}"

# Spec: "the setup code ... as the final output of the deploy." Truly last,
# below the banner above — not before it.
print_setup_code_if_open
