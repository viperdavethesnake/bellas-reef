# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Engine service behaviour: the clock-trust gate (PRD host-facts RTC rule)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from bellasreef_contracts import (
    ActuatorCommand,
    ActuatorState,
    DeviceAssignment,
    PwmLevel,
    ScheduleDefinition,
    StateReason,
)
from bellasreef_control_engine.app import ControlEngine
from bellasreef_control_engine.profiles import ChannelProfile, RampPoint
from bellasreef_control_engine.publisher import CommandPublisher
from bellasreef_db.overrides import (
    ActiveOverride,
    OverrideStore,
    ReleasedOverride,
    ReleaseReason,
    WakeReport,
)
from bellasreef_db.schedules import ScheduleStore


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


def profile() -> ChannelProfile:
    return ChannelProfile(
        channel_id="led-blue",
        anchor="clock",
        points=(RampPoint(at=time(6), duty=0.0), RampPoint(at=time(18), duty=1.0)),
    )


def _assignment(device_id: str, *, adopted: bool) -> DeviceAssignment:
    """Replicated from tests/test_assignments.py — same shape, no cross-import."""
    kwargs: dict[str, Any] = {}
    if adopted:
        kwargs = {"driver_type": "pi-pwm", "binding": {"channel": "0"}, "role": "light"}
    return DeviceAssignment(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="api",
        device_id=device_id,
        adopted=adopted,
        **kwargs,
    )


class _FakePublisher(CommandPublisher):
    """A publisher that is always connected and records what it would send.

    Subclasses CommandPublisher rather than duck-typing so ``engine.publisher``
    stays a real ``CommandPublisher | None`` under mypy --strict, and so
    build_pwm_command (pure, no broker touch) is exercised unchanged.
    """

    def __init__(self) -> None:
        super().__init__("nats://unused:4222")
        self.published: list[ActuatorCommand] = []
        self.audits: list[tuple[str, dict[str, object]]] = []
        #: Count of heartbeat() calls that did not fail. See fail_heartbeats.
        self.heartbeats = 0
        #: When True, heartbeat() raises instead of counting — proves a
        #: failed publish is logged and does not kill the loop.
        self.fail_heartbeats = False
        #: Backs `connected` — settable so tests can simulate the spine
        #: being down (Task 3, 2026-08-23 finding 8) without a real client.
        self._connected = True
        #: One-shot exception for emit() to raise, then clear itself. See
        #: fail_emits_with.
        self._emit_exception: Exception | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def fail_emits_with(self, exc: Exception) -> None:
        self._emit_exception = exc

    async def emit(self, command: ActuatorCommand) -> None:
        if self._emit_exception is not None:
            exc, self._emit_exception = self._emit_exception, None
            raise exc
        self.published.append(command)

    async def publish_audit(self, category: str, event: dict[str, object]) -> None:
        self.audits.append((category, dict(event)))

    async def heartbeat(self, interval_s: float) -> None:
        if self.fail_heartbeats:
            raise RuntimeError("boom")
        self.heartbeats += 1


class _FakeOverrideStore(OverrideStore):
    """An OverrideStore whose active rows live in a dict, not Postgres.

    Subclasses OverrideStore (rather than duck-typing) for the same reason
    _FakePublisher above subclasses CommandPublisher: ``engine.overrides``
    stays a real ``OverrideStore | None`` under mypy --strict. Deliberately
    does not call ``super().__init__`` — it needs no ``AsyncEngine``, since
    every method Postgres-backed methods touch is overridden here.
    """

    def __init__(self) -> None:
        self.rows: dict[str, ActiveOverride] = {}
        self.released: list[tuple[UUID, ReleaseReason]] = []
        #: The store's idea of wall time. None means the real clock; a test
        #: sets it to simulate chrony stepping the clock under a running engine.
        self.wall_now: datetime | None = None
        #: One-shot method-name -> exception to raise on the *next* call to
        #: that method, then clear itself. Simulates a single dropped
        #: Postgres connection (Task 2, 2026-08-23 finding 7) without having
        #: to fail every subsequent call too.
        self._next_failure: dict[str, Exception] = {}

    def fail_next(self, method: str, exc: Exception) -> None:
        self._next_failure[method] = exc

    def _maybe_fail(self, method: str) -> None:
        exc = self._next_failure.pop(method, None)
        if exc is not None:
            raise exc

    def _clock(self, now: datetime | None) -> datetime:
        return now or self.wall_now or datetime.now(UTC)

    @staticmethod
    def _fresh(o: ActiveOverride) -> ActiveOverride:
        # The real store builds a new ActiveOverride from the row on every
        # read; it never hands back an object it returned before. Mirroring
        # that is what lets these tests see whether the engine keeps its own
        # armed deadline or takes whatever the latest read says.
        return ActiveOverride(
            id=o.id, target=o.target, duty=o.duty, expires_at=o.expires_at, transition=o.transition
        )

    async def load_active(self, *, now: datetime | None = None) -> WakeReport:
        # Same contract as the Postgres one: lapse-on-wake by *wall* clock,
        # then re-arm every survivor from that same wall clock, and report
        # what was lapsed so the engine can audit it.
        wall_now = self._clock(now)
        lapsed: list[ReleasedOverride] = []
        for target, o in list(self.rows.items()):
            if o.expires_at <= wall_now:
                self.released.append((o.id, "lapsed"))
                lapsed.append(ReleasedOverride(id=o.id, target=o.target))
                del self.rows[target]
        live: list[ActiveOverride] = []
        for o in self.rows.values():
            fresh = self._fresh(o)
            fresh.arm(wall_now=wall_now)
            live.append(fresh)
        return WakeReport(live=live, lapsed=tuple(lapsed))

    async def list_active(self) -> list[ActiveOverride]:
        # Same contract as the Postgres one: a plain read of what is unreleased,
        # touching neither the rows nor any deadline.
        self._maybe_fail("list_active")
        return [self._fresh(o) for o in self.rows.values()]

    async def release(
        self, override_id: UUID, reason: ReleaseReason, *, now: datetime | None = None
    ) -> bool:
        self._maybe_fail("release")
        self.released.append((override_id, reason))
        for target, override in list(self.rows.items()):
            if override.id == override_id:
                del self.rows[target]
                return True
        return False


class _FakeScheduleStore(ScheduleStore):
    """A ScheduleStore whose assigned curves live in a dict, not Postgres.

    Subclasses ScheduleStore (rather than duck-typing) for the same reason
    _FakeOverrideStore subclasses OverrideStore: ``engine.schedules`` stays a
    real ``ScheduleStore | None`` under mypy --strict. Deliberately does not
    call ``super().__init__`` — it needs no ``AsyncEngine``, since
    ``assigned_curves`` is the only method these tests exercise.
    """

    def __init__(self) -> None:
        self.curves: dict[str, ScheduleDefinition] = {}
        #: When True, assigned_curves raises instead of returning — simulates
        #: a flapping database.
        self.fail = False

    async def assigned_curves(self) -> dict[str, ScheduleDefinition]:
        if self.fail:
            raise RuntimeError("schedule store unavailable")
        return dict(self.curves)


