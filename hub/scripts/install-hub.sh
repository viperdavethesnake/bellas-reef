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

set -uo pipefail

IH_ROOT="${IH_ROOT:-}"
IH_DRY_RUN=0
IH_CHECK_ONLY=0
IH_YES=0
IH_UNINSTALL=0
IH_PURGE=0
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

# How long phase 6 waits for the API to answer. A seam of the same kind as
# IH_ASSUME_NO_TTY above and equally absent from --help: the tests that prove
# the probe retries would otherwise spend thirty seconds each proving nothing
# the four-second version does not. Nobody installing a hub sets it.
IH_API_DEADLINE_SECS="${IH_API_DEADLINE_SECS:-30}"

# Where the image tag comes from. Written by the release workflow into the
# hub checkout; the dev repo does not have one. A seam only so the tests can
# hand the script a manifest without one existing in the tree.
IH_RELEASE_ENV="${IH_RELEASE_ENV:-}"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${IH_RELEASE_ENV:=${REPO_DIR}/deploy/release.env}"

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
  --uninstall    remove the stack, the boot unit and deploy/.env from this machine; keeps backups, /etc/bellasreef, host config, images, and this checkout
  --purge        with --uninstall: also remove the images, the avahi record, /etc/docker/daemon.json (only if untouched since install) and /etc/bellasreef; backups alone survive
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
            --uninstall)  IH_UNINSTALL=1; shift ;;
            --purge)      IH_PURGE=1; shift ;;
            --help|-h)    ih_usage; exit 0 ;;
            *)            printf 'install-hub: unknown option %s\n' "$1" >&2
                          ih_usage >&2
                          exit 2 ;;
        esac
    done
    # --check-only reports and mutates nothing; --uninstall reports nothing
    # and only mutates. Together they say opposite things about the same run.
    if (( IH_CHECK_ONLY && IH_UNINSTALL )); then
        printf 'install-hub: --check-only and --uninstall are contradictory\n' >&2
        ih_usage >&2
        exit 2
    fi
    # --purge on its own would read as "purge what?" — it only sharpens an
    # uninstall, so demanding the pair keeps the destructive flag explicit.
    if (( IH_PURGE && ! IH_UNINSTALL )); then
        printf 'install-hub: --purge requires --uninstall\n' >&2
        ih_usage >&2
        exit 2
    fi
}

# ------------------------------------------------------------------ phase 1

# Two signals stop the run, and one only reports.
#
# Containers or an enabled boot unit mean something got as far as running or
# being supervised: this tool installs, it does not upgrade, repair, or
# reconfigure, and guessing which of those an operator meant is how a working
# hub gets damaged by a tool that was asked to help.
#
# deploy/.env is deliberately NOT one of them. It is written by phase 4, three
# phases before anything starts, so a run that failed at the pull — a registry
# 401, which is the expected first-run failure while the images are private —
# leaves it behind on a machine that is not a hub at all. Latching on it made
# every later run print "nothing has been changed" and exit 0, so the operator
# fixed their credentials, re-ran, and got a green run that installed nothing.
# Phase 4 never overwrites an existing file, so continuing past it is safe.
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
        ih_warn "deploy/.env from an earlier run exists; continuing — it will not be overwritten"
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
# Measured, not guessed (coco, 1 GB Pi 5, 2026-08-31): all six services plus
# live telemetry use ~580 MB with ~400 MB headroom on a board reporting
# 1014464 kB. So the recommendation is 1 GB — the threshold sits at 900 MB
# because a "1 GB" board reports ~990 MB after the kernel's cut, and the
# check must not warn on supported hardware. A 512 MB board genuinely does
# not fit. WARN, never FAIL: it may run, and refusing to try is not our call.
IH_MIN_MEM_KB=921600

# Disk is two-tier, from measurements on the reference Pi (2026-08-17).
#
# One generation of images is 1.69 GB — api 482, control-engine 353,
# hardware-io 348, postgres 415, nats 38, victoria-metrics 52 MB — Docker
# Engine itself is about 0.4 GB, and the data volumes start at roughly 60 MB.
# So a shade over 2 GB has to land before a hub exists at all. The hard floor
# is deliberately NOT that whole figure: measured on the Banana Pi M64
# (2026-08-17), a 4.9 GB disk pulled the images and was left with 3.5 GB, and a
# 4 GB floor then refused the re-run for the very images it had just pulled.
# Free space is not remaining need. The floor is what the stack needs beyond
# its images to start at all — Docker's own state, the data volumes, logs — and
# a pull that runs out of room fails in phase 5 with compose's own ENOSPC
# message, which is a clearer answer than a guess made here.
#
# The 16 GB figure is a different claim. It is the practical minimum from
# docs/hub-platform-requirements.md — room for VictoriaMetrics retention, a
# Postgres that grows, and the second generation of images an upgrade pulls
# before it drops the first. A machine between the two installs fine and will
# run out later, so it is a WARN and the run continues.
#
# Both thresholds are round decimal GB, which is how the messages below say
# them; the free-space figure is df's kB divided the binary way, as before.
IH_MIN_DISK_KB=2000000            # 2 GB hard floor — below this the stack cannot start.
IH_RECOMMENDED_DISK_KB=16000000   # 16 GB practical minimum — below this, warn.

# Docker is unusable for three different reasons and only one of them is
# fixed by installing Docker. Asked separately here so the check's message and
# phase 2's remediation both branch on the same three readings.
ih_docker_present()    { command -v docker >/dev/null 2>&1; }
ih_docker_compose_v2() { docker compose version >/dev/null 2>&1; }
ih_docker_reachable()  { docker info >/dev/null 2>&1; }

# The group database, deliberately, not this session's groups. After a usermod
# the two disagree — the database has the user in the group and the login does
# not — and that disagreement is precisely the state that must not be read as
# "never added", because the answer to it is a re-login, not another usermod.
ih_in_docker_group() {
    # No pipe into grep -q here (see update-hub.sh's --ref validation for the
    # SIGPIPE/pipefail trap this used to match: grep -q exits the instant it
    # matches, and if id/tr still had output queued when it did, they took
    # SIGPIPE and pipefail reported that as the pipeline's exit status).
    # Command substitution reads id's output to completion first.
    local groups
    groups="$(id -nG "$1" 2>/dev/null)"
    case " $groups " in
        *' docker '*) return 0 ;;
        *) return 1 ;;
    esac
}

ih_check_docker() {
    if ! ih_docker_present; then
        ih_fail "docker is not installed"
        return 1
    fi
    if ! ih_docker_compose_v2; then
        ih_fail "docker is installed but Compose v2 is not available"
        return 1
    fi
    # Installed is not the same as reachable. `docker` on PATH with Compose v2
    # says the package is there; it says nothing about whether this user may
    # talk to the socket. Without this probe the first evidence is a permission
    # denial at the pull, three phases later, in a step with no idea what
    # caused it — and the fix is a group membership plus a re-login, which is
    # not guessable from "pull failed".
    if ! ih_docker_reachable; then
        local docker_user
        docker_user="${USER:-$(id -un)}"
        if ih_in_docker_group "$docker_user"; then
            # Already in the group and still refused. Nothing is left to
            # install, so saying "add yourself to the docker group" here sends
            # the operator to do again the thing they have already done.
            ih_fail "docker is installed and ${docker_user} is already in the docker group, but the daemon is still unreachable; either it is not running (check: systemctl status docker) or this login predates the group being granted — log out and back in"
        else
            ih_fail "docker is installed but this user cannot reach the daemon; add yourself to the docker group (sudo usermod -aG docker \$USER), log out and back in"
        fi
        return 1
    fi
    ih_pass "docker with Compose v2"
    return 0
}

# Docker's json-file driver never rotates on its own, and compose.yaml
# carries no per-service logging: block on purpose — the daemon default is
# the one place rotation exists. WARN, never FAIL: the stack runs without it;
# the disk fills later.
ih_docker_daemon_json() { printf '%s' "${IH_ROOT}/etc/docker/daemon.json"; }

ih_check_docker_logging() {
    local f
    f="$(ih_docker_daemon_json)"
    if [[ -f "$f" ]] && grep -q '"max-size"' "$f"; then
        ih_pass "docker log rotation configured"
        return 0
    fi
    if [[ -f "$f" ]]; then
        # Present but silent on rotation. Merging a stranger's JSON blind is
        # how a daemon stops starting, so this is reported and left alone.
        ih_warn "/etc/docker/daemon.json exists but sets no log rotation; container logs grow without bound"
        printf '      Add under "log-opts": { "max-size": "10m", "max-file": "3" }, then restart docker.\n'
    else
        ih_warn "no /etc/docker/daemon.json; container logs grow without bound"
    fi
    return 0
}

# One source for the file's content: the writer below and --purge's
# untouched-since-install comparison must mean the same bytes, or purge
# would refuse to remove the very file the installer wrote.
ih_docker_daemon_json_content() {
    printf '{\n  "log-driver": "json-file",\n  "log-opts": { "max-size": "10m", "max-file": "3" }\n}\n'
}

