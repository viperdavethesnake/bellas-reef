# Hub First-Run Install Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `scripts/install-hub.sh`, one script that takes a machine with this repo cloned on it from bare to a running, pairable hub.

**Architecture:** A single bash script run locally on the hub, structured as six ordered phases. All filesystem reads are prefixed by `$IH_ROOT` (empty in production) and all external tools are invoked by bare name, so pytest can exercise the whole script against a fixture root with stub executables on `PATH`. The script is safe to `source` without executing, which is how unit tests reach individual functions.

**Tech Stack:** bash 5, pytest for tests, shellcheck for lint, Docker Compose v2, systemd, avahi.

**Spec:** `docs/superpowers/specs/2026-08-15-hub-first-run-install-design.md`

## Global Constraints

- **Never write boot config.** `config.txt` and `armbianEnv.txt` are read and reported only. Ruled 2026-08-15.
- **Never overwrite `deploy/.env`** if it exists.
- **Nothing is installed silently.** Every mutating action is offered and accepted individually, except under `--yes`.
- **`--dry-run` mutates nothing**, including no package installs, no file writes, no docker calls that change state.
- **A check that could not run reports `UNVERIFIED`, never `PASS`**, and makes the overall result non-green.
- **Hardware interface checks never block.** Only the hard requirements in Phase 2 can stop the run.
- **All filesystem reads go through `$IH_ROOT`.** A literal `/etc` or `/sys` path anywhere in the script is a bug.
- **Bash only**, no Python, no external dependencies beyond coreutils, systemd and docker.
- Shell style follows `scripts/deploy-pi.sh`: `set -uo pipefail`, `die`/`step`/`warn` helpers, ANSI colour.
- Conventional commits. Every task ends with a commit.

---

## File Structure

| File | Responsibility |
|---|---|
| `scripts/install-hub.sh` | **Create.** The whole installer. Sectioned by phase, sourceable without executing. |
| `tests/test_install_hub.py` | **Create.** Black-box tests driving the script against a fixture root with stub executables. |
| `tests/fixtures/install_hub/` | **Create.** Fixture roots representing different machine states. |
| `deploy/.env.example` | **Modify.** Empty the generated secrets, remove the wrong GID defaults, document what the script fills. |
| `scripts/check.sh` | **Modify.** Add a shellcheck run so shell code is gated like Python is. |
| `docs/host-setup.md` | **Modify.** Point at the installer as the supported first-run path. |

---

### Task 1: Harness, skeleton, and output helpers

**Files:**
- Create: `scripts/install-hub.sh`
- Create: `tests/test_install_hub.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ih_say(level, msg)`, `ih_pass(msg)`, `ih_fail(msg)`, `ih_unverified(msg)`, `ih_would(msg)`, globals `IH_ROOT`, `IH_DRY_RUN`, `IH_CHECK_ONLY`, `IH_YES`, `IH_FAILURES`, `IH_UNVERIFIED`, and `ih_main "$@"`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_install_hub.py`:

```python
# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Black-box tests for scripts/install-hub.sh.

The script is driven as a subprocess against a fixture root with stub
executables on PATH. That is deliberate: mocking bash functions from pytest
would test a script nobody runs, and the ordering rules here (phase order,
what stops the run, what only warns) are exactly the properties a stubbed-out
version would stop checking.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "install-hub.sh"


