# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Threshold evaluation with hysteresis (PRD R12).

The decision is a pure function of one reading, one threshold configuration, and
which bounds are already open. Everything that talks to a broker or a database
lives in :class:`AlertSupervisor` around it, so the part that decides whether a
tank is too cold can be tested exhaustively without either.

**Why hysteresis is not optional.** A DS18B20 at 12-bit resolution quantises to
0.0625 °C, and a probe sitting on a threshold will cross it on quantisation
noise alone. Without a separate clear point, a reading oscillating across the
boundary emits breach/clear/breach/clear at the polling cadence — a stream of
alerts that is worse than none, because it trains the operator to ignore them.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol
from uuid import uuid4

from bellasreef_contracts import (
    AlertBound,
    AlertState,
    SensorAlert,
    SensorReading,
    SensorSilence,
    subjects,
)

log = logging.getLogger(__name__)

__all__ = [
    "SILENCE_CADENCE_MULTIPLE",
    "SILENCE_FLOOR_S",
    "AlertStore",
    "AlertSupervisor",
    "PublishAlert",
    "PublishSilence",
    "SilenceStore",
    "SilenceWatcher",
    "Thresholds",
    "Transition",
    "evaluate",
    "should_evaluate",
    "silence_deadline_s",
]

#: Publishes one alert to one subject. A callable rather than a broker handle so
#: the supervisor can be driven by a list in a test without a NATS server.
PublishAlert = Callable[[str, SensorAlert], Awaitable[None]]
PublishSilence = Callable[[str, SensorSilence], Awaitable[None]]


class AlertStore(Protocol):
    """The persistence the supervisor needs, and nothing more.

    Narrow on purpose: the supervisor should not be able to read schedules or
    write devices just because it happens to hold a database handle.
    """

    async def open_bounds(self) -> Mapping[str, frozenset[str]]:
        """Bounds with an open episode, per device id.

        Returns plain strings, not :data:`AlertBound`. The store cannot promise
        the literal type — ``frozenset`` is invariant, so a store returning
        ``frozenset[str]`` would not satisfy a protocol demanding
        ``frozenset[AlertBound]``. The narrowing happens once, in
        :meth:`AlertSupervisor.prime`, where an unexpected value can be logged
        rather than silently coerced.
        """
        ...

    async def raise_episode(self, alert: SensorAlert) -> None: ...

    async def clear_episode(self, alert: SensorAlert) -> None: ...


@dataclass(frozen=True, slots=True)
class Thresholds:
    """What a sensor is allowed to read, and how far back it must come.

    ``clear_margin`` is expressed in the sensor's own unit rather than as a
    percentage: a percentage of 25 °C and a percentage of pH 8.2 mean wildly
    different things, and this configuration is shared by every sensor type.
    """

    minimum: float | None
    maximum: float | None
    clear_margin: float

    def __post_init__(self) -> None:
        if self.clear_margin <= 0:
            raise ValueError("clear_margin must be > 0")
        if self.minimum is not None and self.maximum is not None:
            if self.minimum >= self.maximum:
                raise ValueError("minimum must be below maximum")
            # Mirrors the clear_zone_is_reachable CHECK. Kept here as well
            # because the engine can be handed thresholds by a test or a future
            # config path that never went through Postgres.
            if self.minimum + self.clear_margin >= self.maximum - self.clear_margin:
                raise ValueError(
                    "clear_margin is wider than half the band: no reading could ever clear"
                )

    @property
    def configured(self) -> bool:
        return self.minimum is not None or self.maximum is not None


@dataclass(frozen=True, slots=True)
class Transition:
    """A change of alert state for one bound. Not emitted when nothing changed."""

    bound: AlertBound
    state: AlertState
    value: float
    threshold: float


