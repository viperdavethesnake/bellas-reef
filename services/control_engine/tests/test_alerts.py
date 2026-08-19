# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Threshold evaluation (PRD R12).

The headline test is :func:`test_a_reading_oscillating_on_the_boundary_alerts_once`.
Every other test here describes a rule; that one describes the failure the rule
exists to prevent, and it is the one that would fail if someone "simplified"
hysteresis away.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from bellasreef_contracts import AlertBound, SensorAlert, SensorReading
from bellasreef_control_engine.alerts import (
    AlertSupervisor,
    Thresholds,
    evaluate,
    should_evaluate,
)


def run[T](scenario: Callable[[], Coroutine[object, object, T]]) -> T:
    """Match the codebase convention: no async plugin, one loop per test."""
    return asyncio.run(scenario())


BAND = Thresholds(minimum=24.0, maximum=27.0, clear_margin=0.5)
NOTHING_OPEN: frozenset[AlertBound] = frozenset()


def reading(value: float | None, *, quality: str = "ok") -> SensorReading:
    return SensorReading(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="hardware-io",
        sensor_id="ds18b20-28-000000bfe244",
        sensor_type="temp",
        value=value,
        unit="degC",
        quality=quality,  # type: ignore[arg-type]
    )


class FakeStore:
    """In-memory ``AlertStore``. Records the order of calls, which is the point."""

    def __init__(self, open_now: Mapping[str, frozenset[str]] | None = None) -> None:
        self._open = dict(open_now or {})
        self.calls: list[tuple[str, str, str]] = []

    async def open_bounds(self) -> Mapping[str, frozenset[str]]:
        return self._open

    async def raise_episode(self, alert: SensorAlert) -> None:
        self.calls.append(("raise", alert.device_id, alert.bound))

    async def clear_episode(self, alert: SensorAlert) -> None:
        self.calls.append(("clear", alert.device_id, alert.bound))


class Recorder:
    def __init__(self) -> None:
        self.published: list[tuple[str, SensorAlert]] = []

    async def __call__(self, subject: str, alert: SensorAlert) -> None:
        self.published.append((subject, alert))


# --------------------------------------------------------------- pure evaluate


def test_a_reading_inside_the_band_says_nothing() -> None:
    assert evaluate(25.5, BAND, NOTHING_OPEN) == []


def test_below_minimum_breaches() -> None:
    (change,) = evaluate(23.9, BAND, NOTHING_OPEN)
    assert (change.bound, change.state) == ("min", "breach")


def test_an_open_breach_does_not_re_announce() -> None:
    assert evaluate(23.0, BAND, frozenset[AlertBound]({"min"})) == []


def test_recovering_into_the_margin_does_not_clear() -> None:
    """24.3 is back above the 24.0 minimum but inside the 0.5 margin.

    This is the whole mechanism: "no longer in breach" and "clear" are
    different questions, and answering the second with the first is what makes
    an alert strobe.
    """
    assert evaluate(24.3, BAND, frozenset[AlertBound]({"min"})) == []


def test_recovering_past_the_margin_clears() -> None:
    (change,) = evaluate(24.5, BAND, frozenset[AlertBound]({"min"}))
    assert (change.bound, change.state) == ("min", "clear")


def test_above_maximum_breaches_and_clears_symmetrically() -> None:
    (breach,) = evaluate(27.1, BAND, NOTHING_OPEN)
    assert (breach.bound, breach.state) == ("max", "breach")
    assert evaluate(26.8, BAND, frozenset[AlertBound]({"max"})) == []  # inside the margin
    (clear,) = evaluate(26.5, BAND, frozenset[AlertBound]({"max"}))
    assert (clear.bound, clear.state) == ("max", "clear")


def test_exactly_on_the_threshold_is_not_a_breach() -> None:
    """The band is inclusive. A tank held precisely at its minimum is at spec,
    not below it, and treating equality as failure would alert on a setpoint."""
    assert evaluate(24.0, BAND, NOTHING_OPEN) == []
    assert evaluate(27.0, BAND, NOTHING_OPEN) == []


