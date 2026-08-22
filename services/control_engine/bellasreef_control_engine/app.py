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
import os
import signal
import time
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from bellasreef_contracts import ScheduleDefinition, SensorReading
from bellasreef_db.alerts import PostgresAlertStore
from bellasreef_db.overrides import ActiveOverride, OverrideStore
from bellasreef_db.schedules import ScheduleStore
from bellasreef_service import (
    Health,
    LivenessGuard,
    MetricsServer,
    clock_is_trusted,
    configure_logging,
    get_logger,
)
from prometheus_client import CollectorRegistry, Counter, Gauge
from sqlalchemy.ext.asyncio import create_async_engine

from bellasreef_control_engine.alerts import AlertSupervisor, SilenceWatcher, Thresholds
from bellasreef_control_engine.assignments import AssignmentLedger
from bellasreef_control_engine.profiles import ChannelProfile
from bellasreef_control_engine.publisher import CommandPublisher
from bellasreef_control_engine.scheduler import HeldTarget, Intent, LightingScheduler

__all__ = ["ControlEngine", "main"]

log = get_logger(__name__)

SERVICE: Final = "control-engine"


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
        self.lighting_schedules = Gauge(
            "bellasreef_lighting_schedules",
            "Number of channel schedules currently applied from Postgres",
            registry=registry,
        )
        self.schedule_reload_errors = Counter(
            "bellasreef_schedule_reload_errors_total",
            "Schedule store read failures during the per-tick reload",
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
        schedule_store: ScheduleStore | None = None,
    ) -> None:
        self.registry = CollectorRegistry()
        self.metrics = _Metrics(self.registry)

        self.scheduler = LightingScheduler(profiles, max_duty_delta_per_s=max_duty_delta_per_s)
        self.overrides = override_store
        self._held: dict[str, ActiveOverride] = {}
        self.publisher = CommandPublisher(nats_url) if nats_url else None
        self.assignments = AssignmentLedger()
        # Forget on the tombstone EVENT, not on a tick's timing. due() only
        # surfaces a channel when it is cold, mid-slew, past the deadband, or
        # past the refresh window — a tombstone landing outside all four of
        # those (e.g. unadopt 30s after publish, re-adopt 30s after that, well
        # inside the deadband and refresh windows) would never appear in
        # _tick's `intents`, so a forget() tied to that loop would silently
        # never run. Wiring it straight to the ledger's own notification
        # covers both the live subscription and the drain/re-drain paths —
        # both go through AssignmentLedger.apply — with no dependency on
        # whether the scheduler happened to have anything due at that moment.
        self.assignments.on_tombstone = self.scheduler.forget
        self._assignments_loaded = False
        self._suppressed_unassigned: set[str] = set()

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

        self.schedules = schedule_store
        self._last_curves: dict[str, ScheduleDefinition] = {}
        self._schedule_read_failing = False

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
            # Wired before connect(), which is when CommandPublisher hands it
            # to nats.py's reconnected_cb. A reconnect means core-subject
            # messages (assignment tombstones included) may have been missed
            # during the gap — see CommandPublisher.connect's docstring — so
            # this forces the _loop retry to re-drain JetStream, which still
            # has them.
            self._wire_reconnect_handling()
            await self.publisher.connect()
            await self.publisher.subscribe_assignments(self.assignments.apply)
            self._assignments_loaded = await self.publisher.load_assignments(self.assignments)

        await self._rearm_overrides()
        await self._start_alerting()

        await self.httpd.start()
        self.liveness.start()
        try:
            await self._loop()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self.liveness.stop()
        await self.httpd.stop()
        if self.publisher is not None:
            await self.publisher.close()
        log.info("stopped")

    def request_stop(self) -> None:
        self._stopping.set()

    def _wire_reconnect_handling(self) -> None:
        """Point the publisher's reconnect hook at ``_on_reconnected``.

        Split out from :meth:`run` so it is callable on its own — the wiring
        itself needs no live connection, and testing it that way avoids
        driving a real NATS reconnect just to prove the two objects are
        pointed at each other.
        """
        if self.publisher is not None:
            self.publisher.on_reconnected = self._on_reconnected

    def _on_reconnected(self) -> None:
        """A NATS reconnect may have missed core-subject messages.

        ``_loop`` already retries ``load_assignments`` while
        ``_assignments_loaded`` is False (written for a stream that
        provisions after startup) — flipping it back here reuses that retry
        to re-drain JetStream, which still has whatever tombstone or bind was
        missed during the gap.
        """
        self._assignments_loaded = False

    # ------------------------------------------------------------- main loop

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            self.liveness.beat()
            self.metrics.loop_beats.inc()
            self.metrics.loop_stall.set(self.liveness.age_s())

            self._refresh_clock_trust()

            if (
                self.publisher is not None
                and self.publisher.connected
                and not self._assignments_loaded
            ):
                # A drain that found no stream yet — the assignment stream may
                # provision after this service starts. The live subscription
                # (started in run(), before this ever ran) alone would miss
                # anything published before it attached.
                self._assignments_loaded = await self.publisher.load_assignments(self.assignments)

            if self._clock_trusted:
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

        # Same cadence, same reason: the API closes an episode when the bound
        # that raised it is removed, and the supervisor's in-memory mirror has
        # to follow or it will treat a re-set band as still in breach.
        if self.alerts is not None:
            await self.alerts.resync()

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

        This is the *only* place ``load_active`` is called, and it runs once,
        before the loop's first tick. That matters: ``load_active`` lapses by
        wall clock and re-arms every survivor from wall clock, which is the
        right thing to do exactly once, at wake, when the monotonic origin
        has just been reborn and the wall clock is all there is to go on.
        Every tick after this, :meth:`_reload_overrides` reads with
        ``list_active`` instead — no lapse, no re-arm — so a hold this process
        is already watching keeps the monotonic deadline armed here.
        """
        if self.overrides is None:
            return
        woke = await self.overrides.load_active()
        self._held = {o.target: o for o in woke.live}
        log.info(
            "overrides re-armed after wake",
            extra={"active": sorted(self._held), "count": len(woke.live)},
        )
        # Lapsed holds are endings only this process witnessed, at wake. Audit
        # them or the trail shows a hold that started and never finished
        # (UX review 2026-08-17, E1). Distinct from "expired": the operator's
        # thirty minutes were not honoured to the end.
        for released in woke.lapsed:
            await self._audit_override_release(released.id, released.target, "lapsed")

    async def _expire_overrides(self) -> None:
        """Release anything whose *monotonic* deadline has passed.

        Must run before :meth:`_reload_overrides` in :meth:`_tick`. The two
        clocks matter here (see ``bellasreef_db.overrides``'s module
        docstring): every override this process is already watching was armed
        with a monotonic deadline precisely so a wall-clock step mid-run
        (chrony correcting after a power cut) cannot shorten or lengthen it.
        Deciding expiry here, against that monotonic deadline, and closing the
        row ourselves with ``release_reason='expired'`` is what keeps that
        promise. Nothing else closes a row mid-run: the reload below reads
        with ``list_active``, which touches neither rows nor deadlines, so a
        row this process is watching is released here, against the monotonic
        clock, or by another client — never by a wall-clock comparison the
        engine did not ask for.
        """
        if self.overrides is None or not self._held:
            return
        for target, override in list(self._held.items()):
            if override.is_expired():
                await self.overrides.release(override.id, "expired")
                del self._held[target]
                self.metrics.suppressed.labels("override_expired").inc()
                log.info("override expired", extra={"target": target})
                await self._audit_override_release(override.id, target, "expired")

    async def _audit_override_release(self, override_id: UUID, target: str, reason: str) -> None:
        """One ``override.released`` row, in the same shape the API writes for a
        manual release, so the audit log reads as one trail whoever ended the
        hold. The API records the endings it causes (manual, superseded); the
        engine is the only witness to the ones the clock causes (expired,
        lapsed) — before this it recorded none of them, and the log showed
        three "started" with no "ended" for one light re-held twice.

        Failure to publish is logged, not raised: the hold is already released
        in the store, and a broker hiccup must not turn a clean expiry into a
        stuck tick.
        """
        if self.publisher is None:
            return
        try:
            await self.publisher.publish_audit(
                "command",
                {
                    "event": "override.released",
                    "override_id": str(override_id),
                    "target": target,
                    "reason": reason,
                    "actor": "control-engine",
                },
            )
        except Exception:
            log.warning(
                "override release not audited",
                extra={"override_id": str(override_id), "target": target, "reason": reason},
                exc_info=True,
            )

    async def _reload_overrides(self) -> None:
        """Pick up creations and releases made by another client mid-run.

        ``self._held`` was previously only ever populated once, by
        ``_rearm_overrides`` at startup — an override the API created or
        released against Postgres *after* that, on a running engine, never
        touched this dict, so it was returned 200 and simply never acted on
        until the next restart. Re-reading the store every tick is the fix:
        a target missing from the fresh read (manual release, supersede)
        drops out here exactly like it would have dropped out of the reply
        to a fresh "what's active" query.

        Two rules keep the monotonic promise intact while doing that:

        * The read is ``list_active``, not ``load_active``. ``load_active``
          lapses rows by wall clock and re-arms every survivor from wall
          clock — right at wake, wrong on a running engine, where a chrony
          step would shorten or lengthen a hold the operator already placed.
        * A target already held **with the same override id** keeps the
          object it was armed with, monotonic deadline and all. Only a row
          this process has not seen before — a new hold, or a superseding
          one on a target it was already watching — is armed here, from the
          moment it is first seen. A never-held row already past its wall
          deadline arms to "now" and is closed by ``_expire_overrides`` on
          the next tick, with reason ``'expired'``.

        ``_expire_overrides`` runs first and has already released anything
        whose monotonic deadline passed, so those rows are no longer
        unreleased by the time this reads and cannot come back.
        """
        if self.overrides is None:
            return
        held: dict[str, ActiveOverride] = {}
        for fresh in await self.overrides.list_active():
            current = self._held.get(fresh.target)
            if current is not None and current.id == fresh.id:
                held[fresh.target] = current
            else:
                fresh.arm()
                held[fresh.target] = fresh
        self._held = held

    async def _reload_schedules(self) -> None:
        """Same contract as _reload_overrides: Postgres is the source of truth and
        the tick re-reads it, so an edit the API made is live within one tick with
        no push channel to desync — the archive's schedules died of exactly that.
        On a read error, keep the last good set: a flapping database must not
        strip the tank's schedule.

        ``ChannelProfile.from_definition`` lives inside this same try/except,
        not just the store read: its ``channel_id`` field carries a pattern
        constraint, and a row with a channel_id that violates it (the API
        validates on write, but a row can predate that check, or be written
        by anything else that talks to this table) would otherwise raise
        outside any handler here and kill the tick loop. Degrading exactly
        like a store read failure — keep the last good profile set, count it,
        warn once per outage — is defense in depth for a check that already
        exists at the API, not the only place it exists.
        """
        if self.schedules is None:
            return
        try:
            curves = await self.schedules.assigned_curves()
            profiles = (
                [ChannelProfile.from_definition(cid, d) for cid, d in sorted(curves.items())]
                if curves != self._last_curves
                else None
            )
        except Exception:
            self.metrics.schedule_reload_errors.inc()
            if not self._schedule_read_failing:  # one log per outage, not per tick
                self._schedule_read_failing = True
                log.warning("schedule reload failed; keeping last good set", exc_info=True)
            return
        self._schedule_read_failing = False
        if profiles is not None:
            self.scheduler.set_profiles(profiles)
            self._last_curves = curves
            self.metrics.lighting_schedules.set(len(profiles))
            log.info("schedules reloaded", extra={"channels": sorted(curves)})

    async def _tick(self, now: datetime) -> None:
        await self._expire_overrides()
        await self._reload_overrides()
        await self._reload_schedules()
        await self._sweep_silence(now)
        held = {t: HeldTarget(o.duty, o.transition) for t, o in self._held.items()}
        intents = self.scheduler.due(now, held)
        for intent in intents:
            if not self.assignments.is_adopted(intent.channel_id):
                # Not an error and not silent: the schedule is config-in-git,
                # adoption is operator state, and the two are allowed to
                # disagree — a profile for a channel nobody has adopted waits.
                # One log per channel, a metric forever, zero commands: the
                # alternative was a command_refused audit row every 5 minutes.
                self.metrics.suppressed.labels("unassigned").inc()
                if intent.channel_id not in self._suppressed_unassigned:
                    self._suppressed_unassigned.add(intent.channel_id)
                    # Forgetting the scheduler's memory of this channel is NOT
                    # done here. due() only surfaces a channel when it is
                    # cold, mid-slew, past the deadband, or past the refresh
                    # window, so a forget() tied to this loop could miss a
                    # tombstone that lands outside all four — see
                    # ControlEngine.__init__, which wires
                    # self.assignments.on_tombstone = self.scheduler.forget so
                    # forgetting happens on the tombstone event instead.
                    log.warning(
                        "channel has a schedule but no adoption; holding",
                        extra={"channel_id": intent.channel_id},
                    )
                continue
            if intent.channel_id in self._suppressed_unassigned:
                self._suppressed_unassigned.discard(intent.channel_id)
                log.info(
                    "channel adopted; scheduling resumes", extra={"channel_id": intent.channel_id}
                )
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

    slew_raw = os.environ.get("BELLASREEF_MAX_DUTY_DELTA_PER_S")
    dsn = os.environ.get("BELLASREEF_DATABASE_URL")
    # One engine, three stores. Building separate engines per store would open
    # separate connection pools to the same database for no reason.
    db = create_async_engine(dsn, future=True) if dsn else None
    if db is None:
        log.warning("no database configured; the engine will schedule nothing")

    engine = ControlEngine(
        [],
        nats_url=os.environ.get("BELLASREEF_NATS_URL"),
        liveness_timeout_s=float(os.environ.get("BELLASREEF_LIVENESS_TIMEOUT_S", "15")),
        metrics_port=int(os.environ.get("BELLASREEF_METRICS_PORT", "9102")),
        max_duty_delta_per_s=float(slew_raw) if slew_raw else None,
        override_store=OverrideStore(db) if db is not None else None,
        alert_store=PostgresAlertStore(db) if db is not None else None,
        schedule_store=ScheduleStore(db) if db is not None else None,
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
