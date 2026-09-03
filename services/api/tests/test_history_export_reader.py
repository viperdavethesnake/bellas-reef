# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Raw sample export: the reader half, and the bytes it renders.

No containers. `HistoryReader` gets a `httpx.MockTransport`, so what is under
test here is the parsing and the rendering rather than VictoriaMetrics — the
round trip against a real VM lives in `test_history.py` and runs in CI.

The exact-bytes assertions matter more than they look. This response is a file
a person opens in a spreadsheet weeks later to work out what a heater did; a
column that quietly changes shape between releases makes the older file the
odd one out, with nothing in it to say so.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from bellasreef_api.history import MAX_EXPORT_WINDOW, HistoryReader, RawSample, csv_rows

_START = datetime(2026, 9, 3, 20, 0, tzinfo=UTC)
_END = _START + timedelta(hours=1)


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


def jsonl(*entries: dict[str, Any]) -> str:
    return "\n".join(json.dumps(entry) for entry in entries) + "\n"


def reader_returning(body: str, seen: list[httpx.Request] | None = None) -> HistoryReader:
    """A reader whose VictoriaMetrics always answers with `body`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, text=body)

    return HistoryReader("http://vm.invalid", transport=httpx.MockTransport(handler))


def samples_of(reader: HistoryReader) -> list[RawSample]:
    async def scenario() -> list[RawSample]:
        return await reader.raw_samples(
            metric="bellasreef_sensor_reading",
            device_id="display-tank",
            start=_START,
            end=_END,
        )

    return run(scenario)


class TestRawSamples:
    def test_entries_with_different_labels_merge_and_sort_by_time(self) -> None:
        """One signal, split only by the label it was written with.

        VictoriaMetrics returns a separate JSONL entry per label set, and the
        entries arrive in no particular order. A caller reading the export as a
        timeline needs one sorted list, not two interleaved ones.
        """
        body = jsonl(
            {
                "metric": {
                    "__name__": "bellasreef_sensor_reading",
                    "device_id": "display-tank",
                    "quality": "ok",
                },
                "values": [24.5],
                "timestamps": [int((_START + timedelta(minutes=30)).timestamp() * 1000)],
            },
            {
                "metric": {
                    "__name__": "bellasreef_sensor_reading",
                    "device_id": "display-tank",
                    "quality": "suspect",
                },
                "values": [24.0],
                "timestamps": [int((_START + timedelta(minutes=10)).timestamp() * 1000)],
            },
        )
        samples = samples_of(reader_returning(body))
        assert [s.value for s in samples] == [24.0, 24.5]
        assert [s.at for s in samples] == [
            _START + timedelta(minutes=10),
            _START + timedelta(minutes=30),
        ]
        assert [s.labels for s in samples] == [{"quality": "suspect"}, {"quality": "ok"}]

    def test_the_identity_labels_are_dropped(self) -> None:
        """`__name__` and `device_id` are the request, not the sample. Both are
        already columns of their own in the rendered file."""
        body = jsonl(
            {
                "metric": {
                    "__name__": "bellasreef_sensor_reading",
                    "device_id": "display-tank",
                },
                "values": [24.0],
                "timestamps": [int(_START.timestamp() * 1000)],
            }
        )
        assert samples_of(reader_returning(body))[0].labels == {}

    def test_unusable_values_are_dropped(self) -> None:
        """NaN is how VictoriaMetrics spells staleness, and `null` is what a
        malformed write leaves behind. Neither is a reading; a row saying "the
        probe read NaN" would be an invention."""
        body = (
            json.dumps(
                {
                    "metric": {"__name__": "bellasreef_sensor_reading"},
                    "values": [24.0, float("nan"), 25.0],
                    "timestamps": [
                        int(_START.timestamp() * 1000),
                        int((_START + timedelta(minutes=1)).timestamp() * 1000),
                        int((_START + timedelta(minutes=2)).timestamp() * 1000),
                    ],
                }
            )
            + "\n"
            + json.dumps(
                {
                    "metric": {"__name__": "bellasreef_sensor_reading"},
                    "values": [None, "warm"],
                    "timestamps": [
                        int((_START + timedelta(minutes=3)).timestamp() * 1000),
                        int((_START + timedelta(minutes=4)).timestamp() * 1000),
                    ],
                }
            )
            + "\n"
        )
        assert [s.value for s in samples_of(reader_returning(body))] == [24.0, 25.0]

    def test_an_empty_export_is_an_empty_list(self) -> None:
        assert samples_of(reader_returning("")) == []

    def test_the_request_names_the_metric_device_and_window(self) -> None:
        seen: list[httpx.Request] = []
        samples_of(reader_returning("", seen))
        assert len(seen) == 1
        request = seen[0]
        assert request.url.path == "/api/v1/export"
        params = request.url.params
        assert params["match[]"] == 'bellasreef_sensor_reading{device_id="display-tank"}'
        assert float(params["start"]) == _START.timestamp()
        assert float(params["end"]) == _END.timestamp()


class TestCsvRendering:
    def test_the_header_and_two_rows_are_exact(self) -> None:
        rendered = "".join(
            csv_rows(
                [
                    RawSample(
                        at=datetime(2026, 9, 3, 20, 15, 4, 123_000, tzinfo=UTC),
                        value=24.5,
                        labels={"quality": "ok"},
                    ),
                    RawSample(
                        at=datetime(2026, 9, 3, 20, 15, 9, 0, tzinfo=UTC),
                        value=24.5625,
                        labels={},
                    ),
                ],
                device_id="display-tank",
                metric="bellasreef_sensor_reading",
            )
        )
        assert rendered == (
            "timestamp,device_id,metric,value,quality\n"
            "2026-09-03T20:15:04.123Z,display-tank,bellasreef_sensor_reading,24.5,ok\n"
            "2026-09-03T20:15:09.000Z,display-tank,bellasreef_sensor_reading,24.5625,\n"
        )

    def test_no_samples_still_renders_the_header(self) -> None:
        """An empty window is a real answer — "nothing was recorded here" — and
        a zero-byte file reads as a failed download instead."""
        rendered = "".join(csv_rows([], device_id="display-tank", metric="m"))
        assert rendered == "timestamp,device_id,metric,value,quality\n"

    def test_a_non_utc_sample_is_rendered_in_utc(self) -> None:
        """One timezone in the file. A spreadsheet sorts these as text, so a
        column mixing offsets sorts wrong and looks fine."""
        at = datetime(2026, 9, 3, 13, 15, 4, 123_000, tzinfo=timezone(timedelta(hours=-7)))
        rendered = "".join(
            csv_rows([RawSample(at=at, value=1.0, labels={})], device_id="d", metric="m")
        )
        assert rendered.splitlines()[1].startswith("2026-09-03T20:15:04.123Z")


def test_the_window_cap_is_31_days() -> None:
    """The route's 422 and the memory it bounds are the same number."""
    assert MAX_EXPORT_WINDOW == timedelta(days=31)


def test_a_transportless_reader_still_builds() -> None:
    """The injected transport is a test seam, not a required argument."""
    assert HistoryReader("http://vm.invalid") is not None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
