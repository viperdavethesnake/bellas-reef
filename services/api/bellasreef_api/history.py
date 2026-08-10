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
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

import httpx
from bellasreef_service import get_logger

log = get_logger(__name__)

__all__ = ["Bucket", "HistoryReader", "Series"]

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
