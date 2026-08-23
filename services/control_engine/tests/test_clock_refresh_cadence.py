# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""control-engine re-evaluates clock trust at a cadence, off the event loop.

Task 4 (2026-08-23): the engine's ``_loop`` iterates at ~1 Hz and used to call
``clock_is_trusted()`` — which shells out to ``timedatectl`` — synchronously
every tick. That is a blocking subprocess landing on the same loop that
publishes heartbeats and runs ``_tick``. These tests pin down the fix: the
predicate is gated to a cadence and run in a thread, while the flip reaction
(scheduler reset, log) — covered by ``TestClockTrustGate`` in test_app.py —
is unchanged.
"""

from __future__ import annotations

import asyncio
import time as time_module
from collections.abc import Callable, Coroutine
from datetime import time as datetime_time
from typing import Any

import pytest
from bellasreef_control_engine.app import ControlEngine
from bellasreef_control_engine.profiles import ChannelProfile, RampPoint


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


def profile() -> ChannelProfile:
    return ChannelProfile(
        channel_id="led-blue",
        anchor="clock",
        points=(
            RampPoint(at=datetime_time(6), duty=0.0),
            RampPoint(at=datetime_time(18), duty=1.0),
        ),
    )


class TestEngineClockRefreshCadence:
    def test_refresh_is_gated_to_the_cadence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine = ControlEngine([profile()], metrics_port=0)
        calls = 0

        def counting() -> bool:
            nonlocal calls
            calls += 1
            return True

        monkeypatch.setattr("bellasreef_control_engine.app.clock_is_trusted", counting)

        async def scenario() -> None:
            engine._clock_refresh_due = 0.0
            await engine._refresh_clock_trust()
            assert calls == 1

            # Due was just pushed ~30s into the future; these must not
            # re-evaluate.
            await engine._refresh_clock_trust()
            await engine._refresh_clock_trust()
            assert calls == 1

        run(scenario)

    def test_refresh_does_not_block_the_event_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine = ControlEngine([profile()], metrics_port=0)

        def slow() -> bool:
            time_module.sleep(0.2)
            return True

        monkeypatch.setattr("bellasreef_control_engine.app.clock_is_trusted", slow)

        async def scenario() -> int:
            engine._clock_refresh_due = 0.0
            other_ticks = 0

            async def ticker() -> None:
                nonlocal other_ticks
                while True:
                    other_ticks += 1
                    await asyncio.sleep(0.02)

            ticker_task = asyncio.ensure_future(ticker())
            await engine._refresh_clock_trust()
            ticker_task.cancel()
            return other_ticks

        other_ticks = run(scenario)
        assert other_ticks >= 5

    def test_refresh_updates_trust_and_reacts_to_a_flip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-check that the cadence/thread rework didn't disturb the
        existing flip reaction (scheduler reset on loss of trust) —
        exercised in depth by TestClockTrustGate in test_app.py; this pins
        it specifically to the new async call path."""
        engine = ControlEngine([profile()], metrics_port=0)
        engine._clock_trusted = True
        engine._clock_was_trusted = True
        monkeypatch.setattr("bellasreef_control_engine.app.clock_is_trusted", lambda: False)

        async def scenario() -> None:
            engine._clock_refresh_due = 0.0
            await engine._refresh_clock_trust()

        run(scenario)

        assert engine._clock_trusted is False
