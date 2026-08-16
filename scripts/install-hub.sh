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
# SC2034 (appears unused): IH_CHECK_ONLY is this skeleton's declared
# interface (see Task 1's Interfaces block) — the phase-gating that reads it
# lands in a later task of this plan. Disabled file-wide rather than
# scattered per-line so the flag block below stays exactly the shape later
# tasks extend.
# shellcheck disable=SC2034

set -uo pipefail

IH_ROOT="${IH_ROOT:-}"
IH_DRY_RUN=0
IH_CHECK_ONLY=0
IH_YES=0
IH_FAILURES=()
IH_UNVERIFIED=()

# A remediation step's own failure (the install ran and exited nonzero) is
# distinct from a check's failure and lives in its own array: phase 2's
# verification pass clears IH_FAILURES/IH_UNVERIFIED and re-derives them from
# a fresh run of the checks, but a failed installer isn't something a check
# can rediscover on its own, so it must survive that reset. IH_ASSUME_NO_TTY
# is a test-only seam (undocumented in --help on purpose) that forces
# ih_confirm's no-tty fail-closed path regardless of whether the process
# actually has a controlling terminal, so tests don't depend on how they
# happen to be launched.
IH_ACTION_FAILURES=()
IH_ASSUME_NO_TTY="${IH_ASSUME_NO_TTY:-0}"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ------------------------------------------------------------------ output

ih_step() { printf '\033[1m▶ %s\033[0m\n' "$1"; }
ih_pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
ih_warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }

ih_fail() {
    printf '  \033[31mFAIL\033[0m  %s\n' "$1"
    IH_FAILURES+=("$1")
}

# Same presentation as ih_fail, but for a remediation action's own failure
# (an install command that ran and exited nonzero) rather than a check's
# result. Kept in a separate array — see IH_ACTION_FAILURES above — so phase
# 2's verification-pass reset cannot silently erase it.
ih_action_fail() {
    printf '  \033[31mFAIL\033[0m  %s\n' "$1"
    IH_ACTION_FAILURES+=("$1")
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

# ------------------------------------------------------------------ phase 1

# Three independent signals, because a half-finished install leaves only some
# of them. Any one is enough to stop: this tool installs, it does not upgrade,
# repair, or reconfigure, and guessing which of those an operator meant is how
# a working hub gets damaged by a tool that was asked to help.
ih_phase1_already_deployed() {
    ih_step "1. is this machine already a hub?"
    local found=0

    local containers
    containers="$(docker ps -a --filter 'name=bellasreef-' --format '{{.Names}}' 2>/dev/null)"
    if [[ -n "$containers" ]]; then
        ih_warn "containers present: $(printf '%s' "$containers" | tr '\n' ' ')"
        found=1
    fi

    if [[ "$(systemctl is-enabled bellasreef.service 2>/dev/null)" == "enabled" ]]; then
        ih_warn "bellasreef.service is enabled"
        found=1
    fi

    local envfile="${IH_ROOT}${REPO_DIR}/deploy/.env"
    if [[ -s "$envfile" ]]; then
        ih_warn "deploy/.env already exists and is not empty"
        found=1
    fi

    if [[ $found -eq 1 ]]; then
        printf '\n'
        ih_warn "This machine already looks like a hub. Nothing has been changed."
        ih_warn "install-hub installs; it does not upgrade or repair."
        return 0
    fi

    ih_pass "no existing deployment found"
    return 1
}

# ------------------------------------------------------------------ phase 2

IH_MIN_KERNEL_MAJOR=6
IH_MIN_MEM_KB=2000000        # 2 GB. Six containers including Postgres and VM.
IH_MIN_DISK_KB=16000000      # 16 GB. Measured images are ~1.6 GB before data.

ih_check_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        ih_fail "docker is not installed"
        return 1
    fi
    if ! docker compose version >/dev/null 2>&1; then
        ih_fail "docker is installed but Compose v2 is not available"
        return 1
    fi
    ih_pass "docker with Compose v2"
    return 0
}

ih_check_arch() {
    local arch
    arch="$(uname -m 2>/dev/null)"
    case "$arch" in
        aarch64|arm64|x86_64|amd64) ih_pass "architecture ${arch}"; return 0 ;;
        "") ih_unverified "could not read the architecture"; return 2 ;;
        *)  ih_fail "architecture ${arch} is not supported; images are arm64 and amd64"; return 1 ;;
    esac
}

ih_check_kernel() {
    local release major
    release="$(uname -r 2>/dev/null)"
    if [[ -z "$release" ]]; then
        ih_unverified "could not read the kernel version"
        return 2
    fi
    major="${release%%.*}"
    if [[ "$major" =~ ^[0-9]+$ ]] && (( major >= IH_MIN_KERNEL_MAJOR )); then
        ih_pass "kernel ${release}"
        return 0
    fi
    ih_fail "kernel ${release} is below the ${IH_MIN_KERNEL_MAJOR}.x floor"
    return 1
}

