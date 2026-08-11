# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""A probe that stops reporting raises its own alert.

The failure this closes is the one that hid a ten-hour outage in plain sight.
hardware-io died, the probe published nothing, and the last reading it had sent
sat on the dashboard looking like a healthy tank. Nothing in the system was
willing to say "I no longer know what the temperature is", because every alert
in it was a statement about a number, and there were no more numbers.

Two rules do most of the work here.

Silence is judged against the probe's *declared cadence*, not a global timeout.
The DS18B20 publishes about every 5.6s; a hypothetical pH probe on a 5-minute
poll is not late at 30s. Six cadences with a 30s floor is the ruling: enough
margin that one skipped read is not an alarm, and the floor stops a fast probe
raising alerts on ordinary jitter.

And a silence suspends threshold evaluation. The last number a dead probe
published is not evidence about now, so re-deciding a breach from it would
either invent a recovery or keep asserting a breach nobody can confirm.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from bellasreef_contracts import SensorReading, SensorSilence
from bellasreef_control_engine.alerts import (
    SILENCE_CADENCE_MULTIPLE,
    SILENCE_FLOOR_S,
    SilenceWatcher,
    silence_deadline_s,
)


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class FakeSilenceStore:
    def __init__(self, cadences: dict[str, float] | None = None) -> None:
        self.cadences_map = cadences if cadences is not None else {"probe": 5.6}
        self.raised: list[tuple[str, datetime | None]] = []
        self.cleared: list[tuple[str, float]] = []
        self.open: set[str] = set()

    async def cadences(self) -> dict[str, float]:
        return self.cadences_map

    async def open_silences(self) -> frozenset[str]:
        return frozenset(self.open)

    async def raise_silence(
        self,
        device_id: str,
        sensor_type: str,
        *,
        at: datetime,
        last_reading_at: datetime | None,
    ) -> None:
        self.raised.append((device_id, last_reading_at))
        self.open.add(device_id)

    async def clear_silence(self, device_id: str, *, at: datetime, value: float) -> None:
        self.cleared.append((device_id, value))
        self.open.discard(device_id)


class Published:
    def __init__(self) -> None:
        self.messages: list[tuple[str, SensorSilence]] = []

    async def __call__(self, subject: str, message: SensorSilence) -> None:
        self.messages.append((subject, message))


def _reading(value: float | None = 23.9, quality: str = "ok") -> SensorReading:
    from uuid import uuid4

    return SensorReading(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="hardware-io",
        sensor_id="probe",
        sensor_type="temp",
        value=value,
        unit="degC",
        quality=quality,  # type: ignore[arg-type]
    )


def _watcher(store: FakeSilenceStore, published: Published) -> SilenceWatcher:
    return SilenceWatcher(store, published)


# ------------------------------------------------------------------ deadline


def test_the_deadline_is_six_cadences() -> None:
    assert silence_deadline_s(60.0) == 60.0 * SILENCE_CADENCE_MULTIPLE


def test_a_fast_probe_gets_the_floor_instead() -> None:
    """A probe polling every second would otherwise be declared dead after six.

    Ordinary scheduling jitter on a serialised 1-Wire bus would trip that
    constantly. The floor binds below 5 s of cadence.
    """
    assert silence_deadline_s(1.0) == SILENCE_FLOOR_S
    assert silence_deadline_s(4.0) == SILENCE_FLOOR_S


def test_the_real_probe_sits_just_above_the_floor() -> None:
    """The DS18B20 publishes about every 5.6s, so 6x is 33.6s and the multiple wins.

    Worth pinning: it lands close enough to the floor that a small change to
    either constant would silently swap which rule governs the only probe
    actually installed.
    """
    assert silence_deadline_s(5.6) == pytest.approx(33.6)


def test_the_floor_and_multiple_are_constants_not_configuration() -> None:
    """A per-device knob here would be a way to switch the alarm off by accident."""
    assert SILENCE_FLOOR_S == 30.0
    assert SILENCE_CADENCE_MULTIPLE == 6


# ------------------------------------------------------------------- raising


def test_a_probe_that_goes_quiet_past_its_deadline_raises() -> None:
    async def scenario() -> None:
        store = FakeSilenceStore({"probe": 60.0})  # deadline 360s
        published = Published()
        watcher = _watcher(store, published)
        await watcher.prime()

        t0 = datetime.now(UTC)
        await watcher.on_reading(_reading(), now=t0)

        # Not yet.
        await watcher.sweep(now=t0 + timedelta(seconds=300))
        assert store.raised == []

        await watcher.sweep(now=t0 + timedelta(seconds=361))
        assert [d for d, _ in store.raised] == ["probe"]

    run(scenario)


