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

import fcntl
import os
import pty
import re
import select
import shutil
import stat
import subprocess
import tempfile
import termios
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HUB_ROOT = REPO_ROOT / "hub"
SCRIPT = HUB_ROOT / "scripts" / "install-hub.sh"


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
# stub them.
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
    """The environment install-hub.sh runs under: fixture root, isolated PATH.

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


def run_script(
    *args: str,
    root: Path,
    stubs: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run install-hub.sh against a fixture root."""
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=script_env(root, stubs, env),
        timeout=60,
    )


def test_the_isolated_path_hides_the_machines_own_tools(tmp_path: Path) -> None:
    # The harness's own contract, and the reason it exists: a deleted stub has
    # to mean "not installed" everywhere, not only on a laptop that happens
    # not to have docker. Asserting on the PATH script_env hands out is what
    # makes this machine-independent — a run of the suite on a runner with
    # /usr/bin/docker used to pass phase 2 in tests whose docker stub had been
    # unlinked, and nothing in the suite noticed.
    stubs = make_stubs(tmp_path)
    (stubs / "docker").unlink()
    path = script_env(tmp_path / "root", stubs)["PATH"]
    # An unlinked stub resolves nowhere, whatever the machine has installed.
    assert shutil.which("docker", path=path) is None, "the machine's own docker leaked in"
    # The same for every other name the harness manages: only a stub may
    # answer, so the farm holds none of them.
    farm = str(real_bin_dir())
    for hidden in sorted(HIDDEN_FROM_PATH):
        assert shutil.which(hidden, path=farm) is None, f"{hidden} leaked in from the real PATH"
    # And the tools nobody stubs are still reachable, or the script cannot run
    # at all.
    for needed in ("bash", "sed", "grep", "tr", "awk", "head", "mktemp", "mv", "chmod"):
        assert shutil.which(needed, path=path) is not None, f"{needed} is missing from the farm"


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


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Strip SGR color codes so a status-line assertion can match the label
    and its message as one run, the way a person reading the terminal would
    — ih_pass/ih_would wrap only the label word in color, so the reset code
    sits directly after it and breaks a literal "PASS  " substring check."""
    return _ANSI_RE.sub("", text)


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


def write_good_docker_daemon_fixture(root: Path) -> None:
    """Populate an $IH_ROOT fixture with a daemon.json that already sets log
    rotation, so ih_check_docker_logging's phase-2 offer never fires.

    A test reaching phase 4/5/6 has nothing to do with docker log rotation,
    and the offer's sudo calls (write, then `systemctl restart docker`) are
    exactly the kind of incidental mutation the phase-5/6 fixture helpers
    below (write_phase5_stubs et al.) exist to keep out of an unrelated
    test's way — this is the same idea applied to phase 2's own offer.
    """
    (root / "etc/docker").mkdir(parents=True, exist_ok=True)
    (root / "etc/docker/daemon.json").write_text(DAEMON_JSON)


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


def test_phase1_warns_but_continues_when_deploy_env_exists(tmp_path: Path) -> None:
    # A lone deploy/.env is not evidence of a hub — it is evidence that phase 4
    # of some earlier run got that far. Phase 5 failing (a registry 401 is the
    # expected first-run failure today) leaves exactly that state, and treating
    # it as "already a hub" made every re-run exit 0 having done nothing. Only
    # running containers or an enabled boot unit stop the run now; the file
    # gets a warning and the run continues, because phase 4 never overwrites it.
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    # REPO_DIR (as the script computes it) is this repo's absolute path, so
    # under a fixture root the script reads ${root}${REPO_DIR}/deploy/.env.
    # Derive that nested path instead of hardcoding it.
    envfile = root.joinpath(*HUB_ROOT.parts[1:]) / "deploy" / ".env"
    envfile.parent.mkdir(parents=True, exist_ok=True)
    envfile.write_text("SOME_SETTING=value\n")
    result = run_script("--check-only", root=root, stubs=stubs)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "deploy/.env" in result.stdout
    assert "already looks like a hub" not in result.stdout, result.stdout
    assert "3. hardware" in result.stdout, "the run stopped at the .env latch"


# The reference Pi's groups: i2c 988, gpio 986 (CLAUDE.md, verified host facts).
GOOD_GETENT = (
    'case "$2" in i2c) echo "i2c:x:988:david" ;; gpio) echo "gpio:x:986:david" ;; *) exit 2 ;; esac'
)

# What git answers for a clean checkout that still carries its .git metadata.
# Phase 4 asks two questions now that the tag comes from deploy/release.env
# rather than from git: does git metadata exist at all (rev-parse
# --is-inside-work-tree), and is the tree dirty (status) — checked only when
# that metadata is there to check.
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


GOOD_GIT = "\n".join(
    [
        'case "$3" in',
        "    status) exit 0 ;;",
        f'    rev-parse) echo "{FAKE_COMMIT}"; exit 0 ;;',
        "    *) exit 1 ;;",
        "esac",
    ]
)

# What `ip -br link` prints. Columns are name, operstate, address, flags; a
# veth carries an @ifN suffix on the name, which is why the script strips it.
IP_BR_LINK = "\n".join(
    [
        "cat <<'EOF'",
        "lo               UNKNOWN        00:00:00:00:00:00 <LOOPBACK,UP,LOWER_UP>",
        "end0             UP             aa:bb:cc:dd:ee:01 <BROADCAST,MULTICAST,UP,LOWER_UP>",
        "wlan0            DOWN           aa:bb:cc:dd:ee:02 <BROADCAST,MULTICAST>",
        "docker0          DOWN           02:42:aa:bb:cc:dd <NO-CARRIER,BROADCAST,MULTICAST,UP>",
        "veth9f21c3a@if7  UP             06:11:22:33:44:55 <BROADCAST,MULTICAST,UP,LOWER_UP>",
        "br-1a2b3c4d5e6f  DOWN           02:42:11:22:33:44 <NO-CARRIER,BROADCAST,MULTICAST,UP>",
        "EOF",
    ]
)


def id_stub(*groups: str) -> str:
    """An `id` stub answering `-un` (the login name) and `-nG` (its groups).

    The script asks `id -nG <user>` — the group database, not this session's
    groups — because after a usermod the two disagree, and that disagreement
    is exactly the state the remediation has to tell apart from "never added".
    """
    return "\n".join(
        [
            'case "$1" in',
            '    -un) echo "${USER:-tester}" ;;',
            f'    -nG) echo "{" ".join(groups)}" ;;',
            "    *) exit 1 ;;",
            "esac",
        ]
    )


ID_NOT_IN_DOCKER_GROUP = id_stub("users", "sudo")
ID_IN_DOCKER_GROUP = id_stub("users", "sudo", "docker")

# A docker that is installed with Compose v2 but whose daemon this user cannot
# reach. `docker ps` (phase 1) still answers, so phase 1 is unaffected.
DOCKER_UNREACHABLE = "\n".join(
    [
        'if [[ "$1" == "compose" ]]; then echo "Docker Compose version v2.29.0"; exit 0; fi',
        'if [[ "$1" == "info" ]]; then exit 1; fi',
        'echo ""',
        "exit 0",
    ]
)

FULL_STUBS = {
    "docker": (
        'if [[ "$1" == "compose" ]]; then echo "Docker Compose version v2.29.0"; '
        'else echo ""; fi; exit 0'
    ),
    "systemctl": "exit 1",
    "uname": 'case "$1" in -m) echo aarch64 ;; -r) echo 6.18.39 ;; *) echo Linux ;; esac',
    "free": 'echo "Mem: 8000000"',
    "df": 'echo "100000000"',
    "timedatectl": "echo yes",
    "getent": "exit 2",
    "git": GOOD_GIT,
    # `ip -br link` on a board with a predictably-named wired NIC. end0 is the
    # M64's; wlan0 is down but is still a LAN interface; docker0 is the bridge
    # the allowlist exists to keep avahi off; lo is never a LAN interface.
    "ip": IP_BR_LINK,
    # Not in the docker group — the ordinary case before an install.
    "id": ID_NOT_IN_DOCKER_GROUP,
}


def make_stubs(tmp_path: Path, overrides: dict[str, str] | None = None) -> Path:
    stubs = tmp_path / "bin"
    merged = dict(FULL_STUBS)
    if overrides:
        merged.update(overrides)
    for name, body in merged.items():
        write_stub(stubs, name, body)
    return stubs


# Every command install-hub.sh's phase-2 remediation can shell out to. Each
# gets a stub that writes a marker file and does nothing real, so a test that
# expects no remediation to run (dry-run, declined, --check-only) can assert
# every marker is absent — a regression that lets a mutating command through
# leaves evidence instead of silently curling and running a real installer,
# editing group membership, or touching a real avahi config.
_MUTATION_GUARD_COMMANDS = ("sh", "curl", "usermod", "apt-get", "cp", "install")


def write_mutation_guard_stubs(stubs: Path, tmp_path: Path) -> dict[str, Path]:
    """Stub every mutating command phase 2 can reach, marker-file-only.

    systemctl is handled separately: phase 1 legitimately calls
    `systemctl is-enabled` (read-only) on every run, so a stub that marks
    itself on any invocation would false-positive on that call. Only
    `enable`/`reload` — the two mutating subcommands remediation uses — are
    marked.
    """
    markers: dict[str, Path] = {}
    for name in _MUTATION_GUARD_COMMANDS:
        marker = tmp_path / f"{name}-was-run"
        write_stub(stubs, name, f'touch "{marker}"; exit 0')
        markers[name] = marker

    systemctl_marker = tmp_path / "systemctl-was-run"
    write_stub(
        stubs,
        "systemctl",
        f'case "$1" in enable|reload|restart) touch "{systemctl_marker}"; exit 0 ;;'
        " *) exit 1 ;; esac",
    )
    markers["systemctl"] = systemctl_marker
    return markers


def systemctl_stub(marker: Path, log: Path | None = None) -> str:
    """A systemctl stub that remembers whether the boot unit was enabled.

    Phase 1 asks `is-enabled` before anything has happened and phase 6 asks
    the same question after phase 5 has run `enable` — so one stub has to
    give two different answers, and the difference has to come from what
    phase 5 actually did. A stub that answered `enabled` unconditionally
    would let phase 6 pass against a phase 5 that never enabled anything,
    which is the entire point of the check.
    """
    record = f'echo "systemctl $1" >> "{log}"; ' if log is not None else ""
    return "\n".join(
        [
            'case "$1" in',
            f"    daemon-reload) {record}exit 0 ;;",
            f'    enable) {record}touch "{marker}"; exit 0 ;;',
            f"    restart) {record}exit 0 ;;",
            f'    is-enabled) if [[ -f "{marker}" ]]; then echo enabled; exit 0; fi;'
            " echo disabled; exit 1 ;;",
            "    *) exit 1 ;;",
            "esac",
        ]
    )


def install_stub(log: Path | None = None) -> str:
    """A stub for `install -m 0644 <src> <dst>` that really copies.

    Phase 5 renders the boot unit for this host and phase 6 reads the
    installed file back, so an install stub that merely exits 0 breaks the
    link between the two — and a phase 6 that never sees a rendered unit
    cannot fail when the rendering is wrong, which is the whole point of the
    check. The real `install` is hidden from PATH (it writes to /etc), so this
    is the only thing that ever runs.
    """
    record_unit = f'echo install-unit >> "{log}"\n' if log is not None else ""
    record_dir = f'echo install-dir >> "{log}"; ' if log is not None else ""
    return (
        f'if [[ "$1" == "-d" ]]; then {record_dir}mkdir -p "${{@: -1}}"; exit 0; fi\n'
        + record_unit
        + 'src="${@: -2:1}"; dst="${@: -1}"\n'
        # `install` takes either a file or a directory as its destination.
        'if [[ "$dst" == */ || -d "$dst" ]]; then dst="${dst%/}/$(basename "$src")"; fi\n'
        'mkdir -p "$(dirname "$dst")" || exit 1\n'
        'cat "$src" > "$dst" || exit 1\n'
        "exit 0"
    )


def boot_unit_marker(tmp_path: Path) -> Path:
    """Where systemctl_stub records that `systemctl enable` was run."""
    return tmp_path / "boot-unit-enabled"


def write_phase5_stubs(stubs: Path) -> None:
    """Make phase 5's mutations inert, for tests that are not about phase 5.

    A full `--yes` run does not stop after writing deploy/.env any more: it
    goes on to pull images, migrate, install the boot unit and start the
    stack. Without these stubs an earlier phase's test reaches the real
    `sudo`, which fails on a password prompt for a reason that has nothing to
    do with what the test is checking. Call this after make_stubs — it
    replaces the phase-1 systemctl stub with one that reports the boot unit
    as not-enabled until phase 5 enables it, and accepts the two subcommands
    phase 5 runs.

    The stubs directory is always tmp_path/"bin", so the marker file lands
    beside it under the test's own tmp_path rather than needing a second
    argument at every call site.
    """
    write_stub(stubs, "sudo", '"$@"')
    write_stub(stubs, "install", install_stub())
    write_stub(stubs, "systemctl", systemctl_stub(boot_unit_marker(stubs.parent)))
    # Phase 6 runs off the end of phase 5 for exactly the same reason, and
    # reaches two more tools. A test that is not about phase 6 still has to
    # get past it.
    write_phase6_stubs(stubs, stubs.parent)


# What avahi-daemon actually logs when it publishes a static service file.
# The journal is the evidence phase 6 reads: avahi-browse is not installed
# by the daemon, and host-setup.md records that browsing from the hub itself
# does not reliably reflect what the daemon published.
AVAHI_JOURNAL_LINE = (
    'Service "bellasreef" (/etc/avahi/services/bellasreef.service) successfully established.'
)


def write_phase6_stubs(
    stubs: Path,
    tmp_path: Path,
    *,
    setup_mode: str = "true",
    avahi_ok: bool = True,
) -> dict[str, Path]:
    """Stub phase 6's two probes: the API's /info and the avahi journal.

    Both append to a log rather than merely exiting 0. --dry-run has to be
    provably able to run neither, and the /info check polls — a test that
    cares whether it retried has to count calls, not assume.
    """
    curl_log = tmp_path / "curl.log"
    journal_log = tmp_path / "journalctl.log"
    write_stub(
        stubs,
        "curl",
        f'echo "$*" >> "{curl_log}"\n'
        f'printf \'{{"contracts_version":"3.7.0","setup_mode":{setup_mode}}}\'\n'
        "exit 0",
    )
    line = AVAHI_JOURNAL_LINE if avahi_ok else "Starting Avahi mDNS/DNS-SD Stack..."
    write_stub(stubs, "journalctl", f"echo ran >> \"{journal_log}\"\necho '{line}'\nexit 0")
    return {"curl": curl_log, "journalctl": journal_log}


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


# The disk check is two-tier, and the two tiers mean different things. Below
# the hard floor the stack cannot even start, so the run stops. Between
# the floor and the practical minimum the install works and the machine is
# known-degraded, which is a WARN — not an UNVERIFIED, because the check ran
# and gave an answer. FULL_STUBS' df reports 100000000 kB (95 GB), the
# comfortable case, so both tiers need their own stub.


def test_phase2_warns_between_the_disk_floor_and_the_practical_minimum(
    tmp_path: Path,
) -> None:
    # 5000000 kB is 5 GB: above the 2 GB hard floor, below the 16 GB
    # practical minimum. The images fit, a second generation of them plus
    # retention does not — so the operator is told, and the run continues.
    stubs = make_stubs(tmp_path, {"df": 'echo "5000000"'})
    root = full_root(tmp_path)
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "WARN" in result.stdout, result.stdout
    assert "practical minimum" in result.stdout, result.stdout
    assert "not for a tank" in result.stdout, result.stdout
    assert "UNVERIFIED" not in result.stdout, "a checked, degraded disk is not an unverified one"
    assert "3. hardware inventory" in result.stdout, "the warn tier stopped the run"
    assert result.returncode == 0, result.stdout + result.stderr


def test_phase2_fails_below_the_disk_hard_floor(tmp_path: Path) -> None:
    # 1500000 kB is 1.5 GB. Below the 2 GB hard floor the stack cannot even
    # start (Docker's own state, the data volumes, logs) — that is a hard
    # stop, and the gate must fire before phase 3. Measured on the M64: 3.5 GB
    # free after the images landed must NOT trip this (it did at 4 GB).
    stubs = make_stubs(tmp_path, {"df": 'echo "1500000"'})
    root = full_root(tmp_path)
    result = run_script(root=root, stubs=stubs)
    assert "FAIL" in result.stdout, result.stdout
    assert "hard floor" in result.stdout, result.stdout
    assert result.returncode != 0
    assert "3. hardware inventory" not in result.stdout, "the gate did not stop the run"


def test_phase2_unverified_when_clock_state_is_unknown(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path, {"timedatectl": "exit 1"})
    result = run_script("--check-only", root=tmp_path / "root", stubs=stubs)
    assert "UNVERIFIED" in result.stdout
    assert result.returncode != 0, "an unverified check must not exit green"


def test_phase2_flags_avahi_advertising_docker_bridges(tmp_path: Path) -> None:
    # Nothing in this script edits avahi-daemon.conf, so the FAIL line is the
    # entire remedy the operator gets. Naming the setting without naming the
    # file or the line to write is a dead end — it sends someone to a
    # configuration format they have never seen to fix a word they have just
    # been told is wrong.
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    (root / "etc/avahi").mkdir(parents=True)
    (root / "etc/avahi/avahi-daemon.conf").write_text("# no allow-interfaces here\n")
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "allow-interfaces" in result.stdout
    assert "/etc/avahi/avahi-daemon.conf" in result.stdout, result.stdout
    # The line names this machine's interfaces — see the test below.
    assert "allow-interfaces=end0,wlan0" in result.stdout, result.stdout


def test_phase2_avahi_remedy_names_this_machines_interfaces(tmp_path: Path) -> None:
    # eth0,wlan0 is Raspberry Pi OS's naming and nobody else's. The M64's
    # wired NIC is end0, and pasting the literal line there produced a valid
    # config allowlisting two interfaces that do not exist — avahi then
    # advertised on nothing, which is worse than the misconfiguration it was
    # meant to fix, because the file now looks correct.
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    (root / "etc/avahi").mkdir(parents=True)
    (root / "etc/avahi/avahi-daemon.conf").write_text("# no allow-interfaces here\n")
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "allow-interfaces=end0,wlan0" in result.stdout, result.stdout
    # Everything the allowlist exists to exclude stays out of the suggestion:
    # Docker's bridge, its per-network bridges, its veth pairs, and loopback.
    suggested = next(line for line in result.stdout.splitlines() if "allow-interfaces=" in line)
    for excluded in ("lo", "docker0", "veth", "br-"):
        assert excluded not in suggested.split("=", 1)[1], suggested


def test_phase2_avahi_remedy_falls_back_when_interfaces_cannot_be_read(
    tmp_path: Path,
) -> None:
    # No `ip`, or an `ip` that answers with nothing. A guessed interface list
    # presented as this machine's would be worse than the generic one, so the
    # fallback says plainly that it could not look.
    stubs = make_stubs(tmp_path)
    (stubs / "ip").unlink()
    root = tmp_path / "root"
    (root / "etc/avahi").mkdir(parents=True)
    (root / "etc/avahi/avahi-daemon.conf").write_text("# no allow-interfaces here\n")
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "allow-interfaces=eth0,wlan0" in result.stdout, result.stdout
    assert "could not read your interfaces" in result.stdout, result.stdout


def test_phase2_offers_only_the_record_when_avahi_is_installed(tmp_path: Path) -> None:
    # ih_check_avahi returns 1 for either sub-failure, so a machine that has
    # avahi and is only missing the service record used to be asked whether to
    # install avahi-daemon — and declining that (or a package manager that has
    # nothing to do) gated away the offer that would have fixed the actual
    # problem. Two findings, two independent remediations.
    stubs = make_stubs(tmp_path)
    write_stub(stubs, "sudo", '"$@"')
    markers = write_mutation_guard_stubs(stubs, tmp_path)
    root = tmp_path / "root"
    (root / "etc/avahi/services").mkdir(parents=True)
    (root / "etc/avahi/avahi-daemon.conf").write_text("allow-interfaces=eth0,wlan0\n")

    result = run_script("--yes", root=root, stubs=stubs)
    assert "install avahi-daemon?" not in result.stdout, "offered to install an installed daemon"
    assert "service record" in result.stdout
    assert markers["cp"].exists(), "the service record was never installed"
    assert not markers["apt-get"].exists(), "an installed avahi-daemon was reinstalled"


def test_phase2_offers_the_daemon_when_avahi_is_absent(tmp_path: Path) -> None:
    # The other half: with no avahi at all, the package offer is the one that
    # has to come first — copying a service record into a services/ directory
    # the package never created fails, and reloading a daemon that is not
    # there fails after it.
    stubs = make_stubs(tmp_path)
    write_stub(stubs, "sudo", '"$@"')
    markers = write_mutation_guard_stubs(stubs, tmp_path)
    root = tmp_path / "root"

    result = run_script("--yes", root=root, stubs=stubs)
    assert "install avahi-daemon?" in result.stdout, result.stdout
    assert markers["apt-get"].exists(), "the daemon install never ran"
    # The stubbed apt-get installs nothing, so the daemon is still absent and
    # the record offer must not fire against a machine with no services/ dir.
    assert not markers["cp"].exists(), "copied a service record with no avahi installed"


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


STOCK_AVAHI_CONF = "[server]\n#host-name=foo\n#allow-interfaces=eth0\n\n[wide-area]\n"


def test_phase2_offers_to_set_avahi_allow_interfaces(tmp_path: Path) -> None:
    # A stock image ships the line commented out, so every clean install
    # used to stop here with a paste-this-yourself message for a value the
    # script had already computed. The list is an exclusion of Docker's
    # bridges, not a choice of the live NIC — a down interface in it is inert.
    stubs = make_stubs(tmp_path)
    write_stub(stubs, "sudo", '"$@"')
    write_stub(stubs, "install", install_stub())
    write_stub(
        stubs,
        "systemctl",
        'case "$1" in restart|reload|enable|daemon-reload) exit 0 ;; *) exit 1 ;; esac',
    )
    root = tmp_path / "root"
    (root / "etc/avahi/services").mkdir(parents=True)
    conf = root / "etc/avahi/avahi-daemon.conf"
    conf.write_text(STOCK_AVAHI_CONF)
    (root / "etc/avahi/services/bellasreef.service").write_text("<service-group/>")

    result = run_script("--yes", root=root, stubs=stubs)
    assert "set avahi allow-interfaces=end0,wlan0? [y/N] y (--yes)" in result.stdout, result.stdout
    text = conf.read_text()
    assert text.startswith("[server]\nallow-interfaces=end0,wlan0\n#host-name=foo\n"), text
    assert "#allow-interfaces=eth0" in text, "the stock commented line must be left alone"
    assert "PASS  avahi allow-interfaces is set" in strip_ansi(result.stdout), result.stdout


def test_phase2_declining_allow_interfaces_leaves_the_printed_remedy(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    write_stub(stubs, "sudo", "exit 1")
    root = tmp_path / "root"
    (root / "etc/avahi/services").mkdir(parents=True)
    conf = root / "etc/avahi/avahi-daemon.conf"
    conf.write_text(STOCK_AVAHI_CONF)
    (root / "etc/avahi/services/bellasreef.service").write_text("<service-group/>")

    result = run_script(root=root, stubs=stubs, env={"IH_ASSUME_NO_TTY": "1"})
    assert result.returncode == 1
    assert conf.read_text() == STOCK_AVAHI_CONF
    assert "Add to /etc/avahi/avahi-daemon.conf, under [server]:" in result.stdout


def test_phase2_never_touches_an_existing_allow_interfaces_line(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    write_stub(stubs, "sudo", "exit 1")
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    conf = root / "etc/avahi/avahi-daemon.conf"
    before = conf.read_text()

    result = run_script(
        "--yes", root=root, stubs=stubs, env={"IH_RELEASE_ENV": str(tmp_path / "none")}
    )
    assert "set avahi allow-interfaces" not in result.stdout
    assert conf.read_text() == before


def test_phase2_no_allow_interfaces_offer_without_a_server_section(tmp_path: Path) -> None:
    # Nothing to insert after. Inventing a [server] header in someone's
    # config is not this script's call; the printed remedy stands.
    stubs = make_stubs(tmp_path)
    write_stub(stubs, "sudo", "exit 1")
    root = tmp_path / "root"
    (root / "etc/avahi/services").mkdir(parents=True)
    conf = root / "etc/avahi/avahi-daemon.conf"
    conf.write_text("# empty\n")
    (root / "etc/avahi/services/bellasreef.service").write_text("<service-group/>")

    result = run_script("--yes", root=root, stubs=stubs)
    assert "set avahi allow-interfaces" not in result.stdout, result.stdout
    assert conf.read_text() == "# empty\n"


def test_phase2_allow_interfaces_dry_run_writes_nothing(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    markers = write_mutation_guard_stubs(stubs, tmp_path)
    root = tmp_path / "root"
    (root / "etc/avahi/services").mkdir(parents=True)
    conf = root / "etc/avahi/avahi-daemon.conf"
    conf.write_text(STOCK_AVAHI_CONF)
    (root / "etc/avahi/services/bellasreef.service").write_text("<service-group/>")

    result = run_script("--dry-run", "--yes", root=root, stubs=stubs)
    assert "would  setting avahi allow-interfaces=end0,wlan0" in strip_ansi(result.stdout), (
        result.stdout
    )
    assert conf.read_text() == STOCK_AVAHI_CONF
    assert not markers["install"].exists()
    assert not markers["systemctl"].exists()


DAEMON_JSON = (
    '{\n  "log-driver": "json-file",\n  "log-opts": { "max-size": "10m", "max-file": "3" }\n}\n'
)


def test_phase2_offers_docker_log_rotation_when_daemon_json_is_absent(tmp_path: Path) -> None:
    # Docker's default json-file driver never rotates, and compose.yaml
    # deliberately carries no per-service logging: block. Six always-on
    # services fill a disk eventually, silently. hub-prereqs documented this
    # as a hand step; the installer does it.
    stubs = make_stubs(tmp_path)
    write_stub(stubs, "sudo", '"$@"')
    write_stub(stubs, "install", install_stub())
    write_stub(
        stubs,
        "systemctl",
        'case "$1" in restart|reload|enable|daemon-reload) exit 0 ;; *) exit 1 ;; esac',
    )
    root = tmp_path / "root"
    write_good_avahi_fixture(root)

    result = run_script(
        "--yes", root=root, stubs=stubs, env={"IH_RELEASE_ENV": str(tmp_path / "none")}
    )
    stdout = strip_ansi(result.stdout)
    assert "configure docker log rotation (json-file, 10m x 3)? [y/N] y (--yes)" in stdout, stdout
    assert (root / "etc/docker/daemon.json").read_text() == DAEMON_JSON
    assert "PASS  docker log rotation configured" in stdout, stdout


def test_phase2_reports_but_never_rewrites_an_existing_daemon_json(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    write_stub(stubs, "sudo", "exit 1")
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    (root / "etc/docker").mkdir(parents=True)
    (root / "etc/docker/daemon.json").write_text('{"data-root": "/mnt/docker"}\n')

    result = run_script(
        "--yes", root=root, stubs=stubs, env={"IH_RELEASE_ENV": str(tmp_path / "none")}
    )
    stdout = strip_ansi(result.stdout)
    assert "configure docker log rotation" not in stdout, stdout
    assert "WARN  /etc/docker/daemon.json exists but sets no log rotation" in stdout, stdout
    assert (root / "etc/docker/daemon.json").read_text() == '{"data-root": "/mnt/docker"}\n'


def test_phase2_declined_log_rotation_is_a_warn_not_a_gate(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    write_stub(stubs, "sudo", "exit 1")
    root = tmp_path / "root"
    write_good_avahi_fixture(root)

    result = run_script("--check-only", root=root, stubs=stubs)
    stdout = strip_ansi(result.stdout)
    assert "WARN  no /etc/docker/daemon.json" in stdout, stdout
    assert result.returncode == 0, "a missing daemon.json must not fail the run"


def test_dry_run_reports_actions_without_running_them(tmp_path: Path) -> None:
    # No avahi fixture, so avahi is broken too: both the docker branch and
    # the avahi branch have something to offer, exercising every mutating
    # route --dry-run must refuse at the point of execution.
    stubs = make_stubs(tmp_path)
    (stubs / "docker").unlink()
    write_stub(stubs, "sudo", '"$@"')
    markers = write_mutation_guard_stubs(stubs, tmp_path)
    result = run_script("--dry-run", "--yes", root=tmp_path / "root", stubs=stubs)
    assert "would" in result.stdout.lower()
    for name, marker in markers.items():
        assert not marker.exists(), f"--dry-run executed {name}"


def test_yes_accepts_offers_without_prompting(tmp_path: Path) -> None:
    # ih_confirm's --yes path prints "(--yes)" and returns without touching
    # /dev/tty or stdin. Prove both: the marker is in the output, and the run
    # completes with stdin closed rather than hanging waiting for an answer
    # (a genuine hang would fail this test with a TimeoutExpired instead of
    # an assertion — either way the "it blocked" case does not pass quietly).
    # Also guard every mutating command --dry-run should have refused, same
    # as the test above, since this run reaches the avahi remediation branch.
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    (root / "etc/avahi").mkdir(parents=True)
    (root / "etc/avahi/avahi-daemon.conf").write_text("# nothing\n")
    write_stub(stubs, "sudo", '"$@"')
    markers = write_mutation_guard_stubs(stubs, tmp_path)
    result = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run", "--yes"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=script_env(root, stubs),
        timeout=60,
    )
    assert result.returncode in (0, 1)
    assert "(--yes)" in result.stdout
    for name, marker in markers.items():
        assert not marker.exists(), f"--dry-run executed {name}"


def test_confirm_prompt_is_visible_on_a_real_tty(tmp_path: Path) -> None:
    # Regression test for a critical finding: `read -p` writes its prompt to
    # stderr, and ih_confirm used to redirect stderr to /dev/null on the same
    # line, muting the prompt in the one mode where it actually asks a human
    # — the script would sit at a blank line while a real operator typed
    # blind. A subprocess with no controlling terminal can't exercise this
    # (ih_confirm fails closed before ever reading), so drive the script
    # under a real pty, same as how the bug was originally found.
    stubs = make_stubs(tmp_path)
    (stubs / "docker").unlink()
    write_stub(stubs, "sudo", "exit 1")  # never legitimately reached
    root = tmp_path / "root"
    write_good_avahi_fixture(root)

    env = script_env(root, stubs)

    # subprocess.Popen with stdin/stdout/stderr pointed at a pty's slave fd
    # does NOT by itself make that pty the child's controlling terminal (the
    # fds are just dup'd in, not opened by path) — /dev/tty inside the
    # script then fails with "Device not configured", a different bug than
    # the one this test exists to catch. preexec_fn runs in the forked
    # child, before Popen's own dup2/exec setup, so it can setsid() (detach
    # from pytest's session) and then TIOCSCTTY the slave fd onto the new
    # session — the same handshake a real login shell performs. Avoids a
    # raw pty.fork() of the whole pytest interpreter, which hung: forking a
    # multi-threaded process such as pytest can deadlock the child on a lock
    # held by another thread at fork time.
    master_fd, slave_fd = pty.openpty()

    def _become_session_leader() -> None:
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

    proc = subprocess.Popen(
        ["bash", str(SCRIPT)],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        preexec_fn=_become_session_leader,
        env=env,
        close_fds=True,
    )
    os.close(slave_fd)
    output = b""
    prompt_seen = False
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master_fd], [], [], 1)
            if not ready:
                continue
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
            if b"install Docker" in output and b"[y/N]" in output:
                # The prompt itself is all this test is about — stop here
                # rather than answering and waiting for a clean exit, which
                # risks a second interactive read or a canonical-mode echo
                # loop the test has no need to navigate.
                prompt_seen = True
                break
    finally:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        os.close(master_fd)

    assert prompt_seen, f"prompt never appeared on the pty; captured: {output!r}"


def test_no_tty_declines_and_leaves_the_failure_standing(tmp_path: Path) -> None:
    # ih_confirm reads only /dev/tty, never stdin, so feeding "n" on stdin
    # (the previous version of this test) never exercises anything — it
    # passes regardless of what's on stdin, because the real deciding factor
    # is whether the process has a controlling terminal at all, which is not
    # something a test should depend on how it happens to be launched.
    # IH_ASSUME_NO_TTY makes that path deterministic: it forces ih_confirm's
    # fail-closed branch the same way a genuinely absent tty would.
    stubs = make_stubs(tmp_path)
    (stubs / "docker").unlink()
    write_stub(stubs, "sudo", '"$@"')
    markers = write_mutation_guard_stubs(stubs, tmp_path)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=script_env(tmp_path / "root", stubs, {"IH_ASSUME_NO_TTY": "1"}),
        timeout=60,
    )
    assert result.returncode != 0
    assert "docker" in result.stdout.lower()
    for name, marker in markers.items():
        assert not marker.exists(), f"declining still ran {name}"


def test_check_only_never_remediates_even_with_yes(tmp_path: Path) -> None:
    # --check-only is documented as "phases 1 to 3 only" and is read by
    # eight other tests in this file as a way to inspect checks without
    # side effects. Once phase 2 gained remediation, --check-only had to
    # gain a matching guard or it would silently stop meaning what its own
    # --help text and every one of those tests assume it means.
    stubs = make_stubs(tmp_path)
    (stubs / "docker").unlink()
    write_stub(stubs, "sudo", '"$@"')
    markers = write_mutation_guard_stubs(stubs, tmp_path)
    result = run_script("--check-only", "--yes", root=tmp_path / "root", stubs=stubs)
    assert result.returncode != 0
    assert "docker" in result.stdout.lower()
    assert "install Docker" not in result.stdout, "--check-only prompted for an install"
    for name, marker in markers.items():
        assert not marker.exists(), f"--check-only --yes ran {name}"


def test_check_only_reports_the_inventory_even_when_phase2_fails(tmp_path: Path) -> None:
    # The Banana Pi M64, exactly: no docker, no avahi. The hardware inventory
    # is the reason anyone runs --check-only on a candidate board, and the
    # post-phase-2 gate used to exit before phase 3 ever ran — so the one
    # question the flag exists to answer went unanswered on precisely the
    # machines it was being asked about. Nothing is mutated either way, so
    # there is nothing to protect by stopping early.
    stubs = make_stubs(tmp_path)
    (stubs / "docker").unlink()
    result = run_script("--check-only", root=tmp_path / "root", stubs=stubs)
    assert "docker is not installed" in result.stdout, result.stdout
    assert "3. hardware inventory" in result.stdout, result.stdout
    # Reported is not the same as passed: the failures still set the exit code.
    assert "requirement(s) failed" in result.stdout, result.stdout
    assert result.returncode != 0


def test_check_only_on_a_clean_machine_still_exits_zero(tmp_path: Path) -> None:
    # The other half of the same change: running phase 3 unconditionally must
    # not cost --check-only its green path.
    stubs = make_stubs(tmp_path)
    root = full_root(tmp_path)
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "3. hardware inventory" in result.stdout, result.stdout
    assert "checks complete (--check-only); nothing was changed" in result.stdout, result.stdout
    assert "requirement(s) failed" not in result.stdout, result.stdout
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_failed_phase2_still_stops_before_phase3_without_check_only(tmp_path: Path) -> None:
    # Unchanged behaviour for a real install: phases 4 to 6 are pointless on a
    # machine that failed a hard requirement, so the gate stops the run where
    # it always did. Only --check-only, which mutates nothing, carries on.
    stubs = make_stubs(tmp_path)
    (stubs / "docker").unlink()
    write_stub(stubs, "sudo", '"$@"')
    write_mutation_guard_stubs(stubs, tmp_path)
    result = run_script(root=tmp_path / "root", stubs=stubs, env={"IH_ASSUME_NO_TTY": "1"})
    assert "docker is not installed" in result.stdout, result.stdout
    assert "3. hardware inventory" not in result.stdout, result.stdout
    assert result.returncode != 0


def test_a_running_but_unsynced_time_daemon_is_a_wait_not_an_install(tmp_path: Path) -> None:
    # Measured on the Banana Pi M64 within a minute of boot: chrony installed,
    # active, not yet synchronised. The remedy is to wait, not to reinstall
    # chrony — which is what --yes would have done on the old single message.
    stubs = make_stubs(tmp_path, {"timedatectl": "echo no"})
    markers = write_mutation_guard_stubs(stubs, tmp_path)
    # After the guard: it rewrites systemctl to a marker stub, and this test
    # needs `is-active chrony` to answer "active" (everything else still 1).
    write_stub(
        stubs,
        "systemctl",
        f'if [[ "$1" == "is-active" && "$2" == "chrony" ]]; then echo active; exit 0; fi\n'
        f'case "$1" in enable|reload) touch "{markers["systemctl"]}"; exit 0 ;; *) exit 1 ;; esac',
    )
    root = full_root(tmp_path)
    result = run_script("--yes", root=root, stubs=stubs)
    assert result.returncode != 0
    assert "give it a minute" in result.stdout, result.stdout
    assert "chrony and fake-hwclock?" not in result.stdout, "offered to reinstall a running daemon"
    for name in ("apt-get", "sh", "curl"):
        assert not markers[name].exists(), f"{name} ran"


def test_unverified_clock_does_not_trigger_remediation(tmp_path: Path) -> None:
    # ih_check_clock returns 2 (UNVERIFIED) when timedatectl itself can't be
    # read, which is not evidence the clock is wrong. Remediation must only
    # fire on a genuine FAIL (rc 1, clock readable and not synchronised) —
    # branching on "any nonzero" would install chrony on the strength of a
    # check that never ran.
    stubs = make_stubs(tmp_path, {"timedatectl": "exit 1"})
    write_stub(stubs, "sudo", '"$@"')
    markers = write_mutation_guard_stubs(stubs, tmp_path)
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    # Otherwise phase 2's own docker-log-rotation offer (docker is present
    # and reachable here) fires under --yes and its `systemctl restart
    # docker` trips this test's systemctl marker for a reason that has
    # nothing to do with the clock.
    write_good_docker_daemon_fixture(root)
    result = run_script("--yes", root=root, stubs=stubs)
    assert "UNVERIFIED" in result.stdout
    assert result.returncode != 0
    assert not markers["apt-get"].exists(), "unverified clock triggered a chrony install"
    assert not markers["systemctl"].exists(), "unverified clock triggered enabling clock units"


def test_accepted_remediation_clears_the_recorded_failure(tmp_path: Path) -> None:
    # Controller ruling: ih_phase2_requirements must end with a verification
    # pass. Without it, a check that fails, gets remediated, and now passes
    # still leaves its original FAIL sitting in IH_FAILURES, so a successful
    # install would still exit non-zero.
    #
    # Proven through the clock branch rather than the docker one: installing
    # Docker adds the user to a group that does not take effect until they log
    # in again, so that remediation now deliberately stops the run (see the
    # test below). Chrony has no such handover — the stubbed apt-get makes the
    # clock synchronised as its side effect, the verification pass sees a good
    # machine, and the run has to report success rather than carrying the
    # original FAIL to the exit code.
    #
    # This run is not --check-only, so it reaches phase 4 and beyond. A real
    # i2c/gpio getent stub is needed so phase 4's own groups check doesn't add
    # an unrelated FAIL and mask what this test is about, and phases 5 and 6
    # are stubbed inert for the same reason.
    installed = tmp_path / "chrony-installed"
    stubs = make_stubs(
        tmp_path,
        {
            "getent": GOOD_GETENT,
            "docker": inert_compose_docker(),
            "timedatectl": f'if [[ -f "{installed}" ]]; then echo yes; else echo no; fi',
        },
    )
    write_stub(stubs, "apt-get", f'touch "{installed}"; exit 0')
    write_phase5_stubs(stubs)
    root = full_root(tmp_path)
    staged_env_path(root)
    result = run_script("--yes", root=root, stubs=stubs, env=FAST_POLL)
    assert installed.exists(), "the offered chrony install never ran"
    assert result.returncode == 0, result.stdout + result.stderr
    assert "requirement(s) failed" not in result.stdout


def test_installing_docker_stops_the_run_rather_than_continuing(tmp_path: Path) -> None:
    # `usermod -aG docker` does not take effect in the session that ran it.
    # Every phase after this one talks to the daemon, so continuing means
    # pulling images as a user who cannot reach it: a guaranteed failure,
    # several phases later, that reads as a broken install rather than as the
    # log-out the operator actually owes. Stop here and say so.
    stubs = make_stubs(tmp_path, {"getent": GOOD_GETENT})
    (stubs / "docker").unlink()
    write_stub(stubs, "sudo", '"$@"')
    write_stub(stubs, "usermod", "exit 0")
    write_stub(
        stubs,
        "sh",
        f'''
cat > "{stubs}/docker" <<'DOCKER_STUB'
#!/usr/bin/env bash
{inert_compose_docker()}
DOCKER_STUB
chmod +x "{stubs}/docker"
exit 0
''',
    )
    root = full_root(tmp_path)
    staged_env_path(root)
    result = run_script("--yes", root=root, stubs=stubs, env=FAST_POLL)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "log out and back in" in result.stdout.lower()
    assert "3. hardware" not in result.stdout, "the run continued past the docker install"


def test_phase2_fails_when_the_daemon_is_unreachable(tmp_path: Path) -> None:
    # docker on PATH and Compose v2 present says the package is installed; it
    # says nothing about whether this user may talk to the socket. A user not
    # in the docker group gets a permission denial at the first pull instead,
    # in a phase with no idea what caused it. Probe it where the answer is
    # cheap and the remedy can be named.
    stubs = make_stubs(
        tmp_path,
        {
            "docker": (
                'if [[ "$1" == "compose" ]]; then echo "Compose version v2.29.0"; exit 0; fi\n'
                'if [[ "$1" == "info" ]]; then exit 1; fi\n'
                "exit 0"
            )
        },
    )
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    result = run_script("--check-only", root=root, stubs=stubs)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "cannot reach the daemon" in result.stdout, result.stdout
    assert "usermod -aG docker" in result.stdout


def test_an_unreachable_daemon_offers_the_group_not_a_reinstall(tmp_path: Path) -> None:
    # The M64, 2026-08-17: docker installed, Compose v2 present, the user not
    # yet in the docker group. ih_check_docker fails at the `docker info`
    # probe, and phase 2 offered to install Docker — so --yes re-ran
    # get.docker.com for five minutes to install a Docker that was already
    # there, then usermod'd a user for the second time. The failing probe
    # names the group; the remediation has to act on the same reading.
    stubs = make_stubs(
        tmp_path,
        {"docker": DOCKER_UNREACHABLE, "id": ID_NOT_IN_DOCKER_GROUP},
    )
    write_stub(stubs, "sudo", '"$@"')
    markers = write_mutation_guard_stubs(stubs, tmp_path)
    root = tmp_path / "root"
    write_good_avahi_fixture(root)

    result = run_script("--yes", root=root, stubs=stubs)
    assert "docker group" in result.stdout, result.stdout
    assert "convenience script" not in result.stdout, "reinstalled an installed Docker"
    assert markers["usermod"].exists(), "the group add never ran"
    assert not markers["curl"].exists(), "re-ran the Docker convenience installer"
    assert not markers["sh"].exists(), "re-ran the Docker convenience installer"
    # The group does not apply to the session that granted it, so this run
    # stops the same way the fresh-install path does.
    assert "log out and back in" in result.stdout, result.stdout
    assert result.returncode != 0


def test_an_unreachable_daemon_offers_nothing_when_already_in_the_group(
    tmp_path: Path,
) -> None:
    # Third state. The user is in the docker group and still cannot reach the
    # daemon, so there is nothing left to install: either dockerd is not
    # running or this login predates the group being granted. Offering an
    # install here is how the reinstall loop starts.
    stubs = make_stubs(
        tmp_path,
        {"docker": DOCKER_UNREACHABLE, "id": ID_IN_DOCKER_GROUP},
    )
    write_stub(stubs, "sudo", '"$@"')
    markers = write_mutation_guard_stubs(stubs, tmp_path)
    root = tmp_path / "root"
    write_good_avahi_fixture(root)

    result = run_script("--yes", root=root, stubs=stubs)
    assert "convenience script" not in result.stdout, result.stdout
    assert "add" not in result.stdout.lower().split("docker group")[0][-60:], result.stdout
    assert "systemctl status docker" in result.stdout, result.stdout
    for name in ("curl", "sh", "usermod"):
        assert not markers[name].exists(), f"already in the group and still ran {name}"
    assert result.returncode != 0


def test_a_missing_docker_still_offers_the_convenience_script(tmp_path: Path) -> None:
    # The state that has not changed: no docker at all is still the one case
    # the convenience installer answers.
    stubs = make_stubs(tmp_path, {"id": ID_NOT_IN_DOCKER_GROUP})
    (stubs / "docker").unlink()
    write_stub(stubs, "sudo", '"$@"')
    markers = write_mutation_guard_stubs(stubs, tmp_path)
    root = tmp_path / "root"
    write_good_avahi_fixture(root)

    result = run_script("--yes", root=root, stubs=stubs)
    assert "convenience script" in result.stdout, result.stdout
    assert markers["sh"].exists(), "the convenience installer never ran"
    assert markers["usermod"].exists(), "the group add never ran"
    assert result.returncode != 0


def test_action_failure_is_not_erased_by_the_verification_pass(tmp_path: Path) -> None:
    # The verification pass clears IH_FAILURES/IH_UNVERIFIED and re-derives
    # them from a fresh run of the checks — correct, that's Ruling 1. But
    # ih_run's own failures (an installer step that ran and exited nonzero)
    # are not something a check can rediscover: ih_check_docker has no way
    # to tell "docker is present because the install worked" from "docker is
    # present and the group-add silently failed." Docker itself installs
    # successfully here (same "sh" side-effect stub as the test above) but
    # `usermod` fails, so the verification pass sees an all-green docker
    # check while a real remediation step failed — the run must still report
    # failure.
    stubs = make_stubs(tmp_path)
    (stubs / "docker").unlink()
    write_stub(stubs, "sudo", '"$@"')
    write_stub(stubs, "usermod", "exit 1")
    write_stub(
        stubs,
        "sh",
        f'''
cat > "{stubs}/docker" <<'DOCKER_STUB'
#!/usr/bin/env bash
if [[ "$1" == "compose" ]]; then echo "Docker Compose version v2.29.0"; else echo ""; fi
exit 0
DOCKER_STUB
chmod +x "{stubs}/docker"
exit 0
''',
    )
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    result = subprocess.run(
        ["bash", str(SCRIPT), "--yes"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=script_env(root, stubs),
        timeout=60,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "docker group" in result.stdout.lower()


def test_phase3_reports_interfaces_without_blocking(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    # Full, passing avahi fixture (not just the services/ directory): this
    # test is about phase 3, and a phase-2 avahi FAIL would trip ih_main's
    # failure gate and exit before phase 3 ever runs.
    write_good_avahi_fixture(root)
    (root / "dev").mkdir(parents=True)
    (root / "dev/i2c-1").write_text("")
    (root / "sys/bus/w1/devices").mkdir(parents=True)
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "I2C" in result.stdout
    assert "1-Wire" in result.stdout
    assert result.returncode == 0, "a missing interface must not fail the run"


def full_root(tmp_path: Path) -> Path:
    """A fixture root that clears every phase-1/2/3 gate cleanly, so a test
    using it reaches phase 4.

    Deliberately does NOT hand-roll the avahi files the way the task-6 brief
    originally sketched (allow-interfaces set but no service record): that
    combination fails ih_check_avahi's service-record check, which trips
    ih_main's post-phase-2 failure gate and exits 1 before phase 3 or phase 4
    ever run — verified empirically while writing these tests, the brief's
    own phase4 tests could not pass against it. write_good_avahi_fixture
    gives the one avahi state that actually clears phase 2.
    """
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    write_good_docker_daemon_fixture(root)
    (root / "dev").mkdir(parents=True, exist_ok=True)
    (root / "dev/i2c-1").write_text("")
    (root / "sys/bus/w1/devices").mkdir(parents=True)
    (root / "sys/class/pwm/pwmchip0").mkdir(parents=True)
    return root


def test_phase4_reads_gids_off_the_machine(tmp_path: Path) -> None:
    stubs = make_stubs(
        tmp_path,
        {"getent": GOOD_GETENT},
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


def test_phase4_gate_stops_the_run_when_a_group_is_missing(tmp_path: Path) -> None:
    # Controller ruling, prompted by the same class of bug as Task 4's I1: a
    # FAIL that only prints and does not count. This is not a hypothetical —
    # it's the Task 10 Banana Pi target verbatim: i2c exists at GID 108,
    # there is no gpio group at all. Without a post-phase-4 gate, the run
    # would fall through to exit 0 with an empty GPIO_GID written into
    # deploy/.env, and compose.yaml's ${GPIO_GID:?} would only surface that
    # at `docker compose up`, after phase 5 has already pulled images and run
    # migrations on a machine that was already known to be unable to start.
    stubs = make_stubs(
        tmp_path, {"getent": 'case "$2" in i2c) echo "i2c:x:108:" ;; *) exit 2 ;; esac'}
    )
    root = full_root(tmp_path)
    result = run_script("--yes", root=root, stubs=stubs)
    assert result.returncode != 0, "a missing gpio group must not exit 0"
    assert "gpio" in result.stdout.lower()
    assert "108" in result.stdout
    # Measured on the Banana Pi M64: Armbian ships no gpio group, and the
    # FAIL alone left the operator nowhere to go.
    assert "sudo groupadd gpio" in result.stdout


def test_phase4_failed_run_leaves_no_env_behind_and_can_be_rerun(tmp_path: Path) -> None:
    # The poison: phase 4 used to ih_fail on a missing group and then write
    # deploy/.env anyway, with an empty GID, before the gate exited 1. Phase 1
    # treats a non-empty deploy/.env as "this machine already looks like a
    # hub" and refuses to run. So one failed run — say, the gpio package not
    # yet installed — left a file the operator was never told about, and every
    # later run stopped at phase 1 with a message about an existing install
    # that had never happened. A failed configure must write nothing, and the
    # next run must reach phase 4 again exactly like the first.
    stubs = make_stubs(
        tmp_path, {"getent": 'case "$2" in i2c) echo "i2c:x:108:" ;; *) exit 2 ;; esac'}
    )
    root = full_root(tmp_path)
    envfile = root.joinpath(*HUB_ROOT.parts[1:]) / "deploy" / ".env"
    # The directory exists on a real machine (it is the repo's deploy/), and
    # without it here `cp` simply fails and the poison never lands — which is
    # how the first version of this test passed against the unfixed script.
    envfile.parent.mkdir(parents=True, exist_ok=True)
    real_envfile = HUB_ROOT / "deploy" / ".env"
    assert not real_envfile.exists(), "this test refuses to run against a real deploy/.env"

    first = run_script("--yes", root=root, stubs=stubs)
    assert first.returncode != 0
    assert not envfile.exists(), "a failed configure wrote deploy/.env: " + (
        envfile.read_text() if envfile.exists() else ""
    )

    second = run_script("--yes", root=root, stubs=stubs)
    assert "already looks like a hub" not in second.stdout, second.stdout
    assert "4. configuration" in second.stdout, "the rerun never reached phase 4"
    assert not real_envfile.exists(), "the real repository's deploy/.env must never be touched"


def test_phase4_never_overwrites_an_existing_env(tmp_path: Path) -> None:
    # Controller ruling: the original version of this test only asserted
    # result.returncode in (0, 1), which passes no matter what the script
    # does. "Never overwrite deploy/.env" is a global constraint of this
    # plan and needs a test that can actually fail: stage a real file at the
    # exact path the script would write to (under IH_ROOT — see Ruling 1 on
    # ih_phase4_configure itself, which is what makes staging under the
    # fixture root instead of the real repo's deploy/.env possible at all),
    # run the script for real (not --dry-run, so the write path is actually
    # exercised), and assert the file comes out byte-identical.
    #
    # Phase 1 used to intercept a non-empty sentinel before phase 4's own
    # guard was ever reached. It no longer latches on deploy/.env (a failed
    # phase 5 leaves one behind on a machine that is not a hub), so the file
    # this test stages now reaches phase 4 for real — which is the guard the
    # invariant actually rests on. The run continues into phases 5 and 6,
    # neither of which this test is about, so both are stubbed inert.
    stubs = make_stubs(tmp_path, {"getent": GOOD_GETENT, "docker": inert_compose_docker()})
    write_phase5_stubs(stubs)
    root = full_root(tmp_path)
    envfile = root.joinpath(*HUB_ROOT.parts[1:]) / "deploy" / ".env"
    envfile.parent.mkdir(parents=True, exist_ok=True)
    # Phase 5 now reads BELLASREEF_BACKUP_DIR/BELLASREEF_ETC_DIR out of this
    # same file to create the bind-mount directories, and this test's whole
    # point is that phase 4 leaves an existing file untouched rather than
    # regenerating it — so the sentinel has to carry both keys itself, or a
    # run through the stubbed-inert phases 5/6 fails on a guard this test
    # isn't about.
    sentinel = (
        "POSTGRES_PASSWORD=already-configured-do-not-touch\n"
        "BELLASREEF_BACKUP_DIR=/home/tester/backups\n"
        "BELLASREEF_ETC_DIR=/etc/bellasreef\n"
    )
    envfile.write_text(sentinel)

    real_envfile = HUB_ROOT / "deploy" / ".env"
    assert not real_envfile.exists(), "this test refuses to run against a real deploy/.env"

    result = run_script("--yes", root=root, stubs=stubs, env=FAST_POLL)
    assert envfile.read_text() == sentinel, "deploy/.env was modified"
    assert not real_envfile.exists(), "the real repository's deploy/.env must never be touched"
    assert result.returncode == 0, result.stdout + result.stderr


def staged_env_path(root: Path) -> Path:
    """The path phase 4 writes to under a fixture root, with deploy/ created.

    The directory exists on a real machine — it is the repo's own deploy/ —
    so a fixture without it makes the write fail for a reason no operator
    would ever hit, and a test built on that passes against a broken script.
    """
    envfile = root.joinpath(*HUB_ROOT.parts[1:]) / "deploy" / ".env"
    envfile.parent.mkdir(parents=True, exist_ok=True)
    return envfile


def test_phase4_writes_a_complete_locked_down_env(tmp_path: Path) -> None:
    # The success path had no test at all: every other phase-4 test drives a
    # failure, so the script could have written nonsense — or nothing — and
    # the suite would have stayed green. This is the file the whole stack
    # then runs on, so assert its actual contents, not that a PASS printed.
    stubs = make_stubs(tmp_path, {"getent": GOOD_GETENT, "docker": inert_compose_docker()})
    # A successful phase 4 now falls through into phases 5 and 6. This test is
    # about the file phase 4 writes, so the tail is stubbed inert and green.
    write_phase5_stubs(stubs)
    root = full_root(tmp_path)
    envfile = staged_env_path(root)
    real_envfile = HUB_ROOT / "deploy" / ".env"
    assert not real_envfile.exists(), "this test refuses to run against a real deploy/.env"

    result = run_script("--yes", root=root, stubs=stubs)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not real_envfile.exists(), "the real repository's deploy/.env must never be touched"
    assert envfile.exists(), "phase 4 reported success but wrote no deploy/.env"

    # The file holds the Postgres password, so the mode is part of the
    # contract, not housekeeping.
    mode = stat.S_IMODE(envfile.stat().st_mode)
    assert mode == 0o600, f"deploy/.env is mode {mode:04o}, not 0600"

    text = envfile.read_text()
    # The reference Pi's groups, read off the stub rather than defaulted.
    assert "I2C_GID=988" in text, text
    assert "GPIO_GID=986" in text, text

    match = re.search(r"^POSTGRES_PASSWORD=([A-Za-z0-9]{32})$", text, re.MULTILINE)
    assert match, "no 32-char alphanumeric POSTGRES_PASSWORD line:\n" + text
    password = match.group(1)

    # Same password in both places, or Alembic and the services authenticate
    # with a credential Postgres was never given.
    expected_url = (
        f"BELLASREEF_DATABASE_URL=postgresql+asyncpg://"
        f"bellasreef:{password}@postgres:5432/bellasreef"
    )
    assert expected_url in text, text

    leftovers = sorted(p.name for p in envfile.parent.iterdir() if p.name != ".env")
    assert leftovers == [], f"phase 4 left temporary files behind: {leftovers}"


def test_phase4_writes_the_two_directory_keys(tmp_path: Path) -> None:
    # compose.yaml bind-mounts the backup directory and /etc/bellasreef by
    # variable now, both interpolated as ${VAR:?} — an .env without them
    # refuses to start the whole stack with an error naming the variable.
    # The backup dir is the operating user's, not a literal /home/david.
    stubs = make_stubs(tmp_path, {"getent": GOOD_GETENT, "docker": inert_compose_docker()})
    write_phase5_stubs(stubs)
    root = full_root(tmp_path)
    envfile = staged_env_path(root)

    result = run_script("--yes", root=root, stubs=stubs, env={**FAST_POLL, "HOME": "/home/tester"})
    assert result.returncode == 0, result.stdout + result.stderr
    text = envfile.read_text()
    assert "BELLASREEF_BACKUP_DIR=/home/tester/backups" in text, text
    assert "BELLASREEF_ETC_DIR=/etc/bellasreef" in text, text


def test_phase4_reads_the_tag_from_release_env(tmp_path: Path) -> None:
    # The hub checkout is bellasreef-hub, not the dev repo: its commit says
    # nothing about which images were built. The release workflow writes the
    # image SHA into deploy/release.env and that is the only source.
    stubs = make_stubs(tmp_path, {"getent": GOOD_GETENT, "docker": inert_compose_docker()})
    write_phase5_stubs(stubs)
    root = full_root(tmp_path)
    envfile = staged_env_path(root)
    manifest = write_release_env(tmp_path / "release.env", version="v0.3.0", tag="f" * 40)

    result = run_script(
        "--yes", root=root, stubs=stubs, env={**FAST_POLL, "IH_RELEASE_ENV": str(manifest)}
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "image tag v0.3.0 (ffffffffffff)" in result.stdout, result.stdout
    assert f"BELLASREEF_TAG={'f' * 40}" in envfile.read_text()


def test_phase4_refuses_a_dirty_checkout(tmp_path: Path) -> None:
    # There is no image for what is sitting in the working tree. Pinning the
    # tag to HEAD on a dirty checkout produces a hub running images that do
    # not contain the change the operator is looking at.
    stubs = make_stubs(
        tmp_path,
        {
            "getent": GOOD_GETENT,
            "git": GOOD_GIT.replace(
                "status) exit 0 ;;", 'status) echo " M services/api/main.py"; exit 0 ;;'
            ),
        },
    )
    write_stub(stubs, "sudo", "exit 1")  # never legitimately reached
    root = full_root(tmp_path)
    envfile = staged_env_path(root)

    result = run_script("--yes", root=root, stubs=stubs)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "uncommitted" in result.stdout.lower(), result.stdout
    assert not envfile.exists(), "a dirty checkout still wrote deploy/.env"


def test_phase4_stops_without_a_release_manifest(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path, {"getent": GOOD_GETENT})
    write_stub(stubs, "sudo", "exit 1")  # never legitimately reached
    root = full_root(tmp_path)
    envfile = staged_env_path(root)

    result = run_script(
        "--yes", root=root, stubs=stubs, env={"IH_RELEASE_ENV": str(tmp_path / "absent")}
    )
    assert result.returncode != 0
    assert "not a released hub" in result.stdout, result.stdout
    assert "bellasreef-hub" in result.stdout
    assert not envfile.exists()


def test_phase4_rejects_a_malformed_release_tag(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path, {"getent": GOOD_GETENT})
    write_stub(stubs, "sudo", "exit 1")
    root = full_root(tmp_path)
    envfile = staged_env_path(root)
    manifest = write_release_env(tmp_path / "release.env", tag="latest")

    result = run_script("--yes", root=root, stubs=stubs, env={"IH_RELEASE_ENV": str(manifest)})
    assert result.returncode != 0
    assert "malformed" in result.stdout, result.stdout
    assert not envfile.exists()


def test_phase4_warns_without_git_metadata_and_continues(tmp_path: Path) -> None:
    # The release tarball has no .git. That is not a dirty checkout; it is a
    # checkout whose cleanliness cannot be checked, and the run says so.
    stubs = make_stubs(tmp_path, {"getent": GOOD_GETENT, "docker": inert_compose_docker()})
    write_phase5_stubs(stubs)
    (stubs / "git").unlink()
    root = full_root(tmp_path)
    envfile = staged_env_path(root)

    result = run_script("--yes", root=root, stubs=stubs, env=FAST_POLL)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not a git checkout" in result.stdout, result.stdout
    assert envfile.exists()


def test_phase4_rejects_an_env_missing_a_substituted_key(tmp_path: Path) -> None:
    # The substitutions are only as good as the example file they run against.
    # A key renamed or dropped in deploy/.env.example leaves sed with nothing
    # to match, and the run continues with a file that is missing a value
    # compose interpolates as ${VAR:?} — which stops the entire stack, several
    # phases later, with an error naming a variable rather than a cause.
    stubs = make_stubs(
        tmp_path,
        {
            "getent": GOOD_GETENT,
            # Only phase 4 calls sed with -e; the two `sed 's/^/      /'`
            # output-indenting calls do not, so this cannot disturb them.
            "sed": (
                'if [[ "$1" == "-e" ]]; then\n'
                '  "${IH_TEST_REAL_BIN}/sed" "$@" | grep -v "^I2C_GID="\n'
                "else\n"
                '  exec "${IH_TEST_REAL_BIN}/sed" "$@"\n'
                "fi"
            ),
        },
    )
    write_stub(stubs, "sudo", "exit 1")  # never legitimately reached
    root = full_root(tmp_path)
    envfile = staged_env_path(root)

    result = run_script("--yes", root=root, stubs=stubs)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "I2C_GID" in result.stdout, result.stdout
    assert not envfile.exists(), "an incomplete deploy/.env was moved into place"


def test_phase4_writes_over_an_empty_env(tmp_path: Path) -> None:
    # An empty deploy/.env is not a configuration. It is a file somebody
    # touched, or a write that died before its first byte — and the previous
    # `-f` guard read it as "already configured, leave it alone", which hands
    # the stack no password, no GIDs and no tag. compose interpolates all of
    # those as ${VAR:?} and refuses to start on any one of them, several
    # phases later, naming a variable rather than a cause. Overwriting an
    # empty file loses nothing; the invariant is about files with content.
    stubs = make_stubs(tmp_path, {"getent": GOOD_GETENT, "docker": inert_compose_docker()})
    write_phase5_stubs(stubs)
    root = full_root(tmp_path)
    envfile = staged_env_path(root)
    envfile.write_text("")
    real_envfile = HUB_ROOT / "deploy" / ".env"
    assert not real_envfile.exists(), "this test refuses to run against a real deploy/.env"

    result = run_script("--yes", root=root, stubs=stubs, env=FAST_POLL)
    assert result.returncode == 0, result.stdout + result.stderr
    text = envfile.read_text()
    assert re.search(r"^POSTGRES_PASSWORD=[A-Za-z0-9]{32}$", text, re.MULTILINE), text
    assert "I2C_GID=988" in text, text
    assert not real_envfile.exists(), "the real repository's deploy/.env must never be touched"


def test_phase4_a_failed_write_leaves_no_partial_env(tmp_path: Path) -> None:
    # The other half of the poisoned-file problem. Gating the write on the
    # GIDs stops a *failed check* from leaving a file behind; it does nothing
    # about the write itself dying partway — sed killed, disk full, power cut
    # between the copy and the substitution. Phase 1 reads any non-empty
    # deploy/.env as "already a hub", so a truncated one blocks every later
    # run just as thoroughly as an empty-GID one did.
    #
    # A sed stub that prints part of a line and then fails is the
    # deterministic stand-in: sed is reached only in phase 4, so stubbing it
    # cannot disturb the earlier phases.
    stubs = make_stubs(
        tmp_path,
        {"getent": GOOD_GETENT, "sed": 'printf "POSTGRES_PASSW"; exit 1'},
    )
    root = full_root(tmp_path)
    envfile = staged_env_path(root)
    real_envfile = HUB_ROOT / "deploy" / ".env"
    assert not real_envfile.exists(), "this test refuses to run against a real deploy/.env"

    result = run_script("--yes", root=root, stubs=stubs)
    assert result.returncode != 0, "a failed write must not report success"
    assert not envfile.exists(), "a partial deploy/.env was left behind:\n" + (
        envfile.read_text() if envfile.exists() else ""
    )
    strays = sorted(p.name for p in envfile.parent.iterdir())
    assert strays == [], f"a failed write left files behind: {strays}"
    assert not real_envfile.exists(), "the real repository's deploy/.env must never be touched"


def test_phase4_rejects_a_password_that_is_not_32_chars(tmp_path: Path) -> None:
    # The PASS line claims "32 chars" on the strength of nothing: no /dev/
    # urandom, a busybox tr without -dc, a locale that makes the character
    # class match nothing — each yields a short or empty password and the
    # claim stands unchallenged. An empty POSTGRES_PASSWORD is not a weak
    # credential, it is a Postgres that refuses every connection, discovered
    # several phases later by something with no idea what caused it.
    #
    # Only the generator passes tr `-dc`; phases 1-3 use `-d` and the plain
    # two-set form, so the stub can shorten the password without disturbing
    # them.
    stubs = make_stubs(
        tmp_path,
        {
            "getent": GOOD_GETENT,
            # The stub runs under the same isolated PATH as the script, so a
            # bare `tr` here would find the stub again and recurse.
            # IH_TEST_REAL_BIN is the symlink farm of real tools, which is
            # where the genuine tr lives — a hardcoded /usr/bin/tr would be a
            # guess about the machine, which is the thing this harness is
            # meant to stop making.
            "tr": (
                'case "$1" in -dc) printf short ;; *) exec "${IH_TEST_REAL_BIN}/tr" "$@" ;; esac'
            ),
        },
    )
    root = full_root(tmp_path)
    envfile = staged_env_path(root)
    real_envfile = HUB_ROOT / "deploy" / ".env"
    assert not real_envfile.exists(), "this test refuses to run against a real deploy/.env"

    result = run_script("--yes", root=root, stubs=stubs)
    assert result.returncode != 0, "a 5-char password must not be accepted"
    assert "32 chars" not in result.stdout, "claimed 32 chars for a 5-char password"
    assert not envfile.exists(), "a bad password was still written to deploy/.env"
    assert not real_envfile.exists(), "the real repository's deploy/.env must never be touched"


def test_phase3_ignores_the_hdmi_i2c_buses(tmp_path: Path) -> None:
    # The reference Pi has /dev/i2c-13 and /dev/i2c-14 — the HDMI DDC buses —
    # present with i2c_arm off (CLAUDE.md, verified host facts). A glob over
    # /dev/i2c-* announces I2C as enabled on that machine and the operator
    # then wonders why their PCA9685 is unreachable. Only i2c-1 is the bus
    # hardware-io uses.
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    (root / "dev").mkdir(parents=True)
    (root / "dev/i2c-13").write_text("")
    (root / "dev/i2c-14").write_text("")
    (root / "proc/device-tree").mkdir(parents=True)
    (root / "proc/device-tree/model").write_text("Raspberry Pi 5 Model B Rev 1.0\x00")
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "I2C          bus 1 absent" in result.stdout, result.stdout


def test_phase3_reports_the_pin_mux_when_pinctrl_is_available(tmp_path: Path) -> None:
    # A channel that exports in sysfs while its pin reads `none` is the
    # standing trap: the pwmchip directory is there, the tank is dark, and
    # nothing in the inventory said so. pinctrl is the only thing that proves
    # a header pin is actually muxed to PWM.
    stubs = make_stubs(tmp_path)
    write_stub(
        stubs,
        "pinctrl",
        "echo '12: a0    pn | lo // GPIO12 = PWM0_CHAN0'\n"
        "echo '13: a0    pn | lo // GPIO13 = PWM0_CHAN1'\n"
        "echo '18: no    pd | lo // GPIO18 = none'\n"
        "echo '19: no    pd | lo // GPIO19 = none'\n"
        "exit 0",
    )
    root = full_root(tmp_path)
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "2 of 4" in result.stdout, result.stdout
    assert "pin mux not verified" not in result.stdout


def test_phase3_says_the_pin_mux_is_unverified_without_pinctrl(tmp_path: Path) -> None:
    # No pinctrl (any board that is not a Pi, and a Pi whose OS does not ship
    # it): the chips are visible and the mux is not knowable from here. Saying
    # "direct PWM channels available" on that evidence is a claim the script
    # cannot support.
    stubs = make_stubs(tmp_path)
    root = full_root(tmp_path)
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "pin mux not verified (pinctrl not installed)" in result.stdout, result.stdout
    assert result.returncode == 0, "an unverified pin mux must not fail a non-blocking phase"


def test_phase3_flags_pwm_chips_with_no_muxed_pins(tmp_path: Path) -> None:
    # The trap in full: sysfs says a chip, pinctrl says no header pin carries
    # any of its channels. That is the overlay problem, and the custom
    # overlay procedure is documented, not reprinted here.
    stubs = make_stubs(tmp_path)
    write_stub(stubs, "pinctrl", "echo '12: no    pd | lo // GPIO12 = none'\nexit 0")
    root = full_root(tmp_path)
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "no header pin is muxed to PWM" in result.stdout, result.stdout
    assert "docs/host-setup.md" in result.stdout, result.stdout


def test_phase3_reports_the_board(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    (root / "proc/device-tree").mkdir(parents=True)
    (root / "proc/device-tree/model").write_text("Raspberry Pi 5 Model B Rev 1.1\x00")
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "board        Raspberry Pi 5 Model B Rev 1.1 (RP1 present)" in result.stdout, (
        result.stdout
    )
    assert result.returncode == 0


def test_phase3_reports_an_older_pi_without_rp1(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    (root / "proc/device-tree").mkdir(parents=True)
    (root / "proc/device-tree/model").write_text("Raspberry Pi 3 Model B Plus Rev 1.3\x00")
    result = run_script("--check-only", root=root, stubs=stubs)
    out = strip_ansi(result.stdout)
    assert (
        "board        Raspberry Pi 3 Model B Plus Rev 1.3 "
        "(no RP1: SoC PWM unavailable in this stack; a PCA9685 over I2C works)" in out
    ), out
    assert result.returncode == 0
    assert result.stderr == "", result.stderr


def test_phase3_reports_a_non_pi_board_quietly(tmp_path: Path) -> None:
    # No /proc/device-tree/model at all (an x86 box, a VM). The line says
    # so and nothing else leaks — a stray "No such file or directory" on
    # stderr here is the bug this test pins.
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    result = run_script("--check-only", root=root, stubs=stubs)
    out = strip_ansi(result.stdout)
    assert "board        unknown — not a Raspberry Pi" in out, out
    assert result.returncode == 0
    assert result.stderr == "", result.stderr


def test_phase3_probes_for_a_pca9685_when_i2c_tools_exist(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    log = tmp_path / "i2cget.log"
    write_stub(stubs, "i2cget", f'echo "$*" >> "{log}"; echo 0x11; exit 0')
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    (root / "dev").mkdir(parents=True)
    (root / "dev/i2c-1").write_text("")
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "I2C          bus 1 present; PCA9685 at 0x40: answering" in result.stdout, result.stdout
    # One MODE1 read at 0x40. 0x70 is the chip's all-call address and is
    # never addressed (CLAUDE.md, verified host facts).
    assert log.read_text().strip() == "-y 1 0x40 0x00"


def test_phase3_says_not_probed_without_i2c_tools(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    (root / "dev").mkdir(parents=True)
    (root / "dev/i2c-1").write_text("")
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "PCA9685 not probed (i2c-tools not installed)" in result.stdout, result.stdout


def test_phase3_counts_ds18b20_probes_and_names_a_floating_bus(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    devices = root / "sys/bus/w1/devices"
    for name in ("w1_bus_master1", "00-100000000000", "00-600000000000"):
        (devices / name).mkdir(parents=True)
    result = run_script("--check-only", root=root, stubs=stubs)
    assert (
        "1-Wire       bus present; DS18B20 probes: 0 (bus up, nothing answering" in result.stdout
    ), result.stdout

    (devices / "28-000000bfe244").mkdir()
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "1-Wire       bus present; DS18B20 probes: 1" in result.stdout, result.stdout


def test_phase3_never_prescribes_boot_config_and_never_prompts(tmp_path: Path) -> None:
    # Nothing hardware-side is required to deploy the stack (ruled
    # 2026-08-30). No config.txt paste, no "reboot and re-run", no
    # "proceed with only these?" — a hub with no PWM and no probes is a
    # valid hub that happens to have nothing attached yet.
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    (root / "proc/device-tree").mkdir(parents=True)
    (root / "proc/device-tree/model").write_text("Raspberry Pi 5 Model B Rev 1.1\x00")
    result = run_script("--check-only", root=root, stubs=stubs)
    for forbidden in ("dtoverlay=", "dtparam=", "reboot", "proceed with only"):
        assert forbidden not in result.stdout, (forbidden, result.stdout)
    assert result.returncode == 0


# install-hub calls compose as `docker compose -f <file> --env-file <file>
# <subcommand> ...`, so a stub that reads "$2" sees `-f` and never matches the
# subcommand it meant to intercept — it falls through to a success exit and
# the test passes against a script that did nothing. Skip the flags and their
# values to find the real subcommand.
_DOCKER_SUBCOMMAND = """
sub=""
if [[ "${1:-}" == "compose" ]]; then
    shift
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -f|--env-file) shift 2 ;;
            -*) shift ;;
            *) sub="$1"; break ;;
        esac
    done
