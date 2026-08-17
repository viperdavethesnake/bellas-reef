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
SCRIPT = REPO_ROOT / "scripts" / "install-hub.sh"


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
# bash, sed, grep, tr, awk, head, tail, cut, mktemp, mv, chmod, rm, id and
# the rest have to keep working. sed and tr are stubbed by individual tests
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


# The reference Pi's groups: i2c 988, gpio 986 (CLAUDE.md, verified host facts).
GOOD_GETENT = (
    'case "$2" in i2c) echo "i2c:x:988:david" ;; gpio) echo "gpio:x:986:david" ;; *) exit 2 ;; esac'
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
_MUTATION_GUARD_COMMANDS = ("sh", "curl", "usermod", "apt-get", "cp")


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
        f'case "$1" in enable|reload) touch "{systemctl_marker}"; exit 0 ;; *) exit 1 ;; esac',
    )
    markers["systemctl"] = systemctl_marker
    return markers


def write_phase5_stubs(stubs: Path) -> None:
    """Make phase 5's mutations inert, for tests that are not about phase 5.

    A full `--yes` run does not stop after writing deploy/.env any more: it
    goes on to pull images, migrate, install the boot unit and start the
    stack. Without these stubs an earlier phase's test reaches the real
    `sudo`, which fails on a password prompt for a reason that has nothing to
    do with what the test is checking. Call this after make_stubs — it
    replaces the phase-1 systemctl stub with one that still reports the boot
    unit as not-enabled but accepts the two subcommands phase 5 runs.
    """
    write_stub(stubs, "sudo", '"$@"')
    write_stub(stubs, "install", "exit 0")
    write_stub(stubs, "systemctl", 'case "$1" in daemon-reload|enable) exit 0 ;; *) exit 1 ;; esac')


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
    result = run_script("--yes", root=root, stubs=stubs)
    assert "UNVERIFIED" in result.stdout
    assert result.returncode != 0
    assert not markers["apt-get"].exists(), "unverified clock triggered a chrony install"
    assert not markers["systemctl"].exists(), "unverified clock triggered enabling clock units"