def test_the_two_bounds_are_independent() -> None:
    """A tank that swung from too cold to too hot must clear the cold alert.

    With a single "in alert" flag this returns nothing and the operator sees a
    stale "too cold" while the heater cooks the tank.
    """
    changes = evaluate(27.5, BAND, frozenset[AlertBound]({"min"}))
    assert {(c.bound, c.state) for c in changes} == {("min", "clear"), ("max", "breach")}


def test_a_single_sided_band_ignores_the_other_side() -> None:
    only_max = Thresholds(minimum=None, maximum=27.0, clear_margin=0.5)
    assert evaluate(-40.0, only_max, NOTHING_OPEN) == []


def test_the_oscillation_the_margin_exists_to_stop() -> None:
    """A probe dithering across its minimum alerts once, not once per sample.

    The sequence is a real DS18B20 pattern: 12-bit resolution quantises to
    0.0625 °C, so a tank sitting on 24.0 will cross it on quantisation alone.
    Without hysteresis this emits eight transitions.
    """
    samples = [23.99, 24.01, 23.98, 24.02, 23.97, 24.03, 23.99, 24.01]
    open_bounds: set[AlertBound] = set()
    transitions = []
    for value in samples:
        for change in evaluate(value, BAND, frozenset(open_bounds)):
            transitions.append(change)
            if change.state == "breach":
                open_bounds.add(change.bound)
            else:
                open_bounds.discard(change.bound)

    assert len(transitions) == 1
    assert (transitions[0].bound, transitions[0].state) == ("min", "breach")


# ------------------------------------------------------------- configuration


def test_a_margin_wider_than_half_the_band_is_refused() -> None:
    """Otherwise the clear zone [min+margin, max-margin] is empty and a breach
    latches forever — mirrors the clear_zone_is_reachable CHECK."""
    with pytest.raises(ValueError, match="wider than half the band"):
        Thresholds(minimum=24.0, maximum=25.0, clear_margin=0.6)


def test_a_non_positive_margin_is_refused() -> None:
    with pytest.raises(ValueError, match="clear_margin must be > 0"):
        Thresholds(minimum=24.0, maximum=27.0, clear_margin=0.0)


def test_an_inverted_band_is_refused() -> None:
    with pytest.raises(ValueError, match="minimum must be below maximum"):
        Thresholds(minimum=27.0, maximum=24.0, clear_margin=0.5)


# ------------------------------------------------------------------- quality


def test_a_faulted_reading_is_not_evaluated() -> None:
    """A dead probe is its own alert class. Reading 'fault' as 'freezing' sends
    someone to the heater when the actual failure is a broken wire."""
    assert not should_evaluate(reading(None, quality="fault"))


def test_a_stale_reading_is_not_evaluated() -> None:
    assert not should_evaluate(reading(23.0, quality="stale"))


def test_a_good_reading_is_evaluated() -> None:
    assert should_evaluate(reading(23.0))


def test_a_faulted_reading_publishes_no_alert() -> None:
    async def scenario() -> None:
        supervisor = AlertSupervisor(FakeStore(), Recorder())
        assert await supervisor.on_reading(reading(None, quality="fault"), BAND) == []

    run(scenario)


# ----------------------------------------------------------------- supervisor


def test_the_episode_is_recorded_before_it_is_announced() -> None:
    """Postgres is the system of record.

    An alert nobody heard is recoverable — a client reads GET /alerts and finds
    it. An alert with no row behind it cannot be reconciled at all.
    """

    async def scenario() -> None:
        order: list[str] = []

        class OrderedStore(FakeStore):
            async def raise_episode(self, alert: SensorAlert) -> None:
                order.append("store")

        async def announce(subject: str, alert: SensorAlert) -> None:
            order.append("publish")

        supervisor = AlertSupervisor(OrderedStore(), announce)
        await supervisor.on_reading(reading(23.0), BAND)
        assert order == ["store", "publish"]

    run(scenario)