fi
"""


def docker_stub(**subcommands: str) -> str:
    """A docker stub that answers what phases 1 and 2 ask (`docker ps`,
    `docker compose version`) and runs the given body for each named compose
    subcommand. Without the compose-version answer, phase 2 fails and the run
    never reaches phase 5 at all."""
    body = [_DOCKER_SUBCOMMAND, 'case "$sub" in']
    body.append('    version) echo "Docker Compose version v2.29.0"; exit 0 ;;')
    for name, action in subcommands.items():
        body.append(f"    {name}) {action} ;;")
    body.append("esac")
    body.append("exit 0")
    return "\n".join(body)


# The services compose.yaml defines. Phase 6 derives what it expects from
# `docker compose config --services` rather than trusting whatever `ps`
# happens to list — an api that never started is invisible to "is anything
# not running", because nothing is what it lists. A stub therefore has to
# answer both, consistently.
COMPOSE_SERVICES = (
    "nats",
    "postgres",
    "victoria-metrics",
    "hardware-io",
    "control-engine",
    "api",
)


def compose_services_stub() -> str:
    return "printf '%s\\n' " + " ".join(COMPOSE_SERVICES) + "; exit 0"


def running_ps_lines(*, missing: str = "", broken: str = "") -> str:
    """`compose ps --format '{{.Name}} {{.State}}'` for a healthy stack.

    `missing` drops a service entirely (it never started); `broken` lists it
    in a state that is not running.
    """
    return "\n".join(
        f"bellasreef-{svc}-1 {'exited' if svc == broken else 'running'}"
        for svc in COMPOSE_SERVICES
        if svc != missing
    )


def inert_compose_docker(setup_code: str = "7KF2-9QMD", ps_line: str | None = None) -> str:
    """A docker stub whose compose subcommands all succeed and say nothing
    interesting — for tests that have to get through phases 5 and 6 to reach
    (or to have already reached) what they are actually about."""
    return docker_stub(
        pull="exit 0",
        run="exit 0",
        up="exit 0",
        config=compose_services_stub(),
        ps=f'echo "{running_ps_lines() if ps_line is None else ps_line}"; exit 0',
        **{"exec": f'echo "{setup_code}"; exit 0'},
    )


def phase5_root(tmp_path: Path) -> Path:
    """A fixture root that reaches phase 5: every phase-1/2/3 gate clear, and
    deploy/ staged so phase 4 can write the .env phase 5 then reads."""
    root = full_root(tmp_path)
    staged_env_path(root)
    return root


def test_phase5_creates_the_bind_mount_directories_owned_by_the_image_uid(tmp_path: Path) -> None:
    # compose.yaml bind-mounts both; a missing host path is auto-created by
    # Docker as root:root, and the api container (uid 1000) then cannot write
    # a backup. The installer creates them owned by the container uid, which
    # makes the operating user's own uid irrelevant.
    log = tmp_path / "actions.log"
    stubs = make_stubs(tmp_path, {"getent": GOOD_GETENT, "docker": inert_compose_docker()})
    write_phase5_stubs(stubs)
    write_stub(stubs, "install", install_stub(log))
    root = phase5_root(tmp_path)

    result = run_script("--yes", root=root, stubs=stubs, env={**FAST_POLL, "HOME": "/home/tester"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "home/tester/backups").is_dir()
    assert (root / "etc/bellasreef").is_dir()
    assert "creating /home/tester/backups (owned by the container uid 1000)" in strip_ansi(
        result.stdout
    ), result.stdout


def test_phase5_leaves_existing_directories_alone(tmp_path: Path) -> None:
    log = tmp_path / "actions.log"
    stubs = make_stubs(tmp_path, {"getent": GOOD_GETENT, "docker": inert_compose_docker()})
    write_phase5_stubs(stubs)
    write_stub(stubs, "install", install_stub(log))
    root = phase5_root(tmp_path)
    (root / "home/tester/backups").mkdir(parents=True)
    (root / "etc/bellasreef").mkdir(parents=True)

    result = run_script("--yes", root=root, stubs=stubs, env={**FAST_POLL, "HOME": "/home/tester"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "install-dir" not in log.read_text()
    assert "directory /home/tester/backups exists; left as is" in strip_ansi(result.stdout), (
        result.stdout
    )


def test_phase5_names_the_fix_on_a_registry_401(tmp_path: Path) -> None:
    # The images are private today, so a pull with no credentials is the most
    # likely way a first install stops. A bare "pull failed" leaves the
    # operator nowhere; the script has to name the one command that fixes it.
    stubs = make_stubs(
        tmp_path,
        {
            "getent": GOOD_GETENT,
            "docker": docker_stub(
                pull='echo "denied: requested access to the resource is denied" >&2; exit 1'
            ),
        },
    )
    # Never legitimately reached: the pull fails first. Stubbed anyway so a
    # regression that runs on past the failure cannot invoke the real sudo.
    write_stub(stubs, "sudo", "exit 1")
    root = phase5_root(tmp_path)
    real_envfile = HUB_ROOT / "deploy" / ".env"
    assert not real_envfile.exists(), "this test refuses to run against a real deploy/.env"

    result = run_script("--yes", root=root, stubs=stubs)
    combined = result.stdout + result.stderr
    assert "5. deploy" in result.stdout, "the run never reached phase 5:\n" + combined
    assert "docker login ghcr.io" in combined
    assert result.returncode != 0
    assert not real_envfile.exists(), "the real repository's deploy/.env must never be touched"


def test_a_failed_deploy_does_not_wedge_the_next_run(tmp_path: Path) -> None:
    # The wedge, end to end: phase 4 writes deploy/.env, phase 5 fails on the
    # registry (the expected first-run failure while the images are private),
    # and the operator fixes their credentials and runs the script again. Phase
    # 1 used to read that leftover .env as "already a hub", print "nothing has
    # been changed" and exit 0 — a re-run that reports success having installed
    # nothing, forever, on a machine one `docker login` away from working.
    root = phase5_root(tmp_path)
    envfile = root.joinpath(*HUB_ROOT.parts[1:]) / "deploy" / ".env"
    real_envfile = HUB_ROOT / "deploy" / ".env"
    assert not real_envfile.exists(), "this test refuses to run against a real deploy/.env"

    stubs = make_stubs(
        tmp_path,
        {
            "getent": GOOD_GETENT,
            "docker": docker_stub(
                pull='echo "denied: requested access to the resource is denied" >&2; exit 1'
            ),
        },
    )
    write_stub(stubs, "sudo", "exit 1")  # never legitimately reached
    first = run_script("--yes", root=root, stubs=stubs)
    assert "5. deploy" in first.stdout, first.stdout + first.stderr
    assert first.returncode != 0
    assert envfile.exists(), "phase 4 wrote no .env, so this is not the wedge"

    # Credentials fixed; nothing else about the machine has changed.
    write_stub(stubs, "docker", inert_compose_docker())
    write_phase5_stubs(stubs)
    second = run_script("--yes", root=root, stubs=stubs, env=FAST_POLL)
    combined = second.stdout + second.stderr
    assert "already looks like a hub" not in second.stdout, combined
    assert "5. deploy" in second.stdout, "the re-run never got past phase 1:\n" + combined
    assert second.returncode == 0, combined
    assert not real_envfile.exists(), "the real repository's deploy/.env must never be touched"


def test_phase5_names_an_unpublished_commit_on_a_pull_failure(tmp_path: Path) -> None:
    # The tag comes from deploy/release.env, not this checkout's commit, so
    # the likeliest pull failure after credentials is a release whose images
    # never made it to the registry: the registry answers "manifest unknown"
    # for a tag that is perfectly correct and simply not there. That is not
    # guessable from the raw error, so name the release.
    stubs = make_stubs(
        tmp_path,
        {
            "getent": GOOD_GETENT,
            "docker": docker_stub(pull='echo "manifest unknown" >&2; exit 1'),
        },
    )
    write_stub(stubs, "sudo", "exit 1")  # never legitimately reached
    root = phase5_root(tmp_path)

    result = run_script("--yes", root=root, stubs=stubs)
    combined = result.stdout + result.stderr
    assert "5. deploy" in result.stdout, combined
    assert result.returncode != 0
    assert f"Release {FAKE_VERSION}" in combined, combined
    assert "has no images on the registry" in combined, combined
    assert "manifest unknown" in combined, "docker's own words were swallowed"


def test_phase5_dry_run_pulls_nothing(tmp_path: Path) -> None:
    # --dry-run mutates nothing, and phase 5 is the phase with the most to
    # mutate: it pulls images, migrates a database and installs a boot unit.
    pulled = tmp_path / "pulled"
    migrated = tmp_path / "migrated"
    started = tmp_path / "started"
    unit_installed = tmp_path / "unit-installed"
    unit_enabled = tmp_path / "unit-enabled"
    stubs = make_stubs(
        tmp_path,
        {
            "getent": GOOD_GETENT,
            "docker": docker_stub(
                pull=f'touch "{pulled}"; exit 0',
                run=f'touch "{migrated}"; exit 0',
                up=f'touch "{started}"; exit 0',
            ),
            "systemctl": (
                f'case "$1" in daemon-reload|enable) touch "{unit_enabled}"; exit 0 ;; '
                "*) exit 1 ;; esac"
            ),
        },
    )
    write_stub(stubs, "sudo", '"$@"')
    write_stub(stubs, "install", f'touch "{unit_installed}"; exit 0')
    root = phase5_root(tmp_path)

    result = run_script("--dry-run", "--yes", root=root, stubs=stubs)
    assert "5. deploy" in result.stdout, result.stdout + result.stderr
    assert "would" in result.stdout.lower()
    for name, marker in (
        ("compose pull", pulled),
        ("the migrations", migrated),
        ("compose up", started),
        ("the boot unit install", unit_installed),
        ("systemctl", unit_enabled),
    ):
        assert not marker.exists(), f"--dry-run ran {name}"


def test_phase5_deploys_in_the_required_order(tmp_path: Path) -> None:
    # The order is the safety property: migrations run before anything starts
    # (no service meets a schema it was not built for), and the boot unit is
    # installed before `up`, so a machine that loses power mid-install comes
    # back to a supervised stack rather than an unsupervised one. Every
    # failure-path test above passes just as well against a phase 5 that
    # stops after the pull, so the success path needs its own test.
    log = tmp_path / "actions.log"
    stubs = make_stubs(
        tmp_path,
        {
            "getent": GOOD_GETENT,
            # ps and exec belong to phase 6, which this run now falls into.
            # They are given non-logging bodies so the ordering assertion
            # below stays about phase 5's four steps.
            "docker": docker_stub(
                pull=f'echo pull >> "{log}"; exit 0',
                run=f'echo migrate >> "{log}"; exit 0',
                # `$*` here is the whole `up ...` invocation: --wait is the
                # difference between "the containers were created" and "the
                # stack is up", and it is what the boot unit and deploy-pi.sh
                # both use.
                up=f'echo "$*" >> "{log}"; exit 0',
                config=compose_services_stub(),
                ps=f'echo "{running_ps_lines()}"; exit 0',
                **{"exec": 'echo "7KF2-9QMD"; exit 0'},
            ),
            "systemctl": systemctl_stub(boot_unit_marker(tmp_path), log),
        },
    )
    write_stub(stubs, "sudo", '"$@"')
    write_stub(stubs, "install", install_stub(log))
    write_phase6_stubs(stubs, tmp_path)
    root = phase5_root(tmp_path)
    real_envfile = HUB_ROOT / "deploy" / ".env"
    assert not real_envfile.exists(), "this test refuses to run against a real deploy/.env"

    result = run_script("--yes", root=root, stubs=stubs)
    assert "5. deploy" in result.stdout, result.stdout + result.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    assert log.read_text().split() == [
        "pull",
        "migrate",
        "install-dir",
        "install-dir",
        "install-unit",
        "systemctl",
        "daemon-reload",
        "systemctl",
        "enable",
        "up",
        "-d",
        "--wait",
    ], log.read_text()
    assert not real_envfile.exists(), "the real repository's deploy/.env must never be touched"


def test_phase5_does_not_start_the_stack_on_a_failed_migration(tmp_path: Path) -> None:
    # A service that meets a schema it was not built for is worse than one
    # that never started: it runs, answers, and is wrong. The `up` must not
    # happen, and the run must not report success.
    #
    # Alembic's own words have to reach the operator too. This is the one
    # first-install failure with no other diagnostic route — an unreachable
    # database, a generated password Postgres never received, and a broken
    # revision chain all present identically as "migrations failed" without
    # them, and none of the three is guessable from that line.
    started = tmp_path / "started"
    stubs = make_stubs(
        tmp_path,
        {
            "getent": GOOD_GETENT,
            "docker": docker_stub(
                pull="exit 0",
                run='echo "alembic: target database is not up to date" >&2; exit 1',
                up=f'touch "{started}"; exit 0',
            ),
        },
    )
    write_stub(stubs, "sudo", "exit 1")  # never legitimately reached
    root = phase5_root(tmp_path)

    result = run_script("--yes", root=root, stubs=stubs)
    assert "5. deploy" in result.stdout
    assert result.returncode != 0
    assert "migration" in result.stdout.lower()
    assert "alembic: target database is not up to date" in result.stdout, (
        "the migration failure hid alembic's output:\n" + result.stdout
    )
    assert not started.exists(), "the stack was started against an unmigrated schema"


# ------------------------------------------------------------------ phase 6

# Phase 6's /info probe polls, because `compose up -d` returns before uvicorn
# has accepted its first connection. The deadline is a test seam (see
# IH_API_DEADLINE_SECS in the script) so the failure tests below spend four
# seconds proving the retry rather than thirty proving nothing extra.
FAST_POLL = {"IH_API_DEADLINE_SECS": "4"}


def phase6_stubs(
    tmp_path: Path,
    *,
    setup_mode: str = "true",
    avahi_ok: bool = True,
    setup_code: str = "7KF2-9QMD",
    ps_line: str | None = None,
) -> tuple[Path, dict[str, Path]]:
    """A stub set that clears phases 1-5 and hands phase 6 a healthy hub.

    Returns the stubs directory and the marker/log files phase 6's own
    commands write to: curl (the /info probe), journalctl (the avahi
    evidence) and the compose `exec` that mints the setup code.
    """
    exec_log = tmp_path / "compose-exec.log"
    stubs = make_stubs(
        tmp_path,
        {
            "getent": GOOD_GETENT,
            "docker": docker_stub(
                pull="exit 0",
                run="exit 0",
                up="exit 0",
                config=compose_services_stub(),
                ps=f'echo "{running_ps_lines() if ps_line is None else ps_line}"; exit 0',
                **{"exec": f'echo "$*" >> "{exec_log}"; echo "{setup_code}"; exit 0'},
            ),
        },
    )
    write_phase5_stubs(stubs)
    markers = write_phase6_stubs(stubs, tmp_path, setup_mode=setup_mode, avahi_ok=avahi_ok)
    markers["exec"] = exec_log
    return stubs, markers


def test_phase6_verifies_a_healthy_hub_and_hands_off(tmp_path: Path) -> None:
    # The success path, end to end: every check green, the hub still in setup
    # mode, and the code the owner has to type printed where they can see it.
    # An install that finishes without ever showing the code has finished
    # nothing the owner can use.
    stubs, markers = phase6_stubs(tmp_path)
    root = phase5_root(tmp_path)
    real_envfile = HUB_ROOT / "deploy" / ".env"
    assert not real_envfile.exists(), "this test refuses to run against a real deploy/.env"

    result = run_script("--yes", root=root, stubs=stubs, env=FAST_POLL)
    combined = result.stdout + result.stderr
    assert "6. verify" in result.stdout, "the run never reached phase 6:\n" + combined
    assert result.returncode == 0, combined
    assert "7KF2-9QMD" in result.stdout, combined
    assert "Pair your phone" in result.stdout
    assert "setup-code" in markers["exec"].read_text()
    # The boot-unit check is answered by what phase 5 actually did, not by a
    # stub that says "enabled" no matter what.
    assert boot_unit_marker(tmp_path).exists(), "phase 5 never enabled the boot unit"
    assert not real_envfile.exists(), "the real repository's deploy/.env must never be touched"


def installed_unit_path(root: Path) -> Path:
    """Where phase 5 installs the boot unit under a fixture root."""
    return root / "etc/systemd/system/bellasreef.service"


def test_phase5_renders_the_boot_unit_for_this_host(tmp_path: Path) -> None:
    # deploy/systemd/bellasreef.service is written for the reference Pi:
    # User=david, WorkingDirectory=/home/david/bellasreef, and absolute
    # /home/david/bellasreef paths in ExecStart and ExecStop. deploy-pi.sh
    # installs it verbatim, which is right for that one machine and wrong for
    # every other. Installed verbatim on a stranger's hub it is a unit that
    # fails at every boot — while phase 6's is-enabled cheerfully reports that
    # the stack survives a power cut. Render it, and prove the rendering with
    # the file that actually landed in /etc/systemd/system.
    stubs, _ = phase6_stubs(tmp_path)
    root = phase5_root(tmp_path)
    env = dict(FAST_POLL)
    env["USER"] = "reef-tester"

    result = run_script("--yes", root=root, stubs=stubs, env=env)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined

    unit = installed_unit_path(root).read_text()
    assert "User=reef-tester" in unit, unit
    assert "/home/david/bellasreef" not in unit, unit
    assert f"WorkingDirectory={HUB_ROOT}" in unit, unit
    for line in ("ExecStart=", "ExecStop="):
        rendered = next(ln for ln in unit.splitlines() if ln.startswith(line))
        assert str(HUB_ROOT / "deploy") in rendered, rendered

    # The repo's own copy is what deploy-pi.sh installs on the reference host.
    # Rendering must not have edited it.
    checked_in = (HUB_ROOT / "deploy/systemd/bellasreef.service").read_text()
    assert "User=david" in checked_in
    assert "/home/david/bellasreef" in checked_in


def test_phase6_fails_when_the_installed_unit_names_a_foreign_host(tmp_path: Path) -> None:
    # The failure this pair of checks exists for: a unit that systemd will
    # happily enable and can never start, because its WorkingDirectory and its
    # compose file paths belong to somebody else's machine. "bellasreef.service
    # enabled; the stack survives a power cut" is exactly the wrong thing to
    # print about that hub.
    stubs, _ = phase6_stubs(tmp_path)
    # An install that reports success and copies nothing, so the foreign unit
    # staged below is what phase 6 reads.
    write_stub(stubs, "install", "exit 0")
    root = phase5_root(tmp_path)
    unit = installed_unit_path(root)
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        "[Service]\nUser=david\nWorkingDirectory=/home/david/bellasreef\n"
        "ExecStart=/usr/bin/docker compose -f /home/david/bellasreef/deploy/compose.yaml up -d\n"
    )

    result = run_script("--yes", root=root, stubs=stubs, env=FAST_POLL)
    combined = result.stdout + result.stderr
    assert "6. verify" in result.stdout, combined
    assert result.returncode != 0, combined
    assert "boot unit" in result.stdout.lower(), combined
    assert str(HUB_ROOT) in result.stdout, combined


def test_phase6_runs_systemd_analyze_when_it_is_available(tmp_path: Path) -> None:
    # systemd-analyze is hidden from PATH by default (see HIDDEN_FROM_PATH), so
    # every other test here exercises the "not installed, skip it silently"
    # path. This one gives the script a systemd-analyze that rejects the unit
    # and proves the answer is read rather than merely obtained.
    stubs, _ = phase6_stubs(tmp_path)
    write_stub(
        stubs,
        "systemd-analyze",
        'echo "bellasreef.service: Unknown key Frobnicate=" >&2; exit 1',
    )
    root = phase5_root(tmp_path)

    result = run_script("--yes", root=root, stubs=stubs, env=FAST_POLL)
    combined = result.stdout + result.stderr
    assert "6. verify" in result.stdout, combined
    assert result.returncode != 0, combined
    assert "Unknown key Frobnicate=" in result.stdout, combined


def test_phase6_polls_the_api_rather_than_asking_once(tmp_path: Path) -> None:
    # `compose up -d` returns as soon as the containers are created, which is
    # before uvicorn is listening. Asking once and failing would make a
    # perfectly good install report a dead API, so the probe retries until a
    # deadline — the same shape deploy-pi.sh uses.
    stubs, markers = phase6_stubs(tmp_path)
    curl_log = markers["curl"]
    write_stub(
        stubs,
        "curl",
        f'echo "$*" >> "{curl_log}"\n'
        f'if (( $(grep -c . "{curl_log}") < 3 )); then exit 7; fi\n'
        'printf \'{"contracts_version":"3.7.0","setup_mode":true}\'\n'
        "exit 0",
    )
    root = phase5_root(tmp_path)

    result = run_script("--yes", root=root, stubs=stubs, env=FAST_POLL)
    combined = result.stdout + result.stderr
    assert "6. verify" in result.stdout, combined
    assert result.returncode == 0, combined
    assert len(curl_log.read_text().splitlines()) >= 3, "the API probe did not retry"
    assert "7KF2-9QMD" in result.stdout, combined


def test_phase6_fails_when_the_api_never_answers(tmp_path: Path) -> None:
    # A hub whose front door never opens is not installed, however many
    # containers are up: no client can pair with it or read a single reading.
    # And there is no point asking it for a setup code — the FAIL is already
    # recorded, and a second failure line about the code would name a symptom
    # rather than the cause.
    stubs, markers = phase6_stubs(tmp_path)
    curl_log = markers["curl"]
    write_stub(stubs, "curl", f'echo "$*" >> "{curl_log}"\nexit 7')
    root = phase5_root(tmp_path)

    result = run_script("--yes", root=root, stubs=stubs, env=FAST_POLL)
    combined = result.stdout + result.stderr
    assert "6. verify" in result.stdout, combined
    assert result.returncode != 0, combined
    assert "API not answering" in result.stdout, combined
    assert len(curl_log.read_text().splitlines()) >= 2, "the failing probe never retried"
    assert not markers["exec"].exists(), "asked a dead API's container for a setup code"


def test_phase6_shows_no_setup_code_on_an_already_paired_hub(tmp_path: Path) -> None:
    # `bellasreef setup-code` rotates rather than reprints: running it on a
    # hub that is already paired would mint a code nobody asked for. The
    # /info body says which state the hub is in, so read it rather than
    # assuming a fresh install is always unpaired.
    stubs, markers = phase6_stubs(tmp_path, setup_mode="false")
    root = phase5_root(tmp_path)

    result = run_script("--yes", root=root, stubs=stubs, env=FAST_POLL)
    combined = result.stdout + result.stderr
    assert "6. verify" in result.stdout, combined
    assert result.returncode == 0, combined
    assert "already paired" in result.stdout.lower(), combined
    assert "Pair your phone" not in result.stdout
    assert not markers["exec"].exists(), "rotated the setup code on a paired hub"


def test_phase6_fails_when_the_boot_unit_is_not_enabled(tmp_path: Path) -> None:
    # Separate from the container check on purpose. Everything else in phase 6
    # proves the hub works now; only this proves it comes back after a power
    # cut, which for a tank controller is the failure found at the worst
    # possible time — and the message has to say so, or "not enabled" reads
    # like a tidiness complaint.
    stubs, _ = phase6_stubs(tmp_path)
    # Phase 5's enable succeeds but records nothing, so is-enabled answers
    # honestly that the unit is not enabled.
    write_stub(
        stubs,
        "systemctl",
        'case "$1" in daemon-reload|enable) exit 0 ;; is-enabled) echo disabled; exit 1 ;; '
        "*) exit 1 ;; esac",
    )
    root = phase5_root(tmp_path)

    result = run_script("--yes", root=root, stubs=stubs, env=FAST_POLL)
    combined = result.stdout + result.stderr
    assert "6. verify" in result.stdout, combined
    assert result.returncode != 0, combined
    assert "enabled" in combined.lower()
    assert "power" in combined.lower(), "the reason the check exists is not explained"


def test_phase6_is_unverified_when_avahi_cannot_be_confirmed(tmp_path: Path) -> None:
    # An unconfirmed mDNS record is not a confirmed one. The app finds the hub
    # by that record, so "could not tell" has to be visible and non-green —
    # the same rule conftest.py applies to a skipped test.
    stubs, _ = phase6_stubs(tmp_path, avahi_ok=False)
    root = phase5_root(tmp_path)

    result = run_script("--yes", root=root, stubs=stubs, env=FAST_POLL)
    combined = result.stdout + result.stderr
    assert "6. verify" in result.stdout, combined
    assert "UNVERIFIED" in result.stdout, combined
    assert "avahi" in result.stdout.lower()
    assert result.returncode != 0, "an unverified check must not exit green"


def test_phase6_fails_when_a_container_is_not_running(tmp_path: Path) -> None:
    stubs, _ = phase6_stubs(tmp_path, ps_line=running_ps_lines(broken="hardware-io"))
    root = phase5_root(tmp_path)

    result = run_script("--yes", root=root, stubs=stubs, env=FAST_POLL)
    combined = result.stdout + result.stderr
    assert "6. verify" in result.stdout, combined
    assert result.returncode != 0, combined
    assert "bellasreef-hardware-io-1" in result.stdout, "the failing service was not named"


def test_phase6_fails_when_an_expected_service_never_started(tmp_path: Path) -> None:
    # "Is anything in the list not running" cannot see a service that is not
    # in the list. A stack whose api container was never created lists five
    # healthy containers and reads as entirely healthy — while the hub has no
    # front door at all. The expected set comes from compose itself, so a
    # service added to compose.yaml is checked here without anyone
    # remembering to update a hardcoded list.
    stubs, _ = phase6_stubs(tmp_path, ps_line=running_ps_lines(missing="control-engine"))
    root = phase5_root(tmp_path)

    result = run_script("--yes", root=root, stubs=stubs, env=FAST_POLL)
    combined = result.stdout + result.stderr
    assert "6. verify" in result.stdout, combined
    assert result.returncode != 0, combined
    assert "control-engine" in result.stdout, "the missing service was not named"


def test_phase6_fails_when_compose_reports_no_containers_at_all(tmp_path: Path) -> None:
    # The trap in "grep for anything not running": an empty listing has
    # nothing that is not running, so a stack that started zero containers
    # reads as a stack that is entirely healthy.
    stubs, _ = phase6_stubs(tmp_path, ps_line="")
    root = phase5_root(tmp_path)

    result = run_script("--yes", root=root, stubs=stubs, env=FAST_POLL)
    combined = result.stdout + result.stderr
    assert "6. verify" in result.stdout, combined
    assert result.returncode != 0, combined
    # The exact line, not a bare "FAIL": any earlier phase failing would
    # satisfy that, which is how this test would keep passing against a
    # phase 6 that never looked at the container list.
    assert "no containers are running" in result.stdout, combined


def test_phase6_fails_when_the_setup_code_is_empty(tmp_path: Path) -> None:
    # The hub says it is in setup mode and then the CLI hands back nothing.
    # Printing an empty code block would send the owner to type a code that
    # does not exist; the honest answer is that the install did not finish.
    stubs, _ = phase6_stubs(tmp_path, setup_code="")
    root = phase5_root(tmp_path)

    result = run_script("--yes", root=root, stubs=stubs, env=FAST_POLL)
    combined = result.stdout + result.stderr
    assert "6. verify" in result.stdout, combined
    assert result.returncode != 0, combined
    assert "could not read the setup code" in result.stdout, combined


def test_phase6_is_unverified_when_setup_mode_cannot_be_read(tmp_path: Path) -> None:
    # The API answered, but the body does not say what state the hub is in —
    # a renamed field, a space after the colon, an HTML 200 from something
    # that is not the API. Neither branch is knowable from that, and the one
    # that reads as harmless ("already paired, nothing to show") is the one
    # that ends the install green without ever showing the owner the code
    # they need. Unknown has to look unknown.
    stubs, markers = phase6_stubs(tmp_path)
    curl_log = markers["curl"]
    write_stub(
        stubs,
        "curl",
        f'echo "$*" >> "{curl_log}"\nprintf \'{{"contracts_version":"3.7.0"}}\'\nexit 0',
    )
    root = phase5_root(tmp_path)

    result = run_script("--yes", root=root, stubs=stubs, env=FAST_POLL)
    combined = result.stdout + result.stderr
    assert "6. verify" in result.stdout, combined
    assert "UNVERIFIED" in result.stdout, combined
    assert "setup mode" in result.stdout.lower(), combined
    assert result.returncode != 0, "an unreadable setup mode must not exit green"
    assert "already paired" not in result.stdout.lower(), "guessed the hub was paired"
    assert not markers["exec"].exists(), "minted a code on a hub of unknown state"


def test_phase6_dry_run_probes_nothing(tmp_path: Path) -> None:
    # --dry-run mutates nothing, and `bellasreef setup-code` mutates: it
    # rotates the code. A dry run that "just verifies" would invalidate a
    # paired hub's code as a side effect of being asked what it would do.
    stubs, markers = phase6_stubs(tmp_path)
    root = phase5_root(tmp_path)

    result = run_script("--dry-run", "--yes", root=root, stubs=stubs, env=FAST_POLL)
    assert "6. verify" in result.stdout, result.stdout + result.stderr
    assert "would" in result.stdout.lower()
    for name in ("curl", "journalctl", "exec"):
        assert not markers[name].exists(), f"--dry-run ran {name}"