def test_accepted_remediation_clears_the_recorded_failure(tmp_path: Path) -> None:
    # Controller ruling: ih_phase2_requirements must end with a verification
    # pass. Without it, a check that fails, gets remediated, and now passes
    # still leaves its original FAIL sitting in IH_FAILURES, so a successful
    # install would still exit non-zero. Prove the opposite: docker is
    # missing, --yes accepts the offered install, the stubbed "sh" (the
    # convenience-script installer's interpreter) makes a working "docker"
    # appear on PATH as its side effect, and the run reports success.
    #
    # This run is not --check-only, so it reaches phase 4, which now gates
    # ih_main on IH_FAILURES the same way phase 2 does. A real i2c/gpio
    # getent stub is needed here so phase 4's own groups check doesn't add
    # an unrelated FAIL and mask what this test is actually about.
    stubs = make_stubs(
        tmp_path,
        {"getent": GOOD_GETENT},
    )
    (stubs / "docker").unlink()
    write_stub(stubs, "sudo", '"$@"')
    write_stub(stubs, "usermod", "exit 0")
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
    # This run goes all the way through phase 5, which is not what the test
    # is about: stub its mutations rather than letting them reach real sudo.
    write_phase5_stubs(stubs)
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    # Phase 4 runs for real here, so it needs a deploy/ directory to write
    # into — as a real machine has. Without one it used to "succeed" anyway:
    # an unchecked cp let the run reach the PASS line with no file on disk.
    # The write is checked now, so the fixture has to be honest.
    staged_env_path(root)
    result = subprocess.run(
        ["bash", str(SCRIPT), "--yes"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=script_env(root, stubs),
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "requirement(s) failed" not in result.stdout


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


def test_phase3_says_which_interfaces_are_absent(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    # A Pi 5, so the config.txt guidance (specific dtparam/dtoverlay lines) is
    # expected rather than the "not a Raspberry Pi" branch.
    (root / "proc/device-tree").mkdir(parents=True)
    (root / "proc/device-tree/model").write_text("Raspberry Pi 5 Model B Rev 1.0\x00")
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "not enabled" in result.stdout.lower()
    assert "dtparam=i2c_arm=on" in result.stdout
    assert result.returncode == 0


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
    envfile = root.joinpath(*REPO_ROOT.parts[1:]) / "deploy" / ".env"
    # The directory exists on a real machine (it is the repo's deploy/), and
    # without it here `cp` simply fails and the poison never lands — which is
    # how the first version of this test passed against the unfixed script.
    envfile.parent.mkdir(parents=True, exist_ok=True)
    real_envfile = REPO_ROOT / "deploy" / ".env"
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
    # A non-empty sentinel here is intercepted by phase 1's own
    # already-deployed check (it uses -s, non-empty) before phase 4's `-f`
    # guard is ever reached — confirmed by running this fixture. That is
    # still a faithful test of the stated invariant: from the outside,
    # nothing about "never overwrite deploy/.env" says which phase has to be
    # the one that stops it, and phase 1 stopping first is itself the
    # correct behaviour for a host that looks already-configured.
    stubs = make_stubs(tmp_path)
    root = full_root(tmp_path)
    envfile = root.joinpath(*REPO_ROOT.parts[1:]) / "deploy" / ".env"
    envfile.parent.mkdir(parents=True, exist_ok=True)
    sentinel = "POSTGRES_PASSWORD=already-configured-do-not-touch\n"
    envfile.write_text(sentinel)

    real_envfile = REPO_ROOT / "deploy" / ".env"
    assert not real_envfile.exists(), "this test refuses to run against a real deploy/.env"

    result = run_script("--yes", root=root, stubs=stubs)
    assert envfile.read_text() == sentinel, "deploy/.env was modified"
    assert not real_envfile.exists(), "the real repository's deploy/.env must never be touched"
    assert result.returncode == 0, result.stdout + result.stderr


def staged_env_path(root: Path) -> Path:
    """The path phase 4 writes to under a fixture root, with deploy/ created.

    The directory exists on a real machine — it is the repo's own deploy/ —
    so a fixture without it makes the write fail for a reason no operator
    would ever hit, and a test built on that passes against a broken script.
    """
    envfile = root.joinpath(*REPO_ROOT.parts[1:]) / "deploy" / ".env"
    envfile.parent.mkdir(parents=True, exist_ok=True)
    return envfile


def test_phase4_writes_a_complete_locked_down_env(tmp_path: Path) -> None:
    # The success path had no test at all: every other phase-4 test drives a
    # failure, so the script could have written nonsense — or nothing — and
    # the suite would have stayed green. This is the file the whole stack
    # then runs on, so assert its actual contents, not that a PASS printed.
    stubs = make_stubs(tmp_path, {"getent": GOOD_GETENT})
    # A successful phase 4 now falls through into phase 5. This test is about
    # the file phase 4 writes, so phase 5's mutations are stubbed inert.
    write_phase5_stubs(stubs)
    root = full_root(tmp_path)
    envfile = staged_env_path(root)
    real_envfile = REPO_ROOT / "deploy" / ".env"
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
    real_envfile = REPO_ROOT / "deploy" / ".env"
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
    real_envfile = REPO_ROOT / "deploy" / ".env"
    assert not real_envfile.exists(), "this test refuses to run against a real deploy/.env"

    result = run_script("--yes", root=root, stubs=stubs)
    assert result.returncode != 0, "a 5-char password must not be accepted"
    assert "32 chars" not in result.stdout, "claimed 32 chars for a 5-char password"
    assert not envfile.exists(), "a bad password was still written to deploy/.env"
    assert not real_envfile.exists(), "the real repository's deploy/.env must never be touched"


def test_phase3_skips_boot_config_on_a_non_pi(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    (root / "proc/device-tree").mkdir(parents=True)
    (root / "proc/device-tree/model").write_text("Some Other Board\x00")
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "not a raspberry pi" in result.stdout.lower()


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


def phase5_root(tmp_path: Path) -> Path:
    """A fixture root that reaches phase 5: every phase-1/2/3 gate clear, and
    deploy/ staged so phase 4 can write the .env phase 5 then reads."""
    root = full_root(tmp_path)
    staged_env_path(root)
    return root


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
    real_envfile = REPO_ROOT / "deploy" / ".env"
    assert not real_envfile.exists(), "this test refuses to run against a real deploy/.env"

    result = run_script("--yes", root=root, stubs=stubs)
    combined = result.stdout + result.stderr
    assert "5. deploy" in result.stdout, "the run never reached phase 5:\n" + combined
    assert "docker login ghcr.io" in combined
    assert result.returncode != 0
    assert not real_envfile.exists(), "the real repository's deploy/.env must never be touched"


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
            "docker": docker_stub(
                pull=f'echo pull >> "{log}"; exit 0',
                run=f'echo migrate >> "{log}"; exit 0',
                up=f'echo up >> "{log}"; exit 0',
            ),
            "systemctl": (
                f'case "$1" in daemon-reload|enable) echo "systemctl $1" >> "{log}"; exit 0 ;; '
                "*) exit 1 ;; esac"
            ),
        },
    )
    write_stub(stubs, "sudo", '"$@"')
    write_stub(stubs, "install", f'echo install-unit >> "{log}"; exit 0')
    root = phase5_root(tmp_path)
    real_envfile = REPO_ROOT / "deploy" / ".env"
    assert not real_envfile.exists(), "this test refuses to run against a real deploy/.env"

    result = run_script("--yes", root=root, stubs=stubs)
    assert "5. deploy" in result.stdout, result.stdout + result.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    assert log.read_text().split() == [
        "pull",
        "migrate",
        "install-unit",
        "systemctl",
        "daemon-reload",
        "systemctl",
        "enable",
        "up",
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