def evaluate(
    value: float, thresholds: Thresholds, open_bounds: frozenset[AlertBound]
) -> list[Transition]:
    """Decide what changed, given a reading and what is already in breach.

    Returns an empty list for the overwhelmingly common case: a reading inside
    the band with nothing open, or a reading still outside a bound that is
    already reported. Silence is the normal output.

    The two bounds are evaluated independently. A probe can be in breach of its
    minimum while its maximum is quiet, and a single "in alert" flag would make
    a tank that swung from too cold to too hot look like it never recovered.
    """
    transitions: list[Transition] = []

    if thresholds.minimum is not None:
        low_open = "min" in open_bounds
        if not low_open and value < thresholds.minimum:
            transitions.append(Transition("min", "breach", value, thresholds.minimum))
        elif low_open and value >= thresholds.minimum + thresholds.clear_margin:
            transitions.append(Transition("min", "clear", value, thresholds.minimum))

    if thresholds.maximum is not None:
        high_open = "max" in open_bounds
        if not high_open and value > thresholds.maximum:
            transitions.append(Transition("max", "breach", value, thresholds.maximum))
        elif high_open and value <= thresholds.maximum - thresholds.clear_margin:
            transitions.append(Transition("max", "clear", value, thresholds.maximum))

    return transitions


def should_evaluate(reading: SensorReading) -> bool:
    """Only a fresh, good reading is evidence about the tank.

    A faulted read means the probe failed, which is its own alert class — a
    dead sensor and a cold tank need different responses, and conflating them
    sends someone to adjust a heater when the actual fault is a broken wire.

    ``stale`` is excluded for the same reason and it is a deliberate widening of
    "not fault": a stale reading is a number from the past being re-presented,
    and raising an alert from it would report a condition that may have ended.
    Gating on ``ok`` also makes ``value`` non-None by the contract's own
    validator, so downstream code never has to re-check.
    """
    return reading.quality == "ok" and reading.value is not None


class AlertSupervisor:
    """Turns sensor readings into alert episodes on the spine and in Postgres.

    Ordering is deliberate: the episode is written to Postgres **before** the
    alert is published. Postgres is the system of record, and an alert nobody
    heard is recoverable — a client reconnecting reads ``GET /api/v1/alerts``
    and sees it. The reverse order gives clients an alert with no record behind
    it, which cannot be reconciled at all.
    """

    def __init__(
        self,
        store: AlertStore,
        publish: PublishAlert,
        *,
        source: str = "control-engine",
    ) -> None:
        self._store = store
        self._publish = publish
        self._source = source
        #: Bounds currently open, per device. Mirrors the open episodes in
        #: Postgres; reloaded from there at startup so a restart mid-breach
        #: neither re-raises nor forgets.
        self._open: dict[str, set[AlertBound]] = {}

    async def prime(self) -> None:
        """Load open episodes so a restart does not re-announce a live breach."""
        self._open = await self._load_open()
        if self._open:
            log.info(
                "resumed open alert episodes",
                extra={
                    "devices": sorted(self._open),
                    "count": sum(len(b) for b in self._open.values()),
                },
            )

    async def resync(self) -> None:
        """Re-read the open episodes so this mirror follows the record.

        The engine is not the only writer that closes an episode: the API
        closes one when the bound that raised it is removed from the band
        (``Store.set_thresholds``), because no reading could ever clear it
        once the evaluator stops looking at that bound. Left un-synced, this
        map would still call the bound open — a re-set band would then never
        re-raise a live breach, and the first in-range reading would try to
        clear a row that is already closed. Called on the threshold-refresh
        cadence, so the mirror is at most one refresh behind the record.

        The read is an await, and the sensor callback runs on another task,
        so an episode can be raised or cleared *while* the record is being
        read. Reconciled rather than replaced: whatever this mirror changed
        during the read is applied on top of what the record said, so a
        breach that began mid-read is not forgotten (a forgotten one would be
        re-raised into the partial unique index and lost).
        """
        before = {device: set(bounds) for device, bounds in self._open.items()}
        fresh = await self._load_open()
        after = self._open
        for device in set(before) | set(after):
            raised = after.get(device, set()) - before.get(device, set())
            cleared = before.get(device, set()) - after.get(device, set())
            if raised:
                fresh.setdefault(device, set()).update(raised)
            if cleared and device in fresh:
                fresh[device].difference_update(cleared)
        # on_reading leaves an empty set behind for every device it has seen;
        # those are not open episodes and must not read as a difference.
        held = {device: bounds for device, bounds in after.items() if bounds}
        result = {device: bounds for device, bounds in fresh.items() if bounds}
        if result != held:
            log.info(
                "open alert episodes resynced from the record",
                extra={
                    "before": {d: sorted(b) for d, b in sorted(held.items())},
                    "after": {d: sorted(b) for d, b in sorted(result.items())},
                },
            )
        self._open = result

    async def _load_open(self) -> dict[str, set[AlertBound]]:
        known: set[str] = {"min", "max"}
        loaded: dict[str, set[AlertBound]] = {}
        for device_id, bounds in (await self._store.open_bounds()).items():
            for bound in bounds:
                if bound not in known:
                    # Unreachable through the CHECK constraint, but a schema this
                    # service does not own could drift. Loud beats coerced.
                    log.error(
                        "ignoring unknown alert bound",
                        extra={"device_id": device_id, "bound": bound},
                    )
                    continue
                loaded.setdefault(device_id, set()).add(bound)  # type: ignore[arg-type]
        return loaded

    async def on_reading(self, reading: SensorReading, thresholds: Thresholds) -> list[SensorAlert]:
        """Evaluate one reading. Returns the alerts published, for tests."""
        if not should_evaluate(reading) or not thresholds.configured:
            return []
        value = reading.value
        assert value is not None  # guaranteed by should_evaluate + the contract

        open_bounds = self._open.setdefault(reading.sensor_id, set())
        transitions = evaluate(value, thresholds, frozenset(open_bounds))

        published: list[SensorAlert] = []
        for change in transitions:
            alert = SensorAlert(
                message_id=uuid4(),
                emitted_at=datetime.now(UTC),
                source=self._source,
                device_id=reading.sensor_id,
                sensor_type=reading.sensor_type,
                state=change.state,
                bound=change.bound,
                value=change.value,
                threshold=change.threshold,
                clear_margin=thresholds.clear_margin,
                unit=reading.unit,
            )

            if change.state == "breach":
                await self._store.raise_episode(alert)
                open_bounds.add(change.bound)
            else:
                await self._store.clear_episode(alert)
                open_bounds.discard(change.bound)

            await self._publish(subjects.alert(reading.sensor_id), alert)
            published.append(alert)
            log.warning(
                "alert %s",
                change.state,
                extra={
                    "device_id": reading.sensor_id,
                    "bound": change.bound,
                    "value": change.value,
                    "threshold": change.threshold,
                },
            )

        return published


