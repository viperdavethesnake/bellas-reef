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
from bellasreef_api.app import build_app
from bellasreef_api.history import MAX_BUCKETS, HistoryReader
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_PG = "BELLASREEF_TEST_DATABASE_URL"
_VM = "BELLASREEF_TEST_VM_URL"

# No blanket module-level skip: `TestBucketSizing`, `TestEnvelope` and
# `TestGaps` read and write real series data and need `_VM`; the endpoint
# validation class added below needs only `_PG` — the naive/aware guard and
# the empty-registry path never reach VictoriaMetrics. Each class below
# declares the environment it actually needs.
_needs_vm = pytest.mark.skipif(not os.environ.get(_VM), reason=f"{_VM} not set")
_needs_pg = pytest.mark.skipif(not os.environ.get(_PG), reason=f"{_PG} not set")


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


@_needs_vm
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


@_needs_vm
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


@_needs_vm
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


# ------------------------------------------------------- endpoint validation
#
# `/api/v1/history` itself, not `HistoryReader`. A dummy `vm_url` is enough:
# these cases either 422 before the handler ever calls `reader.series()`, or
# (the both-aware case) run against an empty device registry, which returns
# with no VictoriaMetrics call at all — see `store.list_devices()` in the
# handler. Real series data belongs in `TestEnvelope`/`TestGaps` above.


async def _fresh_engine() -> AsyncEngine:
    engine = create_async_engine(os.environ[_PG], future=True, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE sensor_alerts, overrides, pairing_windows, pairing_requests, "
                "paired_clients, devices CASCADE"
            )
        )
    return engine


async def _paired(app: Any) -> dict[str, str]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://hub"
    ) as c:
        granted = (await c.post("/api/v1/pair", json={"client_name": "phone"})).json()
        tok = (
            await c.post("/api/v1/token", json={"refresh_token": granted["refresh_token"]})
        ).json()
    return {"Authorization": f"Bearer {tok['access_token']}"}


@_needs_pg
class TestHistoryEndpointValidation:
    """Naive/aware datetimes at the query boundary (defect A).

    FastAPI parses both offset-bearing and offset-free ISO-8601 forms into
    `datetime`, so a naive value reaches `end <= start` — which raises
    `TypeError: can't compare offset-naive and offset-aware datetimes` when
    only one side is naive, and silently uses server-local time when both are
    — instead of the 422 the endpoint's own `responses` block promises.
    """

    def _get(self, start: str, end: str) -> int:
        async def scenario() -> int:
            engine = await _fresh_engine()
            app = build_app(engine, vm_url="http://127.0.0.1:1")
            headers = await _paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                r = await c.get(
                    "/api/v1/history", params={"start": start, "end": end}, headers=headers
                )
            await engine.dispose()
            return r.status_code

        return run(scenario)

    def test_a_naive_start_is_refused_with_422(self) -> None:
        assert self._get("2026-08-10T12:00:00", "2026-08-10T13:00:00Z") == 422

    def test_a_naive_end_is_refused_with_422(self) -> None:
        assert self._get("2026-08-10T12:00:00Z", "2026-08-10T13:00:00") == 422

    def test_both_naive_is_refused_with_422(self) -> None:
        assert self._get("2026-08-10T12:00:00", "2026-08-10T13:00:00") == 422

    def test_both_aware_is_the_existing_behavior(self) -> None:
        """No devices registered, so this is the normal empty result — the
        guard must not reject a window it used to accept."""
        assert self._get("2026-08-10T12:00:00Z", "2026-08-10T13:00:00Z") == 200


# --------------------------------------------------------- export round trip
#
# `/api/v1/history/export` against real samples: written through VictoriaMetrics'
# own import endpoint, read back through the route, compared byte for byte. The
# guards that refuse a request before any of this live in
# `test_history_export_api.py`, which needs no VictoriaMetrics.