def test_the_raise_is_published_with_the_deadline_it_used() -> None:
    """A client cannot recompute this: the deadline is per-probe."""

    async def scenario() -> None:
        store = FakeSilenceStore({"probe": 60.0})
        published = Published()
        watcher = _watcher(store, published)
        await watcher.prime()

        t0 = datetime.now(UTC)
        await watcher.on_reading(_reading(), now=t0)
        await watcher.sweep(now=t0 + timedelta(seconds=400))

        subject, message = published.messages[0]
        assert subject == "bellasreef.silence.probe"
        assert message.state == "breach"
        assert message.silence_threshold_s == 360.0
        assert message.silent_for_s >= 400.0 - 1
        assert message.last_reading_at == t0

    run(scenario)


def test_it_raises_only_once_while_the_probe_stays_quiet() -> None:
    async def scenario() -> None:
        store = FakeSilenceStore({"probe": 60.0})
        watcher = _watcher(store, Published())
        await watcher.prime()

        t0 = datetime.now(UTC)
        await watcher.on_reading(_reading(), now=t0)
        for extra in (400, 500, 600):
            await watcher.sweep(now=t0 + timedelta(seconds=extra))

        assert len(store.raised) == 1

    run(scenario)


def test_a_probe_with_no_declared_cadence_is_never_judged_silent() -> None:
    """There is no expectation to miss, so there is nothing to report."""

    async def scenario() -> None:
        store = FakeSilenceStore({})
        watcher = _watcher(store, Published())
        await watcher.prime()

        t0 = datetime.now(UTC)
        await watcher.on_reading(_reading(), now=t0)
        await watcher.sweep(now=t0 + timedelta(hours=6))

        assert store.raised == []

    run(scenario)


# ------------------------------------------------------------------ clearing


def test_the_first_good_reading_clears_it() -> None:
    async def scenario() -> None:
        store = FakeSilenceStore({"probe": 60.0})
        published = Published()
        watcher = _watcher(store, published)
        await watcher.prime()

        t0 = datetime.now(UTC)
        await watcher.on_reading(_reading(), now=t0)
        await watcher.sweep(now=t0 + timedelta(seconds=400))
        assert store.open == {"probe"}

        back = t0 + timedelta(seconds=420)
        await watcher.on_reading(_reading(24.5), now=back)

        assert store.cleared == [("probe", 24.5)]
        assert store.open == set()
        assert published.messages[-1][1].state == "clear"

    run(scenario)


def test_a_faulted_reading_does_not_clear_a_silence() -> None:
    """A probe answering with an error is still not telling us the temperature.

    The ruling says the first `quality=ok` reading clears it, and this is the
    case that makes the wording matter: a DS18B20 with a bad CRC publishes a
    fault, which proves the wire is alive and proves nothing about the tank.
    """

    async def scenario() -> None:
        store = FakeSilenceStore({"probe": 60.0})
        watcher = _watcher(store, Published())
        await watcher.prime()

        t0 = datetime.now(UTC)
        await watcher.on_reading(_reading(), now=t0)
        await watcher.sweep(now=t0 + timedelta(seconds=400))

        await watcher.on_reading(_reading(None, "fault"), now=t0 + timedelta(seconds=420))
        assert store.cleared == []
        assert store.open == {"probe"}

    run(scenario)


# ------------------------------------------------- suppressing threshold work


def test_threshold_evaluation_is_suspended_while_a_probe_is_silent() -> None:
    async def scenario() -> None:
        store = FakeSilenceStore({"probe": 60.0})
        watcher = _watcher(store, Published())
        await watcher.prime()

        t0 = datetime.now(UTC)
        await watcher.on_reading(_reading(), now=t0)
        assert not watcher.is_silent("probe")

        await watcher.sweep(now=t0 + timedelta(seconds=400))
        assert watcher.is_silent("probe")

    run(scenario)


def test_threshold_evaluation_resumes_with_the_readings() -> None:
    async def scenario() -> None:
        store = FakeSilenceStore({"probe": 60.0})
        watcher = _watcher(store, Published())
        await watcher.prime()

        t0 = datetime.now(UTC)
        await watcher.on_reading(_reading(), now=t0)
        await watcher.sweep(now=t0 + timedelta(seconds=400))
        await watcher.on_reading(_reading(24.5), now=t0 + timedelta(seconds=420))

        assert not watcher.is_silent("probe")

    run(scenario)


def test_a_restart_resumes_an_open_silence_rather_than_re_raising() -> None:
    """The unique index is per (device, class); a second raise would be lost, not doubled."""

    async def scenario() -> None:
        store = FakeSilenceStore({"probe": 60.0})
        store.open = {"probe"}
        watcher = _watcher(store, Published())
        await watcher.prime()

        assert watcher.is_silent("probe")
        await watcher.sweep(now=datetime.now(UTC) + timedelta(hours=1))
        assert store.raised == []

    run(scenario)
