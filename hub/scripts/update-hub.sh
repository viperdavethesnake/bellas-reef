#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
#
# Bella's Reef — move this hub to a newer release. Runs on the hub, from the
# bellasreef-hub clone. NOT YET IMPLEMENTED (skeleton by ruling, 2026-08-30:
# install first). The design as far as it is decided, so it is not re-derived:
#
#   1. installed?     refuse a machine with no bellasreef.service / deploy/.env
#   2. release        default: newest stable v* tag (no -suffix); --pre allows
#                     the newest pre-release; --ref <tag> pins one.
#                     OPEN (David): may a plain run ever move to main? (no)
#   3. checkout       git fetch --tags; git checkout <tag>; then RE-EXEC this
#                     script with --stage2, because the file now running may
#                     have changed under bash.
#   4. backup         mandatory, same mechanism as factory-reset-hub.sh
#   5. deploy         docker compose pull → run --rm api alembic upgrade head
#                     → up -d --wait (app services only change) → rewrite
#                     BELLASREEF_TAG in deploy/.env from deploy/release.env
#   6. verify         THREE outcomes: PASS (fresh telemetry on the wire),
#                     NO DEVICES (registry empty; update complete; prints the
#                     import command; exit 0), FAIL (devices registered and
#                     nothing on the wire within the deadline).
set -uo pipefail

usage() {
    cat <<'USAGE'
update-hub.sh — move this hub to a newer release   (not yet implemented)

  --pre         allow the newest pre-release (vX.Y.Z-rc.N)
  --ref <tag>   pin a specific release tag
  --help        this text

Planned phases: 1 installed?  2 choose release  3 checkout and re-exec
                4 backup  5 pull, migrate, up  6 verify (PASS | NO DEVICES | FAIL)
USAGE
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
esac

printf 'update-hub: not implemented yet — see the header of this file for the design.\n' >&2
exit 70
