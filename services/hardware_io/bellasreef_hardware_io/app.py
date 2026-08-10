# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""hardware-io service entry point.

Session 3 scope: the process skeleton — logging, health, metrics, liveness and
shutdown — wired around the supervisor and the DS18B20 driver. NATS is the next
pass; until then this runs standalone and is exercised by the restart drill.

The loop below is the thing everything else is arranged around. It beats the
watchdog *from inside itself*, so if it stalls the beats stop. Beating from a
separate task would report health the loop no longer has.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from datetime import UTC, datetime
from typing import Final
from uuid import uuid4

from bellasreef_contracts import ActuatorRegistration, Heartbeat
from bellasreef_service.httpd import Health, MetricsServer
from bellasreef_service.logging import configure_logging, get_logger
from bellasreef_service.watchdog import LivenessGuard, SdNotifier, watchdog_interval_s
from prometheus_client import CollectorRegistry, Counter, Gauge

from bellasreef_hardware_io.drivers.onewire import DS18B20, discover_probes
from bellasreef_hardware_io.safety import InterlockSupervisor, SafetyEvent
from bellasreef_hardware_io.spine import CommandConsumer, Spine

__all__ = ["HardwareIO", "clock_is_trusted", "main"]

log = get_logger(__name__)

SERVICE: Final = "hardware-io"

#: Deliberately opt-in. A "hang the service" trigger that is always armed is a
#: liability in production; it exists for the restart drill and nothing else.
FREEZE_DRILL_ENV: Final = "BELLASREEF_ENABLE_FREEZE_DRILL"


def clock_is_trusted() -> bool:
    """Whether the host clock can be believed.

    This board has no RTC battery, so after a power cut the clock is wrong until
    chrony catches up. Anything time-driven must refuse to act until this is
    true — see the systemd unit's ``After=time-sync.target``.

    Falls back to ``BELLASREEF_ASSUME_CLOCK_TRUSTED`` where ``timedatectl`` is
    unreachable (inside a container). That override is explicit on purpose:
    silently assuming a good clock is exactly the failure being guarded against.
    """
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return os.environ.get("BELLASREEF_ASSUME_CLOCK_TRUSTED") == "1"

    if out.returncode != 0:
        return os.environ.get("BELLASREEF_ASSUME_CLOCK_TRUSTED") == "1"
    return out.stdout.strip().lower() == "yes"


class _Metrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.loop_beats = Counter(
            "bellasreef_loop_beats_total", "Supervisor loop iterations", registry=registry
        )
        self.loop_stall = Gauge(
            "bellasreef_loop_stall_seconds",
            "Seconds since the supervisor loop last beat",
            registry=registry,
        )
        self.clock_trusted = Gauge(
            "bellasreef_clock_trusted",
            "1 when the host clock is NTP-synchronised",
            registry=registry,
        )
        self.latched = Gauge(
            "bellasreef_actuator_latched",
            "1 when an actuator is latched off by an interlock",
            ["actuator_id"],
            registry=registry,
        )
        self.sensor_reads = Counter(
            "bellasreef_sensor_reads_total",
            "Sensor reads by outcome",
            ["sensor_id", "quality"],
            registry=registry,
        )
        self.sensor_value = Gauge(
            "bellasreef_sensor_value",
            "Most recent good sensor reading",
            ["sensor_id", "unit"],
            registry=registry,
        )
        self.safety_events = Counter(
            "bellasreef_safety_events_total",
            "Safety events by reason",
            ["reason"],
            registry=registry,
        )


