# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""DS18B20 driver.

Parsing and failure behaviour run everywhere against a fake sysfs tree. The
tests that need the real probe are marked ``hardware`` and excluded from the
default run — CI never touches a tank.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from bellasreef_contracts.driver import CalibrationPoint, OneWireDevice, SensorSample
from bellasreef_hardware_io.drivers import onewire
from bellasreef_hardware_io.drivers.onewire import DS18B20, discover_probes

# A real capture from the attached probe, 2026-08-09.
GOOD = "93 01 7f 80 7f ff 0d 10 bd : crc=bd YES\n93 01 7f 80 7f ff 0d 10 bd t=25187\n"
BAD_CRC = GOOD.replace("YES", "NO")
POWER_ON_RESET = "50 05 4b 46 7f ff 0c 10 1c : crc=1c YES\n50 05 4b 46 7f ff 0c 10 1c t=85000\n"

ROM = "28-000000bfe244"


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


@pytest.fixture
def w1_root(tmp_path: Path) -> Path:
    (tmp_path / ROM).mkdir()
    (tmp_path / ROM / "w1_slave").write_text(GOOD)
    return tmp_path


def _driver(root: Path, **kw: Any) -> DS18B20:
    return DS18B20(OneWireDevice(device_id=ROM), root=root, **kw)


@pytest.fixture(autouse=True)
def _reset_bus_locks() -> None:
    """``_BUS_LOCKS`` is a process-global dict, shared by every ``DS18B20``
    instance in this file that uses the default ``bus_master``. Clearing it
    around each test makes the get-or-create invariant (one lock object per
    bus master, enforced by ``dict.setdefault``) explicit rather than merely
    true by accident because no test's lock happens to survive long enough
    to leak into the next one.
    """
    onewire._BUS_LOCKS.clear()


class TestParsing:
    def test_good_read(self, w1_root: Path) -> None:
        sample = run(_driver(w1_root).read)
        assert sample.quality == "ok"
        assert sample.value == pytest.approx(25.187)
        assert sample.raw == pytest.approx(25.187)
        assert sample.unit == "degC"

    def test_bad_crc_is_a_fault_not_a_value(self, w1_root: Path) -> None:
        """A failing CRC usually means a marginal pull-up. Publishing the
        number would be worse than publishing nothing."""
        (w1_root / ROM / "w1_slave").write_text(BAD_CRC)
        sample = run(_driver(w1_root).read)
        assert sample.quality == "fault"
        assert sample.value is None

    def test_power_on_reset_value_is_rejected(self, w1_root: Path) -> None:
        """85.0 C is the DS18B20's power-up scratchpad value.

        It means "converted nothing yet", and in a reef it is not a plausible
        reading either way.
        """
        (w1_root / ROM / "w1_slave").write_text(POWER_ON_RESET)
        sample = run(_driver(w1_root).read)
        assert sample.quality == "fault"

    def test_missing_probe_does_not_raise(self, tmp_path: Path) -> None:
        """An unplugged probe is an operating condition, not an exception."""
        sample = run(_driver(tmp_path).read)
        assert sample.quality == "fault"
        assert sample.value is None

    def test_negative_temperature_parses(self, w1_root: Path) -> None:
        (w1_root / ROM / "w1_slave").write_text(
            "ff ff 7f 80 7f ff 0c 10 21 : crc=21 YES\nff ff 7f 80 7f ff 0c 10 21 t=-1250\n"
        )
        sample = run(_driver(w1_root).read)
        assert sample.quality == "ok"
        assert sample.value == pytest.approx(-1.25)


class TestCalibration:
    def test_offset_is_applied_and_raw_is_preserved(self, w1_root: Path) -> None:
        driver = _driver(w1_root)

        async def scenario() -> tuple[float | None, float | None]:
            await driver.calibrate([CalibrationPoint(raw=25.187, reference=25.4, unit="degC")])
            s = await driver.read()
            return s.value, s.raw

        value, raw = run(scenario)
        assert value == pytest.approx(25.4)
        # raw survives so a bad calibration stays diagnosable after the fact.
        assert raw == pytest.approx(25.187)

    def test_empty_calibration_is_refused(self, w1_root: Path) -> None:
        with pytest.raises(ValueError, match="at least one"):
            run(lambda: _driver(w1_root).calibrate([]))


