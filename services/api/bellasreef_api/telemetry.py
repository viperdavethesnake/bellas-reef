# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Telemetry into VictoriaMetrics — docs/device-classes.md §4.

What the spine published, not what a gauge happened to read.

The alternative was scraping ``hardware-io:9101/metrics``, and it is wrong twice
over. It samples a Prometheus gauge at *scrape* cadence rather than recording
each value the spine actually carried, so a probe that reported three times
between scrapes contributes one point and the other two never existed. And it
cannot carry the authority labels at all: hardware-io only ever emits
authoritative devices (§3), so a scrape of it is structurally incapable of
describing an advisory series.

**Series identity is the label set.** §4 is explicit that ``control_authority``
has to be present on the first sample ever written, because adding it later
forks every series — history under one identity, new data under another, and
every range query that straddles the change is wrong or needs a rewrite rule
forever. That is why this ships after migration 0008 and not before.

Two deliberate departures from a literal reading of §4, both flagged in the
session log:

* The **age of the last successful exchange is a metric, not a label.** A label
  whose value changes every sample mints a new series every sample; the age also
  has to be *charted* to show "when we stopped knowing", and a label cannot be
  charted. ``command_acked`` stays a label — it is a boolean and its cardinality
  is two.
* ``ActuatorState`` carries no acknowledgement or exchange-age field today, so
  those two are emitted only when a producer supplies them. Nothing can supply
  them yet: there are no advisory devices, because there is no vendor-bridge
  (§8 D3, undecided). The plumbing is here so the first advisory series is
  correctly shaped rather than retrofitted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import datetime
from typing import Any

import httpx
import nats
from bellasreef_contracts import ActuatorState, PwmLevel, SensorAlert, SensorReading, subjects
from bellasreef_service import get_logger
from nats.aio.client import Client
from nats.aio.msg import Msg
from nats.js.api import ConsumerConfig, DeliverPolicy, RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import NotFoundError
from pydantic import ValidationError

from bellasreef_api.store import Store

log = get_logger(__name__)

__all__ = ["TELEMETRY_STREAM", "TelemetryWriter"]

TELEMETRY_STREAM = "BR_TELEMETRY"

#: Sensor readings and alerts are published on core pub/sub so that a consumer
#: coming back online is never handed a burst of stale measurements to act on.
#: That is right for *control* and wrong for *history*: a writer that misses
#: everything published while it was restarting produces a chart with holes and
#: no indication that the holes are the writer's fault rather than the tank's.
#:
#: A stream over the same subjects gives this one consumer durability without
#: changing anything for the core subscribers — JetStream stores messages that
#: match a stream's subjects however they were published, and live subscribers
#: are unaffected.
_TELEMETRY_STREAM_CONFIG = StreamConfig(
    name=TELEMETRY_STREAM,
    subjects=[subjects.ALL_SENSORS, subjects.ALL_ALERTS],
    retention=RetentionPolicy.LIMITS,
    storage=StorageType.FILE,
    # A buffer, not an archive. VictoriaMetrics is the system of record for
    # history; this only has to survive a writer restart.
    max_age=86_400.0,
)


def _millis(when: datetime) -> int:
    """VictoriaMetrics timestamps are milliseconds since the epoch."""
    return int(when.timestamp() * 1000)


def _line(name: str, labels: dict[str, str], value: float, at: datetime) -> str:
    """One `/api/v1/import` JSON line.

    Labels with an empty value are dropped rather than written blank: in
    VictoriaMetrics an empty label and an absent label are the same thing, and
    writing one explicitly only invites the reader to think it means something.
    """
    metric = {"__name__": name, **{k: v for k, v in labels.items() if v}}
    return json.dumps({"metric": metric, "values": [value], "timestamps": [_millis(at)]})