ih_write_docker_daemon_json() {
    local tmp rc f
    f="$(ih_docker_daemon_json)"
    tmp="$(mktemp)" || return 1
    ih_docker_daemon_json_content > "$tmp"
    sudo mkdir -p "$(dirname "$f")" && sudo install -m 0644 "$tmp" "$f"; rc=$?
    rm -f "$tmp"
    return $rc
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
    if (( kb >= IH_RECOMMENDED_DISK_KB )); then
        ih_pass "free disk $(( kb / 1000 / 1000 )) GB"
        return 0
    fi
    if (( kb >= IH_MIN_DISK_KB )); then
        # WARN, not UNVERIFIED. The check ran and gave an answer; the answer is
        # that this machine is knowingly degraded. UNVERIFIED means the check
        # could not run, and conflating the two would hide a measured fact
        # behind a word that means "no measurement".
        ih_warn "free disk $(( kb / 1000 / 1000 )) GB is below the $(( IH_RECOMMENDED_DISK_KB / 1000 / 1000 )) GB practical minimum (docs/hub-platform-requirements.md); fine for a bench, not for a tank — retention and a second image generation will not fit"
        return 0
    fi
    ih_fail "free disk $(( kb / 1000 / 1000 )) GB is below the $(( IH_MIN_DISK_KB / 1000 / 1000 )) GB hard floor; the stack cannot start in this little space"
    return 1
}

# The host paths the compose manifest hard-requires: every `devices:` node
# and every /sys bind-mount source. Docker cannot create a missing /dev node
# or a missing source under /sys, so any absence is a guaranteed phase-5 wall
# — hardware-io simply cannot start. Ruled 2026-08-31 (coco, weighing
# dtparam=i2c_arm=off): that has to be a phase-2 FAIL that names the path and
# the remedy, not a docker error after images are already pulled.
ih_manifest_host_paths() {
    [[ -r "$IH_COMPOSE" ]] || return 1
    grep -E '^[[:space:]]*- /(dev|sys)/' "$IH_COMPOSE" \
        | sed -E 's/^[[:space:]]*- //; s/:.*$//' \
        | sort -u
}

