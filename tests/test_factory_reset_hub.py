# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Black-box tests for hub/scripts/factory-reset-hub.sh — the on-Pi reset."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.hub_script_harness import run_any_script, write_stub

REPO_ROOT = Path(__file__).resolve().parents[1]
HUB_ROOT = REPO_ROOT / "hub"
SCRIPT = HUB_ROOT / "scripts" / "factory-reset-hub.sh"
FAST = {"FR_API_DEADLINE_SECS": "2", "FR_STREAM_DEADLINE_SECS": "2", "FR_POLL_SECS": "0"}

INFO_FRESH = '{"paired_client_count":0,"setup_mode":true}'
INFO_PAIRED = '{"paired_client_count":1,"setup_mode":false}'
HW_LOG = "\n".join(['{"msg":"stream created"}'] * 7 + ['{"msg":"capability announced"}'])


def installed_root(tmp_path: Path) -> Path:
    """A fixture root on which a hub is installed: the boot unit exists and
    deploy/.env is non-empty (the two things the script refuses without)."""
    root = tmp_path / "root"
    (root / "etc/systemd/system").mkdir(parents=True, exist_ok=True)
    (root / "etc/systemd/system/bellasreef.service").write_text("[Unit]\n")
    envfile = root.joinpath(*HUB_ROOT.parts[1:]) / "deploy" / ".env"
    envfile.parent.mkdir(parents=True, exist_ok=True)
    envfile.write_text("POSTGRES_PASSWORD=x\nBELLASREEF_BACKUP_DIR=/home/tester/backups\n")
    return root


def docker_stub(
    log: Path,
    *,
    info: str = INFO_FRESH,
    psql: str = "0\n0",
    psql_rc: int = 0,
    alembic: str = "0020 (head)",
    alembic_rc: int = 0,
    volumes: str = "bellasreef_nats-data\nbellasreef_postgres-data\nbellasreef_vm-data",
    hw_log: str = HW_LOG,
    backup_rc: int = 0,
) -> str:
    return f"""
echo "docker $*" >> "{log}"
case "$1" in
  compose)
    shift; while [[ "$1" == -* ]]; do shift 2; done
    case "$1" in
      exec)
        case "$*" in
          *"bellasreef backup"*) exit {backup_rc} ;;
          *psql*) printf '{psql}\\n'; exit {psql_rc} ;;
          *"alembic current"*) echo "{alembic}"; exit {alembic_rc} ;;
          *"setup-code"*) echo "7KF2-9QMD"; exit 0 ;;
        esac ;;
      down|run) exit 0 ;;
    esac ;;
  volume)
    case "$2" in ls) printf '{volumes}\\n'; exit 0 ;; rm) exit 0 ;; esac ;;
  logs) printf '%s\\n' '{hw_log}'; exit 0 ;;
esac
exit 0
"""


def stubs_for(tmp_path: Path, log: Path, **docker_kwargs: object) -> Path:
    stubs = tmp_path / "bin"
    write_stub(stubs, "docker", docker_stub(log, **docker_kwargs))  # type: ignore[arg-type]
    write_stub(stubs, "sudo", f'echo "sudo $*" >> "{log}"; exit 0')
    write_stub(stubs, "systemctl", f'echo "systemctl $*" >> "{log}"; exit 0')
    write_stub(stubs, "curl", f"printf '%s' '{docker_kwargs.get('info', INFO_FRESH)}'; exit 0")
    return stubs


def run(
    tmp_path: Path, *args: str, confirm: str = "factory-reset\n", **kw: object
) -> tuple[subprocess.CompletedProcess[str], str]:
    log = tmp_path / "actions.log"
    stubs = stubs_for(tmp_path, log, **kw)
    root = installed_root(tmp_path)
    result = run_any_script(SCRIPT, *args, root=root, stubs=stubs, env=FAST, input=confirm)
    return result, (log.read_text() if log.exists() else "")


def test_help_and_unknown_args_exit_before_anything_runs(tmp_path: Path) -> None:
    result, log = run(tmp_path, "--help")
    assert result.returncode == 0 and "DESTROYS" in result.stdout
    assert log == ""
    result, log = run(tmp_path, "--now")
    assert result.returncode == 1
    assert log == ""


def test_refuses_an_uninstalled_hub_before_the_backup(tmp_path: Path) -> None:
    log = tmp_path / "actions.log"
    stubs = stubs_for(tmp_path, log)
    root = tmp_path / "root"
    root.mkdir()
    result = run_any_script(SCRIPT, root=root, stubs=stubs, env=FAST, input="factory-reset\n")
    assert result.returncode == 1
    assert "nothing to reset" in result.stderr, result.stderr
    assert not log.exists()


def test_backup_runs_before_the_prompt_and_a_decline_touches_nothing_else(tmp_path: Path) -> None:
    result, log = run(tmp_path, confirm="no\n")
    assert result.returncode == 1
    assert "not confirmed" in result.stderr
    assert "bellasreef backup" in log
    assert "down" not in log and "volume rm" not in log and "systemctl stop" not in log


def test_a_failed_backup_aborts_with_nothing_touched(tmp_path: Path) -> None:
    result, log = run(tmp_path, backup_rc=1)
    assert result.returncode == 1
    assert "backup failed" in result.stderr
    assert "systemctl stop" not in log


def test_destroys_in_order_and_redeploys_through_the_boot_unit(tmp_path: Path) -> None:
    result, log = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [ln for ln in log.splitlines() if ln]
    stop = next(i for i, ln in enumerate(lines) if ln.startswith("sudo systemctl stop bellasreef"))
    down = next(i for i, ln in enumerate(lines) if " down" in ln)
    rm = next(i for i, ln in enumerate(lines) if "volume rm" in ln)
    migrate = next(i for i, ln in enumerate(lines) if "alembic upgrade head" in ln)
    start = next(
        i for i, ln in enumerate(lines) if ln.startswith("sudo systemctl start bellasreef")
    )
    assert stop < down < rm < migrate < start, lines
    assert "systemctl restart" not in log
    assert "7KF2-9QMD" in result.stdout


def test_a_missing_volume_is_not_found_rather_than_removed_nothing(tmp_path: Path) -> None:
    result, log = run(tmp_path, volumes="bellasreef_nats-data\nbellasreef_postgres-data")
    assert result.returncode == 1
    assert "bellasreef_vm-data" in result.stderr and "not found" in result.stderr
    assert "volume rm" not in log


def test_post_reset_assertions_fail_the_run(tmp_path: Path) -> None:
    result, _ = run(tmp_path, info=INFO_PAIRED)
    assert result.returncode == 1 and "paired_client_count=1" in result.stderr
    result, _ = run(tmp_path, psql="3\n0")
    assert result.returncode == 1 and "3 device(s)" in result.stderr
    result, _ = run(tmp_path, alembic="0019")
    assert result.returncode == 1 and "not at head" in result.stderr
    result, _ = run(tmp_path, hw_log='{"msg":"stream created"}')
    assert result.returncode == 1 and "1 of 7" in result.stderr
    result, _ = run(tmp_path, psql_rc=1)
    assert result.returncode == 1 and "NOT confirmed factory-fresh" in result.stderr
    result, _ = run(tmp_path, alembic_rc=1)
    assert result.returncode == 1 and "not confirmed at head" in result.stderr
