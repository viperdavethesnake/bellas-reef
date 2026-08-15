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
import json
import os
import signal
import subprocess
import time
from datetime import UTC, datetime
from typing import Any, Final
from uuid import uuid4

from bellasreef_contracts import (
    ActuatorLevel,
    ActuatorRegistration,
    ActuatorState,
    DeviceAssignment,
    Heartbeat,
    SensorReading,
    SensorRegistration,
    StateReason,
)
from bellasreef_contracts.driver import SensorSample
from bellasreef_service.httpd import Health, MetricsServer
from bellasreef_service.logging import configure_logging, get_logger
from bellasreef_service.watchdog import LivenessGuard
from prometheus_client import CollectorRegistry, Counter, Gauge
from pydantic import ValidationError

from bellasreef_hardware_io.capabilities import discover_pwm, discover_w1
from bellasreef_hardware_io.drivers.onewire import DS18B20
from bellasreef_hardware_io.factory import build_from_assignments
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


#: Everything about an assignment that changes what this process built:
#: ``(adopted, role, driver_type, canonical binding)``. The binding is
#: serialised with sorted keys so two dicts that differ only in key order
#: compare equal — a re-serialised republish must not read as a change.
#:
#: ``role`` is in here even though it does not pick a driver: it is part of the
#: registration the supervisor holds, so a role edit is a rebuild. When in
#: doubt the tuple grows — a wasted restart costs seconds, a missed one leaves
#: this process running a topology the registry no longer describes.
_AssignmentFingerprint = tuple[bool, str | None, str | None, str]