# --------------------------------------------------------------- silence class

#: How many missed polls make a probe silent, and the shortest deadline we will
#: ever apply.
#:
#: Constants, not configuration. A per-device knob here is a way to turn the
#: dead-probe alarm off by accident, and the failure it detects is the one that
#: hid a ten-hour outage behind a stale number that looked like a healthy tank.
#:
#: Six is margin: a single skipped read on a serialised 1-Wire bus is ordinary,
#: six in a row is not. The floor exists because six cadences of a fast probe is
#: no time at all — the DS18B20 publishes about every 5.6s, so 6x is 33.6s, and
#: a probe polling once a second would otherwise be declared dead after six
#: seconds of perfectly normal scheduling jitter.
SILENCE_CADENCE_MULTIPLE: Final = 6
SILENCE_FLOOR_S: Final = 30.0


def silence_deadline_s(cadence_s: float) -> float:
    """How long this probe may be quiet before it counts as silent."""
    return max(cadence_s * SILENCE_CADENCE_MULTIPLE, SILENCE_FLOOR_S)


class SilenceStore(Protocol):
    """The persistence a silence watcher needs. Mirrors :class:`AlertStore`."""

    async def cadences(self) -> Mapping[str, float]: ...
    async def open_silences(self) -> frozenset[str]: ...
    async def raise_silence(
        self,
        device_id: str,
        sensor_type: str,
        *,
        at: datetime,
        last_reading_at: datetime | None,
    ) -> None: ...
    async def clear_silence(self, device_id: str, *, at: datetime, value: float) -> None: ...


