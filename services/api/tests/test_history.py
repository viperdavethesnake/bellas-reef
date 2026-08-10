# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Range queries and envelope-preserving downsampling.

The two properties under test are the ones a chart cannot reveal once they are
wrong: a spike that survives every zoom level, and a gap that stays a gap.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from bellasreef_api.history import MAX_BUCKETS, HistoryReader

_VM = "BELLASREEF_TEST_VM_URL"

pytestmark = pytest.mark.skipif(not os.environ.get(_VM), reason=f"{_VM} not set")


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


async def write(vm: str, device_id: str, samples: list[tuple[datetime, float]]) -> None:
    lines = [
        json.dumps(
            {
                "metric": {"__name__": "bellasreef_sensor_reading", "device_id": device_id},
                "values": [value],
                "timestamps": [int(at.timestamp() * 1000)],
            }
        )
        for at, value in samples
    ]
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            f"{vm}/api/v1/import",
            content="\n".join(lines).encode(),
            headers={"Content-Type": "application/x-ndjson"},
        )
        r.raise_for_status()


async def wait_until_stored(vm: str, device_id: str, expected: int) -> None:
    """Export, not query: an instant query hides the newest 30s behind
    `-search.latencyOffset`, which reads exactly like a failed write."""
    async with httpx.AsyncClient(timeout=20) as c:
        for _ in range(40):
            r = await c.get(
                f"{vm}/api/v1/export",
                params={"match[]": f'bellasreef_sensor_reading{{device_id="{device_id}"}}'},
            )
            body = r.text.strip()
            if body:
                stored = sum(len(json.loads(line)["values"]) for line in body.splitlines())
                if stored >= expected:
                    return
            await asyncio.sleep(0.5)
    raise AssertionError("samples never became readable")


class TestBucketSizing:
    def test_a_window_divides_into_the_requested_buckets(self) -> None:
        start = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
        assert HistoryReader.bucket_seconds(start, start + timedelta(hours=1), 60) == 60

    def test_the_bucket_count_is_capped(self) -> None:
        """Server-side downsampling is the point. A client asking for a million
        buckets is asking for the raw samples the cap exists to prevent."""
        start = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
        step = HistoryReader.bucket_seconds(start, start + timedelta(days=1), 10_000_000)
        assert step >= 86_400 // MAX_BUCKETS

    def test_a_degenerate_window_still_yields_a_positive_step(self) -> None:
        """VictoriaMetrics rejects a zero step, and a zero-length window is a
        client bug that must not become a 500."""
        at = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
        assert HistoryReader.bucket_seconds(at, at, 240) >= 1


class TestEnvelope:
    @pytest.mark.timeout(120)
    def test_a_spike_inside_a_bucket_survives_downsampling(self) -> None:
        """The property the whole module exists for.

        A 30-second excursion inside a 10-minute bucket vanishes under a plain
        average — and an alert episode raised on that excursion would then band
        a curve that never appears to breach. `maximum` has to carry it.
        """

        async def scenario() -> tuple[float, float, float]:
            vm = os.environ[_VM].rstrip("/")
            device = f"probe-{uuid.uuid4().hex[:8]}"
            base = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)

            samples = [(base + timedelta(seconds=30 * i), 24.0) for i in range(40)]
            # One excursion, well inside a single bucket.
            samples[20] = (samples[20][0], 31.5)
            await write(vm, device, samples)
            await wait_until_stored(vm, device, len(samples))

            reader = HistoryReader(vm)
            series = await reader.series(
                metric="bellasreef_sensor_reading",
                device_id=device,
                unit="degC",
                start=base,
                end=base + timedelta(minutes=20),
                buckets=2,
            )
            assert series.buckets, "no buckets returned"
            return (
                max(b.maximum for b in series.buckets),
                max(b.average for b in series.buckets),
                min(b.minimum for b in series.buckets),
            )

        peak, mean, floor = run(scenario)
        assert peak == pytest.approx(31.5, abs=0.01), "the spike was averaged away"
        assert mean < 31.5, "avg should not equal the peak, or nothing was aggregated"
        assert floor == pytest.approx(24.0, abs=0.01)


class TestGaps:
    @pytest.mark.timeout(120)
    def test_a_window_with_no_samples_produces_no_buckets(self) -> None:
        """Absent, not zero-filled.

        BR_STATE is retained last-value-per-subject, so duty genuinely has holes
        whenever the writer was down. A zero here would draw the light as off;
        an interpolation would draw it as steady. Both are assertions nothing
        measured.
        """

        async def scenario() -> int:
            vm = os.environ[_VM].rstrip("/")
            reader = HistoryReader(vm)
            far_past = datetime(2001, 1, 1, tzinfo=UTC)
            series = await reader.series(
                metric="bellasreef_sensor_reading",
                device_id=f"nothing-{uuid.uuid4().hex[:8]}",
                unit="degC",
                start=far_past,
                end=far_past + timedelta(hours=1),
                buckets=60,
            )
            return len(series.buckets)

        assert run(scenario) == 0

    @pytest.mark.timeout(180)
    def test_a_hole_between_samples_is_left_as_a_hole(self) -> None:
        """Two clusters either side of a gap: the buckets between them must be
        missing entirely, so the client can break the line there."""

        async def scenario() -> tuple[int, int]:
            vm = os.environ[_VM].rstrip("/")
            device = f"probe-{uuid.uuid4().hex[:8]}"
            base = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)

            early = [(base + timedelta(minutes=i), 24.0) for i in range(5)]
            late = [(base + timedelta(minutes=55 + i), 25.0) for i in range(5)]
            await write(vm, device, early + late)
            await wait_until_stored(vm, device, 10)

            series = await HistoryReader(vm).series(
                metric="bellasreef_sensor_reading",
                device_id=device,
                unit="degC",
                start=base,
                end=base + timedelta(minutes=60),
                buckets=60,
            )
            return len(series.buckets), 60

        filled, requested = run(scenario)
        assert 0 < filled < requested, (
            f"{filled} of {requested} buckets filled — a gap was zero-filled or "
            "interpolated rather than left absent"
        )
