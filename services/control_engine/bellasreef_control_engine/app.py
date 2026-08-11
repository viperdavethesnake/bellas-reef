# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""control-engine service entry point.

Owns the lighting schedule and is the sole publisher of actuator commands. No
hardware knowledge: it reaches devices only through the subject contract.

**Clock trust gates everything time-stamped, including the heartbeat.** That
needs saying because it looks severe at first glance. This board has no RTC
battery, so after a power cut the clock is wrong until chrony catches up. A
command's ``expires_at`` and a heartbeat's ``emitted_at`` both come from that
clock — so with an untrusted clock the engine cannot honestly assert *anything*
timestamped, including "I am alive at this moment".

Stopping the heartbeat is therefore the correct signal, not a blunt one:
hardware-io responds by driving every actuator to its declared safe state,
which is exactly what should happen when the controller has lost track of when
it is. Continuing to beat would hold actuators at their last commanded level
through an event we have no way to reason about.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from bellasreef_contracts import SensorReading
from bellasreef_db.alerts import PostgresAlertStore
from bellasreef_db.overrides import ActiveOverride, OverrideStore
from bellasreef_service import (
    Health,
    LivenessGuard,
    MetricsServer,
    SdNotifier,
    clock_is_trusted,
    configure_logging,
    get_logger,
    watchdog_interval_s,
)
from prometheus_client import CollectorRegistry, Counter, Gauge
from sqlalchemy.ext.asyncio import create_async_engine

from bellasreef_control_engine.alerts import AlertSupervisor, SilenceWatcher, Thresholds
from bellasreef_control_engine.profiles import ChannelProfile
from bellasreef_control_engine.publisher import CommandPublisher
from bellasreef_control_engine.scheduler import Intent, LightingScheduler

__all__ = ["ControlEngine", "load_profiles", "main"]

log = get_logger(__name__)

SERVICE: Final = "control-engine"


def load_profiles(path: Path) -> list[ChannelProfile]:
    """Read lighting profiles from JSON.

    Validation failures raise. A controller that started with half a schedule
    because one channel failed to parse would light a tank to a shape nobody
    designed.
    """
    raw = json.loads(path.read_text())
    return [ChannelProfile.model_validate(entry) for entry in raw]


class _Metrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.loop_beats = Counter(
            "bellasreef_loop_beats_total", "Engine loop iterations", registry=registry
        )
        self.loop_stall = Gauge(
            "bellasreef_loop_stall_seconds",
            "Seconds since the last loop beat",
            registry=registry,
        )
        self.clock_trusted = Gauge(
            "bellasreef_clock_trusted",
            "1 when the host clock is NTP-synchronised",
            registry=registry,
        )
        self.alerts = Counter(
            "bellasreef_alerts_total",
            "Threshold alert transitions",
            ["device_id", "bound", "state"],
            registry=registry,
        )
        self.commands = Counter(
            "bellasreef_commands_published_total",
            "Commands published by reason",
            ["actuator_id", "reason"],
            registry=registry,
        )
        self.suppressed = Counter(
            "bellasreef_commands_suppressed_total",
            "Intents not published, by cause",
            ["cause"],
            registry=registry,
        )
        self.channel_duty = Gauge(
            "bellasreef_channel_duty",
            "Last commanded duty per channel",
            ["actuator_id"],
            registry=registry,
        )


