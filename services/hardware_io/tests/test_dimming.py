# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""``to_thread_uncancellable``, tested directly rather than through either
driver's fake bus/sysfs.

Shared by both PWM drivers (pca9685.py, pipwm.py) since the 2026-08-29
review-debt batch, so a bug here is a bug in both silicons at once — it gets
its own coverage independent of either driver.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from bellasreef_hardware_io.drivers.dimming import to_thread_uncancellable


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


def test_an_uncancelled_exception_propagates_normally() -> None:
    """No cancellation in the picture: the offloaded call's own exception
    must reach the caller untouched, exactly as an unshielded
    ``asyncio.to_thread`` call would."""

    def work() -> None:
        raise OSError("bus wedged")

    async def scenario() -> None:
        await to_thread_uncancellable(work)

    with pytest.raises(OSError, match="bus wedged"):
        run(scenario)


def test_cancellation_wins_over_a_later_raised_exception() -> None:
    """2026-08-29 review finding: a cancelled caller must see
    ``CancelledError``, never the offloaded call's own exception — even
    though that exception is real and happens to surface after the
    cancellation, once the blocking call finally returns control.

    The loop-based rewrite for repeated cancellation let
    ``await asyncio.shield(task)`` re-raise the task's own ``OSError``
    straight out of the ``while`` loop, since only ``except
    CancelledError`` was being caught there — the ``if cancelled: raise
    CancelledError`` after the loop was never reached, and the absorbed
    cancellation was silently discarded. The old pre-repeated-cancellation
    code got this right by construction (``except CancelledError: await
    gather(task, return_exceptions=True); raise``), and this test pins that
    precedence back in place.
    """
    calls = 0
    started = threading.Event()
    release = threading.Event()

    def work() -> None:
        nonlocal calls
        calls += 1
        started.set()
        release.wait()
        raise OSError("bus wedged after cancellation")

    async def scenario() -> None:
        task = asyncio.ensure_future(to_thread_uncancellable(work))
        await asyncio.to_thread(started.wait)

        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done(), (
            "the cancellation must not complete the task before the offloaded "
            "call has actually finished"
        )

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario)
    assert calls == 1, "the offloaded call must run exactly once, not be retried"


def test_the_caught_cancellederror_instance_is_reraised_not_a_fresh_one() -> None:
    """``task.cancel(msg)`` messages must survive: the exact
    ``CancelledError`` the awaiter caught is what gets raised, not a freshly
    constructed ``asyncio.CancelledError()`` with no message."""
    started = threading.Event()
    release = threading.Event()

    def work() -> None:
        started.set()
        release.wait()

    async def scenario() -> BaseException:
        task = asyncio.ensure_future(to_thread_uncancellable(work))
        await asyncio.to_thread(started.wait)

        task.cancel("bench shutdown")
        await asyncio.sleep(0.02)
        assert not task.done()

        release.set()
        try:
            await task
        except asyncio.CancelledError as exc:
            return exc
        raise AssertionError("expected CancelledError")

    exc = run(scenario)
    assert isinstance(exc, asyncio.CancelledError)
    assert exc.args and exc.args[0] == "bench shutdown", (
        f"the cancel() message was lost: {exc.args!r}"
    )