def _fingerprint(assignment: DeviceAssignment) -> _AssignmentFingerprint:
    """Reduce an assignment to what hardware-io would build differently."""
    return (
        assignment.adopted,
        assignment.role,
        assignment.driver_type,
        json.dumps(assignment.binding, sort_keys=True),
    )


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

        self.liveness = LivenessGuard(timeout_s=liveness_timeout_s)
        self.httpd = MetricsServer(probe=self.health, registry=self.registry, port=metrics_port)

        self._liveness_timeout_s = liveness_timeout_s
        self._stopping = asyncio.Event()
        self._frozen = False
        self._drill_actuator: object | None = None
        self.spine: Spine | None = None
        self.commands: CommandConsumer | None = None
        self._registrations: list[ActuatorRegistration] = []
        #: What the registry said when this process built its devices, keyed by
        #: device_id. The yardstick the assignment watch measures live traffic
        #: against — see ``_on_assignment_message``.
        self._assignments: dict[str, _AssignmentFingerprint] = {}
        self._beat_seq = 0
        self._sensor_deadlines: dict[str, float] = {}

    # ------------------------------------------------------------- lifecycle

    async def _build_from_registry(self) -> None:
        """Instantiate the devices the registry says this hub owns.

        Replaces the device file as the source of topology. The file's epitaph
        is the identity fork it caused: it let a config author mint a new id for
        hardware that already had one, and a tank's history ran down two
        device_ids for seventy minutes.
        """
        if self.spine is None:
            return
        assignments = await self.spine.read_assignments()
        # Snapshot the registry as read, from the same list the factory
        # consumes — including the unadopted tombstones, which build nothing
        # but are still assignments this process has seen and must not be
        # mistaken for news when they are republished.
        self._remember_assignments(assignments)
        actuators, sensors = build_from_assignments(assignments, open_i2c=self._open_i2c)

        for sensor in sensors:
            self.sensors.append(sensor)

        for built in actuators:
            # Bring the channel up before registering it. The supervisor
            # asserts every actuator into its safe state at startup, and an
            # unopened RP1 channel has no sysfs attributes to write to.
            #
            # A failure here skips the actuator rather than taking the service
            # down, and that asymmetry is deliberate: two lights that could not
            # open took the temperature probe offline with them, which is a far
            # worse outcome than a dark channel. A light that cannot be opened
            # is dark either way; a hub that will not start monitors nothing.
            opener = getattr(built.driver, "open", None)
            if opener is not None:
                try:
                    await opener()
                except Exception:
                    log.exception(
                        "actuator could not be opened; skipped",
                        extra={"actuator_id": built.registration.actuator_id},
                    )
                    continue

            self.supervisor.register(built.registration, built.driver)
            self._registrations.append(built.registration)

        log.info(
            "devices built from registry",
            extra={
                "assignments": len(assignments),
                "sensors": len(sensors),
                "actuators": len(actuators),
            },
        )

    async def _announce_capabilities(self) -> None:
        """Tell the registry what this hub's hardware can offer.

        Read-only discovery: nothing here exports a PWM channel or touches a
        pin. Announcing what exists must never change what it is doing.

        A source that finds nothing still announces an empty list rather than
        staying silent — "this hub has no 1-Wire probes" and "nobody has asked"
        are different answers, and only one of them lets an operator conclude
        the bus is empty.
        """
        if self.spine is None:
            return
        for announcement in (discover_pwm(), discover_w1()):
            if announcement is None:
                continue
            await self.spine.publish_capabilities(announcement)
            log.info(
                "capability announced",
                extra={
                    "hardware_source": announcement.hardware_source,
                    "channels": len(announcement.channels),
                },
            )

    @staticmethod
    def _open_i2c(bus: int) -> Any:
        """Open a real I²C bus. Imported lazily so a Pi-only build still runs.

        A hub with no PCA9685 declared never reaches this, and on a dev machine
        `smbus2` may not be installed at all — which must not stop the tests or
        a PWM-only hub from starting.
        """
        from smbus2 import SMBus

        return SMBus(bus)

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
                # hardware-io handles authoritative devices only
                # (device-classes.md §3). Anything advisory or observe_only
                # belongs to a separate service that is allowed to fail without
                # touching the tank.
                control_authority="authoritative",
                failsafe_capable=True,
                transport="local",
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

        try:
            await self._loop()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
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

    def _remember_assignments(self, assignments: list[DeviceAssignment]) -> None:
        """Record the registry as it was read, for the watch to compare against."""
        self._assignments = {a.device_id: _fingerprint(a) for a in assignments}

    def _on_assignment_message(self, data: bytes) -> None:
        """Decide whether a live assignment message is news.

        Payload-aware since 2026-08-15, and the reason is specific: the API
        republishes *every* adopted assignment from Postgres on every lifespan
        start, so each API restart or deploy put an exact copy of what this
        process already built on the wire. Payload-blind, that echo restarted
        hardware-io — roughly fifteen seconds with no monitoring, a deploy
        telemetry gate that depended on which service came up first, and an API
        crash-loop that flapped hardware-io with it.

        Only an assignment that differs from what was built is news. Everything
        else fails toward the restart path: a device this process never read, a
        payload that will not parse, anything the fingerprint cannot vouch for.
        A wasted restart costs seconds; a wrong ignore leaves the drivers and
        the registry disagreeing with nobody watching.
        """
        try:
            assignment = DeviceAssignment.model_validate_json(data)
        except ValidationError:
            log.warning(
                "assignment did not validate; restarting to rebuild",
                extra={"event": "assignment_restart"},
            )
            self._on_assignment_changed()
            return

        known = self._assignments.get(assignment.device_id)
        if known is not None and known == _fingerprint(assignment):
            log.debug(
                "assignment echo ignored",
                extra={"device_id": assignment.device_id, "event": "assignment_echo"},
            )
            return

        self._on_assignment_changed()

    def _on_assignment_changed(self) -> None:
        """The registry moved under us. Exit cleanly; the restart policy
        rebuilds from the retained registry — the drilled path (ruled
        2026-08-15, restart-on-change over a live add/remove path).

        The decision that a message *is* a change belongs to
        ``_on_assignment_message``; this is only the response to it."""
        if not self._stopping.is_set():
            log.info(
                "assignment changed; exiting to rebuild from registry",
                extra={"event": "assignment_restart"},
            )
        self.request_stop()

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

        # The registry is the source of topology. Built here rather than before
        # the spine connects, because the assignments come *over* the spine —
        # which is the whole point: hardware-io holds no credential and needs
        # none to learn what it owns.
        await self._build_from_registry()

        # Subscribe after the build, not before: the initial read above drains
        # the retained stream on a pull consumer, which this core subscription
        # never sees — but ordering it after is what keeps that true by
        # construction rather than by coincidence of transport.
        await self.spine.watch_assignments(self._on_assignment_message)

        # Capabilities before registrations: what the hardware can offer is
        # true whether or not anyone has bound it, and a client opening a "find
        # devices" screen needs the offer list even on a hub where nothing has
        # been declared yet.
        await self._announce_capabilities()

        for registration in self._registrations:
            await self.spine.publish_registration(registration)

        for driver in self.sensors:
            await self.spine.publish_sensor_registration(
                SensorRegistration(
                    message_id=uuid4(),
                    emitted_at=datetime.now(UTC),
                    source=SERVICE,
                    sensor_id=driver.driver_id,
                    sensor_type=driver.sensor_type,
                    driver_id=driver.driver_id,
                    # hardware-io only ever owns local buses (§3).
                    transport="local",
                    unit=driver.unit,
                    poll_interval_s=driver.poll_interval_s,
                )
            )

        log.info(
            "registrations published",
            extra={"actuators": len(self._registrations), "sensors": len(self.sensors)},
        )

        # Once per actuator, after the safe-state assertion: the drill
        # actuator was driven safe by supervisor.start() above, in run(),
        # before the spine even connected; a registry-built actuator never
        # goes through that call at all — it is registered here, later, after
        # being freshly opened, and a guard starts ``at_safe_state=True`` by
        # construction (safety.py's ``_Guard``). Either way, by this point
        # every actuator in ``self._registrations`` is safe, so a freshly
        # restarted hub tells clients the truth without waiting for a command.
        await self._publish_startup_states()

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

            # Publish every sample, faults included. A consumer needs to know a
            # probe went bad as much as it needs the good readings — silence and
            # "reading fine" must not look the same.
            await self._publish_reading(driver, result)

    async def _publish_reading(self, driver: DS18B20, sample: SensorSample) -> None:
        if self.spine is None:
            return
        try:
            await self.spine.publish_sensor(
                SensorReading(
                    message_id=uuid4(),
                    emitted_at=datetime.now(UTC),
                    source=SERVICE,
                    sensor_id=driver.driver_id,
                    sensor_type=driver.sensor_type,
                    value=sample.value,
                    unit=sample.unit,
                    quality=sample.quality,
                    calibration_id=sample.calibration_id,
                )
            )
        except Exception:
            log.warning(
                "failed to publish a reading",
                extra={"driver_id": driver.driver_id},
                exc_info=True,
            )

    async def _publish_startup_states(self) -> None:
        """Once per registered actuator, tell the wire the post-assertion truth.

        ``registration.safe_state`` rather than reading anything back off the
        driver — no driver in this codebase exposes a current-level getter
        (docs/driver interface), and it would be redundant besides: every
        actuator here is either freshly asserted safe by
        ``supervisor.start()`` or freshly opened and assumed safe by
        construction (see the call site in ``_connect_spine``), so the
        registration's own declared safe state IS the truth right now.
        """
        for registration in self._registrations:
            # Non-None by construction: the supervisor refuses to register an
            # authoritative actuator without the complete safety triple
            # (safety.py's ``InterlockSupervisor.register``), and hardware-io
            # registers authoritative actuators only.
            safe_state = registration.safe_state
            assert safe_state is not None
            await self._publish_state(
                registration.actuator_id,
                safe_state,
                reason="startup",
                latched=self.supervisor.is_latched(registration.actuator_id),
            )

    async def _publish_state(
        self,
        actuator_id: str,
        level: ActuatorLevel,
        *,
        reason: StateReason,
        latched: bool = False,
    ) -> None:
        """Publish one ActuatorState. Guarded exactly like ``_publish_reading``:
        no spine means no publish, and a publish failure is logged and
        swallowed rather than allowed to break the caller.
        """
        if self.spine is None:
            return
        try:
            await self.spine.publish_state(
                ActuatorState(
                    message_id=uuid4(),
                    emitted_at=datetime.now(UTC),
                    source=SERVICE,
                    actuator_id=actuator_id,
                    level=level,
                    reason=reason,
                    since=datetime.now(UTC),
                    latched=latched,
                )
            )
        except Exception:
            log.warning(
                "failed to publish actuator state",
                extra={"actuator_id": actuator_id, "reason": reason},
                exc_info=True,
            )

    def _refresh_clock_trust(self) -> None:
        self.metrics.clock_trusted.set(1.0 if self._clock_trusted else 0.0)

    def _refresh_latch_metrics(self) -> None:
        for actuator_id in self.supervisor.actuator_ids:
            self.metrics.latched.labels(actuator_id).set(
                1.0 if self.supervisor.is_latched(actuator_id) else 0.0
            )

    # ------------------------------------------------------------- reporting

    #: Which SafetyEvent reasons correspond to the supervisor actually driving
    #: an actuator to its safe state, as opposed to merely refusing a command
    #: (clock_untrusted, command_expired, latched, unknown_actuator — none of
    #: those move anything, so publishing a state for them would claim a
    #: transition that never happened). "shutdown" is included per item 3's
    #: scope, but see the report: by the time supervisor.stop() drives it, the
    #: spine is already closed in HardwareIO.shutdown(), so in practice this
    #: one is a no-op today rather than a state that reaches the wire.
    _AUTONOMOUS_TRIP_REASONS: Final[frozenset[str]] = frozenset(
        {"heartbeat_timeout", "max_runtime_exceeded", "shutdown"}
    )

    #: TripReason -> StateReason for the autonomous trips above. Both trips
    #: land the actuator at its declared safe_state; only max_runtime latches.
    _TRIP_STATE_REASON: Final[dict[str, StateReason]] = {
        "heartbeat_timeout": "safe_state",
        "max_runtime_exceeded": "interlock_latch",
        "shutdown": "safe_state",
    }

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
        if event.reason in self._AUTONOMOUS_TRIP_REASONS:
            registration = self.supervisor.registration_of(event.actuator_id)
            safe_state = registration.safe_state
            assert safe_state is not None
            await self._publish_state(
                event.actuator_id,
                safe_state,
                reason=self._TRIP_STATE_REASON[event.reason],
                latched=self.supervisor.is_latched(event.actuator_id),
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
    # Devices are built from the registry once the spine is up — see
    # HardwareIO._build_from_registry. Nothing is read from disk: the device
    # file was retired as a source of topology after it forked a device's
    # identity, and it survives only as an input to `bellasreef devices import`.

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
