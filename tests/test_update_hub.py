# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Black-box tests for scripts/update-hub.sh.

Same harness as install-hub's suite: the script runs as a subprocess against
a fixture root with stub executables on an isolated PATH. The design under
test is the one ruled in the script's own header (2026-08-30): installed? →
choose release → checkout + re-exec → mandatory backup → pull/migrate/up
(app services only) → verify with three outcomes (PASS | NO DEVICES | FAIL).
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.hub_script_harness import (
    FAKE_COMMIT,
    run_any_script,
    strip_ansi,
    write_release_env,
    write_stub,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
HUB_ROOT = REPO_ROOT / "hub"
SCRIPT = HUB_ROOT / "scripts" / "update-hub.sh"

NEW_SHA = "89d143f0123456789abcdef0123456789abcdef0"

#: Fast verify-loop seams, so a FAIL outcome costs one poll, not a minute.
FAST = {"UH_TELEMETRY_DEADLINE_SECS": "2", "UH_POLL_SECS": "1"}


def hub_fixture(tmp_path: Path, deployed_tag: str = "oldsha") -> Path:
    """A root that phase 1 accepts: the boot unit and a populated deploy/.env.

    The .env lives at IH_ROOT + <real repo>/hub/deploy/.env, the same seam
    factory-reset-hub.sh uses, so the script under test resolves it without
    touching the real repository's deploy directory.
    """
    root = tmp_path / "root"
    (root / "etc/systemd/system").mkdir(parents=True)
    (root / "etc/systemd/system/bellasreef.service").write_text("[Unit]\n")
    envdir = root / str(HUB_ROOT / "deploy").lstrip("/")
    envdir.mkdir(parents=True)
    (envdir / ".env").write_text(
        f"POSTGRES_PASSWORD=x\nBELLASREEF_TAG={deployed_tag}\n"
        "BELLASREEF_BACKUP_DIR=/home/tester/backups\n"
    )
    return root


VM_HIT = json.dumps(
    {"status": "success", "data": {"resultType": "vector", "result": [{"value": [1, "3"]}]}}
)
VM_MISS = json.dumps({"status": "success", "data": {"resultType": "vector", "result": []}})


def write_update_stubs(
    stubs: Path,
    tmp_path: Path,
    *,
    tags: str = "v0.1.0\nv0.2.0\nv0.2.0-rc.4\nv0.2.0-rc.9",
    current: str = "v0.1.0",
    device_count: str = "3",
    backup_exit: int = 0,
    vm_body: str = VM_HIT,
) -> dict[str, Path]:
    """Stub git, docker and curl; every call appends to a log."""
    git_log = tmp_path / "git.log"
    docker_log = tmp_path / "docker.log"
    curl_log = tmp_path / "curl.log"
    write_stub(
        stubs,
        "git",
        "\n".join(
            [
                f'echo "$*" >> "{git_log}"',
                'args="$*"',
                'case "$args" in',
                # `tag -l v*` answers the candidate list; `describe` the
                # current checkout; everything else just succeeds.
                f'  *"tag -l"*) printf "%s\\n" {tags.replace(chr(10), " ")} ;;',
                f'  *describe*) echo "{current}" ;;',
                "esac",
                "exit 0",
            ]
        ),
    )
    write_stub(
        stubs,
        "docker",
        "\n".join(
            [
                f'echo "$*" >> "{docker_log}"',
                'args="$*"',
                'case "$args" in',
                f'  *"bellasreef backup"*) exit {backup_exit} ;;',
                f'  *psql*) echo "{device_count}"; exit 0 ;;',
                "esac",
                "exit 0",
            ]
        ),
    )
    write_stub(stubs, "curl", f'echo "$*" >> "{curl_log}"\nprintf %s \'{vm_body}\'\nexit 0')
    return {"git": git_log, "docker": docker_log, "curl": curl_log}


def run_update(
    *args: str,
    root: Path,
    stubs: Path,
    env: dict[str, str] | None = None,
) -> object:
    merged = dict(FAST)
    if env:
        merged.update(env)
    return run_any_script(SCRIPT, *args, root=root, stubs=stubs, env=merged)


def test_help_names_the_phases(tmp_path: Path) -> None:
    result = run_any_script(SCRIPT, "--help", root=tmp_path)
    assert result.returncode == 0
    for phase in ("installed", "release", "backup", "pull", "verify"):
        assert phase in result.stdout.lower(), result.stdout


def test_refuses_a_machine_that_is_not_a_hub(tmp_path: Path) -> None:
    stubs = tmp_path / "bin"
    write_update_stubs(stubs, tmp_path)
    result = run_update(root=tmp_path / "empty-root", stubs=stubs)
    assert result.returncode != 0
    assert "install-hub" in (result.stdout + result.stderr)


def test_a_plain_run_updates_to_the_newest_stable_tag(tmp_path: Path) -> None:
    # v0.2.0-rc.9 sorts newest but carries a pre-release suffix; a plain run
    # must land on v0.2.0. (And never on main: only v* tags are candidates —
    # the header's open question, answered "no".)
    root = hub_fixture(tmp_path)
    stubs = tmp_path / "bin"
    logs = write_update_stubs(stubs, tmp_path)
    write_release_env(tmp_path / "release.env", version="v0.2.0", tag=NEW_SHA)
    result = run_update(
        root=root, stubs=stubs, env={"IH_RELEASE_ENV": str(tmp_path / "release.env")}
    )
    out = strip_ansi(result.stdout)
    assert result.returncode == 0, out + result.stderr
    git = logs["git"].read_text()
    assert "checkout --quiet v0.2.0" in git, git
    assert "checkout --quiet v0.2.0-rc.9" not in git
    docker = logs["docker"].read_text()
    assert "pull" in docker
    assert "alembic upgrade head" in docker
    assert "up -d --wait hardware-io control-engine api" in docker, docker
    assert "PASS" in out


def test_pre_flag_allows_the_newest_prerelease(tmp_path: Path) -> None:
    root = hub_fixture(tmp_path)
    stubs = tmp_path / "bin"
    logs = write_update_stubs(stubs, tmp_path)
    write_release_env(tmp_path / "release.env", version="v0.2.0-rc.9", tag=NEW_SHA)
    result = run_update(
        "--pre", root=root, stubs=stubs, env={"IH_RELEASE_ENV": str(tmp_path / "release.env")}
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "checkout --quiet v0.2.0-rc.9" in logs["git"].read_text()


def test_ref_pins_a_specific_tag(tmp_path: Path) -> None:
    root = hub_fixture(tmp_path, deployed_tag=NEW_SHA)
    stubs = tmp_path / "bin"
    logs = write_update_stubs(stubs, tmp_path)
    write_release_env(tmp_path / "release.env", version="v0.1.0", tag=NEW_SHA)
    result = run_update(
        "--ref",
        "v0.1.0",
        root=root,
        stubs=stubs,
        env={"IH_RELEASE_ENV": str(tmp_path / "release.env")},
    )
    # --ref to the tag we are already on is "nothing to do", which is success.
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already" in strip_ansi(result.stdout).lower()
    assert "checkout" not in logs["git"].read_text()


def test_already_current_deploys_nothing(tmp_path: Path) -> None:
    root = hub_fixture(tmp_path, deployed_tag=FAKE_COMMIT)
    stubs = tmp_path / "bin"
    logs = write_update_stubs(stubs, tmp_path, current="v0.2.0")
    result = run_update(root=root, stubs=stubs)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already" in strip_ansi(result.stdout).lower()
    assert not logs["docker"].exists(), "a no-op update ran docker anyway"


def test_backup_failure_aborts_before_any_deploy(tmp_path: Path) -> None:
    root = hub_fixture(tmp_path)
    stubs = tmp_path / "bin"
    logs = write_update_stubs(stubs, tmp_path, backup_exit=1)
    write_release_env(tmp_path / "release.env", version="v0.2.0", tag=NEW_SHA)
    result = run_update(
        root=root, stubs=stubs, env={"IH_RELEASE_ENV": str(tmp_path / "release.env")}
    )
    assert result.returncode != 0
    docker = logs["docker"].read_text()
    assert "pull" not in docker, "deploy proceeded past a failed backup:\n" + docker
    assert "backup" in (result.stdout + result.stderr).lower()


def test_no_devices_is_a_clean_outcome_that_points_at_adoption(tmp_path: Path) -> None:
    root = hub_fixture(tmp_path)
    stubs = tmp_path / "bin"
    write_update_stubs(stubs, tmp_path, device_count="0", vm_body=VM_MISS)
    write_release_env(tmp_path / "release.env", version="v0.2.0", tag=NEW_SHA)
    result = run_update(
        root=root, stubs=stubs, env={"IH_RELEASE_ENV": str(tmp_path / "release.env")}
    )
    out = strip_ansi(result.stdout)
    assert result.returncode == 0, out + result.stderr
    assert "NO DEVICES" in out, out
    assert "adopt" in out.lower(), out


def test_devices_with_no_telemetry_fails(tmp_path: Path) -> None:
    root = hub_fixture(tmp_path)
    stubs = tmp_path / "bin"
    write_update_stubs(stubs, tmp_path, device_count="2", vm_body=VM_MISS)
    write_release_env(tmp_path / "release.env", version="v0.2.0", tag=NEW_SHA)
    result = run_update(
        root=root, stubs=stubs, env={"IH_RELEASE_ENV": str(tmp_path / "release.env")}
    )
    assert result.returncode != 0
    assert "telemetry" in (result.stdout + result.stderr).lower()


def test_env_tag_is_rewritten_from_the_release_manifest(tmp_path: Path) -> None:
    root = hub_fixture(tmp_path)
    stubs = tmp_path / "bin"
    write_update_stubs(stubs, tmp_path)
    write_release_env(tmp_path / "release.env", version="v0.2.0", tag=NEW_SHA)
    result = run_update(
        root=root, stubs=stubs, env={"IH_RELEASE_ENV": str(tmp_path / "release.env")}
    )
    assert result.returncode == 0, result.stdout + result.stderr
    envfile = root / str(HUB_ROOT / "deploy").lstrip("/") / ".env"
    assert f"BELLASREEF_TAG={NEW_SHA}" in envfile.read_text()


def test_a_plain_run_never_downgrades(tmp_path: Path) -> None:
    # Real trap from the real tag list: v0.1.0 is the newest STABLE tag while
    # the hub runs v0.2.0-rc.4. "Newest stable" must never mean "older than
    # what is running" — that is a downgrade wearing an update's clothes.
    root = hub_fixture(tmp_path, deployed_tag=FAKE_COMMIT)
    stubs = tmp_path / "bin"
    logs = write_update_stubs(stubs, tmp_path, tags="v0.1.0\nv0.2.0-rc.4", current="v0.2.0-rc.4")
    result = run_update(root=root, stubs=stubs)
    out = strip_ansi(result.stdout)
    assert result.returncode == 0, out + result.stderr
    assert "newer" in out.lower() or "nothing" in out.lower(), out
    assert "checkout" not in logs["git"].read_text()
    assert not logs["docker"].exists(), "a refused downgrade ran docker anyway"


def test_graduation_from_rc_to_the_same_version_is_an_upgrade(tmp_path: Path) -> None:
    # The planned path: rc graduates to v0.2.0 when hub testing passes. In
    # semver 0.2.0-rc.4 < 0.2.0, but `sort -V` says the opposite — a naive
    # newest-of comparison would refuse the graduation as a "downgrade".
    root = hub_fixture(tmp_path)
    stubs = tmp_path / "bin"
    logs = write_update_stubs(
        stubs, tmp_path, tags="v0.1.0\nv0.2.0\nv0.2.0-rc.4", current="v0.2.0-rc.4"
    )
    write_release_env(tmp_path / "release.env", version="v0.2.0", tag=NEW_SHA)
    result = run_update(
        root=root, stubs=stubs, env={"IH_RELEASE_ENV": str(tmp_path / "release.env")}
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "checkout --quiet v0.2.0" in logs["git"].read_text(), logs["git"].read_text()


def test_checkout_already_on_target_but_undeployed_still_deploys(tmp_path: Path) -> None:
    # The bootstrap case, verbatim from coco: the rc.4 clone carries only the
    # skeleton, so the first real update is `git checkout v0.2.0-rc.5` by
    # hand and THEN this script. The checkout now matches the target tag, but
    # deploy/.env still names the old image sha — "already on the tag" must
    # mean "already DEPLOYED", or this exits 0 with the old release running.
    root = hub_fixture(tmp_path)
    stubs = tmp_path / "bin"
    logs = write_update_stubs(stubs, tmp_path, current="v0.2.0")
    write_release_env(tmp_path / "release.env", version="v0.2.0", tag=NEW_SHA)
    result = run_update(
        root=root, stubs=stubs, env={"IH_RELEASE_ENV": str(tmp_path / "release.env")}
    )
    out = strip_ansi(result.stdout)
    assert result.returncode == 0, out + result.stderr
    docker = logs["docker"].read_text()
    assert "pull" in docker, "an undeployed checkout was left undeployed:\n" + out
    envfile = root / str(HUB_ROOT / "deploy").lstrip("/") / ".env"
    assert f"BELLASREEF_TAG={NEW_SHA}" in envfile.read_text()


def test_checkout_on_target_and_deployed_is_a_true_no_op(tmp_path: Path) -> None:
    root = hub_fixture(tmp_path, deployed_tag=NEW_SHA)
    stubs = tmp_path / "bin"
    logs = write_update_stubs(stubs, tmp_path, current="v0.2.0")
    write_release_env(tmp_path / "release.env", version="v0.2.0", tag=NEW_SHA)
    result = run_update(
        root=root, stubs=stubs, env={"IH_RELEASE_ENV": str(tmp_path / "release.env")}
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already" in strip_ansi(result.stdout).lower()
    assert not logs["docker"].exists(), "a truly deployed no-op ran docker anyway"