ih_check_memory() {
    local kb
    kb="$(awk '/^Mem/ {print $2; exit}' <(free -k 2>/dev/null) 2>/dev/null)"
    if [[ -z "$kb" || ! "$kb" =~ ^[0-9]+$ ]]; then
        ih_unverified "could not read total memory"
        return 2
    fi
    if (( kb >= IH_MIN_MEM_KB )); then
        ih_pass "memory $(( kb / 1024 )) MB"
        return 0
    fi
    # Warn rather than fail: it may run, and refusing to try is not our call.
    ih_warn "memory $(( kb / 1024 )) MB is below the recommended $(( IH_MIN_MEM_KB / 1024 )) MB"
    return 0
}

ih_check_disk() {
    local kb
    kb="$(df -k --output=avail "${IH_ROOT}/" 2>/dev/null | tail -1 | tr -d ' ')"
    if [[ -z "$kb" || ! "$kb" =~ ^[0-9]+$ ]]; then
        ih_unverified "could not read free disk space"
        return 2
    fi
    if (( kb >= IH_MIN_DISK_KB )); then
        ih_pass "free disk $(( kb / 1024 / 1024 )) GB"
        return 0
    fi
    ih_fail "free disk $(( kb / 1024 / 1024 )) GB is below the $(( IH_MIN_DISK_KB / 1024 / 1024 )) GB floor"
    return 1
}

# An override is a deadline and the API refuses to compute one from a clock
# chrony is about to step. A hub with a wrong clock is a hub that doses at the
# wrong hour, so this is a hard requirement rather than a nicety.
ih_check_clock() {
    local synced
    synced="$(timedatectl show -p NTPSynchronized --value 2>/dev/null)"
    if [[ -z "$synced" ]]; then
        ih_unverified "could not read clock synchronisation state"
        return 2
    fi
    if [[ "$synced" == "yes" ]]; then
        ih_pass "clock synchronised"
        return 0
    fi
    ih_fail "clock is not synchronised; install and enable chrony"
    return 1
}

# Two separate things, both required. The allowlist stops avahi advertising
# Docker's bridge address, which is unreachable from the LAN and made clients
# intermittently resolve the hub to an address that does not work. The service
# record is how the app identifies a reef controller and learns its port; a
# hostname A record alone is not enough.
ih_check_avahi() {
    local conf="${IH_ROOT}/etc/avahi/avahi-daemon.conf"
    local svc="${IH_ROOT}/etc/avahi/services/bellasreef.service"
    local rc=0

    if [[ ! -f "$conf" ]]; then
        ih_fail "avahi-daemon is not installed"
        return 1
    fi

    if grep -qE '^[[:space:]]*allow-interfaces[[:space:]]*=' "$conf"; then
        ih_pass "avahi allow-interfaces is set"
    else
        ih_fail "avahi allow-interfaces is unset; it will advertise Docker bridges"
        rc=1
    fi

    if [[ -f "$svc" ]]; then
        ih_pass "_bellasreef._tcp service record installed"
    else
        ih_fail "_bellasreef._tcp service record missing; the app cannot find this hub"
        rc=1
    fi

    return $rc
}

# ------------------------------------------------------------------ consent

# Nothing is installed silently. --yes accepts everything, --dry-run refuses
# everything at the point of execution rather than at the point of consent, so
# a dry run shows the full set of actions rather than stopping at the first.
ih_confirm() {
    local prompt="$1"
    if (( IH_YES )); then
        printf '  %s [y/N] y (--yes)\n' "$prompt"
        return 0
    fi
    # Fail closed with no controlling terminal, but do it as a readability
    # test rather than by muting stderr: `read -p` writes its prompt to
    # stderr, and redirecting stderr to /dev/null on the same line (the
    # earlier bug here) mutes the prompt even when a real terminal is
    # attached — the one case ih_confirm actually has to ask a human. Only
    # fd 0 is redirected below, so the prompt still reaches the real
    # terminal's stderr when one is present.
    if [[ "$IH_ASSUME_NO_TTY" == "1" ]] || [[ ! -r /dev/tty ]]; then
        return 1
    fi
    local answer
    read -r -p "  ${prompt} [y/N] " answer </dev/tty || answer="n"
    [[ "$answer" =~ ^[Yy] ]]
}

ih_run() {
    local description="$1"; shift
    if (( IH_DRY_RUN )); then
        ih_would "$description: $*"
        return 0
    fi
    if ! "$@"; then
        ih_action_fail "${description} failed"
        return 1
    fi
    ih_pass "$description"
    return 0
}

ih_offer_install() {
    local label="$1"; shift
    if ! ih_confirm "install ${label}?"; then
        ih_warn "declined: ${label}"
        return 1
    fi
    ih_run "installing ${label}" sudo apt-get install -y "$@"
}

# Runs a check with its PASS/FAIL/UNVERIFIED line suppressed. Used only for
# phase 2's attempt pass below: each check already wrote its result to
# IH_FAILURES/IH_UNVERIFIED before any remediation for it could run, so
# printing here would show a FAIL for something the very next line is about
# to offer to fix. The verification pass after remediation prints for real
# and is what the arrays and the exit code are read from.
ih_check_quietly() {
    "$@" >/dev/null 2>&1
}