ih_check_host_paths() {
    local paths missing=() p count
    if ! paths="$(ih_manifest_host_paths)" || [[ -z "$paths" ]]; then
        ih_unverified "could not read the compose manifest's host paths"
        return 2
    fi
    while IFS= read -r p; do
        [[ -e "${IH_ROOT}${p}" ]] || missing+=("$p")
    done <<<"$paths"
    if (( ${#missing[@]} == 0 )); then
        count="$(wc -l <<<"$paths" | tr -d ' ')"
        ih_pass "host paths required by the compose manifest present (${count})"
        return 0
    fi
    local m
    for m in "${missing[@]}"; do
        ih_fail "${m} is required by deploy/compose.yaml but missing — the stack cannot start without it; enable the interface in /boot/firmware/config.txt (see docs/host-setup.md) and reboot"
    done
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
    # Two different failures. Measured on the Banana Pi M64 (2026-08-17): a
    # run within a minute of boot found chrony installed, active, and not yet
    # synchronised — and the old single message told the operator to install
    # it, which under --yes would have reinstalled a working daemon. A daemon
    # that is running but has not converged is a wait, not an install; it is
    # still a FAIL, because nothing may schedule against an untrusted clock.
    local daemon
    for daemon in chrony chronyd systemd-timesyncd; do
        if [[ "$(systemctl is-active "$daemon" 2>/dev/null)" == "active" ]]; then
            ih_fail "clock is not synchronised yet; ${daemon} is running (just booted?) — give it a minute and re-run"
            return 3
        fi
    done
    ih_fail "clock is not synchronised; install and enable chrony"
    return 1
}

# Two separate things, both required. The allowlist stops avahi advertising
# Docker's bridge address, which is unreachable from the LAN and made clients
# intermittently resolve the hub to an address that does not work. The service
# record is how the app identifies a reef controller and learns its port; a
# hostname A record alone is not enough.
# The two halves, asked separately: the remediation for a missing package and
# the remediation for a missing service record are independent, and a single
# "is avahi ok" answer made declining one gate away the other.
ih_avahi_daemon_present() { [[ -f "${IH_ROOT}/etc/avahi/avahi-daemon.conf" ]]; }
ih_avahi_record_present() { [[ -f "${IH_ROOT}/etc/avahi/services/bellasreef.service" ]]; }

# This machine's LAN interfaces, comma-joined, for the allow-interfaces line
# the check below prints.
#
# eth0,wlan0 is Raspberry Pi OS's naming and nobody else's. Debian and Armbian
# use predictable names — end0, enX0, enp1s0, wlp3s0 — and the M64's wired NIC
# is end0. Pasting the literal eth0,wlan0 there on 2026-08-17 produced a
# perfectly valid config file allowlisting two interfaces that do not exist,
# and avahi advertised on nothing. That is worse than the misconfiguration it
# was meant to fix, because the file now looks right.
#
# Excluded: lo, and everything Docker creates — docker0, the per-network br-*
# bridges, and veth pairs. Keeping avahi off those is the entire reason the
# allowlist exists. Interface state is not consulted: a wlan0 that is down
# today is still the interface somebody wants the hub reachable on tomorrow.
#
# Returns non-zero when there is nothing to say. A guessed list presented as
# this machine's would be worse than the generic one.
ih_lan_interfaces() {
    command -v ip >/dev/null 2>&1 || return 1
    local names
    # $1 carries an @ifN suffix on a veth peer, so it is stripped before the
    # name is matched or printed.
    names="$(ip -br link 2>/dev/null \
        | awk '{ sub(/@.*/, "", $1); print $1 }' \
        | grep -vE '^(lo|docker.*|br-.*|veth.*|virbr[0-9]*)$' \
        | tr '\n' ',' \
        | sed 's/,$//')"
    [[ -n "$names" ]] || return 1
    printf '%s' "$names"
}

ih_avahi_allow_interfaces_set() {
    grep -qE '^[[:space:]]*allow-interfaces[[:space:]]*=' "$1"
}

# Inserts the line directly under [server]. Rendered with awk into a temp
# file and installed over the original, rather than sed -i: the two seds
# disagree on `a\` and this has to behave the same on the Pi and on the
# machine running the tests. `sudo install` is what puts it back, so the
# file keeps root ownership and 0644.
ih_set_avahi_allow_interfaces() {
    local conf="$1" lan="$2" tmp rc
    tmp="$(mktemp)" || return 1
    if ! awk -v line="allow-interfaces=${lan}" \
            '{ print } /^\[server\]/ && !done { print line; done=1 }' "$conf" > "$tmp"; then
        rm -f "$tmp"; return 1
    fi
    sudo install -m 0644 "$tmp" "$conf"; rc=$?
    rm -f "$tmp"
    return $rc
}

ih_check_avahi() {
    local conf="${IH_ROOT}/etc/avahi/avahi-daemon.conf"
    local rc=0

    if ! ih_avahi_daemon_present; then
        ih_fail "avahi-daemon is not installed"
        return 1
    fi

    if ih_avahi_allow_interfaces_set "$conf"; then
        ih_pass "avahi allow-interfaces is set"
    else
        # Phase 2 offers to write this line (ih_set_avahi_allow_interfaces);
        # the printed remedy below is for the cases it will not touch: no
        # [server] section, or interfaces it could not read.
        ih_fail "avahi allow-interfaces is unset; it will advertise Docker bridges"
        local lan
        printf '      Add to /etc/avahi/avahi-daemon.conf, under [server]:\n'
        if lan="$(ih_lan_interfaces)"; then
            printf '        allow-interfaces=%s\n' "$lan"
            printf '      Adjust to your LAN interfaces, then restart avahi-daemon.\n'
        else
            printf '        allow-interfaces=eth0,wlan0\n'
            printf '      (could not read your interfaces) Adjust to your LAN\n'
            printf '      interfaces, then restart avahi-daemon.\n'
        fi
        rc=1
    fi

    if ih_avahi_record_present; then
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

# Whether the container uid (1000, the api's user) can write <dir>, judged
# from owner, group and mode — the same standard the created directories meet
# (install -d -o 1000 -g 1000 -m 0755). Supplementary groups and ACLs are not
# consulted. Exists because "directory exists; left as is" once passed a
# root-owned 0755 backups directory on coco — debris from a failed earlier
# install — and the first backup died on it (2026-08-31).
ih_dir_writable_by_container() {
    local dir="$1" uid gid mode bit
    read -r uid gid mode < <(stat -c '%u %g %a' "$dir" 2>/dev/null)
    [[ -n "${mode:-}" ]] || return 1
    # %a prints no leading zeros, and setuid/sticky bits add a fourth digit;
    # pad so the last three characters are always owner/group/other.
    mode="000${mode}"
    if [[ "$uid" == "1000" ]]; then
        bit="${mode: -3:1}"
    elif [[ "$gid" == "1000" ]]; then
        bit="${mode: -2:1}"
    else
        bit="${mode: -1}"
    fi
    (( bit & 2 ))
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
    # Three states, and each gets the one remediation that answers it.
    #
    # Observed on the Banana Pi M64, 2026-08-17: docker was installed with
    # Compose v2, the user was not yet in the docker group for that login, and
    # ih_check_docker failed at the `docker info` probe. Phase 2 offered the
    # convenience script — so --yes spent five minutes re-running
    # get.docker.com to install a Docker that was already there, then
    # usermod'd a user for the second time. The check knew which of the three
    # things was wrong; the remediation did not ask.
    #
    #   (a) no docker, or no Compose v2 — the install is absent or
    #       incomplete, which is the one case the convenience script answers.
    #   (b) installed and complete, daemon unreachable, user not in the
    #       group — a usermod and a re-login, no download.
    #   (c) installed, in the group, daemon still unreachable — nothing to
    #       install. Either dockerd is down or the login predates the group,
    #       and ih_check_docker's own message says both. No offer at all;
    #       offering one here is how the reinstall loop starts.
    if ! ih_check_quietly ih_check_docker; then
        local target_user
        target_user="${USER:-$(id -un)}"
        # Stopping after either accepted remediation, not continuing: the
        # group membership does not apply to the session that granted it, so
        # this process still cannot reach the daemon — and every remaining
        # phase talks to it. Continuing means pulling images as a user who is
        # not yet in the group: a guaranteed failure, several phases later,
        # that reads as a broken install rather than as the log-out the
        # operator actually owes.
        if ! ih_docker_present || ! ih_docker_compose_v2; then
            if (( ! IH_CHECK_ONLY )) && ih_confirm "install Docker with the official convenience script?"; then
                ih_run "installing Docker" sudo sh -c 'curl -fsSL https://get.docker.com | sh'
                ih_run "adding ${target_user} to the docker group" sudo usermod -aG docker "$target_user"
                ih_warn "log out and back in for the docker group to take effect, then re-run this script"
                return 1
            fi
        elif ! ih_docker_reachable && ! ih_in_docker_group "$target_user"; then
            if (( ! IH_CHECK_ONLY )) && ih_confirm "add ${target_user} to the docker group?"; then
                ih_run "adding ${target_user} to the docker group" sudo usermod -aG docker "$target_user"
                ih_warn "log out and back in for the docker group to take effect, then re-run this script"
                return 1
            fi
        fi
    fi

    # Offered only when the file is absent — see ih_check_docker_logging.
    if (( ! IH_CHECK_ONLY )) && ih_docker_present && [[ ! -f "$(ih_docker_daemon_json)" ]]; then
        if ih_confirm "configure docker log rotation (json-file, 10m x 3)?"; then
            if ih_run "writing /etc/docker/daemon.json" ih_write_docker_daemon_json; then
                ih_run "restarting docker" sudo systemctl restart docker
            fi
        fi
    fi

    ih_check_quietly ih_check_arch
    ih_check_quietly ih_check_kernel
    ih_check_quietly ih_check_memory
    ih_check_quietly ih_check_disk
    ih_check_quietly ih_check_host_paths

    # ih_check_clock can return 2 (UNVERIFIED — timedatectl unreadable) or
    # 3 (FAIL — a time daemon is running but has not converged yet) as well
    # as 1 (FAIL — no daemon at all). Only 1 is an install problem: a check
    # that could not run is not evidence the clock is wrong, and a daemon
    # that merely needs another minute is not one to reinstall. `if !` on
    # the bare return code would treat all three as "install chrony".
    local clock_rc=0
    ih_check_quietly ih_check_clock || clock_rc=$?
    if (( clock_rc == 1 )); then
        if (( ! IH_CHECK_ONLY )) && ih_offer_install "chrony and fake-hwclock" chrony fake-hwclock; then
            ih_run "enabling clock units" \
                sudo systemctl enable chrony chrony-wait fake-hwclock-load fake-hwclock-save
        fi
    fi

    if (( ! IH_CHECK_ONLY )) && ! ih_check_quietly ih_check_avahi; then
        # Two independent remediations, because ih_check_avahi fails for two
        # independent reasons. Offering the package to a machine that already
        # has avahi is noise, and — worse — declining that offer used to gate
        # away the service-record offer, which was the only thing actually
        # missing. So: the package is offered only when the daemon is absent,
        # and the record only when the daemon is present and the record is
        # not. The daemon check is re-asked after the install rather than
        # assumed, so a declined or failed package install cannot leave this
        # script cp-ing a record into a services/ directory that was never
        # created, or reloading a daemon that is not there.
        #
        # The allowlist is offered below; the printed remedy in ih_check_avahi
        # covers what the offer will not touch.
        if ! ih_avahi_daemon_present; then
            ih_offer_install "avahi-daemon" avahi-daemon
        fi
        if ih_avahi_daemon_present && ! ih_avahi_record_present; then
            if ih_confirm "install the _bellasreef._tcp service record?"; then
                ih_run "installing the _bellasreef._tcp record" \
                    sudo cp "${REPO_DIR}/deploy/avahi/bellasreef.service" \
                            "${IH_ROOT}/etc/avahi/services/bellasreef.service"
                ih_run "reloading avahi" sudo systemctl reload avahi-daemon
            fi
        fi

        # The allowlist. Only when the daemon is present, the line is absent,
        # this machine's interfaces could be read, and there is a [server]
        # section to put it under — a guessed list or an invented section
        # would be worse than the printed remedy.
        local conf="${IH_ROOT}/etc/avahi/avahi-daemon.conf" lan
        if ih_avahi_daemon_present && ! ih_avahi_allow_interfaces_set "$conf" \
                && lan="$(ih_lan_interfaces)" && grep -q '^\[server\]' "$conf"; then
            if ih_confirm "set avahi allow-interfaces=${lan}?"; then
                if ih_run "setting avahi allow-interfaces=${lan}" ih_set_avahi_allow_interfaces "$conf" "$lan"; then
                    # restart, not reload: interface config is read at start.
                    ih_run "restarting avahi" sudo systemctl restart avahi-daemon
                fi
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
    ih_check_docker_logging
    ih_check_arch
    ih_check_kernel
    ih_check_memory
    ih_check_disk
    ih_check_host_paths
    ih_check_clock
    ih_check_avahi

    return 0
}

# ------------------------------------------------------------------ phase 3

# Board class, not board model. Three cases matter and no others: a Pi 5 has
# the RP1 and its own overlay names, an older Pi has config.txt but no RP1, and
# anything else has neither.
ih_detect_board() {
    local model="${IH_ROOT}/proc/device-tree/model"
    if [[ ! -r "$model" ]]; then
        printf 'other'
        return 0
    fi
    local text
    text="$(tr -d '\0' < "$model")"
    case "$text" in
        *"Raspberry Pi 5"*) printf 'pi5' ;;
        *"Raspberry Pi"*)   printf 'pi' ;;
        *)                  printf 'other' ;;
    esac
}

# Reported, never required. An owner may want temperature and no lights, or
# lights over a PCA9685 and no SoC PWM, or — on the day the hub is installed
# — nothing attached at all. This mirrors capabilities.py, which announces
# what it can prove and holds no view on what should be there. Nothing here
# gates the install, prints boot-config advice, or asks a question; the
# custom overlay procedure for RP1 PWM lives in docs/host-setup.md §9.
ih_phase3_hardware() {
    ih_step "3. hardware inventory (reported, never required)"

    local board
    board="$(ih_detect_board)"
    local model="${IH_ROOT}/proc/device-tree/model" model_text=""
    [[ -r "$model" ]] && model_text="$(tr -d '\0' < "$model")"
    case "$board" in
        pi5)   ih_pass "board        ${model_text} (RP1 present)" ;;
        pi)    ih_pass "board        ${model_text} (no RP1: SoC PWM unavailable in this stack; a PCA9685 over I2C works)" ;;
        *)     ih_warn "board        ${model_text:-unknown} — not a Raspberry Pi" ;;
    esac

    # /dev/i2c-1 specifically, not a glob over /dev/i2c-*: the Pi's HDMI DDC
    # buses are i2c-13/14 and are present with i2c_arm off. One MODE1 read at
    # 0x40 if i2c-tools is here; 0x70 (ALLCALL) is never addressed.
    if [[ -e "${IH_ROOT}/dev/i2c-1" ]]; then
        local pca
        if command -v i2cget >/dev/null 2>&1; then
            if i2cget -y 1 0x40 0x00 >/dev/null 2>&1; then
                pca="PCA9685 at 0x40: answering"
            else
                pca="PCA9685 at 0x40: not answering"
            fi
        else
            pca="PCA9685 not probed (i2c-tools not installed)"
        fi
        ih_pass "I2C          bus 1 present; ${pca}"
    else
        ih_warn "I2C          bus 1 absent"
    fi

    # 28-* is a DS18B20. 00-* entries are what a floating bus enumerates when
    # nothing (or nothing pulled up) is on it — reported as such, so "probes:
    # 0" on a bus that is clearly searching is not mistaken for a dead bus.
    local w1="${IH_ROOT}/sys/bus/w1/devices"
    if [[ -d "$w1" ]]; then
        local probes phantoms
        probes="$(find "$w1" -maxdepth 1 -name '28-*' 2>/dev/null | wc -l | tr -d ' ')"
        phantoms="$(find "$w1" -maxdepth 1 -name '00-*' 2>/dev/null | wc -l | tr -d ' ')"
        if (( probes > 0 )); then
            ih_pass "1-Wire       bus present; DS18B20 probes: ${probes}"
        elif (( phantoms > 0 )); then
            ih_pass "1-Wire       bus present; DS18B20 probes: 0 (bus up, nothing answering — no probe, or no pull-up)"
        else
            ih_pass "1-Wire       bus present; DS18B20 probes: 0"
        fi
    else
        ih_warn "1-Wire       no bus"
    fi

    # A pwmchip in sysfs says nothing about the header: the standing trap
    # (CLAUDE.md, verified host facts) is a chip that exports while every pin
    # reads `none`. pinctrl is the evidence, the same one hardware-io uses.
    if compgen -G "${IH_ROOT}/sys/class/pwm/pwmchip*" >/dev/null 2>&1; then
        if command -v pinctrl >/dev/null 2>&1; then
            local muxed
            muxed="$(pinctrl get 12,13,18,19 2>/dev/null | grep -c 'PWM')"
            [[ "$muxed" =~ ^[0-9]+$ ]] || muxed=0
            if (( muxed > 0 )); then
                ih_pass "SoC PWM      pwmchip present; ${muxed} of 4 header pins muxed to PWM"
            else
                ih_warn "SoC PWM      pwmchip present but no header pin is muxed to PWM (optional; docs/host-setup.md §9)"
            fi
        else
            ih_pass "SoC PWM      pwmchip present; pin mux not verified (pinctrl not installed)"
        fi
    else
        ih_warn "SoC PWM      no pwmchip (optional; RP1 PWM needs the overlay in docs/host-setup.md §9)"
    fi
    return 0
}

