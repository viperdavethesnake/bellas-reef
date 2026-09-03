#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
#
# Bella's Reef — move this hub to a newer release. Runs ON the hub, from the
# bellasreef-hub clone. Implements the design ruled 2026-08-30 (recorded here
# while this was a skeleton) plus the answered open question: a plain run
# NEVER moves to main — only v* tags are ever candidates.
#
#   1. installed?     refuse a machine with no bellasreef.service / deploy/.env
#   2. release        default: newest stable v* tag (no -suffix); --pre allows
#                     the newest pre-release; --ref <tag> pins one.
#   3. checkout       git fetch --tags; git checkout <tag>; then RE-EXEC this
#                     script with --stage2, because the file now running may
#                     have changed under bash.
#   4. backup         mandatory, same mechanism as factory-reset-hub.sh
#   5. deploy         docker compose pull → run --rm api alembic upgrade head
#                     → up -d --wait hardware-io control-engine api (app
#                     services only, named explicitly — the spine data
#                     services are digest-pinned and never touched by an
#                     update) → rewrite BELLASREEF_TAG in deploy/.env from
#                     deploy/release.env. The new tag rides as an environment
#                     override until the deploy succeeds, so a failed update
#                     leaves deploy/.env naming the release that was last
#                     known to run.
#   6. verify         THREE outcomes: PASS (fresh telemetry on the wire),
#                     NO DEVICES (registry empty; update complete; points at
#                     app adoption; exit 0), FAIL (devices registered and
#                     nothing on the wire within the deadline).
#
# IH_ROOT / IH_RELEASE_ENV / UH_*_SECS are test seams, empty/default in
# production — the same seams the other hub scripts use.
set -uo pipefail

usage() {
    cat <<'USAGE'
update-hub.sh — move this hub to a newer release

  --pre         allow the newest pre-release (vX.Y.Z-rc.N)
  --ref <tag>   pin a specific release tag
  --help        this text

Phases: 1 installed?  2 choose release  3 checkout and re-exec
        4 backup  5 pull, migrate, up  6 verify (PASS | NO DEVICES | FAIL)
USAGE
}

ALLOW_PRE=0
PIN_REF=""
STAGE2=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --pre) ALLOW_PRE=1; shift ;;
        --ref) PIN_REF="${2:-}"; [[ -n "$PIN_REF" ]] || { usage >&2; exit 1; }; shift 2 ;;
        --stage2) STAGE2="${2:-}"; [[ -n "$STAGE2" ]] || { usage >&2; exit 1; }; shift 2 ;;
        *) usage >&2; exit 1 ;;
    esac
done

IH_ROOT="${IH_ROOT:-}"
UH_TELEMETRY_DEADLINE_SECS="${UH_TELEMETRY_DEADLINE_SECS:-120}"
UH_POLL_SECS="${UH_POLL_SECS:-5}"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${REPO_DIR}/deploy/compose.yaml"
ENV_FILE="${IH_ROOT}${REPO_DIR}/deploy/.env"
UNIT="${IH_ROOT}/etc/systemd/system/bellasreef.service"
RELEASE_ENV="${IH_RELEASE_ENV:-${REPO_DIR}/deploy/release.env}"
VM_QUERY_URL="http://127.0.0.1:8428/api/v1/query"

die()  { printf '\033[31mupdate-hub: %s\033[0m\n' "$1" >&2; exit 1; }
step() { printf '\033[1m▶ %s\033[0m\n' "$1"; }
pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }

