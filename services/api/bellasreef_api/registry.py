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

import asyncio
import contextlib
from typing import Any

import nats
from bellasreef_contracts import CapabilityAnnouncement, SensorRegistration, subjects
from bellasreef_service import get_logger
from nats.aio.client import Client
from nats.aio.msg import Msg
from nats.js.api import ConsumerConfig, DeliverPolicy
from nats.js.errors import NotFoundError
from pydantic import ValidationError

from bellasreef_api.store import Store

log = get_logger(__name__)


class CapabilityConsumer:
    """Subscribes to capability announcements and stores what the hub can offer.

    Separate from :class:`RegistryConsumer` because the two tiers are separate.
    A registration says "this device exists and here is its safety contract"; an
    announcement says "this hardware could be bound to something". Merging them
    would put a device row in the database for every unclaimed PWM channel,
    which is the conflation the two-tier registry exists to undo.
    """

    RETRY_S = 5.0

    def __init__(self, url: str, store: Store) -> None:
        self._url = url
        self._store = store
        self._nc: Client | None = None
        self._task: asyncio.Task[None] | None = None
        self._subscribed = False

    @property
    def is_running(self) -> bool:
        return self._subscribed and self._nc is not None and self._nc.is_connected

    async def start(self) -> None:
        self._task = asyncio.create_task(self._subscribe_forever())

    async def _subscribe_forever(self) -> None:
        while True:
            try:
                await self._subscribe()
                return
            except asyncio.CancelledError:
                raise
            except NotFoundError:
                log.info(
                    "capability stream not provisioned yet; waiting",
                    extra={"retry_in_s": self.RETRY_S},
                )
            except Exception:
                log.exception("capability consumer could not subscribe; retrying")
            await asyncio.sleep(self.RETRY_S)

    async def _subscribe(self) -> None:
        if self._nc is None or not self._nc.is_connected:
            self._nc = await nats.connect(self._url)
        js = self._nc.jetstream()
        # LAST_PER_SUBJECT for the same reason as the registry: hardware-io
        # announces once, at its own startup, so a plain subscription would see
        # nothing on an API that restarted afterwards.
        await js.subscribe(
            subjects.ALL_CAPABILITIES,
            cb=self._on_message,
            config=ConsumerConfig(deliver_policy=DeliverPolicy.LAST_PER_SUBJECT),
        )
        self._subscribed = True
        log.info("capability consumer subscribed", extra={"subject": subjects.ALL_CAPABILITIES})

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._nc is not None:
            with contextlib.suppress(Exception):
                await self._nc.close()
            self._nc = None

    async def _on_message(self, msg: Msg) -> None:
        try:
            announcement = CapabilityAnnouncement.model_validate_json(msg.data)
        except ValidationError:
            log.warning(
                "capability message did not validate; ignored",
                extra={"subject": msg.subject},
            )
            with contextlib.suppress(Exception):
                await msg.ack()
            return

        try:
            await self._store.replace_capabilities(
                announcement.hardware_source,
                [(c.channel, dict(c.detail)) for c in announcement.channels],
            )
            log.info(
                "capabilities stored",
                extra={
                    "hardware_source": announcement.hardware_source,
                    "channels": len(announcement.channels),
                },
            )
        except Exception:  # broad by design: a bad row must not kill the consumer
            log.exception(
                "could not store capabilities",
                extra={"hardware_source": announcement.hardware_source},
            )
        with contextlib.suppress(Exception):
            await msg.ack()


class RegistryConsumer:
    """Subscribes to registrations and upserts the devices they describe."""

    #: How long to wait between attempts while the stream does not yet exist.
    RETRY_S = 5.0

    def __init__(self, url: str, store: Store) -> None:
        self._url = url
        self._store = store
        self._nc: Client | None = None
        self._task: asyncio.Task[None] | None = None
        self._subscribed = False

    @property
    def is_running(self) -> bool:
        """True once subscribed and still connected.

        Deliberately not "the setup task is alive". These are *push* consumers:
        the task exists only to establish the subscription and then returns, so
        a liveness check on the task reports False exactly when the component is
        working correctly. Work happens in NATS callbacks afterwards.

        The composed-service test caught that distinction — the first version of
        this property would have failed a healthy service, which is the mirror
        image of the bug it exists to prevent.
        """
        return self._subscribed and self._nc is not None and self._nc.is_connected

    async def start(self) -> None:
        """Begin subscribing, retrying until the stream exists.

        hardware-io provisions the streams, and nothing orders the two services
        — under compose either can come up first, and on a bench either gets
        restarted alone. Subscribing once and giving up meant the API only ever
        saw registrations if it happened to start second, which is how the
        devices table stayed empty while hardware-io logged a clean announcement.
        """
        self._task = asyncio.create_task(self._subscribe_forever())

    async def _subscribe_forever(self) -> None:
        while True:
            try:
                await self._subscribe()
                return
            except asyncio.CancelledError:
                raise
            except NotFoundError:
                log.info(
                    "registry stream not provisioned yet; waiting",
                    extra={"retry_in_s": self.RETRY_S},
                )
            except Exception:
                log.exception("registry consumer could not subscribe; retrying")
            await asyncio.sleep(self.RETRY_S)

    async def _subscribe(self) -> None:
        if self._nc is None or not self._nc.is_connected:
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
        self._subscribed = True
        log.info("registry consumer subscribed", extra={"subject": subjects.ALL_REGISTRY})

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
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
                transport=registration.transport,
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
