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

# Reported, never blocking. An owner may want temperature monitoring and no
# lights at all, so a fixed list of required interfaces would be an opinion
# about their tank. This mirrors capabilities.py, which announces what it can
# prove and holds no view on what should be there.
ih_phase3_hardware() {
    ih_step "3. hardware inventory"

    local board
    board="$(ih_detect_board)"

    local i2c_ok=0 w1_ok=0 pwm_ok=0
    compgen -G "${IH_ROOT}/dev/i2c-*" >/dev/null 2>&1 && i2c_ok=1
    [[ -d "${IH_ROOT}/sys/bus/w1/devices" ]] && w1_ok=1
    compgen -G "${IH_ROOT}/sys/class/pwm/pwmchip*" >/dev/null 2>&1 && pwm_ok=1

    if (( i2c_ok )); then
        ih_pass "I2C          enabled       PCA9685 and other I2C devices available"
    else
        ih_warn "I2C          not enabled   no PCA9685 or I2C sensors"
    fi

    if (( w1_ok )); then
        ih_pass "1-Wire       enabled       DS18B20 temperature probes available"
    else
        ih_warn "1-Wire       not enabled   no temperature probes"
    fi

    if (( pwm_ok )); then
        ih_pass "SoC PWM      enabled       direct PWM channels available"
    else
        ih_warn "SoC PWM      not enabled   a PCA9685 still works over I2C"
    fi

    if (( i2c_ok && w1_ok && pwm_ok )); then
        return 0
    fi

    printf '\n'
    case "$board" in
        pi5|pi)
            ih_warn "To enable the missing interfaces, add to /boot/firmware/config.txt:"
            (( i2c_ok )) || printf '      dtparam=i2c_arm=on\n'
            if (( ! w1_ok )); then
                if [[ "$board" == "pi5" ]]; then
                    printf '      [pi5]\n      dtoverlay=w1-gpio-pi5,gpiopin=4\n'
                else
                    printf '      dtoverlay=w1-gpio,gpiopin=4\n'
                fi
            fi
            (( pwm_ok )) || printf '      dtoverlay=pwm-4chan\n'
            ih_warn "Never put a trailing # comment on those lines; the parser folds it in."
            ih_warn "Then reboot and run this script again."
            ;;
        other)
            ih_warn "This is not a Raspberry Pi, so boot config was not inspected."
            ih_warn "Enable the interfaces you need however this board does it, then re-run."
            ;;
    esac

    # --check-only is a read-only inspection: there is nothing to proceed to,
    # so asking would stall a non-interactive run at a question with no
    # consequence. Report the inventory and let ih_main's --check-only exit
    # handle the rest.
    if (( IH_CHECK_ONLY )); then
        return 0
    fi

    printf '\n'
    if ! ih_confirm "proceed with only the interfaces above?"; then
        ih_warn "stopped at your request; nothing has been changed"
        exit 0
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

    if [[ -f "$envfile" ]]; then
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
        ih_would "write deploy/.env with I2C_GID=${i2c_gid:-<missing>} GPIO_GID=${gpio_gid:-<missing>}"
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
        "$example" > "$tmpfile"; then
        rm -f "$tmpfile"
        ih_fail "could not generate deploy/.env from deploy/.env.example"
        return 1
    fi

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
        ih_would "install and enable bellasreef.service"
        ih_would "docker compose up -d"
        return 0
    fi

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
            printf '%s\n' "$pull_output" | sed 's/^/      /'
        fi
        return 1
    fi
    ih_pass "images pulled"

    if ! ih_compose run --rm api sh -c 'cd /app/db && alembic upgrade head' >/dev/null 2>&1; then
        ih_fail "migrations failed; not starting services against an unmigrated schema"
        return 1
    fi
    ih_pass "migrations applied"

    # deploy/systemd/*.service, matching how deploy-pi.sh installs it. The glob
    # is today one file; the app units it once sat beside are deleted.
    ih_run "installing the boot unit" \
        sudo install -m 0644 "${REPO_DIR}"/deploy/systemd/*.service \
                             "${IH_ROOT}/etc/systemd/system/" || return 1
    ih_run "reloading systemd" sudo systemctl daemon-reload || return 1
    ih_run "enabling bellasreef.service" sudo systemctl enable bellasreef.service || return 1

    # compose.yaml requires Pi-5 device nodes and a gpio group. On a machine
    # lacking either, compose fails with an error that does not name the cause.
    local up_output
    if ! up_output="$(ih_compose up -d 2>&1)"; then
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
    ih_phase3_hardware
    if (( IH_CHECK_ONLY )); then
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
        printf '\n'
        (( ${#IH_FAILURES[@]} > 0 ))        && ih_warn "${#IH_FAILURES[@]} requirement(s) failed"
        (( ${#IH_UNVERIFIED[@]} > 0 ))      && ih_warn "${#IH_UNVERIFIED[@]} check(s) could not be verified"
        (( ${#IH_ACTION_FAILURES[@]} > 0 )) && ih_warn "${#IH_ACTION_FAILURES[@]} remediation action(s) failed"
        exit 1
    fi
    # Phase 5 records its own failures through ih_fail/ih_action_fail, so the
    # arrays are already non-empty by the time it returns non-zero — but it
    # exits on its own return code rather than through another array gate:
    # every step past the pull is a mutation, and once one of them fails there
    # is nothing further to attempt on this machine.
    ih_phase5_deploy || exit 1
    return 0
}

# Sourceable without executing, so tests can reach individual functions.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    ih_main "$@"
fi