class ControlEngine:
    def __init__(
        self,
        profiles: list[ChannelProfile],
        *,
        nats_url: str | None = None,
        loop_interval_s: float = 1.0,
        liveness_timeout_s: float = 15.0,
        metrics_port: int = 9102,
        max_duty_delta_per_s: float | None = None,
        override_store: OverrideStore | None = None,
        alert_store: PostgresAlertStore | None = None,
        threshold_refresh_s: float = 30.0,
    ) -> None:
        self.registry = CollectorRegistry()
        self.metrics = _Metrics(self.registry)

        self.scheduler = LightingScheduler(profiles, max_duty_delta_per_s=max_duty_delta_per_s)
        self.overrides = override_store
        self._held: dict[str, ActiveOverride] = {}
        self.publisher = CommandPublisher(nats_url) if nats_url else None

        self._loop_interval_s = loop_interval_s
        self._liveness_timeout_s = liveness_timeout_s
        self._clock_trusted = clock_is_trusted()
        self._clock_was_trusted = self._clock_trusted

        self.alerts: AlertSupervisor | None = None
        self.silence: SilenceWatcher | None = None
        self._alert_store = alert_store
        self._thresholds: dict[str, Thresholds] = {}
        self._threshold_refresh_s = threshold_refresh_s
        self._thresholds_read_at = 0.0

        self.notifier = SdNotifier()
        self.liveness = LivenessGuard(timeout_s=liveness_timeout_s)
        self.httpd = MetricsServer(probe=self.health, registry=self.registry, port=metrics_port)
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        log.info(
            "starting",
            extra={
                "clock_trusted": self._clock_trusted,
                "channels": list(self.scheduler.channel_ids),
                "spine": self.publisher is not None,
            },
        )
        if self.publisher is not None:
            await self.publisher.connect()

        await self._rearm_overrides()
        await self._start_alerting()

        await self.httpd.start()
        self.liveness.start()
        self.notifier.ready()
        try:
            await self._loop()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self.notifier.stopping()
        self.liveness.stop()
        await self.httpd.stop()
        if self.publisher is not None:
            await self.publisher.close()
        log.info("stopped")

    def request_stop(self) -> None:
        self._stopping.set()

    # ------------------------------------------------------------- main loop

    async def _loop(self) -> None:
        ping_every = watchdog_interval_s(default=self._liveness_timeout_s / 3)
        next_ping = 0.0

        while not self._stopping.is_set():
            mono = time.monotonic()
            self.liveness.beat()
            self.metrics.loop_beats.inc()
            self.metrics.loop_stall.set(self.liveness.age_s())

            self._refresh_clock_trust()

            if self._clock_trusted:
                if mono >= next_ping:
                    self.notifier.ping()
                    next_ping = mono + ping_every
                await self._tick(datetime.now(UTC))

            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._loop_interval_s)
            except TimeoutError:
                pass

    # -------------------------------------------------------------- alerting

    async def _start_alerting(self) -> None:
        """Bring up threshold evaluation, if both halves are present.

        Needs a database (to know the bands and to record episodes) and a spine
        (to hear readings and announce breaches). With either missing the engine
        still schedules lighting — alerting is additive, and a controller that
        refuses to dim the lights because it cannot alert would be trading a
        working feature for a broken one.
        """
        if self._alert_store is None or self.publisher is None:
            log.info(
                "threshold alerting disabled",
                extra={
                    "has_store": self._alert_store is not None,
                    "has_spine": self.publisher is not None,
                },
            )
            return

        publisher = self.publisher
        self.alerts = AlertSupervisor(self._alert_store, publisher.publish_alert)
        await self.alerts.prime()
        self.silence = SilenceWatcher(self._alert_store, publisher.publish_silence)
        await self.silence.prime()
        await self._refresh_thresholds(force=True)
        await publisher.subscribe_sensors(self._on_reading)

    async def _refresh_thresholds(self, *, force: bool = False) -> None:
        """Re-read the bands periodically rather than per reading.

        Thresholds are edited by a human through the API, so seconds of lag is
        irrelevant; a database round trip on every sample from every probe is
        not. Polling rather than a change feed because the alternative is a
        second NATS subject whose only subscriber is this cache.
        """
        if self._alert_store is None:
            return
        now = time.monotonic()
        if not force and now - self._thresholds_read_at < self._threshold_refresh_s:
            return
        self._thresholds_read_at = now

        bands: dict[str, Thresholds] = {}
        for device_id, (low, high, margin) in (await self._alert_store.thresholds()).items():
            try:
                bands[device_id] = Thresholds(minimum=low, maximum=high, clear_margin=margin)
            except ValueError:
                # The CHECK constraints make this unreachable from the API, but
                # a hand-edited row must not take the evaluator down with it.
                log.exception("ignoring unusable thresholds", extra={"device_id": device_id})
        self._thresholds = bands

    async def _on_reading(self, reading: SensorReading) -> None:
        if self.alerts is None:
            return

        # Before thresholds, and unconditionally: a probe reporting again has
        # to end its silence even if nobody ever configured a band for it.
        if self.silence is not None:
            await self.silence.on_reading(reading)

        await self._refresh_thresholds()
        thresholds = self._thresholds.get(reading.sensor_id)
        if thresholds is None:
            return

        if self.silence is not None and self.silence.is_silent(reading.sensor_id):
            # Unreachable in practice, since the call above clears the silence
            # for any reading good enough to evaluate. Kept as the explicit
            # statement of the rule: a reading arriving while the probe is
            # recorded silent is not evidence about now.
            return

        published = await self.alerts.on_reading(reading, thresholds)
        for alert in published:
            self.metrics.alerts.labels(alert.device_id, alert.bound, alert.state).inc()
            if self.publisher is not None:
                await self.publisher.publish_audit(
                    "alert",
                    {
                        "message_id": str(alert.message_id),
                        "event": f"alert.{alert.state}",
                        "device_id": alert.device_id,
                        "bound": alert.bound,
                        "value": alert.value,
                        "threshold": alert.threshold,
                        "clear_margin": alert.clear_margin,
                        "unit": alert.unit,
                        "emitted_at": alert.emitted_at.isoformat(),
                    },
                )

    async def _rearm_overrides(self) -> None:
        """Lapse-on-wake, then re-arm what is still owed.

        An override whose deadline passed while we were down is closed by the
        store and never applied — the operator asked for thirty minutes, and
        honouring it hours later is not that.
        """
        if self.overrides is None:
            return
        live = await self.overrides.load_active()
        self._held = {o.target: o for o in live}
        log.info(
            "overrides re-armed after wake",
            extra={"active": sorted(self._held), "count": len(live)},
        )

    async def _expire_overrides(self) -> None:
        """Release anything whose monotonic deadline has passed."""
        if self.overrides is None or not self._held:
            return
        for target, override in list(self._held.items()):
            if override.is_expired():
                await self.overrides.release(override.id, "expired")
                del self._held[target]
                self.metrics.suppressed.labels("override_expired").inc()
                log.info("override expired", extra={"target": target})

    async def _tick(self, now: datetime) -> None:
        await self._expire_overrides()
        await self._sweep_silence(now)
        held = {t: o.duty for t, o in self._held.items()}
        intents = self.scheduler.due(now, held)
        for intent in intents:
            await self._publish(intent, now)

    async def _sweep_silence(self, now: datetime) -> None:
        """Ask the clock whether any probe has gone quiet.

        On the tick rather than on a reading, which is the whole point: a
        reading-driven check can only ever react to a message that arrived, so
        a probe that stops arriving is invisible to it forever. That is exactly
        how a dead probe sat behind a stale number for ten hours.

        Failures are logged, not raised. This runs inside the supervisor loop
        that also feeds the liveness guard, and a database hiccup here must not
        take the lighting schedule down with it.
        """
        if self.silence is None:
            return
        try:
            await self.silence.sweep(now=now)
        except Exception:
            log.exception("silence sweep failed")

    async def _publish(self, intent: Intent, now: datetime) -> None:
        if self.publisher is None or not self.publisher.connected:
            self.metrics.suppressed.labels("no_spine").inc()
            return

        command = self.publisher.build_pwm_command(
            intent.channel_id, intent.duty, reason=f"lighting:{intent.reason}", now=now
        )
        await self.publisher.emit(command)

        # Only after a successful publish. Recording an emission the broker
        # never accepted would make the scheduler skip the next one.
        self.scheduler.mark_emitted(intent, now)
        self.metrics.commands.labels(intent.channel_id, intent.reason).inc()
        self.metrics.channel_duty.labels(intent.channel_id).set(intent.duty)

    def _refresh_clock_trust(self) -> None:
        self._clock_trusted = clock_is_trusted()
        self.metrics.clock_trusted.set(1.0 if self._clock_trusted else 0.0)

        if self._clock_trusted != self._clock_was_trusted:
            if not self._clock_trusted:
                # Emission history was recorded against a clock we no longer
                # believe, so it says nothing about what to send now.
                self.scheduler.reset()
                self.metrics.suppressed.labels("clock_untrusted").inc()
                log.critical(
                    "clock is not synchronised; suspending scheduling AND heartbeats "
                    "so hardware-io drives actuators to safe state",
                    extra={"event": "clock_untrusted"},
                )
            else:
                log.warning("clock re-synchronised; resuming", extra={"event": "clock_trusted"})
            self._clock_was_trusted = self._clock_trusted

    # ------------------------------------------------------------- reporting

    def health(self) -> Health:
        stall = self.liveness.age_s()
        if stall > self._liveness_timeout_s:
            return Health(
                False,
                f"loop stalled for {stall:.1f}s",
                round(stall, 3),
                self._clock_trusted,
                len(self.scheduler.channel_ids),
                (),
            )
        if not self._clock_trusted:
            return Health(
                False,
                "host clock is not synchronised",
                round(stall, 3),
                False,
                len(self.scheduler.channel_ids),
                (),
            )
        if self.publisher is not None and not self.publisher.connected:
            return Health(
                False,
                "spine not connected",
                round(stall, 3),
                True,
                len(self.scheduler.channel_ids),
                (),
            )
        return Health(True, "ok", round(stall, 3), True, len(self.scheduler.channel_ids), ())