class TestConfiguration:
    def test_timeout_below_conversion_time_is_refused(self, w1_root: Path) -> None:
        """831 ms measured. A 0.5 s deadline could never succeed, so accepting
        it would just manufacture permanent faults."""
        with pytest.raises(ValueError, match="below the measured"):
            _driver(w1_root, read_timeout_s=0.5)

    @pytest.mark.parametrize("field", ["poll_interval_s", "read_timeout_s"])
    def test_non_positive_timings_refused(self, w1_root: Path, field: str) -> None:
        with pytest.raises(ValueError):
            _driver(w1_root, **{field: 0.0})

    def test_discovery_finds_only_ds18b20_family(self, tmp_path: Path) -> None:
        for name in (ROM, "10-0000deadbeef", "w1_bus_master1", "28-000000000002"):
            (tmp_path / name).mkdir()
        found = {d.device_id for d in discover_probes(tmp_path)}
        assert found == {ROM, "28-000000000002"}

    def test_discovery_on_a_host_with_no_bus(self, tmp_path: Path) -> None:
        assert discover_probes(tmp_path / "nope") == ()


class TestBusSerialisation:
    def test_slow_probe_does_not_stall_the_event_loop(self, w1_root: Path) -> None:
        """The whole point of the timing rule.

        A probe that takes ~300 ms must not stop other work progressing. If the
        blocking read were not offloaded, the ticker below would barely advance.
        """

        class SlowProbe(DS18B20):
            def _blocking_read(self) -> str:
                time.sleep(0.3)
                return GOOD

        probe = SlowProbe(OneWireDevice(device_id=ROM), root=w1_root)
        ticks = 0

        async def scenario() -> None:
            nonlocal ticks

            async def ticker() -> None:
                nonlocal ticks
                while True:
                    ticks += 1
                    await asyncio.sleep(0.01)

            t = asyncio.create_task(ticker())
            sample = await probe.read()
            t.cancel()
            assert sample.quality == "ok"

        run(scenario)
        # ~30 ticks expected in 300 ms. A blocked loop would yield ~0.
        assert ticks > 10, f"event loop was stalled by the read (only {ticks} ticks)"

    def test_probes_on_one_bus_serialise_against_each_other(self, w1_root: Path) -> None:
        """Two probes on the same bus master must queue, not interleave.

        1-Wire is a single shared line; overlapping conversions corrupt both.
        """
        overlap = 0
        active = 0

        class TrackedProbe(DS18B20):
            def _blocking_read(self) -> str:
                nonlocal overlap, active
                active += 1
                if active > 1:
                    overlap += 1
                time.sleep(0.05)
                active -= 1
                return GOOD

        a = TrackedProbe(OneWireDevice(device_id=ROM), root=w1_root)
        b = TrackedProbe(OneWireDevice(device_id=ROM), root=w1_root)

        async def scenario() -> None:
            await asyncio.gather(a.read(), b.read())

        run(scenario)
        assert overlap == 0, "two probes converted on the same bus simultaneously"