async def _register_sensor(engine: AsyncEngine, device_id: str) -> None:
    """A registered device row, which is what the export route resolves against.

    Inserted directly rather than through the registry consumer: that path is a
    NATS subscription, and this test has no broker.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO devices (id, device_id, kind, driver_id, sensor_type, "
                "poll_interval_s, transport) VALUES (:id, :device_id, 'sensor', "
                "'ds18b20', 'temp', 5.0, 'local')"
            ),
            {"id": uuid.uuid4(), "device_id": device_id},
        )


def _iso(at: datetime) -> str:
    """The Z-suffixed form the API parses, from an aware datetime."""
    return at.isoformat().replace("+00:00", "Z")


@_needs_vm
@_needs_pg
class TestExportRoundTrip:
    @pytest.mark.timeout(120)
    def test_two_written_samples_come_back_as_two_csv_rows(self) -> None:
        """The whole point of the endpoint, end to end.

        The two values are exactly representable in binary floating point, so
        the rendered column is the number that was written rather than the
        nearest thing to it, and the assertion can be on bytes.
        """

        async def scenario() -> tuple[str, str, dict[str, Any]]:
            vm = os.environ[_VM].rstrip("/")
            engine = await _fresh_engine()
            device = f"probe-{uuid.uuid4().hex[:8]}"
            await _register_sensor(engine, device)

            base = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
            await write(vm, device, [(base, 24.5), (base + timedelta(seconds=5), 24.5625)])
            await wait_until_stored(vm, device, 2)

            app = build_app(engine, vm_url=vm)
            headers = await _paired(app)
            window = {
                "device_id": device,
                "start": _iso(base - timedelta(minutes=1)),
                "end": _iso(base + timedelta(minutes=1)),
            }
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                as_csv = await c.get("/api/v1/history/export", params=window, headers=headers)
                as_json = await c.get(
                    "/api/v1/history/export",
                    params={**window, "format": "json"},
                    headers=headers,
                )
            await engine.dispose()

            assert as_csv.status_code == 200, as_csv.text
            assert as_json.status_code == 200, as_json.text
            assert as_csv.headers["content-type"].startswith("text/csv")
            assert as_json.headers["content-type"].startswith("application/json")

            expected = (
                "timestamp,device_id,metric,value,quality\n"
                f"{base.strftime('%Y-%m-%dT%H:%M:%S')}.000Z,{device},"
                "bellasreef_sensor_reading,24.5,\n"
                f"{(base + timedelta(seconds=5)).strftime('%Y-%m-%dT%H:%M:%S')}.000Z,{device},"
                "bellasreef_sensor_reading,24.5625,\n"
            )
            body: dict[str, Any] = as_json.json()
            return as_csv.text, expected, body

        rendered, expected, body = run(scenario)
        assert rendered == expected
        assert body["metric"] == "bellasreef_sensor_reading"
        assert [s["value"] for s in body["samples"]] == [24.5, 24.5625]
        assert [s["quality"] for s in body["samples"]] == [None, None]

    @pytest.mark.timeout(120)
    def test_an_empty_window_is_a_named_file_with_only_a_header(self) -> None:
        """Two things at once, because they are the same download.

        A window with nothing in it is a real answer, and a zero-byte file
        reads as a failed transfer instead. The name is all the share sheet
        shows about the file, so it carries the device and the window.
        """

        async def scenario() -> tuple[str, str, str]:
            vm = os.environ[_VM].rstrip("/")
            engine = await _fresh_engine()
            device = f"probe-{uuid.uuid4().hex[:8]}"
            await _register_sensor(engine, device)
            app = build_app(engine, vm_url=vm)
            headers = await _paired(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://hub"
            ) as c:
                response = await c.get(
                    "/api/v1/history/export",
                    params={
                        "device_id": device,
                        "start": "2026-08-10T12:00:00Z",
                        "end": "2026-08-10T13:30:00Z",
                    },
                    headers=headers,
                )
            await engine.dispose()
            assert response.status_code == 200, response.text
            return device, response.text, response.headers["content-disposition"]

        device, rendered, disposition = run(scenario)
        assert rendered == "timestamp,device_id,metric,value,quality\n"
        assert disposition == (
            f'attachment; filename="bellasreef-{device}-20260810T1200Z-20260810T1330Z.csv"'
        )