async def _amain() -> int:
    configure_logging(service=SERVICE, level=os.environ.get("BELLASREEF_LOG_LEVEL", "INFO"))

    profile_path = os.environ.get("BELLASREEF_LIGHTING_PROFILES")
    profiles = load_profiles(Path(profile_path)) if profile_path else []
    if not profiles:
        log.warning("no lighting profiles configured; the engine will schedule nothing")

    slew_raw = os.environ.get("BELLASREEF_MAX_DUTY_DELTA_PER_S")
    dsn = os.environ.get("BELLASREEF_DATABASE_URL")
    # One engine, two stores. Building two would open two connection pools to
    # the same database for no reason.
    db = create_async_engine(dsn, future=True) if dsn else None

    engine = ControlEngine(
        profiles,
        nats_url=os.environ.get("BELLASREEF_NATS_URL"),
        liveness_timeout_s=float(os.environ.get("BELLASREEF_LIVENESS_TIMEOUT_S", "15")),
        metrics_port=int(os.environ.get("BELLASREEF_METRICS_PORT", "9102")),
        max_duty_delta_per_s=float(slew_raw) if slew_raw else None,
        override_store=OverrideStore(db) if db is not None else None,
        alert_store=PostgresAlertStore(db) if db is not None else None,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, engine.request_stop)

    await engine.run()
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