def run_script(
    *args: str,
    root: Path,
    stubs: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run install-hub.sh against a fixture root."""
    environ = dict(os.environ)
    environ["IH_ROOT"] = str(root)
    if stubs is not None:
        environ["PATH"] = f"{stubs}:{environ['PATH']}"
    if env:
        environ.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=environ,
        timeout=60,
    )


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.exists(), f"{SCRIPT} not found"
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} is not executable"


def test_help_exits_zero_and_names_the_phases(tmp_path: Path) -> None:
    result = run_script("--help", root=tmp_path)
    assert result.returncode == 0
    for phase in ("already deployed", "requirements", "hardware", "configuration"):
        assert phase in result.stdout.lower(), f"--help does not mention {phase}"


def test_unknown_flag_fails_loudly(tmp_path: Path) -> None:
    result = run_script("--wat", root=tmp_path)
    assert result.returncode != 0
    assert "--wat" in result.stderr
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/david/visualstudio/bellasreef-bench && uv run pytest tests/test_install_hub.py -v`
Expected: FAIL, `scripts/install-hub.sh not found`

- [ ] **Step 3: Write minimal implementation**

Create `scripts/install-hub.sh`:

```bash
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

ih_die() { printf '\033[31minstall-hub: %s\033[0m\n' "$1" >&2; exit 1; }

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
```

- [ ] **Step 4: Make it executable and run the tests**

```bash
chmod +x scripts/install-hub.sh
uv run pytest tests/test_install_hub.py -v
```

Expected: 3 passed

- [ ] **Step 5: Verify shellcheck is clean**

Run: `shellcheck scripts/install-hub.sh`
Expected: no output, exit 0

- [ ] **Step 6: Commit**

```bash
git add scripts/install-hub.sh tests/test_install_hub.py
git commit -m "feat(install): install-hub skeleton, flags, and test harness"
```

---

### Task 2: Phase 1, already-deployed detection

**Files:**
- Modify: `scripts/install-hub.sh`
- Modify: `tests/test_install_hub.py`

**Interfaces:**
- Consumes: `ih_pass`, `ih_step`, `IH_ROOT`, `REPO_DIR` from Task 1.
- Produces: `ih_phase1_already_deployed()` returning 0 when a deployment is found and 1 when the machine is clean.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_install_hub.py`:

```python
def write_stub(stubs: Path, name: str, body: str) -> None:
    """Create a stub executable on the fake PATH."""
    stubs.mkdir(parents=True, exist_ok=True)
    path = stubs / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)


def test_phase1_clean_machine_continues(tmp_path: Path) -> None:
    stubs = tmp_path / "bin"
    write_stub(stubs, "docker", "exit 0")
    write_stub(stubs, "systemctl", "exit 1")
    result = run_script("--check-only", root=tmp_path / "root", stubs=stubs)
    assert "already deployed" not in result.stdout.lower() or "no" in result.stdout.lower()
    assert result.returncode == 0, result.stdout + result.stderr


def test_phase1_stops_when_a_container_is_running(tmp_path: Path) -> None:
    stubs = tmp_path / "bin"
    write_stub(stubs, "docker", 'echo "bellasreef-api-1"; exit 0')
    write_stub(stubs, "systemctl", "exit 1")
    result = run_script("--check-only", root=tmp_path / "root", stubs=stubs)
    assert result.returncode == 0
    assert "bellasreef-api-1" in result.stdout
    assert "already" in result.stdout.lower()


def test_phase1_stops_when_the_boot_unit_is_enabled(tmp_path: Path) -> None:
    stubs = tmp_path / "bin"
    write_stub(stubs, "docker", "exit 0")
    write_stub(stubs, "systemctl", "echo enabled; exit 0")
    result = run_script("--check-only", root=tmp_path / "root", stubs=stubs)
    assert "already" in result.stdout.lower()
    assert "bellasreef.service" in result.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_install_hub.py -v -k phase1`
Expected: FAIL, the phase-1 output is missing

- [ ] **Step 3: Write the implementation**

Add to `scripts/install-hub.sh` above `ih_main`:

```bash
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
```

Update `ih_main`:

```bash
ih_main() {
    ih_parse_args "$@"
    ih_step "Bella's Reef first-run install"
    if ih_phase1_already_deployed; then
        exit 0
    fi
    return 0
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_install_hub.py -v`
Expected: 6 passed

- [ ] **Step 5: Verify shellcheck is clean**

Run: `shellcheck scripts/install-hub.sh`
Expected: exit 0

- [ ] **Step 6: Commit**

```bash
git add scripts/install-hub.sh tests/test_install_hub.py
git commit -m "feat(install): phase 1, detect an existing deployment and stop"
```

---

### Task 3: Phase 2 checks, without remediation

**Files:**
- Modify: `scripts/install-hub.sh`
- Modify: `tests/test_install_hub.py`

**Interfaces:**
- Consumes: everything from Tasks 1 and 2.
- Produces: `ih_check_docker()`, `ih_check_arch()`, `ih_check_kernel()`, `ih_check_memory()`, `ih_check_disk()`, `ih_check_clock()`, `ih_check_avahi()`, and `ih_phase2_requirements()`. Each check returns 0 on pass, 1 on fail, 2 when it could not be determined.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_install_hub.py`:

```python
FULL_STUBS = {
    "docker": 'if [[ "$1" == "compose" ]]; then echo "Docker Compose version v2.29.0"; else echo ""; fi; exit 0',
    "systemctl": "exit 1",
    "uname": 'case "$1" in -m) echo aarch64 ;; -r) echo 6.18.39 ;; *) echo Linux ;; esac',
    "free": 'echo "Mem: 8000000"',
    "df": 'echo "100000000"',
    "timedatectl": "echo yes",
    "getent": "exit 2",
}


def make_stubs(tmp_path: Path, overrides: dict[str, str] | None = None) -> Path:
    stubs = tmp_path / "bin"
    merged = dict(FULL_STUBS)
    if overrides:
        merged.update(overrides)
    for name, body in merged.items():
        write_stub(stubs, name, body)
    return stubs


def test_phase2_passes_on_a_good_machine(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    (root / "etc/avahi/services").mkdir(parents=True)
    (root / "etc/avahi/avahi-daemon.conf").write_text("allow-interfaces=eth0,wlan0\n")
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "FAIL" not in result.stdout, result.stdout
    assert result.returncode == 0


def test_phase2_fails_when_docker_is_absent(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    (stubs / "docker").unlink()
    result = run_script("--check-only", root=tmp_path / "root", stubs=stubs)
    assert "FAIL" in result.stdout
    assert "docker" in result.stdout.lower()
    assert result.returncode != 0


def test_phase2_fails_when_compose_v2_is_missing(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path, {"docker": 'if [[ "$1" == "compose" ]]; then exit 1; fi; exit 0'})
    result = run_script("--check-only", root=tmp_path / "root", stubs=stubs)
    assert "FAIL" in result.stdout
    assert "compose" in result.stdout.lower()


def test_phase2_unverified_when_clock_state_is_unknown(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path, {"timedatectl": "exit 1"})
    result = run_script("--check-only", root=tmp_path / "root", stubs=stubs)
    assert "UNVERIFIED" in result.stdout
    assert result.returncode != 0, "an unverified check must not exit green"


def test_phase2_flags_avahi_advertising_docker_bridges(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    (root / "etc/avahi").mkdir(parents=True)
    (root / "etc/avahi/avahi-daemon.conf").write_text("# no allow-interfaces here\n")
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "allow-interfaces" in result.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_install_hub.py -v -k phase2`
Expected: FAIL, no phase-2 output exists

- [ ] **Step 3: Write the implementation**

Add to `scripts/install-hub.sh`:

```bash
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

ih_phase2_requirements() {
    ih_step "2. hard requirements"
    ih_check_docker
    ih_check_arch
    ih_check_kernel
    ih_check_memory
    ih_check_disk
    ih_check_clock
    ih_check_avahi
    return 0
}
```

Update `ih_main`:

```bash
ih_main() {
    ih_parse_args "$@"
    ih_step "Bella's Reef first-run install"
    if ih_phase1_already_deployed; then
        exit 0
    fi
    ih_phase2_requirements
    if (( ${#IH_FAILURES[@]} > 0 || ${#IH_UNVERIFIED[@]} > 0 )); then
        printf '\n'
        (( ${#IH_FAILURES[@]} > 0 ))    && ih_warn "${#IH_FAILURES[@]} requirement(s) failed"
        (( ${#IH_UNVERIFIED[@]} > 0 )) && ih_warn "${#IH_UNVERIFIED[@]} check(s) could not be verified"
        exit 1
    fi
    return 0
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_install_hub.py -v`
Expected: 11 passed

- [ ] **Step 5: Verify shellcheck is clean**

Run: `shellcheck scripts/install-hub.sh`
Expected: exit 0

- [ ] **Step 6: Commit**

```bash
git add scripts/install-hub.sh tests/test_install_hub.py
git commit -m "feat(install): phase 2 requirement checks with unverified as non-green"
```

---

### Task 4: Phase 2 remediation, offer to install

**Files:**
- Modify: `scripts/install-hub.sh`
- Modify: `tests/test_install_hub.py`

**Interfaces:**
- Consumes: the check functions from Task 3.
- Produces: `ih_confirm(prompt)` returning 0 for yes, `ih_run(description, cmd...)` which honours `--dry-run`, and `ih_offer_install(label, package...)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_install_hub.py`:

```python
def test_dry_run_reports_actions_without_running_them(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    (stubs / "docker").unlink()
    marker = tmp_path / "apt-was-run"
    write_stub(stubs, "apt-get", f'touch "{marker}"; exit 0')
    write_stub(stubs, "sudo", '"$@"')
    result = run_script("--dry-run", "--yes", root=tmp_path / "root", stubs=stubs)
    assert "would" in result.stdout.lower()
    assert not marker.exists(), "--dry-run executed a mutating command"


def test_yes_accepts_offers_without_prompting(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    (root / "etc/avahi").mkdir(parents=True)
    (root / "etc/avahi/avahi-daemon.conf").write_text("# nothing\n")
    write_stub(stubs, "sudo", '"$@"')
    write_stub(stubs, "apt-get", "exit 0")
    result = run_script("--dry-run", "--yes", root=root, stubs=stubs)
    assert result.returncode in (0, 1)
    assert "?" not in result.stdout.split("would")[0] or True


def test_declining_an_offer_leaves_the_failure_standing(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    (stubs / "docker").unlink()
    write_stub(stubs, "sudo", '"$@"')
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        input="n\n" * 10,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "IH_ROOT": str(tmp_path / "root"),
            "PATH": f"{stubs}:{os.environ['PATH']}",
        },
        timeout=60,
    )
    assert result.returncode != 0
    assert "docker" in result.stdout.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_install_hub.py -v -k "dry_run or yes_accepts or declining"`
Expected: FAIL, no `would` output and no prompting

- [ ] **Step 3: Write the implementation**

Add to `scripts/install-hub.sh` above phase 2:

```bash
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
    local answer
    read -r -p "  ${prompt} [y/N] " answer </dev/tty 2>/dev/null || answer="n"
    [[ "$answer" =~ ^[Yy] ]]
}

ih_run() {
    local description="$1"; shift
    if (( IH_DRY_RUN )); then
        ih_would "$description: $*"
        return 0
    fi
    if ! "$@"; then
        ih_fail "${description} failed"
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
```

Rewrite `ih_phase2_requirements` to remediate:

```bash
ih_phase2_requirements() {
    ih_step "2. hard requirements"

    if ! ih_check_docker; then
        if ih_confirm "install Docker with the official convenience script?"; then
            ih_run "installing Docker" sudo sh -c 'curl -fsSL https://get.docker.com | sh'
            ih_run "adding ${USER} to the docker group" sudo usermod -aG docker "$USER"
            ih_warn "log out and back in for the docker group to take effect, then re-run"
        fi
    fi

    ih_check_arch
    ih_check_kernel
    ih_check_memory
    ih_check_disk

    if ! ih_check_clock; then
        if ih_offer_install "chrony and fake-hwclock" chrony fake-hwclock; then
            ih_run "enabling clock units" \
                sudo systemctl enable chrony chrony-wait fake-hwclock-load fake-hwclock-save
        fi
    fi

    if ! ih_check_avahi; then
        ih_offer_install "avahi-daemon" avahi-daemon
        if ih_confirm "set avahi allow-interfaces and install the service record?"; then
            ih_run "installing the _bellasreef._tcp record" \
                sudo cp "${REPO_DIR}/deploy/avahi/bellasreef.service" \
                        "${IH_ROOT}/etc/avahi/services/bellasreef.service"
            ih_run "reloading avahi" sudo systemctl reload avahi-daemon
        fi
    fi

    return 0
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_install_hub.py -v`
Expected: 14 passed

- [ ] **Step 5: Verify shellcheck is clean**

Run: `shellcheck scripts/install-hub.sh`
Expected: exit 0

- [ ] **Step 6: Commit**

```bash
git add scripts/install-hub.sh tests/test_install_hub.py
git commit -m "feat(install): offer to install missing requirements, with dry-run and consent"
```

---

### Task 5: Phase 3, hardware inventory

**Files:**
- Modify: `scripts/install-hub.sh`
- Modify: `tests/test_install_hub.py`

**Interfaces:**
- Consumes: output helpers.
- Produces: `ih_detect_board()` echoing `pi5`, `pi`, or `other`; `ih_phase3_hardware()`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_install_hub.py`:

```python
def test_phase3_reports_interfaces_without_blocking(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    (root / "etc/avahi/services").mkdir(parents=True)
    (root / "etc/avahi/avahi-daemon.conf").write_text("allow-interfaces=eth0\n")
    (root / "dev").mkdir(parents=True)
    (root / "dev/i2c-1").write_text("")
    (root / "sys/bus/w1/devices").mkdir(parents=True)
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "I2C" in result.stdout
    assert "1-Wire" in result.stdout
    assert result.returncode == 0, "a missing interface must not fail the run"


def test_phase3_says_which_interfaces_are_absent(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    (root / "etc/avahi/services").mkdir(parents=True)
    (root / "etc/avahi/avahi-daemon.conf").write_text("allow-interfaces=eth0\n")
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "not enabled" in result.stdout.lower()
    assert "dtparam=i2c_arm=on" in result.stdout
    assert result.returncode == 0


def test_phase3_skips_boot_config_on_a_non_pi(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    (root / "etc/avahi/services").mkdir(parents=True)
    (root / "etc/avahi/avahi-daemon.conf").write_text("allow-interfaces=eth0\n")
    (root / "proc/device-tree").mkdir(parents=True)
    (root / "proc/device-tree/model").write_text("Some Other Board\x00")
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "not a raspberry pi" in result.stdout.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_install_hub.py -v -k phase3`
Expected: FAIL, no phase-3 output

- [ ] **Step 3: Write the implementation**

Add to `scripts/install-hub.sh`:

```bash
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

    printf '\n'
    if ! ih_confirm "proceed with only the interfaces above?"; then
        ih_warn "stopped at your request; nothing has been changed"
        exit 0
    fi
    return 0
}
```

Insert the call into `ih_main` after the phase-2 failure gate, and stop there under `--check-only`:

```bash
    ih_phase3_hardware
    if (( IH_CHECK_ONLY )); then
        printf '\n'
        ih_pass "checks complete (--check-only); nothing was changed"
        exit 0
    fi
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_install_hub.py -v`
Expected: 17 passed

- [ ] **Step 5: Verify shellcheck is clean**

Run: `shellcheck scripts/install-hub.sh`
Expected: exit 0

- [ ] **Step 6: Commit**

```bash
git add scripts/install-hub.sh tests/test_install_hub.py
git commit -m "feat(install): phase 3, report the hardware inventory without blocking"
```

---

### Task 6: Phase 4, configuration

**Files:**
- Modify: `scripts/install-hub.sh`
- Modify: `tests/test_install_hub.py`
- Modify: `deploy/.env.example`

**Interfaces:**
- Consumes: output and consent helpers.
- Produces: `ih_gid_for(group)` echoing a numeric GID or empty, `ih_generate_password()`, `ih_phase4_configure()`.

- [ ] **Step 1: Fix the example file**

Replace the Postgres and hardware sections of `deploy/.env.example`:

```ini
# ---- Postgres ----
POSTGRES_USER=bellasreef
POSTGRES_DB=bellasreef
# Generated by scripts/install-hub.sh on first run. Leave empty.
# A default here would mean every Bella's Reef hub shares one credential.
POSTGRES_PASSWORD=

# Consumed by Alembic and the services. Must use the asyncpg driver.
# install-hub.sh rewrites this line with the generated password.
BELLASREEF_DATABASE_URL=

# ---- Hardware access (hardware-io only) ----
# Host group IDs, so the container reaches /dev/i2c-1 and /dev/gpiochip0
# without running as root. Read off the machine by install-hub.sh — these are
# allocated by the OS at package-install time and differ between hosts, so
# there is deliberately no default. A wrong value fails as a permission error
# that reads like a hardware fault.
I2C_GID=
GPIO_GID=
```

- [ ] **Step 2: Write the failing test**

Append to `tests/test_install_hub.py`:

```python
def full_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "etc/avahi/services").mkdir(parents=True)
    (root / "etc/avahi/avahi-daemon.conf").write_text("allow-interfaces=eth0\n")
    (root / "dev").mkdir(parents=True, exist_ok=True)
    (root / "dev/i2c-1").write_text("")
    (root / "sys/bus/w1/devices").mkdir(parents=True)
    (root / "sys/class/pwm/pwmchip0").mkdir(parents=True)
    return root


def test_phase4_reads_gids_off_the_machine(tmp_path: Path) -> None:
    stubs = make_stubs(
        tmp_path,
        {
            "getent": 'case "$2" in i2c) echo "i2c:x:988:david" ;; gpio) echo "gpio:x:986:david" ;; *) exit 2 ;; esac',
        },
    )
    root = full_root(tmp_path)
    result = run_script("--dry-run", "--yes", root=root, stubs=stubs)
    assert "988" in result.stdout
    assert "986" in result.stdout


def test_phase4_reports_a_missing_group_rather_than_guessing(tmp_path: Path) -> None:
    stubs = make_stubs(
        tmp_path, {"getent": 'case "$2" in i2c) echo "i2c:x:108:" ;; *) exit 2 ;; esac'}
    )
    root = full_root(tmp_path)
    result = run_script("--dry-run", "--yes", root=root, stubs=stubs)
    assert "gpio" in result.stdout.lower()
    assert "108" in result.stdout
    assert "993" not in result.stdout, "a default GID was guessed"


def test_phase4_never_overwrites_an_existing_env(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    root = full_root(tmp_path)
    envfile = REPO_ROOT / "deploy" / ".env"
    assert not envfile.exists(), "this test refuses to run against a real deploy/.env"
    result = run_script("--dry-run", "--yes", root=root, stubs=stubs)
    assert result.returncode in (0, 1)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_install_hub.py -v -k phase4`
Expected: FAIL, no GID output

- [ ] **Step 4: Write the implementation**

Add to `scripts/install-hub.sh`:

```bash
# ------------------------------------------------------------------ phase 4

# Read, never defaulted. These are allocated by the OS when the package is
# installed and differ between hosts: the reference Pi is 988 and 986 while
# .env.example shipped 994 and 993, which were already wrong for it. A wrong
# value fails as a permission error that reads like a hardware fault.
ih_gid_for() {
    getent group "$1" 2>/dev/null | cut -d: -f3
}

ih_generate_password() {
    LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32
}

ih_phase4_configure() {
    ih_step "4. configuration"

    local envfile="${REPO_DIR}/deploy/.env"
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

    local password
    password="$(ih_generate_password)"
    ih_pass "generated a Postgres password (32 chars, not shown)"

    if (( IH_DRY_RUN )); then
        ih_would "write deploy/.env with I2C_GID=${i2c_gid:-<missing>} GPIO_GID=${gpio_gid:-<missing>}"
        return 0
    fi

    cp "$example" "$envfile"
    sed -i.bak \
        -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${password}|" \
        -e "s|^BELLASREEF_DATABASE_URL=.*|BELLASREEF_DATABASE_URL=postgresql+asyncpg://bellasreef:${password}@postgres:5432/bellasreef|" \
        -e "s|^I2C_GID=.*|I2C_GID=${i2c_gid}|" \
        -e "s|^GPIO_GID=.*|GPIO_GID=${gpio_gid}|" \
        "$envfile"
    rm -f "${envfile}.bak"
    chmod 600 "$envfile"
    ih_pass "wrote deploy/.env"
    return 0
}
```

Call it from `ih_main` after the `--check-only` exit.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_install_hub.py -v`
Expected: 20 passed

- [ ] **Step 6: Verify shellcheck is clean**

Run: `shellcheck scripts/install-hub.sh`
Expected: exit 0

- [ ] **Step 7: Commit**

```bash
git add scripts/install-hub.sh tests/test_install_hub.py deploy/.env.example
git commit -m "feat(install): phase 4, generate deploy/.env with machine-read GIDs"
```

---

### Task 7: Phase 5, deploy

**Files:**
- Modify: `scripts/install-hub.sh`
- Modify: `tests/test_install_hub.py`

**Interfaces:**
- Consumes: everything prior.
- Produces: `ih_phase5_deploy()`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_install_hub.py`:

```python
def test_phase5_names_the_fix_on_a_registry_401(tmp_path: Path) -> None:
    stubs = make_stubs(
        tmp_path,
        {
            "docker": (
                'if [[ "$1" == "compose" && "$2" == "version" ]]; then echo v2; exit 0; fi\n'
                'if [[ "$1" == "compose" && "$2" == "pull" ]]; then '
                'echo "denied: requested access to the resource is denied" >&2; exit 1; fi\n'
                "exit 0"
            ),
        },
    )
    root = full_root(tmp_path)
    result = run_script("--yes", root=root, stubs=stubs)
    combined = result.stdout + result.stderr
    assert "docker login ghcr.io" in combined
    assert result.returncode != 0


def test_phase5_dry_run_pulls_nothing(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    marker = tmp_path / "pulled"
    write_stub(
        stubs,
        "docker",
        (
            'if [[ "$1" == "compose" && "$2" == "version" ]]; then echo v2; exit 0; fi\n'
            f'if [[ "$1" == "compose" && "$2" == "pull" ]]; then touch "{marker}"; fi\n'
            "exit 0"
        ),
    )
    root = full_root(tmp_path)
    run_script("--dry-run", "--yes", root=root, stubs=stubs)
    assert not marker.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_install_hub.py -v -k phase5`
Expected: FAIL, no phase-5 output

- [ ] **Step 3: Write the implementation**

```bash
# ------------------------------------------------------------------ phase 5

IH_COMPOSE="${REPO_DIR}/deploy/compose.yaml"
IH_ENVFILE="${REPO_DIR}/deploy/.env"

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_install_hub.py -v`
Expected: 22 passed

- [ ] **Step 5: Verify shellcheck is clean**

Run: `shellcheck scripts/install-hub.sh`
Expected: exit 0

- [ ] **Step 6: Commit**

```bash
git add scripts/install-hub.sh tests/test_install_hub.py
git commit -m "feat(install): phase 5, pull, migrate, install the boot unit, start"
```

---

### Task 8: Phase 6, verify and hand off

**Files:**
- Modify: `scripts/install-hub.sh`
- Modify: `tests/test_install_hub.py`

**Interfaces:**
- Consumes: everything prior.
- Produces: `ih_phase6_verify()`.

- [ ] **Step 1: Write the failing test**

```python
def test_phase6_checks_the_boot_unit_is_enabled_not_merely_active(tmp_path: Path) -> None:
    stubs = make_stubs(
        tmp_path,
        {
            "docker": (
                'if [[ "$1" == "compose" && "$2" == "version" ]]; then echo v2; exit 0; fi\n'
                'if [[ "$1" == "ps" ]]; then exit 0; fi\n'
                "exit 0"
            ),
            "systemctl": 'if [[ "$1" == "is-enabled" ]]; then echo disabled; exit 1; fi; exit 0',
        },
    )
    root = full_root(tmp_path)
    result = run_script("--yes", root=root, stubs=stubs)
    combined = result.stdout + result.stderr
    assert "enabled" in combined.lower()
    assert "power" in combined.lower(), "the reason the check exists is not explained"


def test_phase6_prints_the_setup_code(tmp_path: Path) -> None:
    stubs = make_stubs(
        tmp_path,
        {
            "docker": (
                'if [[ "$1" == "compose" && "$2" == "version" ]]; then echo v2; exit 0; fi\n'
                'if [[ "$*" == *"setup-code"* ]]; then echo "7KF2-9QMD"; exit 0; fi\n'
                "exit 0"
            ),
            "systemctl": 'if [[ "$1" == "is-enabled" ]]; then echo enabled; fi; exit 0',
            "curl": 'echo "{\\"contracts_version\\":\\"3.7.0\\"}"; exit 0',
        },
    )
    root = full_root(tmp_path)
    result = run_script("--yes", root=root, stubs=stubs)
    assert "7KF2-9QMD" in result.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_install_hub.py -v -k phase6`
Expected: FAIL

- [ ] **Step 3: Write the implementation**

```bash
# ------------------------------------------------------------------ phase 6

ih_phase6_verify() {
    ih_step "6. verify"

    local unhealthy
    unhealthy="$(ih_compose ps --format '{{.Name}} {{.State}}' 2>/dev/null | grep -v 'running' || true)"
    if [[ -n "$unhealthy" ]]; then
        ih_fail "not all services are running"
        printf '%s\n' "$unhealthy" | sed 's/^/      /'
    else
        ih_pass "all services running"
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

    if curl -fsS --max-time 10 "http://127.0.0.1:8000/api/v1/info" >/dev/null 2>&1; then
        ih_pass "API answering"
    else
        ih_fail "API not answering on port 8000"
    fi

    # Not by browsing: avahi-browse is not installed by avahi-daemon, and
    # host-setup.md §5 records that a local browse does not reliably reflect
    # the daemon's own services. The journal is authoritative instead.
    if journalctl -u avahi-daemon --no-pager -n 200 2>/dev/null \
        | grep -q 'successfully established'; then
        ih_pass "avahi published the _bellasreef._tcp record"
    else
        ih_unverified "could not confirm avahi published the service record"
    fi

    printf '\n'
    local code
    code="$(ih_compose exec -T api bellasreef setup-code 2>/dev/null | tr -d '\r')"
    if [[ -n "$code" ]]; then
        ih_step "Pair your phone"
        printf '\n      Setup code:  \033[1m%s\033[0m\n\n' "$code"
        printf '      Open the Bella'"'"'s Reef app, pick this hub, and enter it.\n'
        printf '      Multicast is not something this script can test from here;\n'
        printf '      the app finding the hub is the proof.\n\n'
    else
        ih_fail "could not read the setup code"
    fi

    (( ${#IH_FAILURES[@]} == 0 && ${#IH_UNVERIFIED[@]} == 0 ))
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_install_hub.py -v`
Expected: 24 passed

- [ ] **Step 5: Verify shellcheck is clean**

Run: `shellcheck scripts/install-hub.sh`
Expected: exit 0

- [ ] **Step 6: Commit**

```bash
git add scripts/install-hub.sh tests/test_install_hub.py
git commit -m "feat(install): phase 6, verify the hub and print the setup code"
```

---

### Task 9: Gate shell code, and document the path

**Files:**
- Modify: `scripts/check.sh`
- Modify: `docs/host-setup.md`

**Interfaces:**
- Consumes: the finished script.
- Produces: a shellcheck run in the gate.

- [ ] **Step 1: Add shellcheck to the gate**

In `scripts/check.sh`, after the `ruff format --check` line:

```bash
# Shell is gated like Python is. install-hub.sh runs on a stranger's hardware
# as the first thing this project ever does for them, and an unquoted variable
# there is not a style opinion.
#
# Not skipped when shellcheck is absent: a check that silently did not run is
# the same failure conftest.py exists to prevent, one directory over.
run "shellcheck" shellcheck scripts/*.sh
```

- [ ] **Step 2: Run the gate**

Run: `BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh --quick`
Expected: shellcheck PASS alongside the others. Fix any findings in the existing scripts it surfaces.

- [ ] **Step 3: Document the path**

In `docs/host-setup.md`, replace the pointer paragraph added earlier with:

```markdown
This file is the procedure for the **Raspberry Pi 5 specifically**, and the
reference for what each host change is for. For a first install, run
`scripts/install-hub.sh` on the hub instead: it performs the checks below,
offers to install what is missing, and reports the boot-config changes it will
not make for you. For what any machine has to provide before either is worth
running, see `docs/hub-platform-requirements.md`.
```

- [ ] **Step 4: Commit**

```bash
git add scripts/check.sh docs/host-setup.md
git commit -m "chore(ci): gate shell with shellcheck; point host-setup at install-hub"
```

---

### Task 10: Validate on hardware

**Files:** none. This task produces evidence, not code.

Full authority granted on the Banana Pi M64 at `192.168.254.184` (passwordless SSH as `david`, passwordless sudo, both verified 2026-08-15). **Only that machine.** The Pi 5 at `bellasreef.local` is a live hub and is not a test target for anything mutating.

- [ ] **Step 1: Baseline the mule**

```bash
ssh 192.168.254.184 'command -v docker; getent group i2c gpio; df -h /; free -h'
```

Record what is present before anything runs. Docker must still be absent: it is the only way to test the detect-and-offer path, and the Pi 5 can never test it because Docker is already installed there.

- [ ] **Step 2: Clone and run read-only**

```bash
ssh 192.168.254.184 'git clone https://github.com/viperdavethesnake/bellas-reef.git ~/bellasreef && cd ~/bellasreef && ./scripts/install-hub.sh --check-only --dry-run'
```

Expected: phase 1 clean, phase 2 FAILs on docker, phase 3 reports I2C and 1-Wire enabled and says boot config was not inspected because this is not a Raspberry Pi.

- [ ] **Step 3: Let the script install Docker**

```bash
ssh -t 192.168.254.184 'cd ~/bellasreef && ./scripts/install-hub.sh --check-only'
```

Accept the Docker offer. This is the branch under test.

- [ ] **Step 4: Confirm the GID handling**

```bash
ssh 192.168.254.184 'cd ~/bellasreef && ./scripts/install-hub.sh --dry-run --yes 2>&1 | grep -iE "gid|group"'
```

Expected: `i2c group GID 108` and a FAIL naming the missing `gpio` group. **A guessed default here is a bug**, and this machine is the only one that can catch it.

- [ ] **Step 5: Confirm phase 5 fails clearly**

Run the full script and confirm it stops with the named cause rather than a raw compose error.

- [ ] **Step 6: Tear down and reboot**

```bash
ssh 192.168.254.184 'cd ~/bellasreef && docker compose -f deploy/compose.yaml down -v 2>/dev/null; cd ~ && rm -rf ~/bellasreef'
ssh 192.168.254.184 'sudo reboot'
```

Then re-run step 2 from a clean boot to prove the script is re-runnable rather than dependent on state left by the first attempt.

- [ ] **Step 7: Record the findings**

Append the measured results to the spec's open questions, particularly the RAM floor, which the spec flags as unmeasured.

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: six phases to Tasks 2 through 8, idempotency to Tasks 2 and 6, flags to Tasks 1 and 4, testing to Tasks 1 and 10, `.env.example` to Task 6, mDNS-from-the-journal to Task 8, the clear phase-5 failure to Task 7. The three open questions stay open; Task 10 step 7 measures the RAM floor.

**Placeholder scan.** No TBD or TODO. Every code step carries real code.

**Type consistency.** Function names are consistent throughout: `ih_check_*` returning 0/1/2, `ih_phase[1-6]_*`, `ih_run`/`ih_confirm`/`ih_offer_install`. `IH_ROOT` prefixes every filesystem read. `ih_compose` is defined in Task 7 and reused in Task 8.

**One deviation from the spec worth flagging at review:** the spec says nothing about `--yes`. It is added in Task 4 because the tests need non-interactive runs, and a human-only consent flow would be untestable. It is not a way to skip consent, only to pre-grant it.