@pytest.fixture
def engine_with_fake_publisher() -> tuple[ControlEngine, list[ActuatorCommand]]:
    """A ControlEngine with one channel profile ("led-blue") and a fake spine.

    Nothing is adopted by default — the whole point of the assignment gate is
    that a schedule alone is not enough.
    """
    engine = ControlEngine([profile()], metrics_port=0)
    fake = _FakePublisher()
    engine.publisher = fake
    return engine, fake.published


@pytest.fixture
def engine_with_fake_store() -> tuple[ControlEngine, list[ActuatorCommand], _FakeOverrideStore]:
    """Same shape as ``engine_with_fake_publisher``, plus a fake OverrideStore.

    Used only by TestLiveOverridePickup below, which needs overrides to flow
    through a store (not be poked directly into ``engine._held``) to prove
    the per-tick reload actually happens.
    """
    store = _FakeOverrideStore()
    engine = ControlEngine([profile()], metrics_port=0, override_store=store)
    fake = _FakePublisher()
    engine.publisher = fake
    return engine, fake.published, store


def run_loop_iterations(engine: ControlEngine, n: int) -> None:
    """Drive the real ``_loop`` for exactly ``n`` complete iterations, then
    stop it.

    Hooks ``liveness.beat()`` — called unconditionally at the very top of
    every iteration, regardless of clock trust or publisher state — to count
    iterations and request a stop once ``n`` have completed. Requesting the
    stop mid-iteration lets that iteration finish (heartbeat included) before
    ``_loop``'s own ``while not self._stopping.is_set()`` check exits it, so
    exactly ``n`` iterations run, not ``n - 1``. ``_loop_interval_s`` is
    zeroed so the inter-iteration wait resolves immediately once stopping is
    requested, and ``_assignments_loaded`` is preset True so the unrelated
    JetStream drain path (exercised by TestReconnectReDrain) is not hit here.
    """
    engine._loop_interval_s = 0.0
    engine._assignments_loaded = True
    count = 0
    real_beat = engine.liveness.beat

    def counting_beat() -> None:
        nonlocal count
        real_beat()
        count += 1
        if count >= n:
            engine.request_stop()

    engine.liveness.beat = counting_beat  # type: ignore[method-assign]
    asyncio.run(engine._loop())


def force_clock_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``clock_is_trusted`` for tests that drive the real ``_loop``.

    ``_refresh_clock_trust`` calls the real predicate on its first call (the
    30 s cadence gate starts due immediately, see ``_CLOCK_REFRESH_S``), and
    it shells out to ``timedatectl`` — absent on macOS dev shells, where it
    falls back to ``BELLASREEF_ASSUME_CLOCK_TRUSTED`` (unset here, so False).
    CI sets that env var to "1". Monkeypatching the name as ``app.py``
    imports it makes the outcome independent of the host this test happens to
    run on.
    """
    monkeypatch.setattr("bellasreef_control_engine.app.clock_is_trusted", lambda: True)


def force_clock_untrusted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same as force_clock_trusted, pinned False — see its docstring."""
    monkeypatch.setattr("bellasreef_control_engine.app.clock_is_trusted", lambda: False)


def prime_scheduler_memory(engine: ControlEngine, channel_id: str, *, duty: float) -> None:
    """Seed the scheduler's emission memory as if a prior tick had commanded
    ``duty`` on ``channel_id`` — without going through a real ``_tick``."""
    engine.scheduler._last_duty[channel_id] = duty


