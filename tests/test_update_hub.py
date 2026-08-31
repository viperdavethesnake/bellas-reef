# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""update-hub.sh is a skeleton by ruling (2026-08-30): usage and the recorded
design, and an honest not-implemented exit. These tests pin exactly that."""

from __future__ import annotations

from pathlib import Path

from tests.hub_script_harness import run_any_script

SCRIPT = Path(__file__).resolve().parents[1] / "hub/scripts/update-hub.sh"


def test_help_names_the_planned_phases(tmp_path: Path) -> None:
    result = run_any_script(SCRIPT, "--help", root=tmp_path)
    assert result.returncode == 0
    for phase in ("installed", "release", "backup", "pull", "verify"):
        assert phase in result.stdout.lower(), result.stdout


def test_a_plain_run_says_not_implemented_and_exits_70(tmp_path: Path) -> None:
    result = run_any_script(SCRIPT, root=tmp_path)
    assert result.returncode == 70
    assert "not implemented" in result.stderr.lower(), result.stderr