class TestBusLockSurvivesTimeout:
    """Task 3: the bus lock must be held for the reader thread's true lifetime.

    Previously the lock was an ``asyncio.Lock`` held by ``async with`` around
    ``wait_for``. On timeout, ``wait_for`` cancels the AWAIT, not the thread —
    the sysfs read kept running on the bus, but the ``async with`` unwound and
    released the lock anyway, so a second probe could start converting on the
    wire while the first was still mid-read. Fixed by moving the lock to a
    ``threading.Lock`` acquired and released inside the worker thread itself,
    which has no cancellation hole to unwind through.

    (No multi-loop test for the dissolved asyncio.Lock loop-binding defect:
    a ``threading.Lock`` has no event loop to bind to, so that failure mode is
    structurally impossible now, not merely untested.)
    """

    def test_timeout_does_not_release_the_bus(self, w1_root: Path) -> None:
        probe1_entered = threading.Event()
        release_probe1 = threading.Event()
        probe1_finished = threading.Event()
        second_thread_started = threading.Event()
        second_entered = threading.Event()
        overlap_detected = threading.Event()

        class StragglerProbe(DS18B20):
            def _blocking_read(self) -> str:
                probe1_entered.set()
                # Held open by the test, not by a sleep duration, so the
                # ordering assertion below does not depend on timing luck.
                release_probe1.wait()
                probe1_finished.set()
                return GOOD

        class SecondProbe(DS18B20):
            def _read_locked(self, lock: threading.Lock) -> str:
                # Set BEFORE contesting the lock, so the test can wait for
                # this thread to have actually started — deterministically —
                # rather than guessing how long "a moment" is. The lock is
                # still held by the straggler at this point, so whatever
                # scheduling gap exists between this line and the
                # ``with lock:`` below cannot let this thread reach
                # ``_blocking_read`` early; it can only block on acquire.
                second_thread_started.set()
                return super()._read_locked(lock)

            def _blocking_read(self) -> str:
                if not probe1_finished.is_set():
                    overlap_detected.set()
                second_entered.set()
                return GOOD

        # Same default bus_master, so both probes resolve to one lock.
        probe1 = StragglerProbe(OneWireDevice(device_id=ROM), root=w1_root, read_timeout_s=1.0)
        probe2 = SecondProbe(OneWireDevice(device_id=ROM), root=w1_root, read_timeout_s=1.0)

        async def scenario() -> None:
            probe2_task: asyncio.Task[SensorSample] | None = None
            try:
                sample1 = await probe1.read()
                assert sample1.quality == "fault"
                # The straggler thread is still running the "conversion" —
                # proven by the fact that only the test itself can release it.
                assert probe1_entered.is_set()
                assert not probe1_finished.is_set()

                probe2_task = asyncio.create_task(probe2.read())
                # Deterministic: wait for probe 2's worker thread to have
                # actually started, rather than sleeping a guessed duration.
                # It must still be unable to pass the lock while probe 1's
                # straggler holds it — this is the assertion the old code
                # failed.
                await asyncio.to_thread(second_thread_started.wait)
                assert not second_entered.is_set(), (
                    "probe 2 entered the blocking body while probe 1's "
                    "straggler thread was still running — the bus lock was "
                    "released early"
                )

                release_probe1.set()
                sample2 = await probe2_task
                assert sample2.quality == "ok"
            finally:
                # Release the straggler thread no matter how the scenario
                # above ends. Left blocked, it would still be sitting in
                # release_probe1.wait() when asyncio.run() tears the loop
                # down and its executor shutdown joins every worker thread —
                # which would hang the whole test rather than fail it.
                release_probe1.set()
                if probe2_task is not None and not probe2_task.done():
                    probe2_task.cancel()

        run(scenario)
        assert not overlap_detected.is_set(), (
            "probe 2 ran before probe 1's thread released the lock"
        )
        assert probe1_finished.is_set()
        assert second_entered.is_set()


# ------------------------------------------------------------ real hardware


@pytest.mark.hardware
class TestAgainstRealProbe:
    """Runs only on the Pi with a probe fitted. Never in CI."""

    def test_reads_a_plausible_tank_temperature(self) -> None:
        probes = discover_probes()
        if not probes:
            pytest.skip("no DS18B20 on this host")

        sample = run(DS18B20(probes[0]).read)
        assert sample.quality == "ok"
        assert sample.value is not None
        assert 0.0 < sample.value < 60.0, f"implausible reading {sample.value}"

    def test_measured_read_cost_matches_the_documented_figure(self) -> None:
        probes = discover_probes()
        if not probes:
            pytest.skip("no DS18B20 on this host")

        driver = DS18B20(probes[0])
        start = time.monotonic()
        run(driver.read)
        elapsed = time.monotonic() - start

        # Documented at 831 ms. Assert the order of magnitude, not the exact
        # number — this is a check that the conversion is real, and that the
        # figure in CLAUDE.md has not silently drifted.
        assert 0.5 < elapsed < 2.0, f"read took {elapsed:.3f}s"
