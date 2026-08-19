# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Range queries over VictoriaMetrics, downsampled without hiding anything.

Two properties this module exists to guarantee, both of which are easy to break
by accident and impossible to notice afterwards from the chart alone.

**The envelope survives every zoom.** A naive downsample takes one value per
bucket — usually the average — and a 30-second excursion inside a 10-minute
bucket disappears. That is not a cosmetic loss on a reef controller: the spike
*is* the event, and the alert episode recorded next to it would then sit over a
curve that appears never to have breached. Every bucket therefore carries
``min``, ``avg`` and ``max``, and the client draws the min/max band as well as
the mean.

**A gap is a gap.** Buckets with no samples are absent from the response, never
zero-filled and never interpolated. `BR_STATE` is retained last-value-per-
subject, so actuator duty genuinely has holes whenever the telemetry writer was
down; drawing a line across one would assert continuity that nothing measured.
The client breaks the line instead.

**A step signal is bucketed as a step signal.** Actuator duty is published on
change, not on a cadence: a light held at 0.3 since yesterday has no sample in
the last hour and is nonetheless at 0.3 for all of it. Sampled-signal rollups
returned nothing for that hour, and gave a one-minute hold in a 45-minute bucket
a sample-average that said 47.8 % — how bright, not how brief. :func:`step_buckets`
holds each value until the next change and takes the time-weighted min/avg/max
inside every bucket (H1/H2, 2026-08-18). The rule above still stands where it
applies: a bucket *before the first sample ever recorded* is absent, because
"off" would be an invention there.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from bellasreef_service import get_logger

log = get_logger(__name__)

__all__ = ["Bucket", "HistoryReader", "Series", "step_buckets"]

#: How far before a window a step series looks for the value it enters the
#: window holding. A light's last change can be days old; thirty days is well
#: past any hold and still a handful of samples to read.
STEP_LOOKBACK_S = 30 * 86_400

#: Ceiling on buckets per series. A phone rendering a 44pt-tall sparkline cannot
#: use more resolution than this, and the point of downsampling server-side is
#: that 20k raw samples never cross the network.
MAX_BUCKETS = 1000
DEFAULT_BUCKETS = 240


@dataclass(frozen=True, slots=True)
class Bucket:
    """One downsampled interval. ``avg`` is the line; ``min``/``max`` the band."""

    at: datetime
    minimum: float
    average: float
    maximum: float


@dataclass(frozen=True, slots=True)
class Series:
    device_id: str
    metric: str
    unit: str
    buckets: list[Bucket]


class HistoryReader:
    """Reads VictoriaMetrics. Owns no state beyond its HTTP client."""

    def __init__(self, vm_url: str) -> None:
        self._vm_url = vm_url.rstrip("/")

    @staticmethod
    def bucket_seconds(start: datetime, end: datetime, buckets: int) -> int:
        """Seconds per bucket for the requested window and resolution.

        At least one second: VictoriaMetrics rejects a zero step, and a window
        shorter than the bucket count cannot be resolved more finely than the
        clock anyway.
        """
        span = max((end - start).total_seconds(), 1.0)
        return max(1, math.ceil(span / max(1, min(buckets, MAX_BUCKETS))))

    async def series(
        self,
        *,
        metric: str,
        device_id: str,
        unit: str,
        start: datetime,
        end: datetime,
        buckets: int = DEFAULT_BUCKETS,
    ) -> Series:
        """One series, downsampled to `buckets` intervals with its envelope.

        Three `*_over_time` range queries rather than one. VictoriaMetrics will
        happily return a single aggregate, but the whole point is that the
        aggregate is what loses the spike — so min, avg and max are fetched and
        aligned on the same step, and a bucket exists only where all three do.
        """
        step = self.bucket_seconds(start, end, buckets)
        selector = f'{metric}{{device_id="{device_id}"}}'

        async with httpx.AsyncClient(timeout=20.0) as client:
            results = {}
            for name, fn in (
                ("minimum", "min_over_time"),
                ("average", "avg_over_time"),
                ("maximum", "max_over_time"),
            ):
                response = await client.get(
                    f"{self._vm_url}/api/v1/query_range",
                    params={
                        "query": f"{fn}({selector}[{step}s])",
                        "start": start.timestamp(),
                        "end": end.timestamp(),
                        "step": f"{step}s",
                    },
                )
                response.raise_for_status()
                results[name] = _points(response.json())

        # Intersection, not union. A bucket missing from any of the three is a
        # bucket with no samples; inventing the absent value would be the
        # interpolation this module refuses to do.
        common = set(results["minimum"]) & set(results["average"]) & set(results["maximum"])
        ordered = sorted(common)
        return Series(
            device_id=device_id,
            metric=metric,
            unit=unit,
            buckets=[
                Bucket(
                    at=datetime.fromtimestamp(ts, tz=start.tzinfo),
                    minimum=results["minimum"][ts],
                    average=results["average"][ts],
                    maximum=results["maximum"][ts],
                )
                for ts in ordered
            ],
        )

    async def step_series(
        self,
        *,
        metric: str,
        device_id: str,
        unit: str,
        start: datetime,
        end: datetime,
        buckets: int = DEFAULT_BUCKETS,
    ) -> Series:
        """One piecewise-constant series, bucketed by :func:`step_buckets`.

        Raw samples via ``/api/v1/export`` from ``STEP_LOOKBACK_S`` before the
        window: the value the window opens on is the last change before it,
        which for a light can be days old. Every label variant of the metric
        (the ``reason`` a state was published with) is merged — they are one
        signal, split only by why each value was set.
        """
        step = self.bucket_seconds(start, end, buckets)
        selector = f'{metric}{{device_id="{device_id}"}}'
        lookback_start = start - timedelta(seconds=STEP_LOOKBACK_S)
        samples: list[tuple[datetime, float]] = []
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                f"{self._vm_url}/api/v1/export",
                params={
                    "match[]": selector,
                    "start": lookback_start.timestamp(),
                    "end": end.timestamp(),
                },
            )
            response.raise_for_status()
            for line in response.text.splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                for ts_ms, raw in zip(
                    entry.get("timestamps", []), entry.get("values", []), strict=True
                ):
                    try:
                        value = float(raw)
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(value):
                        continue
                    samples.append((datetime.fromtimestamp(ts_ms / 1000, tz=start.tzinfo), value))
        return Series(
            device_id=device_id,
            metric=metric,
            unit=unit,
            buckets=step_buckets(samples, start=start, end=end, step_s=step),
        )