# ------------------------------------------------------------------ phase 4

# Read, never defaulted. These are allocated by the OS when the package is
# installed and differ between hosts: the reference Pi is 988 and 986, while
# .env.example shipped 994 and 993, which were already wrong for it. A wrong
# value fails as a permission error that reads like a hardware fault, so a
# missing group is reported here and left empty rather than guessed.
ih_gid_for() {
    getent group "$1" 2>/dev/null | cut -d: -f3
}

ih_generate_password() {
    LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32
}

# The example file is read bare (real repo file, read-only, must exist for
# the script to work at all) but the file this writes is always under
# $IH_ROOT — unlike every other write in this script, deploy/.env is a real
# path on the machine running the installer, and a bare path here would mean
# any test that reaches this phase without --dry-run writes and chmod 600s
# the developer's actual repository checkout.
ih_phase4_configure() {
    ih_step "4. configuration"

    local envfile="${IH_ROOT}${REPO_DIR}/deploy/.env"
    local example="${REPO_DIR}/deploy/.env.example"

    # -s, not -f: an existing but EMPTY deploy/.env is not a configuration, it
    # is a file somebody touched (or a write that died before its first byte),
    # and treating it as one leaves the stack with no password, no GIDs and no
    # tag — every one of which compose interpolates as ${VAR:?} and refuses to
    # start on. Overwriting an empty file loses nothing; the invariant that
    # matters is that a file with content in it is never touched.
    if [[ -s "$envfile" ]]; then
        ih_pass "deploy/.env already exists; leaving it untouched"
        return 0
    fi

    local i2c_gid gpio_gid
    i2c_gid="$(ih_gid_for i2c)"
    gpio_gid="$(ih_gid_for gpio)"

    if [[ -n "$i2c_gid" ]]; then
        ih_pass "i2c group GID ${i2c_gid}"
    else
        ih_fail "no i2c group on this machine; hardware-io cannot open /dev/i2c-*"
    fi

    if [[ -n "$gpio_gid" ]]; then
        ih_pass "gpio group GID ${gpio_gid}"
    else
        ih_fail "no gpio group on this machine; compose requires GPIO_GID and will refuse to start"
        # A Pi gets the group from its OS packages. Other boards (the Banana Pi
        # M64 on Armbian, measured 2026-08-17) have none, and compose only needs
        # the GID for hardware-io's group_add, so an empty group is enough.
        printf '      If this board has no gpio group, create one and re-run:\n'
        printf '        sudo groupadd gpio\n'
    fi

    # Nothing is written past a failed check. This phase used to fall through
    # and write deploy/.env with an empty GID before ih_main's gate exited 1 —
    # and phase 1 reads a non-empty deploy/.env as "already a hub", so one
    # failed run (gpio package not installed yet, say) left a file the
    # operator was never told about and made every later run stop at phase 1
    # for an install that never happened. A configure that fails leaves the
    # machine exactly as it found it, so the rerun reaches this point again.
    if [[ -z "$i2c_gid" || -z "$gpio_gid" ]]; then
        return 1
    fi

    # The images come from the registry; compose and the scripts come from
    # this checkout. What holds them to the same build is the release
    # manifest the release workflow wrote beside compose.yaml — this
    # checkout's own git commit is a bellasreef-hub commit and says nothing
    # about which images exist.
    if [[ ! -r "$IH_RELEASE_ENV" ]]; then
        ih_fail "no deploy/release.env; this checkout is not a released hub"
        printf '      Clone the hub repo instead: https://github.com/viperdavethesnake/bellasreef-hub\n'
        return 1
    fi
    local version tag
    version="$(sed -n 's/^BELLASREEF_VERSION=//p' "$IH_RELEASE_ENV" | head -1)"
    tag="$(sed -n 's/^BELLASREEF_TAG=//p' "$IH_RELEASE_ENV" | head -1)"
    if [[ -z "$version" || ! "$tag" =~ ^[0-9a-f]{40}$ ]]; then
        ih_fail "deploy/release.env is malformed (version='${version}', tag='${tag}'); it is written by the release workflow, not by hand"
        return 1
    fi
    # An edited hub checkout is not a release. Checked only when there is
    # git metadata to check against: the release tarball has none, and that
    # is a limit on what can be verified, not evidence of edits.
    if command -v git >/dev/null 2>&1 && git -C "$REPO_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        if [[ -n "$(git -C "$REPO_DIR" status --porcelain 2>/dev/null)" ]]; then
            ih_fail "this checkout has uncommitted changes; an edited hub checkout is not a release"
            return 1
        fi
    else
        ih_warn "not a git checkout; cannot confirm these files are unmodified"
    fi
    ih_pass "image tag ${version} (${tag:0:12})"
    local commit="$tag"

    # Checked, not assumed. /dev/urandom missing, a busybox tr without -dc, a
    # locale that makes the class match nothing: each of those yields a short
    # or empty password, and the PASS line below would still claim 32 chars.
    # An empty POSTGRES_PASSWORD is not a weak credential, it is a database
    # that refuses every connection later, in a phase that has no idea why.
    local password
    password="$(ih_generate_password)"
    if [[ ! "$password" =~ ^[A-Za-z0-9]{32}$ ]]; then
        ih_fail "password generation produced ${#password} usable character(s), not 32; /dev/urandom or tr is not behaving"
        return 1
    fi
    ih_pass "generated a Postgres password (32 chars, not shown)"

    if (( IH_DRY_RUN )); then
        ih_would "write deploy/.env with I2C_GID=${i2c_gid:-<missing>} GPIO_GID=${gpio_gid:-<missing>} BELLASREEF_TAG=${version} BELLASREEF_BACKUP_DIR=${HOME}/backups"
        return 0
    fi

    # Built beside deploy/.env under a temporary name and moved into place
    # only once every step has succeeded. Two reasons, both about what is
    # left behind when this goes wrong:
    #
    #   - phase 1 reads a non-empty deploy/.env as "this machine is already a
    #     hub" and stops. A half-written file — sed killed partway, the box
    #     losing power, a full disk — would therefore block every later run
    #     for an install that never happened. mv within one directory is
    #     atomic, so deploy/.env either does not exist or is complete.
    #   - the file carries the Postgres password. mktemp creates at 0600
    #     (umask 077 covers any implementation that does not), so the secret
    #     is never written into a world-readable file and then narrowed
    #     afterwards.
    #
    # Each step is checked. `set -e` is deliberately not in force in this
    # script, so an unchecked cp/sed/chmod would fail silently and still
    # reach the PASS line.
    local tmpfile
    tmpfile="$(umask 077; mktemp "${envfile}.XXXXXX")"
    if [[ -z "$tmpfile" || ! -f "$tmpfile" ]]; then
        ih_fail "could not create a temporary file next to deploy/.env"
        return 1
    fi
    if ! chmod 600 "$tmpfile"; then
        rm -f "$tmpfile"
        ih_fail "could not restrict permissions on the new deploy/.env"
        return 1
    fi

    if ! sed \
        -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${password}|" \
        -e "s|^BELLASREEF_DATABASE_URL=.*|BELLASREEF_DATABASE_URL=postgresql+asyncpg://bellasreef:${password}@postgres:5432/bellasreef|" \
        -e "s|^I2C_GID=.*|I2C_GID=${i2c_gid}|" \
        -e "s|^GPIO_GID=.*|GPIO_GID=${gpio_gid}|" \
        -e "s|^BELLASREEF_TAG=.*|BELLASREEF_TAG=${commit}|" \
        -e "s|^BELLASREEF_BACKUP_DIR=.*|BELLASREEF_BACKUP_DIR=${HOME}/backups|" \
        -e "s|^BELLASREEF_ETC_DIR=.*|BELLASREEF_ETC_DIR=/etc/bellasreef|" \
        "$example" > "$tmpfile"; then
        rm -f "$tmpfile"
        ih_fail "could not generate deploy/.env from deploy/.env.example"
        return 1
    fi

    # The substitutions are only as good as the example file they ran against.
    # A key renamed or dropped in deploy/.env.example leaves sed with nothing
    # to match and no error to report, and compose interpolates all five as
    # ${VAR:?} — which refuses to start the entire stack, phases later, with a
    # message that names a variable rather than a cause.
    local key
    for key in POSTGRES_PASSWORD BELLASREEF_DATABASE_URL I2C_GID GPIO_GID BELLASREEF_TAG BELLASREEF_BACKUP_DIR BELLASREEF_ETC_DIR; do
        if ! grep -qE "^${key}=." "$tmpfile"; then
            rm -f "$tmpfile"
            ih_fail "the generated deploy/.env has no ${key} value; deploy/.env.example may have changed"
            return 1
        fi
    done

    if ! mv "$tmpfile" "$envfile"; then
        rm -f "$tmpfile"
        ih_fail "could not move the generated file into place at deploy/.env"
        return 1
    fi
    ih_pass "wrote deploy/.env"
    return 0
}