class TelemetryWriter:
    """Durable consumer → VictoriaMetrics.

    Placed in the API service because it is the process that already owns
    Postgres — the authority label has to be looked up per device — and already
    runs a JetStream consumer. It is explicitly *not* in hardware-io: §3 keeps
    that process Postgres-free and small, and an HTTP client with retry state
    does not belong in the one thing allowed to drive a heater.
    """

    RETRY_S = 5.0
    #: How long a looked-up authority is trusted before re-reading. Authority
    #: changes only by re-registration, so this is about picking up a new device
    #: promptly, not about staleness of a fast-moving value.
    AUTHORITY_TTL_S = 30.0

    def __init__(self, nats_url: str, vm_url: str, store: Store) -> None:
        self._nats_url = nats_url
        self._vm_url = vm_url.rstrip("/")
        self._store = store
        self._nc: Client | None = None
        self._task: asyncio.Task[None] | None = None
        self._http: httpx.AsyncClient | None = None
        self._authority: dict[str, tuple[str, float]] = {}

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._http = httpx.AsyncClient(timeout=10.0)
        self._task = asyncio.create_task(self._subscribe_forever())

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
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _subscribe_forever(self) -> None:
        while True:
            try:
                await self._subscribe()
                return
            except asyncio.CancelledError:
                raise
            except NotFoundError:
                log.info(
                    "state stream not provisioned yet; waiting", extra={"retry_in_s": self.RETRY_S}
                )
            except Exception:
                log.exception("telemetry writer could not subscribe; retrying")
            await asyncio.sleep(self.RETRY_S)

    async def _subscribe(self) -> None:
        if self._nc is None or not self._nc.is_connected:
            self._nc = await nats.connect(self._nats_url)
        js = self._nc.jetstream()

        # This writer owns its own stream: it is the only consumer, and the
        # subjects overlap nothing hardware-io provisions. NATS refuses two
        # streams over the same subject, which is why actuator state is read
        # from BR_STATE below rather than folded in here.
        try:
            await js.add_stream(_TELEMETRY_STREAM_CONFIG)
            log.info("stream created", extra={"stream": TELEMETRY_STREAM})
        except Exception:
            await js.update_stream(_TELEMETRY_STREAM_CONFIG)

        await js.subscribe(
            subjects.ALL_SENSORS,
            durable="telemetry-sensors",
            cb=self._on_sensor,
            config=ConsumerConfig(deliver_policy=DeliverPolicy.NEW),
        )
        await js.subscribe(
            subjects.ALL_ALERTS,
            durable="telemetry-alerts",
            cb=self._on_alert,
            config=ConsumerConfig(deliver_policy=DeliverPolicy.NEW),
        )
        # BR_STATE is retained last-value-per-subject, so a restart cannot
        # recover intermediate transitions that happened while this was down.
        # Recorded here rather than papered over: the gap is in the stream's
        # retention policy, not in this consumer.
        await js.subscribe(
            subjects.ALL_STATE,
            durable="telemetry-state",
            cb=self._on_state,
            config=ConsumerConfig(deliver_policy=DeliverPolicy.NEW),
        )
        log.info("telemetry writer subscribed", extra={"vm": self._vm_url})

    # ------------------------------------------------------------- authority

    async def authority_of(self, device_id: str) -> str:
        """The label value for this device's control authority.

        Three outcomes, and the difference between them matters enough to be in
        the series identity:

        ``authoritative`` / ``advisory`` / ``observe_only``
            what the device declared.
        ``not_applicable``
            the hub knows the device and it carries no authority — every sensor,
            by §2. An alert on a probe is not an episode of unknown provenance;
            the question simply does not apply to it.
        ``unknown``
            the hub has no row for this device. Never silently promoted to
            ``authoritative``: a wrong guess writes a claim into history as
            though it were a measurement, and the label *is* the series, so it
            cannot be corrected in place afterwards.
        """
        cached = self._authority.get(device_id)
        now = time.monotonic()
        if cached is not None and now - cached[1] < self.AUTHORITY_TTL_S:
            return cached[0]
        try:
            known, declared = await self._store.control_authority_of(device_id)
        except Exception:
            log.exception("could not read control_authority", extra={"device_id": device_id})
            return "unknown"
        authority = declared or ("not_applicable" if known else "unknown")
        self._authority[device_id] = (authority, now)
        return authority

    # -------------------------------------------------------------- handlers

    async def _on_sensor(self, msg: Msg) -> None:
        await self._handle(msg, self._sensor_lines)

    async def _on_state(self, msg: Msg) -> None:
        await self._handle(msg, self._state_lines)

    async def _on_alert(self, msg: Msg) -> None:
        await self._handle(msg, self._alert_lines)

    async def _handle(self, msg: Msg, build: Any) -> None:
        try:
            lines = await build(msg.data)
        except ValidationError:
            # A payload that does not satisfy the contract is a producer bug.
            # Acked so it does not redeliver forever; it will never parse.
            log.warning("undecodable telemetry payload; dropped", extra={"subject": msg.subject})
            with contextlib.suppress(Exception):
                await msg.ack()
            return
        except Exception:
            log.exception("could not build telemetry", extra={"subject": msg.subject})
            with contextlib.suppress(Exception):
                await msg.ack()
            return

        if not lines:
            with contextlib.suppress(Exception):
                await msg.ack()
            return

        try:
            await self._push(lines)
        except Exception:
            # NOT acked: the write failed, so the message must be redelivered.
            # Acking here is how a telemetry pipeline silently loses data while
            # reporting itself healthy.
            log.exception("VictoriaMetrics write failed; leaving unacked")
            return

        with contextlib.suppress(Exception):
            await msg.ack()

    async def _sensor_lines(self, payload: bytes) -> list[str]:
        reading = SensorReading.model_validate_json(payload)
        if reading.value is None:
            # A faulted read has no number. Recording the fault as a value would
            # put a fabricated point on the chart; recording quality separately
            # keeps "the probe failed" visible without inventing a temperature.
            return [
                _line(
                    "bellasreef_sensor_fault",
                    {"device_id": reading.sensor_id, "sensor_type": reading.sensor_type},
                    1.0,
                    reading.emitted_at,
                )
            ]
        return [
            _line(
                "bellasreef_sensor_reading",
                {
                    "device_id": reading.sensor_id,
                    "sensor_type": reading.sensor_type,
                    "unit": reading.unit,
                    "quality": reading.quality,
                },
                reading.value,
                reading.emitted_at,
            )
        ]

    async def _state_lines(self, payload: bytes) -> list[str]:
        state = ActuatorState.model_validate_json(payload)
        authority = await self.authority_of(state.actuator_id)
        level = state.level
        value = level.duty if isinstance(level, PwmLevel) else float(level.on)

        labels = {
            "device_id": state.actuator_id,
            "actuator_class": level.kind,
            "control_authority": authority,
            "reason": state.reason,
        }
        # §4: advisory series say whether the command was acknowledged. A
        # boolean is a sound label; the exchange *age* is not, and is emitted
        # as its own series below.
        acked = getattr(state, "command_acked", None)
        if authority == "advisory" and acked is not None:
            labels["command_acked"] = "true" if acked else "false"

        lines = [_line("bellasreef_actuator_level", labels, value, state.emitted_at)]

        age = getattr(state, "last_exchange_age_s", None)
        if authority == "advisory" and age is not None:
            lines.append(
                _line(
                    "bellasreef_actuator_last_exchange_age_seconds",
                    {"device_id": state.actuator_id, "control_authority": authority},
                    float(age),
                    state.emitted_at,
                )
            )
        return lines

    async def _alert_lines(self, payload: bytes) -> list[str]:
        alert = SensorAlert.model_validate_json(payload)
        # §4: an episode carries the authority of the device that produced it.
        # Today every alert is a threshold breach on a *sensor*, and §2 confines
        # the authority axis to actuators, so this resolves to
        # ``not_applicable`` for every episode we can currently produce. §4's
        # intent — telling a fail-safe-backed episode from one that is not —
        # needs either sensors to carry an authority of their own or this label
        # to be transport-based. Flagged, not resolved here.
        authority = await self.authority_of(alert.device_id)
        return [
            _line(
                "bellasreef_alert_state",
                {
                    "device_id": alert.device_id,
                    "sensor_type": alert.sensor_type,
                    "bound": alert.bound,
                    "control_authority": authority,
                },
                1.0 if alert.state == "breach" else 0.0,
                alert.emitted_at,
            )
        ]

    # ------------------------------------------------------------------ push

    async def _push(self, lines: list[str]) -> None:
        if self._http is None:
            raise RuntimeError("telemetry writer not started")
        response = await self._http.post(
            f"{self._vm_url}/api/v1/import",
            content="\n".join(lines).encode(),
            headers={"Content-Type": "application/x-ndjson"},
        )
        response.raise_for_status()