def test_a_breach_publishes_on_the_devices_own_subject() -> None:
    async def scenario() -> None:
        publish = Recorder()
        supervisor = AlertSupervisor(FakeStore(), publish)

        await supervisor.on_reading(reading(23.0), BAND)

        (subject, alert) = publish.published[0]
        assert subject == "bellasreef.alert.ds18b20-28-000000bfe244"
        assert (alert.state, alert.bound, alert.value, alert.threshold) == (
            "breach",
            "min",
            23.0,
            24.0,
        )
        assert alert.clear_margin == 0.5

    run(scenario)


def test_priming_from_an_open_episode_does_not_re_announce() -> None:
    """A restart mid-breach must not tell the operator it just happened again."""

    async def scenario() -> None:
        store = FakeStore({"ds18b20-28-000000bfe244": frozenset[AlertBound]({"min"})})
        publish = Recorder()
        supervisor = AlertSupervisor(store, publish)
        await supervisor.prime()

        await supervisor.on_reading(reading(23.0), BAND)  # still cold
        assert publish.published == []

        await supervisor.on_reading(reading(24.6), BAND)  # recovered past the margin
        assert [a.state for _, a in publish.published] == ["clear"]

    run(scenario)


def test_resync_forgets_an_episode_closed_elsewhere() -> None:
    """The API closes an episode when its bound is removed from the band.

    Without a resync the mirror keeps calling the bound open, so a re-set band
    never re-raises a live breach: the reading below is still cold and would
    have gone unannounced. With it, the record wins and the breach is raised
    afresh — the row the API closed is history, this is a new episode.
    """

    async def scenario() -> None:
        store = FakeStore({"ds18b20-28-000000bfe244": frozenset[AlertBound]({"min"})})
        publish = Recorder()
        supervisor = AlertSupervisor(store, publish)
        await supervisor.prime()

        store._open = {}  # the API closed it when the operator removed the bound
        await supervisor.resync()

        await supervisor.on_reading(reading(23.0), BAND)  # band re-set, still cold
        assert [a.state for _, a in publish.published] == ["breach"]
        assert store.calls == [("raise", "ds18b20-28-000000bfe244", "min")]

    run(scenario)


def test_resync_is_a_no_op_when_the_record_agrees() -> None:
    async def scenario() -> None:
        store = FakeStore({"ds18b20-28-000000bfe244": frozenset[AlertBound]({"min"})})
        publish = Recorder()
        supervisor = AlertSupervisor(store, publish)
        await supervisor.prime()
        await supervisor.on_reading(reading(23.0), BAND)  # seen, still open, nothing said

        await supervisor.resync()

        await supervisor.on_reading(reading(23.0), BAND)
        assert publish.published == []

    run(scenario)


def test_a_device_with_no_configured_band_is_skipped() -> None:
    async def scenario() -> None:
        publish = Recorder()
        supervisor = AlertSupervisor(FakeStore(), publish)
        empty = Thresholds(minimum=None, maximum=None, clear_margin=0.5)

        assert await supervisor.on_reading(reading(-40.0), empty) == []
        assert publish.published == []

    run(scenario)


def test_resync_keeps_an_episode_raised_while_the_record_was_being_read() -> None:
    """The record read is an await and the sensor callback is another task: a
    breach can begin between the read starting and finishing. Replacing the
    mirror with the read would forget it, and the next reading would re-raise
    into the partial unique index and lose the alert. Reconciled: the raise
    survives the resync."""

    async def scenario() -> None:
        store = FakeStore()
        publish = Recorder()
        supervisor = AlertSupervisor(store, publish)
        await supervisor.prime()

        # Make the read slow enough to interleave, and raise during it.
        original = store.open_bounds

        async def slow_read() -> Mapping[str, frozenset[str]]:
            await supervisor.on_reading(reading(23.0), BAND)  # breach raised mid-read
            return await original()

        store.open_bounds = slow_read  # type: ignore[method-assign]
        await supervisor.resync()

        # A second cold reading must NOT re-raise: the mirror still holds it.
        await supervisor.on_reading(reading(23.0), BAND)
        assert [a.state for _, a in publish.published] == ["breach"]
        assert store.calls == [("raise", "ds18b20-28-000000bfe244", "min")]

    run(scenario)