def prime_expired_hold(
    engine: ControlEngine, store: _FakeOverrideStore, target: str
) -> ActiveOverride:
    """Seed both the store and ``engine._held`` with an override already
    armed and past its monotonic deadline, as ``_rearm_overrides`` or a
    prior tick's ``_reload_overrides`` would have left it. Mirrors the
    deadline-forcing trick in
    ``test_expiry_bookkeeping_still_releases_and_audits_the_row``.

    Must land in ``store.rows`` too, not only ``_held``: a real release
    failure leaves the row genuinely still active in Postgres, so
    ``_reload_overrides``' next ``list_active()`` would see it. Seeding
    only ``_held`` would make that read come back empty and wipe the retry
    candidate that ``_expire_overrides`` just decided to keep — a test
    artefact, not the outage being simulated.
    """
    override = ActiveOverride(
        id=uuid4(),
        target=target,
        duty=0.5,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    override.arm(monotonic_now=-1_000_000.0, wall_now=datetime.now(UTC))
    store.rows[target] = override
    engine._held[target] = override
    return override


def prime_due_intent(engine: ControlEngine, channel_id: str, *, duty: float) -> None:
    """Make ``scheduler.due()`` surface an ``Intent`` for ``channel_id`` at
    ``duty`` on the very next ``_tick`` — adopting the channel and holding
    it via an override, the same no-profile-needed path
    ``TestHeldUnprofiledChannelPublishes`` uses."""
    engine.assignments.apply(_assignment(channel_id, adopted=True))
    engine._held[channel_id] = ActiveOverride(
        id=uuid4(),
        target=channel_id,
        duty=duty,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def scheduler_has_no_emission_recorded(engine: ControlEngine, channel_id: str) -> bool:
    """True when ``mark_emitted`` has never run for ``channel_id`` — the
    scheduler's own emission memory, checked directly rather than via
    ``due()`` so the assertion doesn't depend on a second tick."""
    return channel_id not in engine.scheduler._last_duty


def make_state(
    actuator_id: str, *, reason: StateReason, source: str = "hardware-io"
) -> ActuatorState:
    """One ActuatorState, shaped like what hardware-io actually publishes.
    The level itself is irrelevant to _on_actuator_state — only reason and
    actuator_id are — so a fixed PwmLevel(duty=0.0) stands in for all cases."""
    now = datetime.now(UTC)
    return ActuatorState(
        message_id=uuid4(),
        emitted_at=now,
        source=source,
        actuator_id=actuator_id,
        level=PwmLevel(duty=0.0),
        reason=reason,
        since=now,
    )


class TestHeartbeat:
    """`_loop` publishes the liveness beacon hardware-io's interlock
    supervisor watches for (module docstring). Silence is deliberate in
    exactly two cases: the clock is untrusted, or the loop has stalled."""

    def test_loop_publishes_heartbeat_each_tick(
        self,
        engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine, _ = engine_with_fake_publisher
        fake = engine.publisher
        assert isinstance(fake, _FakePublisher)
        force_clock_trusted(monkeypatch)

        run_loop_iterations(engine, 3)

        assert fake.heartbeats == 3

    def test_no_heartbeat_while_clock_untrusted(
        self,
        engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine, _ = engine_with_fake_publisher
        fake = engine.publisher
        assert isinstance(fake, _FakePublisher)
        force_clock_untrusted(monkeypatch)

        run_loop_iterations(engine, 3)

        assert fake.heartbeats == 0

    def test_heartbeat_publish_failure_does_not_kill_the_loop(
        self,
        engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        engine, _ = engine_with_fake_publisher
        fake = engine.publisher
        assert isinstance(fake, _FakePublisher)
        force_clock_trusted(monkeypatch)
        fake.fail_heartbeats = True

        run_loop_iterations(engine, 2)  # would raise out of _loop before the fix

        # Every attempt raised, so the counter never incremented — the proof
        # that the loop survived is that it completed both iterations anyway.
        assert fake.heartbeats == 0
        samples = [s for m in engine.metrics.loop_beats.collect() for s in m.samples]
        beats = sum(s.value for s in samples if s.name == "bellasreef_loop_beats_total")
        assert beats == 2


class TestActuatorStateForgetting:
    """Finding 6: hardware-io restarts rebuild dark, but the engine's memory
    said 0.8 — the 300s refresh then snapped a dark channel to curve duty in
    one step, bypassing the slew. An autonomous state from hardware-io means
    the scheduler's memory is no longer true."""

    def test_autonomous_safe_state_forgets_the_channel(self) -> None:
        engine = ControlEngine([profile()], metrics_port=0)
        prime_scheduler_memory(engine, "pca9685-0", duty=0.8)

        engine._on_actuator_state(make_state("pca9685-0", reason="safe_state"))

        assert "pca9685-0" not in engine.scheduler._last_duty  # cold again

    def test_startup_and_latch_states_also_forget(self) -> None:
        engine = ControlEngine([profile()], metrics_port=0)
        for reason in ("startup", "interlock_latch"):
            prime_scheduler_memory(engine, "ch", duty=0.5)
            engine._on_actuator_state(make_state("ch", reason=reason))
            assert "ch" not in engine.scheduler._last_duty

    def test_commanded_state_does_not_forget(self) -> None:
        """'commanded' states are echoes of our own publishes — forgetting on
        them would cold-start every channel on every command and defeat the
        deadband."""
        engine = ControlEngine([profile()], metrics_port=0)
        prime_scheduler_memory(engine, "ch", duty=0.5)

        engine._on_actuator_state(make_state("ch", reason="commanded"))

        assert engine.scheduler._last_duty.get("ch") == 0.5

    def test_manual_override_state_does_not_forget(self) -> None:
        """Pins the frozenset's boundary explicitly. 'manual_override' is a
        real StateReason the contract defines, but it names a person's action
        — already communicated through this engine's own override machinery
        — not something hardware-io decided autonomously; hardware-io's
        _TRIP_STATE_REASON never emits it. Forgetting on it would be wrong
        for the same reason as 'commanded': it is not evidence the scheduler's
        memory has gone stale."""
        engine = ControlEngine([profile()], metrics_port=0)
        prime_scheduler_memory(engine, "ch", duty=0.5)

        engine._on_actuator_state(make_state("ch", reason="manual_override"))

        assert engine.scheduler._last_duty.get("ch") == 0.5


class TestClockTrustGate:
    """No scheduled emission while the clock is unsynced.

    This board has no RTC battery, so after a power cut the clock is wrong
    until chrony catches up. A command's expires_at comes from that clock.
    """

    def test_no_commands_are_emitted_while_the_clock_is_untrusted(self) -> None:
        async def scenario() -> int:
            engine = ControlEngine([profile()], metrics_port=0)
            engine._clock_trusted = False
            # A tick would emit if the gate were not honoured; there is no
            # publisher, so any attempt is counted as suppressed.
            await engine._tick(datetime(2026, 6, 1, 12, tzinfo=UTC))
            return len(engine.scheduler.due(datetime(2026, 6, 1, 12, tzinfo=UTC)))

        # The scheduler still has an outstanding intent: nothing was published,
        # so nothing was marked emitted.
        assert run(scenario) == 1

    def test_health_is_503_while_the_clock_is_untrusted(self) -> None:
        engine = ControlEngine([profile()], metrics_port=0)
        engine._clock_trusted = False
        health = engine.health()
        assert health.healthy is False
        assert "clock" in health.reason

    def test_losing_clock_trust_resets_emission_history(self) -> None:
        """What was emitted against a clock we no longer believe says nothing
        about what to emit now."""
        engine = ControlEngine([profile()], metrics_port=0)
        now = datetime(2026, 6, 1, 12, tzinfo=UTC)
        intent = engine.scheduler.due(now)[0]
        engine.scheduler.mark_emitted(intent, now)
        assert engine.scheduler.due(now) == []

        # Losing trust resets history; the engine does this in
        # _refresh_clock_trust when the flag flips.
        engine.scheduler.reset()
        assert len(engine.scheduler.due(now)) == 1


class TestSpineHealthGate:
    """``health()`` (Task 3, 2026-08-23 finding 8) already consulted
    ``publisher.connected`` before this fix — the lying part was
    ``connected`` itself (it asked ``_js is not None``, which stayed True
    through a RECONNECTING window), not this gate. Fixing the property
    fixes the health report for free; these lock the existing wiring in
    with an explicit assertion instead of leaving it implicit."""

    def test_health_is_503_when_the_spine_is_not_connected(self) -> None:
        engine = ControlEngine([profile()], metrics_port=0)
        engine._clock_trusted = True
        fake = _FakePublisher()
        fake._connected = False
        engine.publisher = fake

        health = engine.health()

        assert health.healthy is False
        assert health.reason == "spine not connected"

    def test_health_is_ok_when_the_spine_is_connected(self) -> None:
        engine = ControlEngine([profile()], metrics_port=0)
        engine._clock_trusted = True
        engine.publisher = _FakePublisher()

        health = engine.health()

        assert health.healthy is True
        assert health.reason == "ok"


class TestAssignmentGate:
    """`_tick` publishes only intents whose channel is adopted (PRD spine plan)."""

    def test_unadopted_channel_is_suppressed_not_published(
        self, engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]]
    ) -> None:
        """A profile for a channel nobody adopted must produce zero commands."""
        engine, published = engine_with_fake_publisher  # profiles include "led-blue"
        asyncio.run(engine._tick(datetime.now(UTC)))
        assert published == []

    def test_adopted_channel_publishes(
        self, engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]]
    ) -> None:
        engine, published = engine_with_fake_publisher
        engine.assignments.apply(_assignment("led-blue", adopted=True))
        asyncio.run(engine._tick(datetime.now(UTC)))
        assert [c.actuator_id for c in published] == ["led-blue"]

    def test_adoption_mid_run_starts_cold_from_safe_duty(
        self, engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]]
    ) -> None:
        """Suppressed ticks must not mark_emitted: the first real command after
        adoption is the cold 'initial' intent slewing up from SAFE_DUTY, not a
        mid-ramp jump."""
        engine, published = engine_with_fake_publisher
        asyncio.run(engine._tick(datetime.now(UTC)))  # suppressed
        engine.assignments.apply(_assignment("led-blue", adopted=True))
        asyncio.run(engine._tick(datetime.now(UTC)))
        assert published[0].reason == "lighting:initial"

    def test_readoption_after_tombstone_starts_cold_not_from_stale_duty(
        self, engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]]
    ) -> None:
        """A channel that was adopted, published to, then unadopted, then
        re-adopted must cold-start again — not jump straight to the duty the
        scheduler remembers from before the tombstone.

        hardware-io rebuilds the driver dark on adoption, so a scheduler that
        still remembers the pre-tombstone duty would command a pop from 0 to
        whatever it last emitted, with no slew, the instant a channel is
        re-adopted. Timestamps are spread across the ramp (08:00 -> 08:30 ->
        14:00) so the duty genuinely moves between ticks — this is the "slow"
        path, distinct from test_tombstone_forgets_immediately_even_when_no_tick_is_due
        below, which is the same defect on a tombstone that never appears in
        any tick's due intents at all. Forgetting is driven by the tombstone
        event (AssignmentLedger.on_tombstone), not tick timing, so both are
        covered by the same fix.
        """
        engine, published = engine_with_fake_publisher
        first = datetime(2026, 6, 1, 8, tzinfo=UTC)
        second = datetime(2026, 6, 1, 8, 30, tzinfo=UTC)
        third = datetime(2026, 6, 1, 14, tzinfo=UTC)

        engine.assignments.apply(_assignment("led-blue", adopted=True))
        asyncio.run(engine._tick(first))  # cold "initial" publish
        assert published[0].reason == "lighting:initial"

        engine.assignments.apply(_assignment("led-blue", adopted=False))
        asyncio.run(engine._tick(second))  # tombstoned; suppressed

        engine.assignments.apply(_assignment("led-blue", adopted=True))
        asyncio.run(engine._tick(third))  # re-adopted, hours later on the ramp

        assert published[-1].reason == "lighting:initial"

    def test_tombstone_forgets_immediately_even_when_no_tick_is_due(
        self, engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]]
    ) -> None:
        """Reproduction from the scoped re-review of this branch's first
        pass at finding 2.

        A forget() called from inside `_tick`'s `for intent in intents:` loop
        only ever runs for a channel `due()` actually surfaces — which
        happens only when it is cold, mid-slew, past the 0.005 deadband, or
        past the 300s refresh window. Unadopt 30s after publish and re-adopt
        30s after that lands well inside both windows: the channel never
        appears in `intents` while suppressed, so a tick-scoped forget()
        never runs at all — not "late", not "throttled", *never*, until
        something else makes the channel due again. The next due tick then
        publishes a "ramp" continuation from the stale pre-tombstone duty:
        the exact pop, on a channel that was dark the whole time in between.

        Forgetting must therefore be driven by the tombstone event itself
        (AssignmentLedger.on_tombstone -> LightingScheduler.forget, wired in
        ControlEngine.__init__), which fires the moment apply() sees
        adopted=False regardless of what any tick is doing — specifically,
        it fires from the ``apply(adopted=False)`` call below, *before*
        ``_tick(unadopt_at)`` ever runs. That is what makes the channel cold
        again at ``readopt_at``: a cold intent bypasses the deadband/refresh
        gates entirely and always emits (see ``due()``), so the very next
        tick where the channel is adopted is already "the next due tick" —
        no need to wait out the 300s refresh window to see the effect.

        Under the tick-scoped forget this replaces, none of that happens:
        ``_last_duty`` is still the pre-tombstone value at ``readopt_at``,
        the delta is inside the deadband and 60s is inside the refresh
        window, so due() does not even surface the channel — nothing
        publishes at ``readopt_at`` at all, and the eventual first
        publish (whenever the schedule next drifts past the deadband or the
        refresh window elapses) is a "ramp" continuation from the stale
        duty, not "initial".
        """
        engine, published = engine_with_fake_publisher
        t0 = datetime(2026, 6, 1, 8, 0, 0, tzinfo=UTC)
        unadopt_at = datetime(2026, 6, 1, 8, 0, 30, tzinfo=UTC)
        readopt_at = datetime(2026, 6, 1, 8, 1, 0, tzinfo=UTC)

        engine.assignments.apply(_assignment("led-blue", adopted=True))
        asyncio.run(engine._tick(t0))  # cold "initial" publish
        assert published[0].reason == "lighting:initial"

        # Tombstone 30s later: well inside the scheduler's deadband (0.005)
        # and refresh window (300s). apply() fires on_tombstone here,
        # synchronously, regardless of whether the following tick finds
        # anything due.
        engine.assignments.apply(_assignment("led-blue", adopted=False))
        asyncio.run(engine._tick(unadopt_at))
        assert len(published) == 1, "30s in, well inside deadband/refresh: nothing was due"

        # Re-adopt 30s after that — still well inside both windows by the
        # clock alone. The tombstone already forgot this channel, so it is
        # cold again: this tick is the proof.
        engine.assignments.apply(_assignment("led-blue", adopted=True))
        asyncio.run(engine._tick(readopt_at))

        assert len(published) == 2
        assert published[-1].reason == "lighting:initial"