ih_phase2_requirements() {
    ih_step "2. hard requirements"

    # --check-only means exactly phases 1-3, no mutation (see --help). Every
    # remediation offer below is gated on this so that a failed check is
    # still reported — the check calls themselves are unconditional — but
    # never turns into a prompt or an action. `(( ! IH_CHECK_ONLY )) && ...`
    # short-circuits before ih_confirm/ih_offer_install run at all, so
    # --check-only --yes cannot install anything either.
    if ! ih_check_quietly ih_check_docker; then
        if (( ! IH_CHECK_ONLY )) && ih_confirm "install Docker with the official convenience script?"; then
            local target_user
            target_user="${USER:-$(id -un)}"
            ih_run "installing Docker" sudo sh -c 'curl -fsSL https://get.docker.com | sh'
            ih_run "adding ${target_user} to the docker group" sudo usermod -aG docker "$target_user"
            ih_warn "log out and back in for the docker group to take effect, then re-run"
        fi
    fi

    ih_check_quietly ih_check_arch
    ih_check_quietly ih_check_kernel
    ih_check_quietly ih_check_memory
    ih_check_quietly ih_check_disk

    # ih_check_clock can return 2 (UNVERIFIED — timedatectl unreadable) as
    # well as 1 (FAIL — genuinely unsynchronised). Those are not the same
    # thing: a check that could not run is not evidence the clock is wrong,
    # so only a real FAIL offers to install chrony. `if !` on the bare
    # return code would treat both as "failed" and install on the strength
    # of a check that never ran.
    local clock_rc=0
    ih_check_quietly ih_check_clock || clock_rc=$?
    if (( clock_rc == 1 )); then
        if (( ! IH_CHECK_ONLY )) && ih_offer_install "chrony and fake-hwclock" chrony fake-hwclock; then
            ih_run "enabling clock units" \
                sudo systemctl enable chrony chrony-wait fake-hwclock-load fake-hwclock-save
        fi
    fi

    if ! ih_check_quietly ih_check_avahi; then
        # ih_offer_install's own return gates the record install, the same
        # way the clock branch above gates enabling units on its offer's
        # result: declining the avahi-daemon package must not leave the
        # script trying to cp a record into a services/ directory that was
        # never created, or reload a daemon that was never installed.
        #
        # The prompt only promises the service record — nothing in this
        # branch ever edits avahi-daemon.conf's allow-interfaces, so it must
        # never claim to.
        if (( ! IH_CHECK_ONLY )) && ih_offer_install "avahi-daemon" avahi-daemon; then
            if ih_confirm "install the _bellasreef._tcp service record?"; then
                ih_run "installing the _bellasreef._tcp record" \
                    sudo cp "${REPO_DIR}/deploy/avahi/bellasreef.service" \
                            "${IH_ROOT}/etc/avahi/services/bellasreef.service"
                ih_run "reloading avahi" sudo systemctl reload avahi-daemon
            fi
        fi
    fi

    # The attempt pass above recorded every check's result before its own
    # remediation had a chance to run, so IH_FAILURES/IH_UNVERIFIED describe
    # a machine that (if anything was accepted and actually applied) no
    # longer exists. Clear the slate and re-run every check once, for real —
    # this pass, not the one above, is what ih_main reads to decide the exit
    # code, and it is the only one the user sees printed in full.
    #
    # IH_ACTION_FAILURES is deliberately NOT cleared here: an installer that
    # ran and failed is not something the checks above can rediscover (e.g.
    # ih_check_docker has no way to tell "docker is present because the
    # install worked" from "docker is present and the group-add silently
    # failed") — see ih_main, which reads all three arrays.
    IH_FAILURES=()
    IH_UNVERIFIED=()
    printf '\n'
    ih_step "2. hard requirements (re-checking)"
    ih_check_docker
    ih_check_arch
    ih_check_kernel
    ih_check_memory
    ih_check_disk
    ih_check_clock
    ih_check_avahi

    return 0
}

ih_main() {
    ih_parse_args "$@"
    ih_step "Bella's Reef first-run install"
    if ih_phase1_already_deployed; then
        exit 0
    fi
    ih_phase2_requirements
    if (( ${#IH_FAILURES[@]} > 0 || ${#IH_UNVERIFIED[@]} > 0 || ${#IH_ACTION_FAILURES[@]} > 0 )); then
        printf '\n'
        (( ${#IH_FAILURES[@]} > 0 ))        && ih_warn "${#IH_FAILURES[@]} requirement(s) failed"
        (( ${#IH_UNVERIFIED[@]} > 0 ))      && ih_warn "${#IH_UNVERIFIED[@]} check(s) could not be verified"
        (( ${#IH_ACTION_FAILURES[@]} > 0 )) && ih_warn "${#IH_ACTION_FAILURES[@]} remediation action(s) failed"
        exit 1
    fi
    return 0
}

# Sourceable without executing, so tests can reach individual functions.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    ih_main "$@"
fi