class SilenceWatcher:
    """Raises an alert when a probe stops reporting, and clears it when it returns.

    Every other alert in this system is a statement about a number. This one is
    a statement about the absence of numbers, which is why it needs its own
    class, its own message type, and a clock rather than a reading to fire it.

    The clock part is the important design point. A threshold evaluator is
    driven by readings, so it can only ever react to something that arrived —
    which means a probe that stops arriving is invisible to it forever. This is
    driven by :meth:`sweep` on a timer instead, and readings only ever *cancel*
    it.
    """

    def __init__(self, store: SilenceStore, publish: PublishSilence) -> None:
        self._store = store
        self._publish = publish
        self._last_seen: dict[str, datetime] = {}
        self._sensor_types: dict[str, str] = {}
        self._silent: set[str] = set()

    async def prime(self) -> None:
        """Resume silences already open in Postgres.

        Without this a restart would find an empty set, try to raise a second
        episode for a probe already recorded as silent, and lose it to the
        partial unique index — the alert vanishes rather than duplicating,
        which is the worse of the two failures.
        """
        self._silent = set(await self._store.open_silences())
        if self._silent:
            log.info("resumed open silence episodes", extra={"devices": sorted(self._silent)})

    def is_silent(self, device_id: str) -> bool:
        """Whether threshold evaluation should be suspended for this probe.

        The last number a dead probe published is not evidence about now.
        Evaluating it would either invent a recovery or keep asserting a breach
        nobody can confirm.
        """
        return device_id in self._silent

    async def on_reading(self, reading: SensorReading, *, now: datetime | None = None) -> None:
        """Note that a probe is alive, and clear any silence it was under.

        Gated on ``should_evaluate`` rather than mere arrival. A DS18B20 with a
        bad CRC publishes a fault: that proves the wire is alive and proves
        nothing about the tank, so it must not silence the alarm that says we do
        not know the temperature.
        """
        if not should_evaluate(reading):
            return
        value = reading.value
        assert value is not None  # guaranteed by should_evaluate

        at = now or datetime.now(UTC)
        self._last_seen[reading.sensor_id] = at
        self._sensor_types[reading.sensor_id] = reading.sensor_type

        if reading.sensor_id not in self._silent:
            return

        silent_for = 0.0
        await self._store.clear_silence(reading.sensor_id, at=at, value=value)
        self._silent.discard(reading.sensor_id)
        cadence = (await self._store.cadences()).get(reading.sensor_id)
        await self._emit(
            reading.sensor_id,
            reading.sensor_type,
            state="clear",
            silent_for_s=silent_for,
            threshold_s=silence_deadline_s(cadence) if cadence else SILENCE_FLOOR_S,
            last_reading_at=at,
            at=at,
        )
        log.warning("probe reporting again", extra={"device_id": reading.sensor_id})

    async def sweep(self, *, now: datetime | None = None) -> None:
        """Check every probe against its own deadline. Driven by a timer."""
        at = now or datetime.now(UTC)
        cadences = await self._store.cadences()

        for device_id, cadence in cadences.items():
            if device_id in self._silent:
                continue
            last = self._last_seen.get(device_id)
            if last is None:
                # Never seen since this process started. Deliberately not an
                # alert: the engine may have started before hardware-io, and
                # accusing a probe of being dead because we have not been
                # listening long enough is a false alarm on every boot.
                continue

            deadline = silence_deadline_s(cadence)
            silent_for = (at - last).total_seconds()
            if silent_for < deadline:
                continue

            sensor_type = self._sensor_types.get(device_id, "unknown")
            await self._store.raise_silence(device_id, sensor_type, at=at, last_reading_at=last)
            self._silent.add(device_id)
            await self._emit(
                device_id,
                sensor_type,
                state="breach",
                silent_for_s=silent_for,
                threshold_s=deadline,
                last_reading_at=last,
                at=at,
            )
            log.critical(
                "probe has stopped reporting; the tank is not being monitored",
                extra={
                    "device_id": device_id,
                    "silent_for_s": round(silent_for, 1),
                    "deadline_s": deadline,
                },
            )

    async def _emit(
        self,
        device_id: str,
        sensor_type: str,
        *,
        state: AlertState,
        silent_for_s: float,
        threshold_s: float,
        last_reading_at: datetime | None,
        at: datetime,
    ) -> None:
        await self._publish(
            subjects.silence(device_id),
            SensorSilence(
                message_id=uuid4(),
                emitted_at=at,
                source="control-engine",
                device_id=device_id,
                sensor_type=sensor_type,
                state=state,
                silent_for_s=silent_for_s,
                silence_threshold_s=threshold_s,
                last_reading_at=last_reading_at,
            ),
        )
