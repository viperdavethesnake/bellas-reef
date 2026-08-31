# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Subprocess-and-stubs harness shared by the hub scripts' black-box suites.

`install-hub.sh`, `update-hub.sh` and `factory-reset-hub.sh` are all driven as
subprocesses against a fixture root with stub executables on an isolated PATH
— mocking bash functions from pytest would test a script nobody runs, and the
ordering rules these suites check (phase order, what stops the run, what only
warns) are exactly the properties a stubbed-out version would stop checking.
This module is the part of that approach none of the three scripts' tests
need to repeat: the isolated PATH itself, the fixture-root environment, a
generic subprocess runner, and the release-manifest fixtures every run needs
whether or not a given test cares about it.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

# Names this harness owns. Nothing on the machine's real PATH may answer for
# any of them: a test that stubs one has to see its stub, and a test that
# deletes one has to see "not installed" — on a developer laptop with no
# docker and on a CI runner with /usr/bin/docker alike.
#
# This is not hypothetical tidiness. Seven tests model "docker is not
# installed" by unlinking the docker stub, which on the dev Mac genuinely
# leaves no docker anywhere on PATH. On the GitHub Actions runner
# /usr/bin/docker sits further down the same PATH, `command -v docker` finds
# it, phase 2 passes, and the two tests that assert on phase-2 remediation
# fail for a reason that has nothing to do with the script. The leak runs
# both ways: a machine missing some tool the script needs would fail tests
# that a machine having it passes.
#
# It is a hidden set rather than an allowlist because the script and the
# tests' own helpers reach for far more of the system than they stub —
# bash, sed, grep, tr, awk, head, tail, cut, mktemp, mv, chmod, rm and
# the rest have to keep working (`id` is hidden and stubbed like the others).
# sed and tr are stubbed by individual tests
# but deliberately NOT hidden: the stubs directory comes first on PATH, so an
# override already wins, and hiding them would break every test that does not
# stub them. "sleep" is likewise not hidden — the reset script's retries use
# seams instead of a stubbed sleep.
HIDDEN_FROM_PATH = frozenset(
    {
        # FULL_STUBS — the phase-1/2 machine survey.
        "docker",
        "systemctl",
        "uname",
        "free",
        "df",
        "timedatectl",
        "getent",
        # Phase 4 pins BELLASREEF_TAG to the checkout's commit, so git is a
        # tool the script now depends on — and "git is not installed" has to
        # be reachable in a test on a machine that certainly has it.
        "git",
        # Phase-2 remediation and phase-5 deployment: every command that
        # mutates a real machine. Hidden so a test that forgets to stub one
        # gets "command not found" instead of touching the developer's box.
        "sudo",
        "sh",
        "curl",
        "apt-get",
        "usermod",
        "cp",
        "install",
        # Phase 3's pin-mux evidence. A sysfs pwmchip directory exists whether
        # or not any header pin is muxed to PWM (the standing trap in
        # CLAUDE.md's verified host facts), so pinctrl is what turns "chips
        # present" into "pins muxed" — and its absence has to be testable on a
        # machine that has it.
        "pinctrl",
        # Phase 3's PCA9685 probe. Hidden so a bench machine with i2c-tools
        # does not answer for a fixture that never had a bus.
        "i2cget",
        # Phase 6's boot-unit checks. systemd-analyze absent means the check
        # is skipped, which is what the script must do on a host without it;
        # a runner's own systemd-analyze answering here would make that path
        # untestable and the tool's presence a property of the machine.
        "systemd-analyze",
        # Phase 2 asks whether this user is in the docker group before it
        # offers to do anything about an unreachable daemon. On a CI runner
        # the real `id` says yes and on a laptop it says no, which would make
        # the offered remediation a property of the machine running the suite.
        "id",
        # Phase 6's identity block. The runner's own hostname is not the hub's.
        "hostname",
        # Phase 2's avahi remedy names this machine's own interfaces, read
        # from `ip -br link`. A runner's real `ip` would make the suggested
        # allow-interfaces line a property of the machine running the suite.
        "ip",
        # Phase 6's two probes. journalctl is the avahi evidence and curl is
        # the API probe: on a Linux runner the machine's own journalctl
        # answers for a service the fixture never installed, and either
        # answer — a stray match or a real "no entries" — is the runner
        # talking, not the script.
        "journalctl",
    }
)