class TestHeldUnprofiledChannelPublishes:
    """An override on an adopted channel with no lighting profile — every
    channel adopted through the app, spec 2026-08-15 — must reach the
    publisher. Before Task 1's fix, ``_tick``'s ``self.scheduler.due(now,
    held)`` never surfaced such a channel at all, so the held duty was
    stored, re-armed and expired but never once commanded."""

    def test_active_override_on_unprofiled_channel_publishes(
        self, engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]]
    ) -> None:
        engine, published = engine_with_fake_publisher
        # "pi-pwm-0" is adopted but has no entry in the "led-blue" lighting
        # profile the fixture ships — exactly the adopted-but-unprofiled shape.
        engine.assignments.apply(_assignment("pi-pwm-0", adopted=True))
        engine._held["pi-pwm-0"] = ActiveOverride(
            id=uuid4(),
            target="pi-pwm-0",
            duty=0.5,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        asyncio.run(engine._tick(datetime.now(UTC)))
        assert [c.actuator_id for c in published] == ["pi-pwm-0"]
        assert published[0].level.duty == pytest.approx(0.5)  # type: ignore[union-attr]

    def test_release_of_the_hold_publishes_duty_zero(
        self, engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]]
    ) -> None:
        """Release is just another target change: the same tick machinery
        slews the synthetic channel back toward SAFE_DUTY once nothing owes
        it a duty anymore — this is what `_expire_overrides` produces once an
        override's deadline passes, exercised here without a real store."""
        engine, published = engine_with_fake_publisher
        engine.assignments.apply(_assignment("pi-pwm-0", adopted=True))
        engine._held["pi-pwm-0"] = ActiveOverride(
            id=uuid4(),
            target="pi-pwm-0",
            duty=0.5,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        asyncio.run(engine._tick(datetime.now(UTC)))
        assert published[0].level.duty == pytest.approx(0.5)  # type: ignore[union-attr]

        del engine._held["pi-pwm-0"]  # what an expiry/release leaves behind
        asyncio.run(engine._tick(datetime.now(UTC)))
        assert [c.actuator_id for c in published] == ["pi-pwm-0", "pi-pwm-0"]
        assert published[1].level.duty == pytest.approx(0.0)  # type: ignore[union-attr]

    def test_snap_hold_arrives_and_releases_in_one_tick_each(self) -> None:
        """The one seam ``_tick`` owns: turning a stored ``ActiveOverride``'s
        ``transition`` into the scheduler's ``HeldTarget``. Built directly
        rather than via ``engine_with_fake_publisher`` because that fixture
        does not expose ``max_duty_delta_per_s``, and this test needs a slew
        small enough that a *ramp* hold could not reach duty 1.0 in a single
        tick — so a snap hold arriving at 1.0 immediately, and releasing back
        to 0.0 immediately, is proof the transition survived the seam, not
        an artefact of an unconfigured slew.

        A hardcoded ``HeldTarget(o.duty, "ramp")`` in ``_tick`` would pass
        every other test in this file, because ``ActiveOverride.transition``
        defaults to ``"ramp"`` — this is the one that would catch it, both by
        the arrival duty (a 0.01/s ramp could not reach 1.0 in one tick) and
        by the release reason (only a remembered "snap" hold releases with
        reason ``"release"``; a ramp hold's release is an ordinary "converge"
        or "ramp").
        """
        engine = ControlEngine([profile()], metrics_port=0, max_duty_delta_per_s=0.01)
        fake = _FakePublisher()
        engine.publisher = fake
        published = fake.published
        # "pi-pwm-0" is adopted but has no entry in the "led-blue" lighting
        # profile — the same adopted-but-unprofiled shape as this class's
        # other tests.
        engine.assignments.apply(_assignment("pi-pwm-0", adopted=True))
        engine._held["pi-pwm-0"] = ActiveOverride(
            id=uuid4(),
            target="pi-pwm-0",
            duty=1.0,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            transition="snap",
        )
        asyncio.run(engine._tick(datetime.now(UTC)))
        assert [c.actuator_id for c in published] == ["pi-pwm-0"]
        assert published[0].level.duty == pytest.approx(1.0)  # type: ignore[union-attr]
        assert published[0].reason == "lighting:hold"

        del engine._held["pi-pwm-0"]  # what an expiry/release leaves behind
        asyncio.run(engine._tick(datetime.now(UTC)))
        assert [c.actuator_id for c in published] == ["pi-pwm-0", "pi-pwm-0"]
        assert published[1].level.duty == pytest.approx(0.0)  # type: ignore[union-attr]
        assert published[1].reason == "lighting:release"


class TestLiveOverridePickup:
    """An override created or released through the store while the engine is
    already running must be picked up on the *next tick* — not only at the
    next restart. Before this fix, ``self._held`` was populated exactly once,
    by ``_rearm_overrides`` at startup; ``_expire_overrides`` only ever
    deleted from it. A hold the API placed against a running engine was
    stored and returned 200, and never once reached ``scheduler.due``."""

    def test_an_override_created_after_start_is_commanded_on_the_next_tick(
        self,
        engine_with_fake_store: tuple[ControlEngine, list[ActuatorCommand], _FakeOverrideStore],
    ) -> None:
        engine, published, store = engine_with_fake_store
        engine.assignments.apply(_assignment("pi-pwm-0", adopted=True))

        # First tick: nothing in the store yet, matching an engine that has
        # been running for a while with no override placed.
        asyncio.run(engine._tick(datetime.now(UTC)))
        assert published == []

        # A client places an override directly against the store — exactly
        # what the API's create endpoint does against a live engine.
        store.rows["pi-pwm-0"] = ActiveOverride(
            id=uuid4(),
            target="pi-pwm-0",
            duty=0.5,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        asyncio.run(engine._tick(datetime.now(UTC)))
        assert [c.actuator_id for c in published] == ["pi-pwm-0"]
        assert published[0].level.duty == pytest.approx(0.5)  # type: ignore[union-attr]

    def test_a_release_in_the_store_stops_actuation_on_the_next_tick(
        self,
        engine_with_fake_store: tuple[ControlEngine, list[ActuatorCommand], _FakeOverrideStore],
    ) -> None:
        engine, published, store = engine_with_fake_store
        engine.assignments.apply(_assignment("pi-pwm-0", adopted=True))
        store.rows["pi-pwm-0"] = ActiveOverride(
            id=uuid4(),
            target="pi-pwm-0",
            duty=0.5,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

        asyncio.run(engine._tick(datetime.now(UTC)))
        assert published[0].level.duty == pytest.approx(0.5)  # type: ignore[union-attr]

        # Another client (a DELETE through the API) releases it directly in
        # the store — not through this engine's ``_held`` at all.
        del store.rows["pi-pwm-0"]

        asyncio.run(engine._tick(datetime.now(UTC)))
        assert [c.actuator_id for c in published] == ["pi-pwm-0", "pi-pwm-0"]
        assert published[1].level.duty == pytest.approx(0.0)  # type: ignore[union-attr]

    def test_expiry_bookkeeping_still_releases_and_audits_the_row(
        self,
        engine_with_fake_store: tuple[ControlEngine, list[ActuatorCommand], _FakeOverrideStore],
    ) -> None:
        """The monotonic-deadline expiry path (``_expire_overrides``) must
        keep doing its own job — releasing with reason 'expired' and
        recording the metric/log — rather than silently being subsumed by
        the reload's ``load_active`` call."""
        engine, _published, store = engine_with_fake_store
        engine.assignments.apply(_assignment("pi-pwm-0", adopted=True))
        override_id = uuid4()
        override = ActiveOverride(
            id=override_id,
            target="pi-pwm-0",
            duty=0.5,
            expires_at=datetime.now(UTC) + timedelta(seconds=1),
        )
        # Force the armed monotonic deadline into the past regardless of this
        # process's real uptime, so the tick below sees it as already due.
        override.arm(monotonic_now=-1_000_000.0, wall_now=datetime.now(UTC))
        store.rows["pi-pwm-0"] = override
        # Seed _held as _rearm_overrides would at startup, so the monotonic
        # deadline armed above is the one _expire_overrides evaluates.
        engine._held["pi-pwm-0"] = override

        asyncio.run(engine._tick(datetime.now(UTC)))

        samples = [s for m in engine.metrics.suppressed.collect() for s in m.samples]
        expired_total = sum(
            s.value
            for s in samples
            if s.name == "bellasreef_commands_suppressed_total"
            and s.labels.get("cause") == "override_expired"
        )
        assert expired_total == 1.0
        assert store.released == [(override_id, "expired")]
        assert "pi-pwm-0" not in engine._held
        # E1 (UX review 2026-08-17): the API audits `override.created` and the
        # manual `override.released`, and nothing audited an expiry — so the
        # log showed holds starting and never ending. Expiry is an ending the
        # engine alone knows about; it must say so, in the same event the API
        # uses for a manual release, so the log reads as one trail.
        fake = engine.publisher
        assert isinstance(fake, _FakePublisher)
        assert fake.audits == [
            (
                "command",
                {
                    "event": "override.released",
                    "override_id": str(override_id),
                    "target": "pi-pwm-0",
                    "reason": "expired",
                    "actor": "control-engine",
                },
            )
        ]

    def test_lapse_on_wake_releases_and_audits_the_row(
        self,
        engine_with_fake_store: tuple[ControlEngine, list[ActuatorCommand], _FakeOverrideStore],
    ) -> None:
        """A hold whose deadline passed while the engine was down is lapsed at
        wake and never applied (``load_active``). That is an ending too, and
        the only witness is the engine, at wake — so it audits it, reason
        'lapsed', distinct from 'expired' because the operator's thirty
        minutes were not honoured to the end."""
        engine, _published, store = engine_with_fake_store
        override_id = uuid4()
        store.rows["pi-pwm-0"] = ActiveOverride(
            id=override_id,
            target="pi-pwm-0",
            duty=0.5,
            expires_at=datetime.now(UTC) - timedelta(minutes=5),
        )

        asyncio.run(engine._rearm_overrides())

        assert store.released == [(override_id, "lapsed")]
        assert engine._held == {}
        fake = engine.publisher
        assert isinstance(fake, _FakePublisher)
        assert fake.audits == [
            (
                "command",
                {
                    "event": "override.released",
                    "override_id": str(override_id),
                    "target": "pi-pwm-0",
                    "reason": "lapsed",
                    "actor": "control-engine",
                },
            )
        ]

    def test_a_wall_clock_step_does_not_shorten_a_held_override(
        self,
        engine_with_fake_store: tuple[ControlEngine, list[ActuatorCommand], _FakeOverrideStore],
    ) -> None:
        """The per-tick reload must not re-couple a running override to the
        wall clock. ``bellasreef_db.overrides`` promises that *within a run*
        the deadline is monotonic: chrony stepping the clock (this board has
        no RTC battery) can neither shorten nor lengthen a hold the operator
        already placed. ``load_active`` lapses by wall clock and re-arms from
        wall clock on every call — correct at wake, wrong every tick. So the
        reload has to read without lapsing and keep the object (and armed
        monotonic deadline) it is already watching for an unchanged id."""
        engine, published, store = engine_with_fake_store
        engine.assignments.apply(_assignment("pi-pwm-0", adopted=True))
        placed = datetime.now(UTC)
        override_id = uuid4()
        store.rows["pi-pwm-0"] = ActiveOverride(
            id=override_id,
            target="pi-pwm-0",
            duty=0.5,
            expires_at=placed + timedelta(hours=1),
        )

        asyncio.run(engine._tick(placed))
        assert published[-1].level.duty == pytest.approx(0.5)  # type: ignore[union-attr]
        armed = engine._held["pi-pwm-0"]
        assert armed.monotonic_deadline is not None

        # chrony steps the clock two hours forward under the running engine.
        # Monotonic time has barely moved: the operator's hour is still owed.
        store.wall_now = placed + timedelta(hours=2)
        asyncio.run(engine._tick(store.wall_now))

        assert engine._held["pi-pwm-0"] is armed  # same object, same deadline
        assert store.released == []
        assert "pi-pwm-0" in store.rows
        assert published[-1].level.duty == pytest.approx(0.5)  # type: ignore[union-attr]

    def test_a_superseding_override_on_the_same_target_is_re_armed(
        self,
        engine_with_fake_store: tuple[ControlEngine, list[ActuatorCommand], _FakeOverrideStore],
    ) -> None:
        """Keeping the held object is keyed on the override *id*, not the
        target: a new row on the same target (the API superseded the old
        one) is a different hold with its own deadline and must be armed
        afresh — and its duty is what gets commanded."""
        engine, published, store = engine_with_fake_store
        engine.assignments.apply(_assignment("pi-pwm-0", adopted=True))
        store.rows["pi-pwm-0"] = ActiveOverride(
            id=uuid4(),
            target="pi-pwm-0",
            duty=0.5,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        asyncio.run(engine._tick(datetime.now(UTC)))
        first = engine._held["pi-pwm-0"]

        replacement = uuid4()
        store.rows["pi-pwm-0"] = ActiveOverride(
            id=replacement,
            target="pi-pwm-0",
            duty=0.8,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        asyncio.run(engine._tick(datetime.now(UTC)))

        held = engine._held["pi-pwm-0"]
        assert held is not first
        assert held.id == replacement
        assert held.monotonic_deadline is not None
        assert published[-1].level.duty == pytest.approx(0.8)  # type: ignore[union-attr]

    def test_overrides_none_drill_mode_ticks_without_crashing_or_commanding(self) -> None:
        """No OverrideStore configured (drill mode) must not crash the reload
        step, and must not conjure commands out of nowhere."""
        engine = ControlEngine([profile()], metrics_port=0)
        fake = _FakePublisher()
        engine.publisher = fake
        engine.assignments.apply(_assignment("pi-pwm-0", adopted=True))
        assert engine.overrides is None

        asyncio.run(engine._tick(datetime.now(UTC)))

        assert fake.published == []


class TestOverrideStoreOutage:
    """`_reload_overrides` and `_expire_overrides` (Task 2, 2026-08-23 finding
    7): one dropped Postgres connection in either the per-tick read
    (``list_active``) or a release must not unwind `_tick` -> `_loop` ->
    `run()` and crash-loop the engine — the same contract `_reload_schedules`
    already had (see TestScheduleReload below)."""

    def test_tick_survives_a_list_active_outage(
        self,
        engine_with_fake_store: tuple[ControlEngine, list[ActuatorCommand], _FakeOverrideStore],
    ) -> None:
        """One dropped connection out of ``list_active()`` used to raise
        straight out of ``_reload_overrides``, stripping all lighting control
        for the length of the flap."""
        engine, _published, store = engine_with_fake_store
        store.fail_next("list_active", ConnectionError("server closed the connection"))

        asyncio.run(engine._tick(datetime.now(UTC)))  # must not raise

        samples = [s for m in engine.metrics.override_io_errors.collect() for s in m.samples]
        error_total = sum(
            s.value for s in samples if s.name == "bellasreef_override_io_errors_total"
        )
        assert error_total == 1.0

    def test_a_failed_release_keeps_the_hold_for_retry_not_a_leak(
        self,
        engine_with_fake_store: tuple[ControlEngine, list[ActuatorCommand], _FakeOverrideStore],
    ) -> None:
        """A release() that raises must not drop the entry from ``_held``:
        the monotonic deadline is already past, so deleting it here would
        leave the row open in Postgres with nobody retrying — the entry
        must stay held and be retried on the very next tick."""
        engine, _published, store = engine_with_fake_store
        override = prime_expired_hold(engine, store, "pca9685-0")
        store.fail_next("release", ConnectionError("server closed the connection"))

        asyncio.run(engine._tick(datetime.now(UTC)))  # must not raise

        assert "pca9685-0" in engine._held  # not silently dropped on a failed release
        assert store.released == []
        samples = [s for m in engine.metrics.override_io_errors.collect() for s in m.samples]
        error_total = sum(
            s.value for s in samples if s.name == "bellasreef_override_io_errors_total"
        )
        assert error_total == 1.0

        # The store recovers: the same expired entry retries cleanly on the
        # next tick and is finally released, proving this is a retry, not a
        # permanent leak of the "expired but never closed" kind.
        asyncio.run(engine._tick(datetime.now(UTC)))
        assert "pca9685-0" not in engine._held
        assert store.released == [(override.id, "expired")]

    def test_tick_survives_both_outages_in_sequence(
        self,
        engine_with_fake_store: tuple[ControlEngine, list[ActuatorCommand], _FakeOverrideStore],
    ) -> None:
        """The exact scenario from the finding: a read outage on one tick,
        then a release outage (with an already-expired hold in play) on the
        next — neither may raise, and the second must retain the hold."""
        engine, _published, store = engine_with_fake_store
        store.fail_next("list_active", ConnectionError("server closed the connection"))
        asyncio.run(engine._tick(datetime.now(UTC)))  # must not raise

        store.fail_next("release", ConnectionError("server closed the connection"))
        prime_expired_hold(engine, store, "pca9685-0")
        asyncio.run(engine._tick(datetime.now(UTC)))  # must not raise; hold stays for retry

        assert "pca9685-0" in engine._held  # not silently dropped on a failed release

    def test_warns_once_per_outage_not_once_per_tick(
        self,
        engine_with_fake_store: tuple[ControlEngine, list[ActuatorCommand], _FakeOverrideStore],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Mirrors ``TestScheduleReload``'s dedup check for
        ``_schedule_read_failing``: a store that stays down across several
        ticks logs the warning once, at the start of the outage, not on
        every tick it stays down."""
        engine, _published, store = engine_with_fake_store

        with caplog.at_level(logging.WARNING, logger="bellasreef_control_engine.app"):
            for _ in range(3):
                # fail_next is one-shot; re-arming it before every tick
                # simulates a store that stays down across the whole outage,
                # the same as a real dropped connection would.
                store.fail_next("list_active", ConnectionError("server closed the connection"))
                asyncio.run(engine._tick(datetime.now(UTC)))

        warnings = [r for r in caplog.records if "override reload failed" in r.message]
        assert len(warnings) == 1  # one log per outage, not per failing tick


class TestPublishFailureSuppression:
    """`_publish` (Task 3, 2026-08-23 finding 8): a PubAck timeout during a
    NATS restart used to unwind `_tick` and crash-loop the engine, with
    `health()` reporting "spine ok" the whole window because `connected`
    only checked the handle, not the client (see TestSpineHealthGate and
    test_publisher.py's TestConnectedProperty for that half of the fix)."""

    def test_publish_failure_suppresses_instead_of_killing(
        self, engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]]
    ) -> None:
        engine, published = engine_with_fake_publisher
        fake = engine.publisher
        assert isinstance(fake, _FakePublisher)
        fake.fail_emits_with(TimeoutError("nats: request timeout"))
        prime_due_intent(engine, "pca9685-0", duty=0.5)

        asyncio.run(engine._tick(datetime.now(UTC)))  # must not raise

        assert published == []  # nothing landed
        assert scheduler_has_no_emission_recorded(engine, "pca9685-0")  # mark_emitted skipped

        samples = [s for m in engine.metrics.suppressed.collect() for s in m.samples]
        suppressed_total = sum(
            s.value
            for s in samples
            if s.name == "bellasreef_commands_suppressed_total"
            and s.labels.get("cause") == "publish_failed"
        )
        assert suppressed_total == 1.0

    def test_publish_recovers_on_the_next_tick(
        self, engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]]
    ) -> None:
        """The suppression is a retry, not a permanent drop: once the broker
        answers again, the very next tick's intent is still due (mark_emitted
        never ran) and lands normally."""
        engine, published = engine_with_fake_publisher
        fake = engine.publisher
        assert isinstance(fake, _FakePublisher)
        fake.fail_emits_with(TimeoutError("nats: request timeout"))
        prime_due_intent(engine, "pca9685-0", duty=0.5)

        asyncio.run(engine._tick(datetime.now(UTC)))
        assert published == []

        asyncio.run(engine._tick(datetime.now(UTC)))
        assert [c.actuator_id for c in published] == ["pca9685-0"]
        assert published[0].level.duty == pytest.approx(0.5)  # type: ignore[union-attr]


class TestReconnectReDrain:
    """A NATS reconnect can miss core-subject messages (e.g. a tombstone) sent
    during the gap. `_wire_reconnect_handling` points the publisher's
    `on_reconnected` at `_on_reconnected`, which flips `_assignments_loaded`
    back to False so `_loop`'s existing retry re-drains JetStream — which
    still has whatever was missed.
    """

    def test_wiring_points_the_publisher_at_on_reconnected(
        self, engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]]
    ) -> None:
        engine, _ = engine_with_fake_publisher
        assert engine.publisher is not None

        engine._wire_reconnect_handling()

        # Bound-method identity is not stable across attribute reads, so this
        # proves the wiring by effect rather than by inspecting the callable:
        # invoking whatever got wired must produce exactly _on_reconnected's
        # effect. test_reconnect_makes_the_next_loop_iteration_redrain below
        # proves the far end of the same wiring, through the real loop.
        engine._assignments_loaded = True
        assert engine.publisher.on_reconnected is not None
        engine.publisher.on_reconnected()
        assert engine._assignments_loaded is False

    def test_on_reconnected_flips_assignments_loaded_false(
        self, engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]]
    ) -> None:
        engine, _ = engine_with_fake_publisher
        engine._assignments_loaded = True

        engine._on_reconnected()

        assert engine._assignments_loaded is False

    def test_wiring_is_a_no_op_with_no_spine(self) -> None:
        """An engine with no publisher (no BELLASREEF_NATS_URL) must not blow
        up when run() calls this unconditionally-safe wiring step."""
        engine = ControlEngine([profile()], metrics_port=0)
        assert engine.publisher is None
        engine._wire_reconnect_handling()  # must not raise

    def test_reconnect_makes_the_next_loop_iteration_redrain(
        self, engine_with_fake_publisher: tuple[ControlEngine, list[ActuatorCommand]]
    ) -> None:
        """End-to-end through the real `_loop`, not a re-statement of its
        condition: after a reconnect, one iteration must call
        load_assignments again and pick up whatever it returns."""
        engine, _ = engine_with_fake_publisher
        assert engine.publisher is not None
        engine._loop_interval_s = 0.0
        engine._assignments_loaded = True
        engine._clock_trusted = False  # keep the iteration to just the redrain check

        redrain_calls = 0

        async def fake_load_assignments(ledger: object) -> bool:
            nonlocal redrain_calls
            redrain_calls += 1
            engine.request_stop()  # stop after the one iteration under test
            return True

        engine.publisher.load_assignments = fake_load_assignments  # type: ignore[method-assign]
        engine._wire_reconnect_handling()

        # Simulates what nats.py does: fires the callback it was handed.
        engine.publisher.on_reconnected()  # type: ignore[misc]
        assert engine._assignments_loaded is False

        asyncio.run(engine._loop())

        assert redrain_calls == 1
        assert engine._assignments_loaded is True