def _points(payload: dict[str, object]) -> dict[float, float]:
    """`{timestamp: value}` from a query_range response.

    Non-finite values are dropped rather than passed through: VictoriaMetrics
    encodes staleness as NaN, and a NaN reaching a chart renders as a break in
    some clients and as zero in others. Dropping it makes the bucket absent,
    which is the one interpretation that is true.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    result = data.get("result")
    if not isinstance(result, list) or not result:
        return {}

    points: dict[float, float] = {}
    for entry in result:
        for timestamp, raw in entry.get("values", []):
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                points[float(timestamp)] = value
    return points


def step_buckets(
    samples: list[tuple[datetime, float]], *, start: datetime, end: datetime, step_s: int
) -> list[Bucket]:
    """Time-weighted min/avg/max per bucket of a piecewise-constant signal.

    ``samples`` may reach back before ``start`` — that is where the value the
    window opens on comes from — and need not be sorted. Two samples at one
    instant keep the last (three ``reason`` label variants land as separate
    series in VictoriaMetrics; merged, they can collide on a timestamp).

    A bucket has a value only from the first sample onward: before a device
    ever reported, its state is unknown, not zero.
    """
    if not samples or end <= start:
        return []
    ordered: dict[datetime, float] = {}
    for at, value in sorted(samples, key=lambda s: s[0]):
        ordered[at] = value  # last wins on a shared timestamp
    points = sorted(ordered.items())

    step = timedelta(seconds=step_s)
    out: list[Bucket] = []
    t0 = start
    while t0 < end:
        t1 = min(t0 + step, end)
        # The value in force at t0: the last sample at or before it.
        held: float | None = None
        for at, value in points:
            if at <= t0:
                held = value
            else:
                break
        inside = [(at, value) for at, value in points if t0 < at < t1]
        if held is None and not inside:
            t0 = t1
            continue
        # Walk the step function across [t0, t1). If nothing was in force at
        # t0 the covered span starts at the first change inside the bucket.
        segments: list[tuple[float, float]] = []  # (seconds, value)
        cursor = t0 if held is not None else inside[0][0]
        current = held if held is not None else inside[0][1]
        for at, value in inside:
            if at > cursor:
                segments.append(((at - cursor).total_seconds(), current))
            cursor, current = at, value
        if t1 > cursor:
            segments.append(((t1 - cursor).total_seconds(), current))
        covered = sum(seconds for seconds, _ in segments)
        if covered <= 0:
            t0 = t1
            continue
        values = [value for _, value in segments]
        out.append(
            Bucket(
                at=t0,
                minimum=min(values),
                average=sum(seconds * value for seconds, value in segments) / covered,
                maximum=max(values),
            )
        )
        t0 = t1
    return out