# ------------------------------------------------------------------ phase 5

# compose.yaml is a tracked repo file — read-only, and required for the script
# to work at all — so it is read bare, the same rule as deploy/.env.example.
# The env file is the one phase 4 wrote, which lives under $IH_ROOT: it is a
# real path on the machine being installed, and a bare path here would mean
# any test reaching this phase hands docker the developer's own checkout.
IH_COMPOSE="${REPO_DIR}/deploy/compose.yaml"
IH_ENVFILE="${IH_ROOT}${REPO_DIR}/deploy/.env"

ih_compose() {
    docker compose -f "$IH_COMPOSE" --env-file "$IH_ENVFILE" "$@"
}

ih_phase5_deploy() {
    ih_step "5. deploy"

    if (( IH_DRY_RUN )); then
        ih_would "docker compose pull"
        ih_would "docker compose run --rm api alembic upgrade head"
        ih_would "create the backup and /etc/bellasreef directories"
        ih_would "install and enable bellasreef.service"
        ih_would "docker compose up -d"
        return 0
    fi

    # Said before it happens, not after. On a Pi this is three multi-hundred-
    # megabyte images over whatever the tank shelf's wifi manages, and a
    # silent terminal for several minutes is indistinguishable from a hang.
    ih_step "pulling images — this takes a few minutes on a Pi"
    local pull_output
    if ! pull_output="$(ih_compose pull 2>&1)"; then
        # Registry auth is temporary scaffolding: the images are private today
        # and this whole branch is deleted when they go public. The script does
        # not manage credentials, so it names the one command that fixes it.
        if grep -qiE 'denied|unauthorized|401' <<<"$pull_output"; then
            ih_fail "the registry refused the pull; these images are private"
            printf '      Fix with:\n        docker login ghcr.io -u <github-username>\n'
            printf '      using a token with the read:packages scope, then re-run.\n'
        else
            ih_fail "docker compose pull failed"
            # The tag came from deploy/release.env, not this checkout's git
            # history — the release workflow publishes
            # ghcr.io/viperdavethesnake/bellasreef-<svc>:<sha> when it retags,
            # so "manifest unknown" means that release's images are not on the
            # registry, not that this commit's CI is still running.
            local version tag
            version="$(sed -n 's/^BELLASREEF_VERSION=//p' "$IH_RELEASE_ENV" | head -1)"
            tag="$(sed -n 's/^BELLASREEF_TAG=//p' "$IH_RELEASE_ENV" | head -1)"
            printf '      Release %s (%s) has no images on the registry.\n' "$version" "${tag:0:12}"
            printf '      Re-clone bellasreef-hub at a released tag, or check the release workflow run for this version.\n'
            printf '%s\n' "$pull_output" | sed 's/^/      /'
        fi
        return 1
    fi
    ih_pass "images pulled"

    # Captured, then tailed — not piped through tail in the pipeline. This is
    # the one failure on a first install the operator has no other way to
    # diagnose (unreachable database, a generated password Postgres never got,
    # a broken revision chain), and deploy-pi.sh records why the obvious form
    # is wrong: `cmd | tail` makes the exit status tail's, so the failure
    # itself disappears along with the output that explains it.
    local migrate_output
    if ! migrate_output="$(ih_compose run --rm api sh -c 'cd /app/db && alembic upgrade head' 2>&1)"; then
        ih_fail "migrations failed; not starting services against an unmigrated schema"
        printf '%s\n' "$migrate_output" | tail -5 | sed 's/^/      /'
        return 1
    fi
    ih_pass "migrations applied"

    # The two host paths compose.yaml bind-mounts into api. Docker would
    # auto-create a missing one as root:root, and the container runs as
    # 1000 — so `bellasreef backup` would fail on the day it was needed.
    # Created here owned by the image uid; an existing directory is left
    # exactly as found. `${IH_ROOT}` prefixes the host path; the value in
    # .env stays the bare path the container sees on a real machine.
    local backup_dir etc_dir d
    backup_dir="$(sed -n 's/^BELLASREEF_BACKUP_DIR=//p' "$IH_ENVFILE" | head -1)"
    etc_dir="$(sed -n 's/^BELLASREEF_ETC_DIR=//p' "$IH_ENVFILE" | head -1)"
    if [[ -z "$backup_dir" || -z "$etc_dir" ]]; then
        ih_fail "deploy/.env has no BELLASREEF_BACKUP_DIR / BELLASREEF_ETC_DIR; cannot create the bind-mount directories"
        return 1
    fi
    for d in "$backup_dir" "$etc_dir"; do
        if [[ -d "${IH_ROOT}${d}" ]]; then
            # An existing directory meets the same standard as a created one.
            # "left as is" once passed root-owned debris from a failed earlier
            # install, and the first backup died on it (coco, 2026-08-31).
            if ih_dir_writable_by_container "${IH_ROOT}${d}"; then
                ih_pass "directory ${d} exists; left as is"
            elif ih_confirm "chown ${d} to the container uid 1000?"; then
                ih_run "chowning ${d} to the container uid" \
                    sudo chown 1000:1000 "${IH_ROOT}${d}" || return 1
            else
                ih_fail "directory ${d} exists but the container uid 1000 cannot write it; the first backup would fail. Fix: sudo chown 1000:1000 ${d}"
                return 1
            fi
        else
            ih_run "creating ${d} (owned by the container uid 1000)" \
                sudo install -d -m 0755 -o 1000 -g 1000 "${IH_ROOT}${d}" || return 1
        fi
    done

    # Rendered for this host, not copied. The checked-in unit is written for
    # the reference Pi — User=david, WorkingDirectory=/home/david/bellasreef,
    # and absolute /home/david/bellasreef paths in ExecStart/ExecStop — which
    # is correct for the machine deploy-pi.sh deploys to and wrong for every
    # other one. Installed verbatim on a stranger's hub it is a unit that fails
    # at every boot, while phase 6's is-enabled still reports that the stack
    # survives a power cut. The repo file is left exactly as it is; only the
    # copy under /etc/systemd/system is substituted.
    local unit_src="${REPO_DIR}/deploy/systemd/bellasreef.service"
    local unit_user="${USER:-$(id -un)}"
    local rendered
    rendered="$(mktemp)"
    if [[ -z "$rendered" || ! -f "$rendered" ]]; then
        ih_fail "could not create a temporary file for the boot unit"
        return 1
    fi
    if ! sed -e "s|^User=.*|User=${unit_user}|" \
             -e "s|/home/david/bellasreef|${REPO_DIR}|g" \
             "$unit_src" > "$rendered"; then
        rm -f "$rendered"
        ih_fail "could not render the boot unit for this host from ${unit_src}"
        return 1
    fi
    ih_run "installing the boot unit" \
        sudo install -m 0644 "$rendered" \
                             "${IH_ROOT}/etc/systemd/system/bellasreef.service" \
        || { rm -f "$rendered"; return 1; }
    rm -f "$rendered"
    ih_run "reloading systemd" sudo systemctl daemon-reload || return 1
    ih_run "enabling bellasreef.service" sudo systemctl enable bellasreef.service || return 1

    # compose.yaml requires Pi-5 device nodes and a gpio group. On a machine
    # lacking either, compose fails with an error that does not name the cause.
    # --wait, matching the boot unit and deploy-pi.sh: without it `up -d`
    # returns when the containers have been created, which says nothing about
    # whether any of them stayed up. Phase 6 would then be racing the stack it
    # is meant to be verifying.
    local up_output
    if ! up_output="$(ih_compose up -d --wait 2>&1)"; then
        ih_fail "the stack did not start"
        if grep -qiE 'gpiomem|1f00098000|required variable|is not set' <<<"$up_output"; then
            printf '      This machine is missing hardware compose.yaml requires\n'
            printf '      (Pi 5 device nodes, or a gpio group). It cannot run the stack.\n'
        fi
        printf '%s\n' "$up_output" | sed 's/^/      /'
        return 1
    fi
    ih_pass "stack started"
    return 0
}


