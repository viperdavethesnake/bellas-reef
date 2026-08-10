# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Persistence for alert episodes (PRD R12).

An episode spans two events. Raising inserts the row; clearing stamps it. There
is deliberately no update path that reopens a cleared episode — a tank that goes
back out of range gets a new row, so the history reads as a sequence of distinct
incidents rather than one row that flickered.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from bellasreef_db.models import AlertEpisode, Device

__all__ = ["AlertLike", "AlertRecord", "AlertStoreError", "PostgresAlertStore"]


class AlertLike(Protocol):
    """The shape of a wire alert, without importing the wire.

    ``bellasreef-db`` deliberately does not depend on ``bellasreef-contracts``:
    the schema and the wire agree on these fields today, and a direct import
    would make every contract bump a database-package bump. Structural typing
    gets the type checking without the coupling.

    Read-only properties rather than bare annotations, because the contract
    models are frozen and a mutable-attribute protocol would not match them.
    """

    @property
    def device_id(self) -> str: ...
    @property
    def sensor_type(self) -> str: ...
    @property
    def bound(self) -> str: ...
    @property
    def threshold(self) -> float: ...
    @property
    def clear_margin(self) -> float: ...
    @property
    def unit(self) -> str: ...
    @property
    def emitted_at(self) -> datetime: ...
    @property
    def value(self) -> float: ...


class AlertStoreError(RuntimeError):
    """Raised when an episode cannot be opened or closed as instructed."""


@dataclass(frozen=True, slots=True)
class AlertRecord:
    """One episode, as the API renders it."""

    id: uuid.UUID
    device_id: str
    sensor_type: str
    bound: str
    threshold: float
    clear_margin: float
    unit: str
    raised_at: datetime
    raised_value: float
    cleared_at: datetime | None
    cleared_value: float | None

    @property
    def active(self) -> bool:
        return self.cleared_at is None


def _record(row: AlertEpisode) -> AlertRecord:
    return AlertRecord(
        id=row.id,
        device_id=row.device_id,
        sensor_type=row.sensor_type,
        bound=row.bound,
        threshold=row.threshold,
        clear_margin=row.clear_margin,
        unit=row.unit,
        raised_at=row.raised_at,
        raised_value=row.raised_value,
        cleared_at=row.cleared_at,
        cleared_value=row.cleared_value,
    )


class PostgresAlertStore:
    """Implements the control engine's ``AlertStore`` protocol, plus API reads."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    # ------------------------------------------------------- engine side

    async def open_bounds(self) -> Mapping[str, frozenset[str]]:
        """Which bounds are currently in breach, per device.

        Read at engine startup. Without it a restart during a breach would find
        an empty in-memory map, decide the next reading is a fresh breach, and
        collide with the partial unique index — the alert would be lost rather
        than duplicated, which is the worse of the two failures.
        """
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(AlertEpisode.device_id, AlertEpisode.bound).where(
                        AlertEpisode.cleared_at.is_(None)
                    )
                )
            ).all()

        grouped: dict[str, set[str]] = {}
        for device_id, bound in rows:
            grouped.setdefault(device_id, set()).add(bound)
        return {device_id: frozenset(bounds) for device_id, bounds in grouped.items()}

    async def raise_episode(self, alert: AlertLike) -> None:
        """Open an episode from a wire alert."""
        async with self._sessions() as session, session.begin():
            session.add(
                AlertEpisode(
                    id=uuid.uuid4(),
                    device_id=alert.device_id,
                    sensor_type=alert.sensor_type,
                    bound=alert.bound,
                    threshold=alert.threshold,
                    clear_margin=alert.clear_margin,
                    unit=alert.unit,
                    raised_at=alert.emitted_at,
                    raised_value=alert.value,
                )
            )

    async def clear_episode(self, alert: AlertLike) -> None:
        """Stamp the open episode for this device and bound.

        Scoped by ``cleared_at IS NULL`` so a duplicate clear cannot rewrite the
        closing reading of an episode that already ended.
        """
        async with self._sessions() as session, session.begin():
            # `.returning(id)` rather than `rowcount`: on an async engine the
            # generic Result carries no row count, and counting the returned ids
            # is both typed and exact.
            closed = (
                (
                    await session.execute(
                        update(AlertEpisode)
                        .where(
                            AlertEpisode.device_id == alert.device_id,
                            AlertEpisode.bound == alert.bound,
                            AlertEpisode.cleared_at.is_(None),
                        )
                        .values(
                            cleared_at=alert.emitted_at,
                            cleared_value=alert.value,
                        )
                        .returning(AlertEpisode.id)
                    )
                )
                .scalars()
                .all()
            )
            if not closed:
                raise AlertStoreError(
                    f"no open episode for {alert.device_id!r}/{alert.bound!r} to clear"
                )

    async def thresholds(self) -> Mapping[str, tuple[float | None, float | None, float]]:
        """Configured bands, keyed by device id: ``(min, max, clear_margin)``.

        Only devices with at least one bound set are returned, so the engine's
        hot path is a dict lookup that misses for every unconfigured sensor
        rather than a row per reading.

        The margin is non-null by the ``thresholds_require_clear_margin``
        constraint whenever a bound is set, so the ``or 0.0`` below is
        unreachable in practice — it exists because the column is nullable and
        the type checker is right to insist.
        """
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(
                        Device.device_id,
                        Device.alert_min,
                        Device.alert_max,
                        Device.alert_clear_margin,
                    ).where(
                        Device.kind == "sensor",
                        or_(Device.alert_min.is_not(None), Device.alert_max.is_not(None)),
                    )
                )
            ).all()
        return {device_id: (low, high, margin or 0.0) for device_id, low, high, margin in rows}

    # ---------------------------------------------------------- API side

    async def active(self) -> Sequence[AlertRecord]:
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(AlertEpisode)
                    .where(AlertEpisode.cleared_at.is_(None))
                    .order_by(AlertEpisode.raised_at.desc())
                )
            ).scalars()
        return [_record(row) for row in rows]

    async def recent(self, limit: int = 50) -> Sequence[AlertRecord]:
        """Most recently raised episodes, cleared or not.

        Ordered by ``raised_at`` rather than ``cleared_at``: an operator asking
        "what has been going on" is asking when things started, and sorting by
        clear time buries an ongoing breach below older resolved ones.
        """
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(AlertEpisode).order_by(AlertEpisode.raised_at.desc()).limit(limit)
                )
            ).scalars()
        return [_record(row) for row in rows]