# </dev/null on every compose call, same reasoning as factory-reset-hub.sh:
# a docker exec that inherits stdin can eat input meant for this script.
compose() { docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@" </dev/null; }

# ------------------------------------------------------------ 1. installed?
[[ -f "$UNIT" && -s "$ENV_FILE" ]] \
    || die "nothing to update: no bellasreef.service or no deploy/.env on this machine — run scripts/install-hub.sh first"

if [[ -z "$STAGE2" ]]; then
    # -------------------------------------------------------- 2. release
    step "choosing a release"
    git -C "$REPO_DIR" fetch --tags --quiet || die "git fetch --tags failed; cannot see releases"

    current="$(git -C "$REPO_DIR" describe --tags --exact-match 2>/dev/null || true)"

    if [[ -n "$PIN_REF" ]]; then
        target="$PIN_REF"
        # No pipe here on purpose. This used to be
        # `git tag -l 'v*' | grep -qx "$target"`, which is a SIGPIPE trap
        # under `set -o pipefail`: grep -q exits the instant it matches, and
        # if git still had unread tag names queued in the pipe at that
        # moment (past the kernel pipe buffer, tens of KB on a clone with
        # many tags), git took SIGPIPE and pipefail reported THAT as the
        # pipeline's exit status, refusing a perfectly valid tag. Command
        # substitution below reads git's output to completion before
        # anything is matched, so there is no reader-exits-early race for
        # pipefail to catch.
        candidate_tags="$(git -C "$REPO_DIR" tag -l 'v*')" || die "git tag -l failed; cannot see releases"
        case $'\n'"$candidate_tags"$'\n' in
            *$'\n'"$target"$'\n'*) ;;
            *) die "--ref ${target} is not a v* release tag this clone can see" ;;
        esac
    else
        # Only v* tags are candidates, ever — a plain run never moves to
        # main (the header's open question, answered: no).
        tags="$(git -C "$REPO_DIR" tag -l 'v*' | sort -V)"
        if (( ! ALLOW_PRE )); then
            tags="$(grep -v -- '-' <<<"$tags" || true)"
        fi
        target="$(tail -1 <<<"$tags")"
        [[ -n "$target" ]] || die "no candidate release tags found (need a v* tag$( (( ALLOW_PRE )) || printf ' without a pre-release suffix'))"
    fi

    if [[ -n "$current" && "$current" == "$target" ]]; then
        # Same tag is not the same as deployed: "on the tag" is a property of
        # the checkout, "deployed" is deploy/.env naming the tag's image sha.
        # The first run of this script on any hub arrives exactly this way —
        # the older release shipped only the skeleton, so the operator
        # checks the new tag out by hand and THEN runs this; exiting 0 here
        # would leave the old images running while everything says updated.
        deployed="$(sed -n 's/^BELLASREEF_TAG=//p' "$ENV_FILE" | head -1)"
        manifest="$(sed -n 's/^BELLASREEF_TAG=//p' "$RELEASE_ENV" 2>/dev/null | head -1)"
        if [[ -n "$manifest" && "$deployed" == "$manifest" ]]; then
            pass "already on ${target} and its images are deployed; nothing to update"
            exit 0
        fi
        # Fall through: the checkout below is a no-op at the same tag, and
        # the re-exec lands in stage 2, which is the deploy this hub needs.
        step "checkout already at ${target}; deploy/.env is behind — deploying"
    fi
    # Never move backwards on a plain run: with only pre-releases shipped,
    # "newest stable" can be OLDER than what is running (v0.1.0 vs
    # v0.2.0-rc.4 — the real tag list the day this was written). A downgrade
    # is --ref territory, stated explicitly, never a default.
    #
    # The comparison is semver-aware, not bare `sort -V`: sort -V ranks
    # v0.2.0-rc.4 ABOVE v0.2.0, which would refuse the rc→final graduation
    # this project explicitly plans as an "update to something older". Bases
    # compare by version sort; on an equal base, the tag without a
    # pre-release suffix is the newer one.
    if [[ -z "$PIN_REF" && -n "$current" && "$current" != "$target" ]]; then
        current_base="${current%%-*}"
        target_base="${target%%-*}"
        current_is_newer=0
        if [[ "$current_base" == "$target_base" ]]; then
            # Equal base: final beats rc; two pre-releases fall back to sort -V.
            if [[ "$current" != *-* && "$target" == *-* ]]; then
                current_is_newer=1
            elif [[ "$current" == *-* && "$target" == *-* ]] \
                && [[ "$(printf '%s\n%s\n' "$current" "$target" | sort -V | tail -1)" == "$current" ]]; then
                current_is_newer=1
            fi
        elif [[ "$(printf '%s\n%s\n' "$current_base" "$target_base" | sort -V | tail -1)" == "$current_base" ]]; then
            current_is_newer=1
        fi
        if (( current_is_newer )); then
            pass "running ${current}, which is newer than the newest candidate ${target}; nothing to update (use --ref to pin a specific release)"
            exit 0
        fi
    fi
    printf '  %s -> %s\n' "${current:-<untagged checkout>}" "$target"

    # -------------------------------------------- 3. checkout and re-exec
    step "checking out ${target}"
    git -C "$REPO_DIR" checkout --quiet "$target" || die "git checkout ${target} failed"
    # The file now running may have changed under bash; re-exec the new one.
    stage2_args=(--stage2 "$target")
    (( ALLOW_PRE )) && stage2_args+=(--pre)
    exec bash "${BASH_SOURCE[0]}" "${stage2_args[@]}"
fi

# ---------------------------------------------------------------- stage 2
target="$STAGE2"

