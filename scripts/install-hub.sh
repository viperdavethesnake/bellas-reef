#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
#
# Bella's Reef — first-run install.
#
# Takes a machine with this repo cloned on it from bare to a running hub.
# Run it on the hub itself; there is no second machine.
#
# Six phases, in this order:
#   1. already deployed?      cheapest check, short-circuits everything
#   2. hard requirements      docker, clock, avahi — failure stops the run
#   3. hardware inventory     reported, never blocking
#   4. configuration          deploy/.env from the example
#   5. deploy                 pull, migrate, boot unit, up
#   6. verify and hand off    then print the setup code
#
# Testability: every filesystem read is prefixed with $IH_ROOT (empty in
# production) and every external tool is called by bare name, so the test
# suite can point the whole script at a fixture directory with stub
# executables on PATH. A literal /etc or /sys path in this file is a bug.
#
# SC2034 (appears unused): IH_DRY_RUN, IH_CHECK_ONLY, IH_YES and REPO_DIR are
# this skeleton's declared interface (see Task 1's Interfaces block) — the
# phases that read them land in later tasks of this plan. Disabled file-wide
# rather than scattered per-line so the flag block below stays exactly the
# shape later tasks extend.
# shellcheck disable=SC2034

set -uo pipefail

IH_ROOT="${IH_ROOT:-}"
IH_DRY_RUN=0
IH_CHECK_ONLY=0
IH_YES=0
IH_FAILURES=()
IH_UNVERIFIED=()

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ------------------------------------------------------------------ output

ih_step() { printf '\033[1m▶ %s\033[0m\n' "$1"; }
ih_pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
ih_warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }

ih_fail() {
    printf '  \033[31mFAIL\033[0m  %s\n' "$1"
    IH_FAILURES+=("$1")
}

# A check that could not run is not a check that passed. It is recorded
# separately and makes the overall result non-green, for the same reason
# conftest.py fails the gate on a skipped test.
ih_unverified() {
    printf '  \033[33mUNVERIFIED\033[0m  %s\n' "$1"
    IH_UNVERIFIED+=("$1")
}

ih_would() { printf '  \033[36mwould\033[0m  %s\n' "$1"; }

ih_usage() {
    cat <<'USAGE'
install-hub.sh — Bella's Reef first-run install

Run this on the hub itself, from a clone of this repo.

  --check-only   phases 1 to 3 only: already deployed, requirements, hardware
  --dry-run      report every action, mutate nothing
  --yes          accept every offered action without prompting
  --help         this text

Phases:
  1. already deployed?   exits if this machine is already a hub
  2. requirements        docker, clock, mDNS — a failure stops the run
  3. hardware            inventory of what this machine can control
  4. configuration       writes deploy/.env
  5. deploy              pulls images, migrates, installs the boot unit
  6. verify              proves it came up, prints the pairing setup code
USAGE
}

ih_parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --check-only) IH_CHECK_ONLY=1; shift ;;
            --dry-run)    IH_DRY_RUN=1; shift ;;
            --yes)        IH_YES=1; shift ;;
            --help|-h)    ih_usage; exit 0 ;;
            *)            printf 'install-hub: unknown option %s\n' "$1" >&2
                          ih_usage >&2
                          exit 2 ;;
        esac
    done
}

ih_main() {
    ih_parse_args "$@"
    ih_step "Bella's Reef first-run install"
    return 0
}

# Sourceable without executing, so tests can reach individual functions.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    ih_main "$@"
fi