# ------------------------------------------------------------------ uninstall

# Removes the stack from this machine: the containers and their data volumes,
# the boot unit, and deploy/.env. Runs instead of the six phases (see
# ih_main), works from any state — a full install, a half-created one, or
# nothing at all — and reports what it found with the same ih_pass/ih_warn
# vocabulary the phases use.
#
# Kept, deliberately: the backups directory, /etc/bellasreef, docker's own
# log-rotation config, avahi's config, a docker login, pulled images, and
# this checkout. None of those are this script's to remove.
IH_UNINSTALL_VOLUMES=(bellasreef_postgres-data bellasreef_vm-data bellasreef_nats-data)

# Destructive enough to ask every time — --yes is for the install's offered
# remediations, not for this. There is no bypass flag for this prompt. The
# typed word names what happens: 'purge' will not confirm a plain uninstall
# and 'uninstall' will not confirm a purge.
ih_uninstall_confirm() {
    local word="uninstall"
    (( IH_PURGE )) && word="purge"
    printf '\n'
    printf '  This removes the Bella'"'"'s Reef stack from this machine.\n'
    printf '\n'
    printf '  This permanently deletes:\n'
    printf '    - the data volumes: %s\n' "${IH_UNINSTALL_VOLUMES[*]}"
    printf '      (every pairing, every device, all history)\n'
    printf '    - the bellasreef.service boot unit\n'
    printf '    - deploy/.env\n'
    if (( IH_PURGE )); then
        printf '    - the pulled images\n'
        printf '    - the _bellasreef._tcp avahi record\n'
        printf '    - /etc/docker/daemon.json (only if untouched since install)\n'
        printf '    - /etc/bellasreef\n'
    fi
    printf '\n'
    printf '  This keeps:\n'
    printf '    - the backups directory\n'
    if (( ! IH_PURGE )); then
        printf '    - /etc/bellasreef\n'
        printf '    - /etc/docker/daemon.json\n'
        printf '    - the avahi configuration\n'
        printf '    - the pulled images\n'
    fi
    printf '    - the docker login on this machine\n'
    printf '    - this checkout\n'
    printf '\n'
    # A plain `read -p` prints nothing at all when stdin is not a terminal —
    # which is exactly how the test suite drives this — so the prompt is
    # printed here rather than left to -p, and read separately.
    printf "  Type '%s' to proceed: " "$word"
    local answer
    read -r answer
    [[ "$answer" == "$word" ]]
}

