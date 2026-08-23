# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""hardware-io re-evaluates clock trust at runtime, off the event loop.

Finding 4 (2026-08-23): trust was evaluated once at __init__ and frozen — a
power cut left every command rejected_clock until a manual restart, because
nothing ever asked ``clock_is_trusted()`` again. The loop must pick up a
change within one refresh interval, and the check itself (``timedatectl``,
possibly the adjtimex oracle underneath it) must not block the event loop
that also has to keep beating the liveness watchdog.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from bellasreef_hardware_io import app as app_module
from bellasreef_hardware_io.app import HardwareIO


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


def force_clock_refresh_due(service: HardwareIO) -> None:
    """Make the next ``_refresh_clock_trust_async()`` call actually refresh,
    bypassing the 30 s cadence gate — the way 30 s of real wall-clock time
    would, without a test sleeping for it."""
    service._clock_refresh_due = 0.0


class TestClockTrustRefresh:
    def test_hardware_io_refreshes_clock_trust(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service = HardwareIO(metrics_port=0)
        answers = iter([False, True])
        monkeypatch.setattr(app_module, "clock_is_trusted", lambda: next(answers))

        async def scenario() -> None:
            force_clock_refresh_due(service)
            await service._refresh_clock_trust_async()
            first_trusted = service._clock_trusted
            assert first_trusted is False

            force_clock_refresh_due(service)
            await service._refresh_clock_trust_async()
            second_trusted = service._clock_trusted
            assert second_trusted is True
            supervisor_trusted = service.supervisor._clock_trusted
            assert supervisor_trusted is True

        run(scenario)

    def test_refresh_is_gated_to_the_cadence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Within the 30 s window, a second call must not re-evaluate —
        otherwise the whole point (killing the per-tick blocking subprocess)
        is lost."""
        service = HardwareIO(metrics_port=0)
        calls = 0

        def counting() -> bool:
            nonlocal calls
            calls += 1
            return True

        monkeypatch.setattr(app_module, "clock_is_trusted", counting)

        async def scenario() -> None:
            force_clock_refresh_due(service)
            await service._refresh_clock_trust_async()
            assert calls == 1

            # Not due yet: due was just pushed ~30s into the future.
            await service._refresh_clock_trust_async()
            await service._refresh_clock_trust_async()
            assert calls == 1

        run(scenario)

    def test_refresh_does_not_block_the_event_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """asyncio.to_thread must actually be used — a synchronous call to a
        slow predicate on the loop itself would stall everything else the
        loop is doing (beats, sensor polling, command drain)."""
        service = HardwareIO(metrics_port=0)

        def slow() -> bool:
            time.sleep(0.2)
            return True

        monkeypatch.setattr(app_module, "clock_is_trusted", slow)

        async def scenario() -> int:
            force_clock_refresh_due(service)

            other_ticks = 0

            async def ticker() -> None:
                nonlocal other_ticks
                while True:
                    other_ticks += 1
                    await asyncio.sleep(0.02)

            ticker_task = asyncio.ensure_future(ticker())
            await service._refresh_clock_trust_async()
            ticker_task.cancel()
            return other_ticks

        other_ticks = run(scenario)
        # The slow predicate takes ~0.2s; if it blocked the loop, the
        # concurrent ticker would get 0 (or 1) iterations in.
        assert other_ticks >= 5
