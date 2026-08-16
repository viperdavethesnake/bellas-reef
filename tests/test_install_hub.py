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


def write_stub(stubs: Path, name: str, body: str) -> None:
    """Create a stub executable on the fake PATH."""
    stubs.mkdir(parents=True, exist_ok=True)
    path = stubs / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)


def write_good_avahi_fixture(root: Path) -> None:
    """Populate an $IH_ROOT fixture with everything ih_check_avahi wants:
    the allowlisted daemon config and an installed _bellasreef._tcp record."""
    (root / "etc/avahi/services").mkdir(parents=True)
    (root / "etc/avahi/avahi-daemon.conf").write_text("allow-interfaces=eth0,wlan0\n")
    (root / "etc/avahi/services/bellasreef.service").write_text(
        "<service-group><name>bellasreef</name></service-group>\n"
    )


def test_phase1_clean_machine_continues(tmp_path: Path) -> None:
    # Phase 1 finding nothing means ih_main falls through into phase 2, so a
    # clean-machine fixture here needs phase 2 to also see a good machine —
    # otherwise this is no longer testing phase 1 in isolation, it is
    # testing phase 1 against a phase 2 that is bound to fail.
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "no existing deployment found" in result.stdout
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


def test_phase1_stops_when_deploy_env_exists(tmp_path: Path) -> None:
    stubs = tmp_path / "bin"
    write_stub(stubs, "docker", "exit 0")
    write_stub(stubs, "systemctl", "exit 1")
    root = tmp_path / "root"
    # REPO_DIR (as the script computes it) is this repo's absolute path, so
    # under a fixture root the script reads ${root}${REPO_DIR}/deploy/.env.
    # Derive that nested path instead of hardcoding it.
    envfile = root.joinpath(*REPO_ROOT.parts[1:]) / "deploy" / ".env"
    envfile.parent.mkdir(parents=True, exist_ok=True)
    envfile.write_text("SOME_SETTING=value\n")
    result = run_script("--check-only", root=root, stubs=stubs)
    assert result.returncode == 0
    assert "already" in result.stdout.lower()
    assert "deploy/.env" in result.stdout


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
    write_good_avahi_fixture(root)
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


def test_phase2_fails_when_the_service_record_is_missing(tmp_path: Path) -> None:
    # The services/ directory exists (avahi-daemon is installed and the
    # allowlist is set) but nobody has written bellasreef.service into it.
    # This is exactly the case a later task's remediation must detect and
    # fix, so the check has to name it and the run must not exit green.
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    (root / "etc/avahi/services").mkdir(parents=True)
    (root / "etc/avahi/avahi-daemon.conf").write_text("allow-interfaces=eth0,wlan0\n")
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "service record" in result.stdout.lower()
    assert result.returncode != 0