class HardwareIO:
    """The service.

    Owns the supervisor, the sensor drivers, and the loop that keeps both
    honest.
    """

    def __init__(
        self,
        *,
        loop_interval_s: float = 1.0,
        liveness_timeout_s: float = 15.0,
        metrics_port: int = 9101,
    ) -> None:
        self.registry = CollectorRegistry()
        self.metrics = _Metrics(self.registry)

        self._loop_interval_s = loop_interval_s
        self._clock_trusted = clock_is_trusted()

        self.supervisor = InterlockSupervisor(
            on_event=self._on_safety_event, clock_trusted=self._clock_trusted
        )
        self.sensors: list[DS18B20] = []

        self.notifier = SdNotifier()
        self.liveness = LivenessGuard(timeout_s=liveness_timeout_s)
        self.httpd = MetricsServer(probe=self.health, registry=self.registry, port=metrics_port)

        self._liveness_timeout_s = liveness_timeout_s
        self._stopping = asyncio.Event()
        self._frozen = False
        self._drill_actuator: object | None = None
        self.spine: Spine | None = None
        self.commands: CommandConsumer | None = None
        self._registrations: list[ActuatorRegistration] = []
        self._beat_seq = 0
        self._sensor_deadlines: dict[str, float] = {}

    # ------------------------------------------------------------- lifecycle

    def discover(self) -> None:
        """Attach every DS18B20 the kernel has enumerated."""
        for probe in discover_probes():
            driver = DS18B20(probe)
            self.sensors.append(driver)
            log.info(
                "sensor attached",
                extra={"driver_id": driver.driver_id, "sensor_type": driver.sensor_type},
            )

    def register_drill_actuator(self) -> None:
        """Attach a fake actuator so the restart drill has something to assert.

        Real actuator drivers arrive with the PCA9685 in session 4. Without at
        least one registered actuator, "the restart re-ran the startup
        safe-state assertion" would be a claim about an empty loop — technically
        true and worth nothing.

        Gated behind the same drill flag as the freeze trigger, so this never
        exists in a production process.
        """
        from datetime import UTC, datetime
        from uuid import uuid4

        from bellasreef_contracts import ActuatorRegistration, BinaryLevel

        from bellasreef_hardware_io.fakes import FakeActuator

        off = BinaryLevel(on=False)
        actuator = FakeActuator("drill-dummy", off)
        # Come up energised, so the startup assertion has real work to do and a
        # no-op would be visible as a failure.
        actuator.level = BinaryLevel(on=True)

        self.supervisor.register(
            ActuatorRegistration(
                message_id=uuid4(),
                emitted_at=datetime.now(UTC),
                source=SERVICE,
                actuator_id="drill-dummy",
                actuator_class="binary",
                role="outlet",
                driver_id="fake-actuator",
                safe_state=off,
                max_runtime_s=3600.0,
                heartbeat_timeout_s=30.0,
            ),
            actuator,
        )
        self._drill_actuator = actuator
        self._registrations.append(self.supervisor.registration_of("drill-dummy"))
        log.warning(
            "drill actuator registered, starting ENERGISED", extra={"actuator_id": "drill-dummy"}
        )

    async def run(self) -> None:
        log.info(
            "starting",
            extra={
                "clock_trusted": self._clock_trusted,
                "sd_notify": self.notifier.enabled,
                "sensors": len(self.sensors),
                "actuators": len(self.supervisor.actuator_ids),
            },
        )

        # Drives every actuator to its declared safe state before anything else
        # is allowed to happen. Startup asserts safe state; it never assumes it.
        await self.supervisor.start()
        log.info(
            "startup safe-state assertion complete",
            extra={
                "event": "safe_state_asserted",
                "actuators": len(self.supervisor.actuator_ids),
                "drill_actuator_safe": (
                    self._drill_actuator.is_safe()  # type: ignore[attr-defined]
                    if self._drill_actuator is not None
                    else None
                ),
            },
        )

        await self._connect_spine()

        await self.httpd.start()
        self.liveness.start()
        self.notifier.ready()
        self.notifier.status("running")

        try:
            await self._loop()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self.notifier.stopping()
        self.liveness.stop()
        await self.httpd.stop()
        if self.spine is not None:
            await self.spine.close()
        try:
            await self.supervisor.stop()
        except ExceptionGroup:
            log.exception("one or more actuators failed to reach safe state on shutdown")
        log.info("stopped")

    def request_stop(self) -> None:
        self._stopping.set()

    async def _connect_spine(self) -> None:
        """Attach to the spine if one is configured.

        Optional on purpose: the supervisor and its interlocks must work with
        no broker at all, and the restart drill exercises exactly that path.
        """
        url = os.environ.get("BELLASREEF_NATS_URL")
        if not url:
            log.info("no BELLASREEF_NATS_URL; running without the spine")
            return

        self.spine = Spine(url)
        await self.spine.connect()
        await self.spine.provision()

        for registration in self._registrations:
            await self.spine.publish_registration(registration)
        log.info("registrations published", extra={"count": len(self._registrations)})

        self.commands = CommandConsumer(self.spine, self.supervisor)
        await self.commands.subscribe()

    async def _beat_and_serve(self) -> None:
        """Publish a heartbeat and take one pass at the command queue."""
        if self.spine is None:
            return
        self._beat_seq += 1
        await self.spine.publish_heartbeat(
            Heartbeat(
                message_id=uuid4(),
                emitted_at=datetime.now(UTC),
                source=SERVICE,
                component=SERVICE,
                sequence=self._beat_seq,
                interval_s=self._loop_interval_s,
            )
        )
        if self.commands is not None:
            await self.commands.drain_once(timeout=0.2)

    # ------------------------------------------------------------- main loop

    async def _loop(self) -> None:
        ping_every = watchdog_interval_s(default=self._liveness_timeout_s / 3)
        next_ping = 0.0

        while not self._stopping.is_set():
            now = time.monotonic()

            if self._frozen:
                # The drill: block the event loop from inside the loop itself,
                # exactly as a deadlock would. Nothing below runs, no beats are
                # emitted, and the supervisor is meant to be restarted for us.
                log.critical("freeze drill engaged; blocking the event loop")
                while True:
                    time.sleep(3600)

            # Beat from *inside* the loop. This is the whole design: a beat
            # emitted by a separate task would keep asserting health that this
            # loop no longer has.
            self.liveness.beat()
            self.metrics.loop_beats.inc()
            self.metrics.loop_stall.set(self.liveness.age_s())

            if now >= next_ping:
                self.notifier.ping()
                next_ping = now + ping_every

            self._refresh_clock_trust()
            await self._beat_and_serve()
            await self._poll_due_sensors(now)
            self._refresh_latch_metrics()

            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._loop_interval_s)
            except TimeoutError:
                pass

    async def _poll_due_sensors(self, now: float) -> None:
        """Poll each driver on its own declared cadence, concurrently.

        Never sequentially: a DS18B20 costs ~831 ms, and iterating drivers in
        this loop would make one slow probe everyone else's problem.
        """
        due = [s for s in self.sensors if now >= self._sensor_deadlines.get(s.driver_id, 0.0)]
        if not due:
            return

        results = await asyncio.gather(*(s.read() for s in due), return_exceptions=True)
        for driver, result in zip(due, results, strict=True):
            self._sensor_deadlines[driver.driver_id] = now + driver.poll_interval_s
            if isinstance(result, BaseException):
                # The contract says read() must not raise. If one does, that is
                # a driver bug — record it, do not let it stop the loop.
                log.error(
                    "driver raised from read(), violating the contract",
                    extra={"driver_id": driver.driver_id},
                    exc_info=result,
                )
                self.metrics.sensor_reads.labels(driver.driver_id, "fault").inc()
                continue

            self.metrics.sensor_reads.labels(driver.driver_id, result.quality).inc()
            if result.quality == "ok" and result.value is not None:
                self.metrics.sensor_value.labels(driver.driver_id, result.unit).set(result.value)

    def _refresh_clock_trust(self) -> None:
        self.metrics.clock_trusted.set(1.0 if self._clock_trusted else 0.0)

    def _refresh_latch_metrics(self) -> None:
        for actuator_id in self.supervisor.actuator_ids:
            self.metrics.latched.labels(actuator_id).set(
                1.0 if self.supervisor.is_latched(actuator_id) else 0.0
            )

    # ------------------------------------------------------------- reporting

    async def _on_safety_event(self, event: SafetyEvent) -> None:
        self.metrics.safety_events.labels(event.reason).inc()
        log.warning(
            "safety event",
            extra={
                "actuator_id": event.actuator_id,
                "reason": event.reason,
                "detail": event.detail,
            },
        )

    def health(self) -> Health:
        stall = self.liveness.age_s()
        latched = tuple(a for a in self.supervisor.actuator_ids if self.supervisor.is_latched(a))

        if stall > self._liveness_timeout_s:
            reason = f"supervisor loop stalled for {stall:.1f}s"
            healthy = False
        elif not self._clock_trusted:
            reason = "host clock is not synchronised"
            healthy = False
        else:
            reason = "ok"
            healthy = True

        return Health(
            healthy=healthy,
            reason=reason,
            loop_stall_s=round(stall, 3),
            clock_trusted=self._clock_trusted,
            actuators=len(self.supervisor.actuator_ids),
            latched=latched,
        )

    # ------------------------------------------------------------- drill hook

    def enable_freeze(self) -> None:
        """Arm the freeze drill. Only reachable when the env flag is set."""
        self._frozen = True


async def _amain() -> int:
    configure_logging(service=SERVICE, level=os.environ.get("BELLASREEF_LOG_LEVEL", "INFO"))

    service = HardwareIO(
        liveness_timeout_s=float(os.environ.get("BELLASREEF_LIVENESS_TIMEOUT_S", "15")),
        metrics_port=int(os.environ.get("BELLASREEF_METRICS_PORT", "9101")),
    )
    service.discover()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, service.request_stop)

    if os.environ.get(FREEZE_DRILL_ENV) == "1":
        loop.add_signal_handler(signal.SIGUSR1, service.enable_freeze)
        service.register_drill_actuator()
        log.warning("freeze drill armed on SIGUSR1 — do not enable in production")

    await service.run()
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