# Step 2: stack + volumes down. A working deploy/.env means docker compose
# knows how to do this properly; anything else (no env file, or compose
# itself failing) falls back to removing by name — the volumes are named the
# same regardless of what wrote them.
ih_uninstall_stack() {
    local envfile="$1"

    if (( IH_DRY_RUN )); then
        ih_would "docker compose -f ${IH_COMPOSE} down -v --remove-orphans"
        ih_would "remove any leftover bellasreef-* containers and the data volumes by name"
        return 0
    fi

    if [[ -s "$envfile" ]]; then
        if ih_compose down -v --remove-orphans; then
            ih_pass "stack stopped and volumes removed (docker compose down -v)"
            return 0
        fi
        ih_warn "docker compose down failed; removing containers and volumes by name instead"
    else
        ih_warn "no deploy/.env found; removing containers and volumes by name"
    fi

    local -a ids=()
    local line
    while IFS= read -r line; do
        [[ -n "$line" ]] && ids+=("$line")
    done < <(docker ps -aq --filter 'name=bellasreef-' 2>/dev/null)
    if (( ${#ids[@]} > 0 )); then
        ih_run "removing ${#ids[@]} leftover container(s)" docker rm -f "${ids[@]}"
    else
        ih_warn "no leftover bellasreef-* containers found"
    fi

    local existing_volumes
    existing_volumes="$(docker volume ls -q 2>/dev/null)"
    local vol
    for vol in "${IH_UNINSTALL_VOLUMES[@]}"; do
        if grep -qx "$vol" <<<"$existing_volumes"; then
            ih_run "removing volume ${vol}" docker volume rm "$vol"
        else
            ih_warn "volume ${vol} not present"
        fi
    done
}

# The scorched-earth half of --uninstall --purge (ruled 2026-08-31): the
# images, the avahi record, daemon.json when it is still exactly what the
# installer wrote, and /etc/bellasreef. Backups survive, along with this
# checkout and the docker login — none of the three is this script's to
# remove even at its most destructive.
ih_purge_extras() {
    local images="$1"

    if (( IH_DRY_RUN )); then
        ih_would "remove the stack's images (read from compose config)"
        ih_would "remove /etc/avahi/services/bellasreef.service"
        ih_would "remove /etc/docker/daemon.json if untouched since install"
        ih_would "remove /etc/bellasreef"
        return 0
    fi

    # The list was read from compose config before the stack came down —
    # afterwards there is no deploy/.env left to ask. Without one there is no
    # authoritative list, so only images that name bellasreef go, and the
    # spine images are left rather than guessed at.
    local -a imgs=()
    local line
    if [[ -n "$images" ]]; then
        while IFS= read -r line; do
            [[ -n "$line" ]] && imgs+=("$line")
        done <<<"$images"
    else
        ih_warn "no deploy/.env; removing only images that name bellasreef (spine images left)"
        while IFS= read -r line; do
            [[ -n "$line" ]] && imgs+=("$line")
        done < <(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep bellasreef)
    fi
    if (( ${#imgs[@]} > 0 )); then
        ih_run "removing ${#imgs[@]} image(s)" docker rmi -f "${imgs[@]}"
    else
        ih_warn "no images to remove"
    fi

    # avahi watches its services directory; removing the record unpublishes
    # it without a daemon restart. The allow-interfaces line stays — it is
    # an exclusion of Docker bridges, not part of the stack.
    local record="${IH_ROOT}/etc/avahi/services/bellasreef.service"
    if [[ -f "$record" ]]; then
        ih_run "removing the _bellasreef._tcp service record" sudo rm -f "$record"
    else
        ih_warn "no _bellasreef._tcp service record installed"
    fi

    local dj
    dj="$(ih_docker_daemon_json)"
    if [[ ! -f "$dj" ]]; then
        ih_warn "no /etc/docker/daemon.json to remove"
    elif [[ "$(cat "$dj")" == "$(ih_docker_daemon_json_content)" ]]; then
        ih_run "removing /etc/docker/daemon.json (untouched since install)" sudo rm -f "$dj"
    else
        ih_warn "/etc/docker/daemon.json differs from what this installer writes; left in place"
    fi

    if [[ -d "${IH_ROOT}/etc/bellasreef" ]]; then
        ih_run "removing /etc/bellasreef" sudo rm -rf "${IH_ROOT}/etc/bellasreef"
    else
        ih_warn "no /etc/bellasreef to remove"
    fi
}

ih_uninstall_summary() {
    printf '\n'
    ih_step "uninstall complete"
    if (( IH_PURGE )); then
        printf '      Removed: the stack, its data volumes, the boot unit, deploy/.env,\n'
        printf '      the images, the avahi record, /etc/bellasreef.\n'
        printf '      Kept: backups, docker login, this checkout.\n'
    else
        printf '      Removed: the stack, its data volumes, the boot unit, deploy/.env.\n'
        printf '      Kept: backups, /etc/bellasreef, /etc/docker/daemon.json, avahi\n'
        printf '      configuration, docker login, pulled images, this checkout.\n'
    fi
    printf '\n'
    printf '      This checkout remains; rm -rf %s removes it.\n' "$REPO_DIR"
    if (( ! IH_PURGE )); then
        printf '      Host configuration the installer offered (docker log rotation,\n'
        printf '      avahi) was left in place.\n'
    fi
}

ih_uninstall() {
    ih_step "Bella's Reef uninstall"

    if (( IH_DRY_RUN )); then
        if (( IH_PURGE )); then
            ih_would "prompt for typed confirmation ('purge')"
        else
            ih_would "prompt for typed confirmation ('uninstall')"
        fi
    elif ! ih_uninstall_confirm; then
        ih_warn "uninstall not confirmed; nothing was changed"
        return 1
    fi

    local unit_file="${IH_ROOT}/etc/systemd/system/bellasreef.service"
    local envfile="${IH_ROOT}${REPO_DIR}/deploy/.env"
    local had_unit=0
    [[ -f "$unit_file" ]] && had_unit=1

    # Read while deploy/.env still exists: compose config is the one
    # authoritative list of what --purge should remove, and it is gone two
    # steps from now.
    local purge_images=""
    if (( IH_PURGE && ! IH_DRY_RUN )) && [[ -s "$envfile" ]]; then
        purge_images="$(ih_compose config --images 2>/dev/null)" || purge_images=""
    fi

    if (( had_unit )); then
        ih_run "boot unit disabled and stopped" sudo systemctl disable --now bellasreef.service
    else
        ih_warn "no boot unit installed"
    fi

    ih_uninstall_stack "$envfile"

    if (( had_unit )); then
        ih_run "boot unit file removed" sudo rm -f "$unit_file"
        ih_run "systemd reloaded" sudo systemctl daemon-reload
    fi

    if [[ -f "$envfile" ]]; then
        ih_run "deploy/.env removed" rm -f "$envfile"
    else
        ih_warn "no deploy/.env to remove"
    fi

    (( IH_PURGE )) && ih_purge_extras "$purge_images"

    ih_uninstall_summary
    return 0
}

# ------------------------------------------------------------------ phase 6

IH_API_INFO_URL="http://127.0.0.1:8000/api/v1/info"

# Polled, not asked once. `compose up -d` returns when the containers have
# been created, which is well before uvicorn accepts a connection — api has
# no compose healthcheck for `up` to wait on — so a single curl here would
# report a dead API on a perfectly good install. Same deadline loop as
# deploy-pi.sh's, and the body is kept because the setup-code step needs it.
ih_api_info() {
    local deadline body=""
    deadline=$(( $(date +%s) + IH_API_DEADLINE_SECS ))
    while :; do
        body="$(curl -fsS --max-time 10 "$IH_API_INFO_URL" 2>/dev/null)"
        [[ -n "$body" ]] && break
        (( $(date +%s) >= deadline )) && break
        sleep 2
    done
    printf '%s' "$body"
}

ih_phase6_verify() {
    ih_step "6. verify"

    # Returns before anything runs, not after each command is skipped: every
    # step below either reads the machine or, in the setup-code case, changes
    # it. `bellasreef setup-code` rotates the code rather than reprinting it,
    # so a dry run that "only verifies" would invalidate a paired hub's code
    # as a side effect of being asked what it would do.
    if (( IH_DRY_RUN )); then
        ih_would "verify services, boot unit, API, avahi; print the setup code"
        return 0
    fi

    # An empty listing is checked separately from a listing with something
    # broken in it. "Anything that is not running" finds nothing in an empty
    # one, so a stack that started zero containers would otherwise read as a
    # stack that is entirely healthy.
    local ps_output
    ps_output="$(ih_compose ps --format '{{.Name}} {{.State}}' 2>/dev/null)"
    if [[ -z "$ps_output" ]]; then
        ih_fail "no containers are running; the stack did not come up"
    else
        local unhealthy
        unhealthy="$(grep -v 'running' <<<"$ps_output")"
        if [[ -n "$unhealthy" ]]; then
            ih_fail "not all services are running"
            printf '%s\n' "$unhealthy" | sed 's/^/      /'
        else
            ih_pass "all services running"
        fi

        # And every service compose defines has to be in that listing at all.
        # "Anything that is not running" cannot see a container that was never
        # created: a stack missing its api lists five healthy containers and
        # reads as healthy, while the hub has no front door. The expected set
        # is asked of compose rather than hardcoded, so a service added to
        # compose.yaml is checked here without anyone remembering to.
        local expected svc line state
        expected="$(ih_compose config --services 2>/dev/null)"
        if [[ -z "$expected" ]]; then
            ih_unverified "could not read the service list from compose.yaml"
        else
            while read -r svc; do
                [[ -z "$svc" ]] && continue
                # Compose names containers <project>-<service>-<index>, so the
                # service name is a token in the middle rather than the whole
                # field.
                line="$(grep -E "(^|-)${svc}-[0-9]+[[:space:]]" <<<"$ps_output" | head -1)"
                if [[ -z "$line" ]]; then
                    ih_fail "service ${svc} has no container; it never started"
                    continue
                fi
                state="${line##* }"
                [[ "$state" != "running" ]] && ih_fail "service ${svc} is ${state}, not running"
            done <<<"$expected"
        fi
    fi

    # Separate from the container check on purpose. Every other check here
    # proves the hub works now; only this one proves it comes back after a
    # power cut, which for a tank controller is the failure you find at the
    # worst possible time.
    if [[ "$(systemctl is-enabled bellasreef.service 2>/dev/null)" == "enabled" ]]; then
        ih_pass "bellasreef.service enabled; the stack survives a power cut"
    else
        ih_fail "bellasreef.service is NOT enabled; the stack will not return after a power cut"
    fi

    # Enabled says systemd will try to start it at boot. It says nothing about
    # whether the unit can work here, and the checked-in file names the
    # reference host in four places — so an unrendered unit is enabled,
    # reported green, and fails at every boot on a machine nobody is watching.
    # Phase 5 renders it; this reads back what actually landed.
    local unit_path="${IH_ROOT}/etc/systemd/system/bellasreef.service"
    local unit_user="${USER:-$(id -un)}"
    if [[ ! -r "$unit_path" ]]; then
        ih_fail "no boot unit at /etc/systemd/system/bellasreef.service"
    else
        local exec_line user_line
        exec_line="$(grep -m1 '^ExecStart=' "$unit_path")"
        user_line="$(grep -m1 '^User=' "$unit_path")"
        if [[ "$exec_line" != *"$REPO_DIR"* ]]; then
            ih_fail "the installed boot unit does not run from ${REPO_DIR}; it will fail at boot"
            printf '      %s\n' "${exec_line:-<no ExecStart line>}"
        elif [[ "$user_line" != "User=${unit_user}" ]]; then
            ih_fail "the installed boot unit runs as ${user_line#User=}, not ${unit_user}; it will fail at boot"
        else
            ih_pass "boot unit rendered for this host (${unit_user}, ${REPO_DIR})"
        fi

        # Skipped silently when the tool is absent rather than recorded as
        # UNVERIFIED: the content assertion above is the check that matters,
        # and systemd-analyze is a second opinion on syntax that not every
        # host ships.
        if command -v systemd-analyze >/dev/null 2>&1; then
            local verify_output
            if verify_output="$(systemd-analyze verify "$unit_path" 2>&1)"; then
                ih_pass "systemd-analyze accepts the boot unit"
            else
                ih_fail "systemd-analyze rejected the installed boot unit"
                printf '%s\n' "$verify_output" | sed 's/^/      /'
            fi
        fi
    fi

    local info
    info="$(ih_api_info)"
    if [[ -n "$info" ]]; then
        ih_pass "API answering"
    else
        ih_fail "API not answering on port 8000"
    fi

    # Not by browsing: avahi-browse is not installed by avahi-daemon, and
    # host-setup.md §5 records that a local browse does not reliably reflect
    # the daemon's own services. The journal is authoritative instead.
    #
    # grep here (no -q) rather than grep -q: without -q, grep keeps reading
    # until the input is exhausted instead of exiting the instant it finds a
    # match, so journalctl can never take SIGPIPE for output still queued
    # behind a match and poison the pipeline's exit status under pipefail
    # (the same trap update-hub.sh's --ref validation hit).
    if journalctl -u avahi-daemon --no-pager -n 200 2>/dev/null \
        | grep 'successfully established' >/dev/null; then
        ih_pass "avahi published the _bellasreef._tcp record"
    else
        ih_unverified "could not confirm avahi published the service record"
    fi

    # Only in setup mode, and only if the API answered at all. `bellasreef
    # setup-code` mints a new code rather than reprinting the current one, so
    # running it on a hub somebody has already paired would silently break
    # that pairing's successor. If /info never answered there is nothing to
    # read the mode from and the FAIL above already says why — a second
    # failure line about the code would name a symptom, not the cause.
    #
    # sed with [a-z]* rather than \(true\|false\): \| is a GNU extension and
    # matches nothing on a BSD sed, which is the same trap deploy-pi.sh
    # records having fallen into.
    if [[ -n "$info" ]]; then
        local setup_mode
        setup_mode="$(sed -n 's/.*"setup_mode":\([a-z]*\).*/\1/p' <<<"$info")"
        # Three cases, not two. An empty extraction — a renamed field, a
        # space after the colon, an HTML 200 from something that is not the
        # API — is not evidence the hub is paired, and "already paired,
        # nothing to show" is the reading that ends the install green while
        # never showing the owner the code they cannot get in without. The
        # unknown case is the whole reason this phase has an UNVERIFIED.
        case "$setup_mode" in
            true)
                local code
                code="$(ih_compose exec -T api bellasreef setup-code 2>/dev/null | tr -d '\r')"
                if [[ -n "$code" ]]; then
                    printf '\n'
                    ih_step "Pair your phone"
                    # Verbatim, indented. The CLI's output is already labeled
                    # and already carries its own instruction — wrapping it
                    # in a second label printed "Setup code:  Setup code:"
                    # on coco's maiden install (2026-08-31).
                    printf '\n'
                    printf '%s\n' "$code" | sed 's/^/      /'
                    printf '\n      Multicast is not something this script can test from here;\n'
                    printf '      the app finding the hub is the proof.\n\n'
                else
                    ih_fail "could not read the setup code"
                fi
                ;;
            false)
                ih_pass "hub already paired; no setup code to show"
                ;;
            *)
                ih_unverified "could not read setup mode from /api/v1/info"
                ;;
        esac
    fi

    ih_phase6_handoff

    (( ${#IH_FAILURES[@]} == 0 && ${#IH_UNVERIFIED[@]} == 0 ))
}

# The two things the setup code does not say: what to do next, and which
# machine this transcript is about.
ih_phase6_handoff() {
    local version tag
    printf '\n'
    ih_step "Next: adopt your hardware"
    # The app is the product mechanism — proven on coco's maiden install:
    # probe adopted from the app in three taps, no YAML, no token. `devices
    # import` is the bulk/restore path (its own docstring says "explicitly
    # not the product mechanism") and lives in the docs, not here.
    printf '      A fresh hub knows what it can control, but nothing is adopted yet.\n'
    printf '      In the app: System > Hardware lists every channel and probe this hub\n'
    printf '      discovered. Adopt what the hub should use; controls live on the tab\n'
    printf '      that uses the device.\n'
    printf '      Restoring many devices at once instead? bellasreef devices import -\n'
    printf '      docs/host-setup.md section 10.\n'

    version="$(sed -n 's/^BELLASREEF_VERSION=//p' "$IH_RELEASE_ENV" 2>/dev/null | head -1)"
    tag="$(sed -n 's/^BELLASREEF_TAG=//p' "$IH_RELEASE_ENV" 2>/dev/null | head -1)"
    printf '\n'
    ih_step "This hub"
    local model="${IH_ROOT}/proc/device-tree/model" board="unknown"
    [[ -r "$model" ]] && board="$(tr -d '\0' < "$model")"
    printf '      hostname   %s\n' "$(command -v hostname >/dev/null 2>&1 && hostname 2>/dev/null || echo unknown)"
    printf '      board      %s\n' "$board"
    printf '      memory     %s MB\n' "$(awk '/^Mem/ {print int($2/1024); exit}' <(free -k 2>/dev/null) 2>/dev/null)"
    printf '      free disk  %s GB\n' "$(df -k --output=avail "${IH_ROOT}/" 2>/dev/null | tail -1 | awk '{print int($1/1000/1000)}')"
    # LAN addresses are what the app can reach; the docker bridges are real
    # but internal, and printing them unlabeled read as three equally-good
    # addresses on coco's maiden install. Ruled 2026-08-31: label, don't
    # hide. The interface split is the same one ih_lan_interfaces and the
    # avahi allowlist already use.
    local addr_out lan_addrs internal_addrs
    if command -v ip >/dev/null 2>&1 && addr_out="$(ip -br addr 2>/dev/null)" && [[ -n "$addr_out" ]]; then
        lan_addrs="$(awk '{ name=$1; sub(/@.*/, "", name);
            if (name == "lo" || name ~ /^(docker|br-|veth|virbr)/) next;
            for (i = 3; i <= NF; i++) if ($i ~ /^[0-9]+\./) { sub(/\/.*/, "", $i); printf "%s ", $i } }' \
            <<<"$addr_out" | sed 's/ $//')"
        internal_addrs="$(awk '{ name=$1; sub(/@.*/, "", name);
            if (name !~ /^(docker|br-|virbr)/) next;
            for (i = 3; i <= NF; i++) if ($i ~ /^[0-9]+\./) { sub(/\/.*/, "", $i); printf "%s ", $i } }' \
            <<<"$addr_out" | sed 's/ $//')"
        printf '      addresses  %s (LAN)\n' "${lan_addrs:-none found}"
        if [[ -n "$internal_addrs" ]]; then
            printf '                 %s (internal - docker bridges)\n' "$internal_addrs"
        fi
    else
        printf '      addresses  %s\n' "$(hostname -I 2>/dev/null | sed 's/ *$//')"
    fi
    printf '      release    %s (%s)\n' "${version:-unknown}" "${tag:0:12}"
    printf '      checkout   %s\n' "$REPO_DIR"
}

# The three counts a gate reports, printed the same way wherever a gate fires.
ih_gate_summary() {
    printf '\n'
    (( ${#IH_FAILURES[@]} > 0 ))        && ih_warn "${#IH_FAILURES[@]} requirement(s) failed"
    (( ${#IH_UNVERIFIED[@]} > 0 ))      && ih_warn "${#IH_UNVERIFIED[@]} check(s) could not be verified"
    (( ${#IH_ACTION_FAILURES[@]} > 0 )) && ih_warn "${#IH_ACTION_FAILURES[@]} remediation action(s) failed"
    return 0
}

ih_main() {
    ih_parse_args "$@"
    if (( IH_UNINSTALL )); then
        ih_uninstall
        return $?
    fi
    ih_step "Bella's Reef first-run install"
    if ih_phase1_already_deployed; then
        exit 0
    fi
    # Phase 2 returns non-zero for the one thing the arrays cannot express: it
    # installed Docker and the operator now has to log out before anything
    # else in this script can work. That is not a failed check — every check
    # may well pass on the next run — so it stops the run without necessarily
    # adding to any of the three arrays.
    local phase2_rc=0
    ih_phase2_requirements || phase2_rc=$?
    local phase2_bad=0
    if (( phase2_rc != 0 || ${#IH_FAILURES[@]} > 0 || ${#IH_UNVERIFIED[@]} > 0 || ${#IH_ACTION_FAILURES[@]} > 0 )); then
        phase2_bad=1
    fi

    # The gate stops a real install here, and deliberately does not stop a
    # --check-only one.
    #
    # For an install the reasoning is unchanged: phases 4 to 6 configure,
    # pull, migrate and start, and none of that is worth attempting on a
    # machine that just failed a hard requirement.
    #
    # --check-only mutates nothing, and phase 3 — the hardware inventory — is
    # the reason anybody runs it. Exiting here meant a candidate board that
    # fails phase 2 never printed the one thing the flag exists to produce,
    # which is exactly the machine somebody is inspecting. The M64 (no docker,
    # no avahi) hit that: every run stopped one phase short of its answer.
    # The failures are still counted, below, and still set the exit code.
    if (( phase2_bad && ! IH_CHECK_ONLY )); then
        ih_gate_summary
        exit 1
    fi
    ih_phase3_hardware
    if (( IH_CHECK_ONLY )); then
        # Reported is not passed. The inventory printed either way; the
        # phase-2 result is what decides whether this run was green.
        if (( phase2_bad )); then
            ih_gate_summary
            exit 1
        fi
        printf '\n'
        ih_pass "checks complete (--check-only); nothing was changed"
        exit 0
    fi
    ih_phase4_configure
    # Same gate as after phase 2, for the same reason: a FAIL that only
    # prints is not a FAIL that stops anything. A missing i2c or gpio group
    # ships an empty *_GID into deploy/.env, and compose.yaml interpolates
    # both as ${I2C_GID:?}/${GPIO_GID:?} — the `:?` form refuses to start the
    # *entire* stack, not just hardware-io, with an error that names the
    # variable rather than the cause. Without this gate, phase 5 (once it
    # exists) would proceed to pull images and run migrations on a machine
    # this phase already proved cannot come up.
    if (( ${#IH_FAILURES[@]} > 0 || ${#IH_UNVERIFIED[@]} > 0 || ${#IH_ACTION_FAILURES[@]} > 0 )); then
        ih_gate_summary
        exit 1
    fi
    # Phase 5 records its own failures through ih_fail/ih_action_fail, so the
    # arrays are already non-empty by the time it returns non-zero — but it
    # exits on its own return code rather than through another array gate:
    # every step past the pull is a mutation, and once one of them fails there
    # is nothing further to attempt on this machine.
    ih_phase5_deploy || exit 1
    # Phase 6 changes nothing, so unlike phase 5 there is no reason to stop at
    # the first bad answer — every check runs and the summary counts what came
    # back, the same shape as the phase-2 and phase-4 gates. An UNVERIFIED is
    # counted with the failures deliberately: a check that could not run has
    # not shown the operator a working hub.
    if ! ih_phase6_verify; then
        printf '\n'
        (( ${#IH_FAILURES[@]} > 0 ))   && ih_warn "${#IH_FAILURES[@]} check(s) failed"
        (( ${#IH_UNVERIFIED[@]} > 0 )) && ih_warn "${#IH_UNVERIFIED[@]} check(s) could not be verified"
        ih_warn "the hub is not ready to hand over"
        exit 1
    fi
    return 0
}

# Sourceable without executing, so tests can reach individual functions.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    ih_main "$@"
fi