new_tag="$(sed -n 's/^BELLASREEF_TAG=//p' "$RELEASE_ENV" 2>/dev/null | head -1)"
new_version="$(sed -n 's/^BELLASREEF_VERSION=//p' "$RELEASE_ENV" 2>/dev/null | head -1)"
[[ "$new_tag" =~ ^[0-9a-f]{40}$ ]] \
    || die "deploy/release.env is missing or its BELLASREEF_TAG is not a 40-hex image tag — this checkout (${target}) is not a released payload"

# ------------------------------------------------------------- 4. backup
STAMP="$(date +%Y%m%d-%H%M%S)"
backup_dir="$(sed -n 's/^BELLASREEF_BACKUP_DIR=//p' "$ENV_FILE" | head -1)"
BACKUP_NAME="bellasreef-pre-update-${STAMP}.tar.gz"
step "taking pre-update backup to ${backup_dir:-/backups}/${BACKUP_NAME}"
compose exec -T api bellasreef backup --out "/backups/${BACKUP_NAME}" \
    || die "backup failed; aborting with nothing deployed"

# ------------------------------------------------------------- 5. deploy
# BELLASREEF_TAG rides as an environment override (compose gives the OS
# environment precedence over --env-file) until the deploy succeeds; only
# then is deploy/.env rewritten, so a failed update leaves it naming the
# release that was last known to run.
step "pulling the ${target} app images"
BELLASREEF_TAG="$new_tag" compose pull --quiet hardware-io control-engine api \
    || die "image pull failed — if this is an auth error, docker login ghcr.io and re-run"

step "migrating the database"
BELLASREEF_TAG="$new_tag" compose run --rm api sh -c 'cd /app/db && alembic upgrade head' \
    || die "migrations failed; the running stack was not touched — the previous release is still up"

step "recreating the app services (spine data services are never touched)"
BELLASREEF_TAG="$new_tag" compose up -d --wait hardware-io control-engine api \
    || die "compose up did not converge; check 'docker compose ps' — the data services are untouched"

step "recording ${target} in deploy/.env"
if sed -i.update-bak -e "s|^BELLASREEF_TAG=.*|BELLASREEF_TAG=${new_tag}|" "$ENV_FILE"; then
    rm -f "${ENV_FILE}.update-bak"
else
    die "deployed, but could not rewrite BELLASREEF_TAG in deploy/.env — fix it by hand to ${new_tag} or the next 'systemctl start' runs the old release"
fi

# ------------------------------------------------------------- 6. verify
step "verifying (PASS | NO DEVICES | FAIL)"
# shellcheck disable=SC2016 # single-quoted on purpose: $POSTGRES_USER/$POSTGRES_DB
# expand inside the postgres container's own sh, not in this shell.
device_count="$(compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT count(*) FROM devices;"' 2>/dev/null | tr -d '[:space:]')"
[[ "$device_count" =~ ^[0-9]+$ ]] || die "could not count devices after the deploy — the stack is up but unverified; check 'docker compose ps' and the api logs"

if [[ "$device_count" == "0" ]]; then
    printf '  \033[33mNO DEVICES\033[0m  the registry is empty, so there is no telemetry to verify.\n'
    printf '      The update is complete (%s, images %s).\n' "${new_version:-$target}" "${new_tag:0:12}"
    printf '      Next: adopt hardware from the app (System > Hardware), or restore\n'
    printf '      many devices at once with bellasreef devices import (docs/host-setup.md\n'
    printf '      section 10).\n'
    exit 0
fi

deadline=$(( $(date +%s) + UH_TELEMETRY_DEADLINE_SECS ))
fresh=""
while :; do
    body="$(curl -fsS --max-time 10 --get "$VM_QUERY_URL" \
        --data-urlencode 'query=count({__name__=~"bellasreef.*"})' 2>/dev/null || true)"
    # An instant query only returns series with samples inside VM's staleness
    # window, so a non-empty result IS freshness — the same wire check the
    # deploy discipline names. Whitespace stripped first: JSON emitters
    # disagree about a space after the colon and the check must not.
    body="${body//[[:space:]]/}"
    if [[ "$body" == *'"result":[{'* ]]; then
        fresh="yes"
        break
    fi
    (( $(date +%s) >= deadline )) && break
    sleep "$UH_POLL_SECS"
done

if [[ -z "$fresh" ]]; then
    die "devices are registered (${device_count}) but no telemetry reached VictoriaMetrics within ${UH_TELEMETRY_DEADLINE_SECS}s — the update deployed but is NOT verified; check docker logs bellasreef-hardware-io-1"
fi

pass "telemetry on the wire; ${new_version:-$target} (images ${new_tag:0:12}) is live"
