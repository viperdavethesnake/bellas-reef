# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Turns registration announcements into rows in ``devices``.

hardware-io announces on ``bellasreef.registry.>`` and never touches Postgres.
This is the other half: the API owns the database, so the API is what listens
and writes. The direction matters — it is what lets a phase-2 ESP32 spoke join
by publishing a registration, with no notion that a database exists.

The stream is retained last-value-per-subject, so a consumer starting at any
time sees the current registration for every device rather than only whatever
happens to be announced next.
"""

from __future__ import annotations

import contextlib
from typing import Any

import nats
from bellasreef_contracts import SensorRegistration, subjects
from bellasreef_service import get_logger
from nats.aio.client import Client
from nats.aio.msg import Msg
from nats.js.api import ConsumerConfig, DeliverPolicy
from pydantic import ValidationError

from bellasreef_api.store import Store

log = get_logger(__name__)


class RegistryConsumer:
    """Subscribes to registrations and upserts the devices they describe."""

    def __init__(self, url: str, store: Store) -> None:
        self._url = url
        self._store = store
        self._nc: Client | None = None

    async def start(self) -> None:
        self._nc = await nats.connect(self._url)
        js = self._nc.jetstream()
        # LAST_PER_SUBJECT: replay the current registration for every device,
        # then follow along. A plain subscription would only see hardware that
        # happened to announce after this process started, which in practice
        # means "none of it" — hardware-io announces once, at its own startup.
        await js.subscribe(
            subjects.ALL_REGISTRY,
            cb=self._on_message,
            config=ConsumerConfig(deliver_policy=DeliverPolicy.LAST_PER_SUBJECT),
        )
        log.info("registry consumer subscribed", extra={"subject": subjects.ALL_REGISTRY})

    async def close(self) -> None:
        if self._nc is not None:
            with contextlib.suppress(Exception):
                await self._nc.close()
            self._nc = None

    async def _on_message(self, msg: Msg) -> None:
        # Actuator registrations share this subject family. Only sensors are
        # upserted here for now; an actuator carries a safety contract that
        # deserves its own handling rather than a best-effort row.
        try:
            registration = SensorRegistration.model_validate_json(msg.data)
        except ValidationError:
            log.debug(
                "registry message is not a sensor registration; ignored",
                extra={"subject": msg.subject},
            )
            with contextlib.suppress(Exception):
                await msg.ack()
            return

        try:
            created = await self._store.upsert_sensor(
                device_id=registration.sensor_id,
                driver_id=registration.driver_id,
                sensor_type=registration.sensor_type,
                poll_interval_s=registration.poll_interval_s,
            )
        except Exception:  # broad by design: a bad row must not kill the consumer
            log.exception("could not upsert a registration", extra={"subject": msg.subject})
        else:
            if created:
                log.info("device registered", extra={"device_id": registration.sensor_id})

        with contextlib.suppress(Exception):
            await msg.ack()


def build_registry_consumer(url: str | None, store: Store) -> Any:
    return RegistryConsumer(url, store) if url else None