class TestScheduleReload:
    """`_reload_schedules` (Task 7): the engine re-reads Postgres every tick,
    so an edit the API made is live within one tick with no push channel to
    desync — the archive's schedules died of exactly that."""

    def test_schedule_edit_is_live_within_a_tick(self) -> None:
        store = _FakeScheduleStore()
        curve_a = ScheduleDefinition(
            name="a",
            points=(RampPoint(at=time(0, 0), duty=0.3), RampPoint(at=time(12, 0), duty=0.3)),
        )
        curve_b = ScheduleDefinition(
            name="b",
            points=(RampPoint(at=time(0, 0), duty=0.7), RampPoint(at=time(12, 0), duty=0.7)),
        )
        store.curves = {"pi-pwm-0": curve_a}
        engine = ControlEngine([], metrics_port=0, schedule_store=store)
        fake = _FakePublisher()
        engine.publisher = fake
        engine.assignments.apply(_assignment("pi-pwm-0", adopted=True))

        asyncio.run(engine._tick(datetime(2026, 6, 1, 6, tzinfo=UTC)))
        assert fake.published[-1].level.duty == pytest.approx(0.3)  # type: ignore[union-attr]

        # The API edits the assignment against Postgres, mid-run — not
        # through anything on this engine object.
        store.curves = {"pi-pwm-0": curve_b}
        asyncio.run(engine._tick(datetime(2026, 6, 1, 6, 1, tzinfo=UTC)))
        assert fake.published[-1].level.duty == pytest.approx(0.7)  # type: ignore[union-attr]

    def test_schedule_store_error_keeps_last_good_set(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = _FakeScheduleStore()
        curve_a = ScheduleDefinition(
            name="a",
            points=(RampPoint(at=time(0, 0), duty=0.0), RampPoint(at=time(12, 0), duty=1.0)),
        )
        store.curves = {"pi-pwm-0": curve_a}
        engine = ControlEngine([], metrics_port=0, schedule_store=store)
        fake = _FakePublisher()
        engine.publisher = fake
        engine.assignments.apply(_assignment("pi-pwm-0", adopted=True))

        asyncio.run(engine._tick(datetime(2026, 6, 1, 6, tzinfo=UTC)))
        assert fake.published[-1].level.duty == pytest.approx(0.5)  # type: ignore[union-attr]

        store.fail = True
        with caplog.at_level(logging.WARNING, logger="bellasreef_control_engine.app"):
            asyncio.run(engine._tick(datetime(2026, 6, 1, 9, tzinfo=UTC)))
            asyncio.run(engine._tick(datetime(2026, 6, 1, 12, tzinfo=UTC)))

        # Kept the last good curve throughout the outage: the duty at 12:00
        # follows curve_a's own ramp, not some fallback or frozen value — the
        # schedule keeps ticking, only the store read is broken.
        assert fake.published[-1].level.duty == pytest.approx(1.0)  # type: ignore[union-attr]

        samples = [s for m in engine.metrics.schedule_reload_errors.collect() for s in m.samples]
        error_total = sum(
            s.value for s in samples if s.name == "bellasreef_schedule_reload_errors_total"
        )
        assert error_total == 2.0

        warnings = [r for r in caplog.records if "schedule reload failed" in r.message]
        assert len(warnings) == 1  # one log per outage, not per failing tick

    def test_malformed_channel_id_row_keeps_last_good_set(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Whole-branch review finding: `ChannelProfile.from_definition` used
        to live outside `_reload_schedules`' try/except, so a row whose
        channel_id violates ChannelProfile's pattern (predating the API's
        write-time check, or written by anything else that talks to this
        table) raised straight out of the tick loop and crash-looped the
        engine. It must now degrade exactly like a store read failure: keep
        the last good profile set, count it, warn once per outage."""
        store = _FakeScheduleStore()
        good = ScheduleDefinition(
            name="a",
            points=(RampPoint(at=time(0, 0), duty=0.4), RampPoint(at=time(12, 0), duty=0.4)),
        )
        store.curves = {"pi-pwm-0": good}
        engine = ControlEngine([], metrics_port=0, schedule_store=store)
        fake = _FakePublisher()
        engine.publisher = fake
        engine.assignments.apply(_assignment("pi-pwm-0", adopted=True))

        asyncio.run(engine._tick(datetime(2026, 6, 1, 6, tzinfo=UTC)))
        assert fake.published[-1].level.duty == pytest.approx(0.4)  # type: ignore[union-attr]

        # A row with a pattern-violating channel_id appears in the store.
        # The API's write-time check (assign_schedule) should have stopped
        # this from ever landing, but the engine must survive it regardless.
        store.curves = {"LED-Blue": good}
        with caplog.at_level(logging.WARNING, logger="bellasreef_control_engine.app"):
            asyncio.run(engine._tick(datetime(2026, 6, 1, 9, tzinfo=UTC)))
            asyncio.run(engine._tick(datetime(2026, 6, 1, 12, tzinfo=UTC)))

        # Kept the last good schedule set throughout: pi-pwm-0 is still
        # commanded from the good curve (a flat 0.4), not dropped and not
        # crashed by the bad row.
        assert fake.published[-1].level.duty == pytest.approx(0.4)  # type: ignore[union-attr]

        samples = [s for m in engine.metrics.schedule_reload_errors.collect() for s in m.samples]
        error_total = sum(
            s.value for s in samples if s.name == "bellasreef_schedule_reload_errors_total"
        )
        assert error_total == 2.0

        warnings = [r for r in caplog.records if "schedule reload failed" in r.message]
        assert len(warnings) == 1  # one log per outage, not per failing tick

    def test_no_store_means_no_schedules_and_no_crash(self) -> None:
        """schedule_store=None (db-less dev/drill mode) must not crash the
        reload step, and must not conjure schedules out of nowhere."""
        engine = ControlEngine([], metrics_port=0)
        fake = _FakePublisher()
        engine.publisher = fake
        engine.assignments.apply(_assignment("pi-pwm-0", adopted=True))
        assert engine.schedules is None

        asyncio.run(engine._tick(datetime.now(UTC)))

        assert fake.published == []

    def test_unassign_walks_channel_dark(self) -> None:
        store = _FakeScheduleStore()
        curve = ScheduleDefinition(
            name="a",
            points=(RampPoint(at=time(0, 0), duty=0.6), RampPoint(at=time(12, 0), duty=0.6)),
        )
        store.curves = {"pi-pwm-0": curve}
        engine = ControlEngine([], metrics_port=0, schedule_store=store, max_duty_delta_per_s=0.05)
        fake = _FakePublisher()
        engine.publisher = fake
        engine.assignments.apply(_assignment("pi-pwm-0", adopted=True))

        t0 = datetime(2026, 6, 1, 6, 0, 0, tzinfo=UTC)
        asyncio.run(engine._tick(t0))  # cold; slew starts from SAFE_DUTY
        asyncio.run(engine._tick(t0 + timedelta(seconds=15)))  # settles at 0.6
        assert fake.published[-1].level.duty == pytest.approx(0.6)  # type: ignore[union-attr]

        # The API unassigns the schedule against Postgres.
        store.curves = {}

        asyncio.run(engine._tick(t0 + timedelta(seconds=20)))  # partial converge toward 0
        partial = fake.published[-1].level.duty  # type: ignore[union-attr]
        assert 0.0 < partial < 0.6

        asyncio.run(engine._tick(t0 + timedelta(seconds=40)))  # fully dark
        assert fake.published[-1].level.duty == pytest.approx(0.0)  # type: ignore[union-attr]
        settled_count = len(fake.published)

        # Converged and resting at SAFE_DUTY: the channel goes quiet, no
        # further commands.
        asyncio.run(engine._tick(t0 + timedelta(seconds=41)))
        assert len(fake.published) == settled_count
