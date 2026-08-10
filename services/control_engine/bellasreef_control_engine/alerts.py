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
from typing import Protocol
from uuid import uuid4

from bellasreef_contracts import AlertBound, AlertState, SensorAlert, SensorReading, subjects

log = logging.getLogger(__name__)

__all__ = [
    "AlertStore",
    "AlertSupervisor",
    "PublishAlert",
    "Thresholds",
    "Transition",
    "evaluate",
    "should_evaluate",
]

#: Publishes one alert to one subject. A callable rather than a broker handle so
#: the supervisor can be driven by a list in a test without a NATS server.
PublishAlert = Callable[[str, SensorAlert], Awaitable[None]]


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
        known: set[str] = {"min", "max"}
        resumed: dict[str, set[AlertBound]] = {}
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
                resumed.setdefault(device_id, set()).add(bound)  # type: ignore[arg-type]
        self._open = resumed
        if self._open:
            log.info(
                "resumed open alert episodes",
                extra={
                    "devices": sorted(self._open),
                    "count": sum(len(b) for b in self._open.values()),
                },
            )

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