_real_bin_dir: Path | None = None


def real_bin_dir() -> Path:
    """A directory of symlinks to every executable on the inherited PATH
    except the names in HIDDEN_FROM_PATH.

    Built once per test session and cached: /usr/bin alone holds a thousand
    entries, and this runs for every subprocess the suite launches. Earlier
    PATH entries win, the same shadowing rule the real PATH search uses.
    """
    global _real_bin_dir
    if _real_bin_dir is not None:
        return _real_bin_dir

    farm = Path(tempfile.mkdtemp(prefix="ih-real-bin-"))
    seen: set[str] = set()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        try:
            names = os.listdir(entry)
        except OSError:
            continue
        for name in names:
            if name in seen or name in HIDDEN_FROM_PATH:
                continue
            source = Path(entry) / name
            # An entry that cannot even be stat'd (macOS keeps a few of those
            # under /usr/sbin) is not something the script could have run
            # either, so skipping it changes nothing but the traceback.
            try:
                if source.is_dir() or not os.access(source, os.X_OK):
                    continue
                (farm / name).symlink_to(source)
            except OSError:
                continue
            seen.add(name)
    _real_bin_dir = farm
    return farm


def script_env(
    root: Path,
    stubs: Path | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """The environment a hub script runs under: fixture root, isolated PATH.

    PATH is the stubs directory (when there is one) followed by the symlink
    farm — never the inherited PATH — so what the script can find is exactly
    what a test decided it should find. IH_TEST_REAL_BIN points at the farm
    for the one stub that needs to reach a real tool it is shadowing.
    """
    environ = dict(os.environ)
    farm = real_bin_dir()
    environ["IH_ROOT"] = str(root)
    environ["PATH"] = f"{stubs}{os.pathsep}{farm}" if stubs is not None else str(farm)
    environ["IH_TEST_REAL_BIN"] = str(farm)
    environ["IH_RELEASE_ENV"] = str(default_release_env())
    if extra:
        environ.update(extra)
    return environ


def run_any_script(
    script: Path,
    *args: str,
    root: Path,
    stubs: Path | None = None,
    env: dict[str, str] | None = None,
    input: str | None = None,  # noqa: A002 - matches subprocess.run's own kwarg
) -> subprocess.CompletedProcess[str]:
    """Run any of the hub scripts against a fixture root."""
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        env=script_env(root, stubs, env),
        input=input,
        timeout=60,
    )


def write_stub(stubs: Path, name: str, body: str) -> None:
    """Create a stub executable on the fake PATH."""
    stubs.mkdir(parents=True, exist_ok=True)
    path = stubs / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)


# release.env fixtures. IH_RELEASE_ENV is the seam that points a script at one
# — the dev repo has no deploy/release.env of its own, since the release
# workflow is what writes it.
FAKE_COMMIT = "0123456789abcdef0123456789abcdef01234567"

FAKE_VERSION = "v0.2.0-rc.2"


def write_release_env(path: Path, version: str = FAKE_VERSION, tag: str = FAKE_COMMIT) -> Path:
    """A deploy/release.env as the release workflow writes it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"BELLASREEF_VERSION={version}\nBELLASREEF_TAG={tag}\nBELLASREEF_CONTRACTS=4.2.0\n"
    )
    return path


_default_release_env: Path | None = None


def default_release_env() -> Path:
    """The manifest every run sees unless a test says otherwise. The dev repo
    has no deploy/release.env — the release workflow writes it — so the
    script is pointed at one through the IH_RELEASE_ENV seam."""
    global _default_release_env
    if _default_release_env is None:
        _default_release_env = write_release_env(
            Path(tempfile.mkdtemp(prefix="ih-release-")) / "release.env"
        )
    return _default_release_env


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Strip SGR color codes so a status-line assertion can match the label
    and its message as one run, the way a person reading the terminal would
    — ih_pass/ih_would wrap only the label word in color, so the reset code
    sits directly after it and breaks a literal "PASS  " substring check."""
    return _ANSI_RE.sub("", text)
