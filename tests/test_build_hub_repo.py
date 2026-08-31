# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""scripts/build-hub-repo.sh assembles exactly the user-facing payload."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build-hub-repo.sh"
SHA = "0123456789abcdef0123456789abcdef01234567"

# The whole contract in one set. A file added under hub/ shows up here or the
# test fails, which is the point: the payload is reviewed, not accumulated.
EXPECTED = {
    "LICENSE",
    "README.md",
    ".gitignore",
    "deploy/compose.yaml",
    "deploy/.env.example",
    "deploy/release.env",
    "deploy/systemd/bellasreef.service",
    "deploy/avahi/bellasreef.service",
    "deploy/config/devices.yaml.example",
    "scripts/install-hub.sh",
    "scripts/update-hub.sh",
    "scripts/factory-reset-hub.sh",
    "docs/host-setup.md",
    "docs/hub-platform-requirements.md",
    "docs/backup-restore.md",
}


def build(
    out: Path, version: str = "v0.2.0-rc.2", sha: str = SHA
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), str(out), version, sha], capture_output=True, text=True, check=False
    )


def relative_files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def test_assembles_exactly_the_payload(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = build(out)
    assert result.returncode == 0, result.stderr
    assert relative_files(out) == EXPECTED


def test_release_env_pins_version_tag_and_contracts(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert build(out).returncode == 0
    lines = (out / "deploy/release.env").read_text().splitlines()
    assert lines[0] == "BELLASREEF_VERSION=v0.2.0-rc.2"
    assert lines[1] == f"BELLASREEF_TAG={SHA}"
    # The contracts version is the one the avahi record advertises, which
    # check.sh already gates against the installed package.
    record = (REPO_ROOT / "hub/deploy/avahi/bellasreef.service").read_text()
    advertised = record.split("<txt-record>contracts=")[1].split("<")[0]
    assert lines[2] == f"BELLASREEF_CONTRACTS={advertised}"


def test_payload_compose_has_no_build_blocks(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert build(out).returncode == 0
    assert "build:" not in (out / "deploy/compose.yaml").read_text()


def test_rejects_a_bad_version_or_sha(tmp_path: Path) -> None:
    assert build(tmp_path / "a", version="0.2.0").returncode == 2
    assert build(tmp_path / "b", sha="abc").returncode == 2
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()


def test_refuses_a_non_empty_output_directory(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "stale").write_text("")
    result = build(out)
    assert result.returncode == 1
    assert "not empty" in result.stderr


def test_assembler_never_packs_a_secrets_file(tmp_path: Path) -> None:
    # A developer's own hub/deploy/.env (or a stray hub/deploy/.env.local)
    # must never ride along into the payload just because it happens to
    # exist in the dev tree when the assembler runs.
    dev_env = REPO_ROOT / "hub/deploy/.env"
    assert not dev_env.exists(), "dev tree already has hub/deploy/.env; refusing to overwrite it"
    dev_env.write_text("POSTGRES_PASSWORD=should-never-ship\n")
    try:
        out = tmp_path / "out"
        result = build(out)
        assert result.returncode == 0, result.stderr
        assert not (out / "deploy/.env").exists()
        assert (out / "deploy/.env.example").exists()
    finally:
        dev_env.unlink(missing_ok=True)
