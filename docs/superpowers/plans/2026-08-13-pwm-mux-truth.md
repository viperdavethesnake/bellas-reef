# PWM Mux-Truth Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** hardware-io announces the PWM channels the operator's overlay actually muxes to pins — read from the live pin mux, not a hand-maintained map — and finds its PWM chip by hardware identity, never by sysfs index. Approved 2026-08-13 (brainstorm options A + B2 + C).

**Architecture:** All in `services/hardware_io/bellasreef_hardware_io/capabilities.py` + its tests. Chip resolution scans `/sys/class/pwm/*` for the device whose resolved path ends in the RP1 PWM0 block name (`1f00098000.pwm`) and whose `of_node/compatible` is `raspberrypi,rp1-pwm`. Channel→gpio derivation runs `pinctrl get` at announce time (injectable runner for tests) and parses `GPIO<n> = PWM0_CHAN<c>` lines. Any failure (chip not found, pinctrl missing/unparseable) → announce nothing for pi-pwm, `log.critical` — honest absence over guessed presence. `PWM_CHANNEL_GPIO` (the hand map) is deleted.

**Tech Stack:** Python 3.13, mypy --strict, pytest with tmp_path fake sysfs trees, subprocess for pinctrl.

## Global Constraints

- Repo `/Users/david/visualstudio/bellasreef`. Branch `fix/pwm-mux-truth` off current main. Backend flow: local gate `BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh` → push (`BELLASREEF_ALLOW_ENV_SKIPS=1 git push`) → PR → CI green → controller merges/deploys. mypy --strict and ruff must stay clean; run `uv run ruff format services/hardware_io/` before the gate.
- TDD per task: failing test → RED run → implement → GREEN full-file run → commit. Test command: `uv run pytest services/hardware_io/tests/test_capabilities.py -v` from repo root.
- Verified board facts to encode exactly (measured 2026-08-13): `/sys/class/pwm/pwmchip0 -> /sys/devices/platform/axi/1000120000.pcie/1f00098000.pwm/pwm/pwmchip0` (ours, PWM0) and `pwmchip1 -> .../1f0009c000.pwm/...` (PWM1, the fan header's block — never announce it); both report npwm 4; `of_node/compatible` of ours is `raspberrypi,rp1-pwm` (NUL-terminated in sysfs); pinctrl output lines look exactly like `12: a0    pd | lo // GPIO12 = PWM0_CHAN0` and `18: no    pd | -- // GPIO18 = none`; pinctrl lives at `/usr/sbin/pinctrl` (not on the service PATH).
- Existing tests in `services/hardware_io/tests/test_capabilities.py` (5, from `e30286d`) will need rework in Task 2 — reworking them to the new truth source is in-scope, deleting coverage is not.
- Conventional commits with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Interfaces already in the codebase

- `capabilities.py` today: `PWM_CHIP = Path("/sys/class/pwm/pwmchip0")` (index-addressed — the bug), `PWM_CHANNEL_GPIO: dict[int, int] = {0: 12, 1: 13}` (the hand map — being deleted), `discover_pwm(chip: Path = PWM_CHIP) -> CapabilityAnnouncement | None` (filters by the map, announces `detail={"chip": chip.name, "gpio": gpio}`), `discover_w1(...)` (untouched), `log` a module logger, `CapabilityChannel`/`CapabilityAnnouncement` from `bellasreef_contracts`.
- Caller: `app.py` calls `discover_pwm()` with no args at startup and publishes if non-None. Its call site must keep working with no-arg defaults.

---

### Task 1: The chip is found by identity, not index

**Files:**
- Modify: `services/hardware_io/bellasreef_hardware_io/capabilities.py`
- Test: `services/hardware_io/tests/test_capabilities.py` (append a new class; existing tests untouched in this task)

**Interfaces:**
- Produces: `RP1_PWM0_DEVICE: Final = "1f00098000.pwm"`, `RP1_PWM_COMPATIBLE: Final = "raspberrypi,rp1-pwm"`, and `find_pwm_chip(pwm_class: Path = Path("/sys/class/pwm")) -> Path | None` — returns the chip directory (the entry under `pwm_class`) whose resolved device is RP1 PWM0 and whose compatible matches, else None (with a critical log naming what was found instead). Task 2 rewires `discover_pwm` onto it.

- [ ] **Step 1: Write the failing tests** (append to the test file)

```python
class TestFindPwmChip:
    """The chip index has moved between kernel releases (CLAUDE.md, verified
    host facts) and the second RP1 PWM instance drives the fan header.
    Announcing by index risks offering the fan as lighting; identity is the
    only safe address."""

    def _sysfs(self, tmp_path: Path, chips: dict[str, tuple[str, str | None]]) -> Path:
        """Build /sys/class/pwm with symlinks into a fake device tree.

        chips maps class entry name -> (device block name, compatible or None).
        """
        devices = tmp_path / "devices"
        pwm_class = tmp_path / "class-pwm"
        pwm_class.mkdir()
        for entry, (block, compatible) in chips.items():
            chip_dir = devices / block / "pwm" / entry
            chip_dir.mkdir(parents=True)
            (chip_dir / "npwm").write_text("4\n")
            if compatible is not None:
                of_node = devices / block / "of_node"
                of_node.mkdir(exist_ok=True)
                (of_node / "compatible").write_bytes(compatible.encode() + b"\x00")
                (chip_dir / "device").symlink_to(devices / block)
            (pwm_class / entry).symlink_to(chip_dir)
        return pwm_class

    def test_the_rp1_pwm0_block_is_found_wherever_its_index_lands(self, tmp_path: Path) -> None:
        """The fan's block sits at index 0 here — the bounce CLAUDE.md warns
        about — and identity must still pick ours at index 1."""
        pwm_class = self._sysfs(
            tmp_path,
            {
                "pwmchip0": ("1f0009c000.pwm", "raspberrypi,rp1-pwm"),
                "pwmchip1": ("1f00098000.pwm", "raspberrypi,rp1-pwm"),
            },
        )
        chip = find_pwm_chip(pwm_class)
        assert chip is not None
        assert chip.name == "pwmchip1"

    def test_a_matching_name_with_the_wrong_compatible_is_refused(self, tmp_path: Path) -> None:
        pwm_class = self._sysfs(tmp_path, {"pwmchip0": ("1f00098000.pwm", "some,other-pwm")})
        assert find_pwm_chip(pwm_class) is None

    def test_no_rp1_block_present_finds_nothing(self, tmp_path: Path) -> None:
        pwm_class = self._sysfs(tmp_path, {"pwmchip0": ("1f0009c000.pwm", "raspberrypi,rp1-pwm")})
        assert find_pwm_chip(pwm_class) is None

    def test_a_missing_class_directory_finds_nothing(self, tmp_path: Path) -> None:
        assert find_pwm_chip(tmp_path / "absent") is None
```

- [ ] **Step 2: RED** — run the test command; expect NameError/ImportError on `find_pwm_chip` (add it to the file's import list from `bellasreef_hardware_io.capabilities`).

- [ ] **Step 3: Implement** (in capabilities.py, above `discover_pwm`; add `Final` to imports if absent)

```python
#: The RP1's first PWM block — the one the overlay muxes to header pins.
#: The SECOND instance (1f0009c000.pwm) drives the fan header; announcing it
#: would offer the fan as lighting. Both measured on this board 2026-08-13.
RP1_PWM0_DEVICE: Final = "1f00098000.pwm"
RP1_PWM_COMPATIBLE: Final = "raspberrypi,rp1-pwm"


def find_pwm_chip(pwm_class: Path = Path("/sys/class/pwm")) -> Path | None:
    """Locate the RP1 PWM0 chip by hardware identity, never by index.

    The pwmchipN index has moved between kernel releases (CLAUDE.md, verified
    host facts), so each class entry is resolved to the device it fronts and
    matched on the block name plus the device-tree compatible.
    """
    if not pwm_class.is_dir():
        return None
    for entry in sorted(pwm_class.iterdir()):
        try:
            device = (entry / "device").resolve()
        except OSError:
            continue
        if device.name != RP1_PWM0_DEVICE:
            continue
        try:
            compatible = (device / "of_node" / "compatible").read_bytes()
        except OSError:
            log.critical(
                "RP1 PWM0 block found but its compatible is unreadable",
                extra={"chip": str(entry)},
            )
            return None
        if RP1_PWM_COMPATIBLE not in compatible.decode(errors="replace"):
            log.critical(
                "the device at the RP1 PWM0 address is not an rp1-pwm",
                extra={"chip": str(entry), "compatible": compatible.decode(errors="replace")},
            )
            return None
        return entry
    log.critical("no RP1 PWM0 block under %s — pi-pwm will not be announced", pwm_class)
    return None
```

Note the test's symlink layout: `entry` IS the chip dir via symlink and `entry / "device"` is a symlink to the block dir — `.resolve()` lands on the block, `.name` is the block name. The real sysfs has one more level (`<block>/pwm/<chip>`), and `chip/device` points at the block there too, so the same code walks both.

- [ ] **Step 4: GREEN** — full test file passes (9 tests: 5 existing + 4 new).

- [ ] **Step 5: Commit** — `fix(hardware-io): the PWM chip is found by identity, not index` (+ trailer).

---

### Task 2: The announcement mirrors the live pin mux

**Files:**
- Modify: `services/hardware_io/bellasreef_hardware_io/capabilities.py`
- Modify: `services/hardware_io/tests/test_capabilities.py` (rework `TestDiscoverPwm`)

**Interfaces:**
- Consumes: `find_pwm_chip` from Task 1.
- Produces: `PINCTRL: Final = "/usr/sbin/pinctrl"`; `read_pwm_mux(runner: ...) -> dict[int, int] | None` returning channel→gpio derived from pinctrl (None = could not read, distinct from {} = readable-but-nothing-muxed); `discover_pwm(pwm_class: Path = Path("/sys/class/pwm"), mux_reader: Callable[[], dict[int, int] | None] = read_pwm_mux) -> CapabilityAnnouncement | None`. `PWM_CHANNEL_GPIO` and the `chip: Path` parameter are DELETED; `app.py`'s no-arg call keeps working.

- [ ] **Step 1: Rework the tests** — replace `TestDiscoverPwm` (its map-based assertions describe the deleted design) with:

```python
def _rp1_class(tmp_path: Path) -> Path:
    devices = tmp_path / "devices"
    chip = devices / "1f00098000.pwm" / "pwm" / "pwmchip0"
    chip.mkdir(parents=True)
    (chip / "npwm").write_text("4\n")
    of_node = devices / "1f00098000.pwm" / "of_node"
    of_node.mkdir()
    (of_node / "compatible").write_bytes(b"raspberrypi,rp1-pwm\x00")
    (chip / "device").symlink_to(devices / "1f00098000.pwm")
    pwm_class = tmp_path / "class-pwm"
    pwm_class.mkdir()
    (pwm_class / "pwmchip0").symlink_to(chip)
    return pwm_class


#: pinctrl output exactly as this board prints it (2026-08-13).
TWO_MUXED = """\
12: a0    pd | lo // GPIO12 = PWM0_CHAN0
13: a0    pd | lo // GPIO13 = PWM0_CHAN1
18: no    pd | -- // GPIO18 = none
19: no    pd | -- // GPIO19 = none
"""

FOUR_MUXED = TWO_MUXED.replace(
    "18: no    pd | -- // GPIO18 = none", "18: a3    pd | lo // GPIO18 = PWM0_CHAN2"
).replace("19: no    pd | -- // GPIO19 = none", "19: a3    pd | lo // GPIO19 = PWM0_CHAN3")


class TestDiscoverPwm:
    """The announcement mirrors the operator's overlay, read from the live
    mux. Two channels muxed -> two announced; the full four-channel setup ->
    four announced, zero code change. Unreadable mux -> honest absence."""

    def test_two_muxed_channels_announce_two(self, tmp_path: Path) -> None:
        announcement = discover_pwm(_rp1_class(tmp_path), mux_reader=lambda: _parse(TWO_MUXED))
        assert announcement is not None
        assert [(c.channel, c.detail["gpio"]) for c in announcement.channels] == [
            ("0", 12),
            ("1", 13),
        ]

    def test_four_muxed_channels_announce_four(self, tmp_path: Path) -> None:
        announcement = discover_pwm(_rp1_class(tmp_path), mux_reader=lambda: _parse(FOUR_MUXED))
        assert announcement is not None
        assert [(c.channel, c.detail["gpio"]) for c in announcement.channels] == [
            ("0", 12),
            ("1", 13),
            ("2", 18),
            ("3", 19),
        ]

    def test_channels_beyond_npwm_are_never_announced(self, tmp_path: Path) -> None:
        """npwm still bounds the mux: a pin claiming a channel the chip does
        not report must not conjure one."""
        pwm_class = _rp1_class(tmp_path)
        chip = (pwm_class / "pwmchip0").resolve()
        (chip / "npwm").write_text("1\n")
        announcement = discover_pwm(pwm_class, mux_reader=lambda: _parse(FOUR_MUXED))
        assert announcement is not None
        assert [c.channel for c in announcement.channels] == ["0"]

    def test_an_unreadable_mux_announces_nothing(self, tmp_path: Path) -> None:
        assert discover_pwm(_rp1_class(tmp_path), mux_reader=lambda: None) is None

    def test_a_readable_mux_with_nothing_muxed_announces_nothing(self, tmp_path: Path) -> None:
        assert discover_pwm(_rp1_class(tmp_path), mux_reader=lambda: {}) is None

    def test_a_missing_chip_announces_nothing(self, tmp_path: Path) -> None:
        empty = tmp_path / "class-pwm"
        empty.mkdir()
        assert discover_pwm(empty, mux_reader=lambda: _parse(TWO_MUXED)) is None


class TestReadPwmMux:
    def test_this_boards_output_parses(self) -> None:
        assert _parse(TWO_MUXED) == {0: 12, 1: 13}

    def test_garbage_yields_no_reading(self) -> None:
        assert _parse("not pinctrl output at all\n") == {}
```

with `_parse` imported as the pure parsing half (see Step 3): `from bellasreef_hardware_io.capabilities import parse_pinctrl as _parse` (adjust the import line at the top of the file: `find_pwm_chip`, `discover_pwm`, `parse_pinctrl`; `PWM_CHANNEL_GPIO` import goes away).

- [ ] **Step 2: RED** — run; expect failures on the new API (`mux_reader` unknown, `parse_pinctrl` missing, `PWM_CHANNEL_GPIO` import error).

- [ ] **Step 3: Implement.** Delete `PWM_CHANNEL_GPIO` and its comment block; delete `PWM_CHIP`. Add:

```python
#: Absolute because the service PATH does not include /usr/sbin (CLAUDE.md,
#: "PATH trap").
PINCTRL: Final = "/usr/sbin/pinctrl"

#: One pinctrl line: "12: a0    pd | lo // GPIO12 = PWM0_CHAN0"
_PINCTRL_LINE = re.compile(r"//\s*GPIO(\d+)\s*=\s*PWM0_CHAN(\d+)\s*$")


def parse_pinctrl(output: str) -> dict[int, int]:
    """channel -> gpio for every pin the mux ties to the RP1 PWM0 block."""
    mux: dict[int, int] = {}
    for line in output.splitlines():
        if match := _PINCTRL_LINE.search(line):
            mux[int(match.group(2))] = int(match.group(1))
    return mux


def read_pwm_mux() -> dict[int, int] | None:
    """The live pin mux, from ``pinctrl get``. None means it could not be
    read — which callers must treat as "announce nothing", never as "nothing
    is muxed": a hub that cannot see the mux must not guess at it.
    """
    try:
        result = subprocess.run(  # noqa: S603
            [PINCTRL, "get"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.critical("pinctrl could not run: %s — pi-pwm will not be announced", exc)
        return None
    if result.returncode != 0:
        log.critical(
            "pinctrl exited %d: %s — pi-pwm will not be announced",
            result.returncode,
            result.stderr.strip(),
        )
        return None
    return parse_pinctrl(result.stdout)
```

and rewrite `discover_pwm`:

```python
def discover_pwm(
    pwm_class: Path = Path("/sys/class/pwm"),
    mux_reader: Callable[[], dict[int, int] | None] = read_pwm_mux,
) -> CapabilityAnnouncement | None:
    """The RP1's pin-backed PWM channels, read from the live mux.

    The announcement mirrors the operator's overlay: whatever ``pinctrl``
    says is muxed to the PWM0 block is what the hub offers, bounded by the
    chip's ``npwm``. There is no hand-maintained map to drift — an overlay
    change is reflected at the next startup, and a mux that cannot be read
    announces nothing rather than guessing (the two pinless RP1 channels
    shipped as adoptable ghosts on 2026-08-13; never again by construction).
    """
    chip = find_pwm_chip(pwm_class)
    if chip is None:
        return None
    try:
        npwm = int((chip / "npwm").read_text().strip())
    except (OSError, ValueError):
        log.warning("pwm chip present but npwm unreadable", extra={"chip": str(chip)})
        return None

    mux = mux_reader()
    if mux is None:
        return None
    channels = [
        CapabilityChannel(channel=str(index), detail={"chip": chip.name, "gpio": mux[index]})
        for index in sorted(mux)
        if index < npwm
    ]
    if not channels:
        log.critical("the RP1 PWM0 block has no pins muxed — pi-pwm not announced")
        return None

    return CapabilityAnnouncement(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="hardware-io",
        hardware_source="pi-pwm",
        channels=channels,
    )
```

Add `re`, `subprocess`, `Callable` (from `collections.abc`) to imports; update `__all__` (drop `PWM_CHANNEL_GPIO`/`PWM_CHIP`, add `PINCTRL`, `find_pwm_chip`, `parse_pinctrl`, `read_pwm_mux`). Check `app.py`'s call site still compiles (no-arg call — it does).

- [ ] **Step 4: GREEN** — full file (12 tests), then the whole gate: `uv run ruff format services/hardware_io/ && BELLASREEF_ALLOW_ENV_SKIPS=1 ./scripts/check.sh`.

- [ ] **Step 5: Commit** — `feat(hardware-io): the PWM announcement mirrors the live pin mux` (+ trailer), then `BELLASREEF_ALLOW_ENV_SKIPS=1 git push -u origin fix/pwm-mux-truth` and `gh pr create --fill`. Do not merge — the controller does.

---

### Task 3: Docs and the blocking-flag rule (controller, after merge+deploy)

- [ ] Host-setup: in §9's PWM area, add the verified four-channel table (ch0→GPIO12/pin32, ch1→GPIO13/pin33, ch2→GPIO18/pin12, ch3→GPIO19/pin35 — first two pinctrl-verified live, latter two verified as available functions 2026-08-13) and the extension path: change the overlay, reboot, `pinctrl get 18,19` to verify, restart hardware-io — channels appear in the app with no code change.
- [ ] CLAUDE.md, Code standards or Deployment discipline area: one bullet — "A recorded measured-vs-documented discrepancy is a blocking flag: no dependent config or unit ships on top of it until it is resolved on hardware or explicitly accommodated in the design (the npwm=4-vs-archive-2 question sat unresolved under two days of PWM work, 2026-08-13)."
- [ ] Deploy, then verify on the hub: capabilities table still shows exactly pi-pwm 0→12 and 1→13 (same registry outcome, now derived from the mux), journald shows no critical from discovery, telemetry fresh.

## Self-Review

- Option A → Task 1 (identity + compatible guard, fan-block hazard encoded in test 1). B2 → Task 2 (mux-derived, npwm-bounded, None-vs-{} distinction, honest absence on every failure). C → Task 3 (CLAUDE.md rule). 4-channel table → Task 3.
- Placeholders: none. Type consistency: `find_pwm_chip`/`parse_pinctrl`/`read_pwm_mux`/`mux_reader` names match across tasks; `Final`/`Callable` imports named.
- Risk noted: the fake-sysfs symlink layout differs one level from real sysfs; the code path (`entry/"device"` resolve → block name) is layout-agnostic, and Task 3's live verify is the real-tree check.
