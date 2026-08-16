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
import select
import subprocess
import termios
import time
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
        env={
            **os.environ,
            "IH_ROOT": str(root),
            "PATH": f"{stubs}:{os.environ['PATH']}",
        },
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

    env = {
        **os.environ,
        "IH_ROOT": str(root),
        "PATH": f"{stubs}:{os.environ['PATH']}",
    }

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
        env={
            **os.environ,
            "IH_ROOT": str(tmp_path / "root"),
            "IH_ASSUME_NO_TTY": "1",
            "PATH": f"{stubs}:{os.environ['PATH']}",
        },
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
    stubs = make_stubs(tmp_path)
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
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    result = subprocess.run(
        ["bash", str(SCRIPT), "--yes"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "IH_ROOT": str(root),
            "PATH": f"{stubs}:{os.environ['PATH']}",
        },
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
        env={
            **os.environ,
            "IH_ROOT": str(root),
            "PATH": f"{stubs}:{os.environ['PATH']}",
        },
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


def test_phase3_skips_boot_config_on_a_non_pi(tmp_path: Path) -> None:
    stubs = make_stubs(tmp_path)
    root = tmp_path / "root"
    write_good_avahi_fixture(root)
    (root / "proc/device-tree").mkdir(parents=True)
    (root / "proc/device-tree/model").write_text("Some Other Board\x00")
    result = run_script("--check-only", root=root, stubs=stubs)
    assert "not a raspberry pi" in result.stdout.lower()
