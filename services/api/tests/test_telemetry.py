# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Telemetry writer — docs/device-classes.md §4.

Most of this needs no environment at all: the label set is a pure function of a
payload plus a device's declared authority, and the label set *is* the series
identity, so it is the thing worth pinning hardest.

The one test that needs a real VictoriaMetrics writes and reads back, because
the failure this whole component exists to prevent is a pipeline that reports
itself healthy while nothing lands.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from bellasreef_api.store import Store
from bellasreef_api.telemetry import TelemetryWriter
from bellasreef_contracts import (
    ActuatorState,
    BinaryLevel,
    PwmLevel,
    SensorAlert,
    SensorReading,
)

_VM = "BELLASREEF_TEST_VM_URL"

AT = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


class FakeStore:
    """Answers the one question the writer asks Postgres."""

    def __init__(self, answer: tuple[bool, str | None], transport: str | None = "local") -> None:
        self.answer = answer
        self.transport = transport

    async def control_authority_of(self, device_id: str) -> tuple[bool, str | None]:
        return self.answer

    async def device_labels(self, device_id: str) -> tuple[bool, str | None, str | None]:
        known, authority = self.answer
        return (known, authority, self.transport if known else None)


def writer(
    authority: tuple[bool, str | None] = (True, "authoritative"),
    transport: str | None = "local",
) -> TelemetryWriter:
    return TelemetryWriter(
        "nats://unused", "http://unused", cast(Store, FakeStore(authority, transport))
    )


def reading(**over: Any) -> bytes:
    payload: dict[str, Any] = {
        "message_id": uuid4(),
        "emitted_at": AT,
        "source": "hardware-io",
        "sensor_id": "probe-1",
        "sensor_type": "temp",
        "value": 24.5,
        "unit": "degC",
        "quality": "ok",
    }
    payload.update(over)
    return SensorReading(**payload).model_dump_json().encode()


def state(level: Any, **over: Any) -> bytes:
    payload: dict[str, Any] = {
        "message_id": uuid4(),
        "emitted_at": AT,
        "source": "hardware-io",
        "actuator_id": "led-blue",
        "level": level,
        "reason": "commanded",
        "since": AT,
    }
    payload.update(over)
    return ActuatorState(**payload).model_dump_json().encode()


def alert(**over: Any) -> bytes:
    payload: dict[str, Any] = {
        "message_id": uuid4(),
        "emitted_at": AT,
        "source": "control-engine",
        "device_id": "probe-1",
        "sensor_type": "temp",
        "state": "breach",
        "bound": "max",
        "value": 29.0,
        "threshold": 27.0,
        "clear_margin": 0.5,
        "unit": "degC",
    }
    payload.update(over)
    return SensorAlert(**payload).model_dump_json().encode()


def parse(lines: list[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in lines]


class TestLineFormat:
    def test_timestamps_are_milliseconds(self) -> None:
        """VictoriaMetrics stores millisecond precision; seconds would place
        every sample in January 1970."""
        (line,) = parse(run(lambda: writer()._sensor_lines(reading())))
        assert line["timestamps"] == [int(AT.timestamp() * 1000)]

    def test_a_reading_becomes_one_labelled_sample(self) -> None:
        (line,) = parse(run(lambda: writer()._sensor_lines(reading())))
        assert line["values"] == [24.5]
        assert line["metric"] == {
            "__name__": "bellasreef_sensor_reading",
            "device_id": "probe-1",
            "sensor_type": "temp",
            "unit": "degC",
            "quality": "ok",
        }

    def test_a_faulted_reading_writes_a_fault_not_a_number(self) -> None:
        """A fault has no value. Writing one would put a fabricated point on the
        chart, which is the same lie as showing a stale reading as current."""
        (line,) = parse(run(lambda: writer()._sensor_lines(reading(value=None, quality="fault"))))
        assert line["metric"]["__name__"] == "bellasreef_sensor_fault"
        assert "bellasreef_sensor_reading" not in json.dumps(line)


class TestAuthorityLabel:
    def test_actuator_state_always_carries_the_authority(self) -> None:
        """§4: required on all actuator state series, on the first sample ever
        written, because the label set is the series identity."""
        (line,) = parse(run(lambda: writer()._state_lines(state(PwmLevel(duty=0.6)))))
        assert line["metric"]["control_authority"] == "authoritative"
        assert line["values"] == [0.6]

    def test_a_binary_level_is_one_or_zero(self) -> None:
        (line,) = parse(run(lambda: writer()._state_lines(state(BinaryLevel(on=True)))))
        assert line["values"] == [1.0]

    def test_an_unknown_device_is_never_promoted_to_authoritative(self) -> None:
        """The dangerous default. A guess here writes a claim into history as
        though it were a measurement, and history cannot be relabelled in place."""
        (line,) = parse(run(lambda: writer((False, None))._state_lines(state(PwmLevel(duty=0.6)))))
        assert line["metric"]["control_authority"] == "unknown"

    def test_a_device_that_carries_no_authority_is_not_unknown(self) -> None:
        """Sensors carry no authority by §2. "unknown" would say the lookup
        failed; the truth is that the question does not apply."""
        (line,) = parse(run(lambda: writer((True, None))._alert_lines(alert())))
        assert line["metric"]["control_authority"] == "not_applicable"

    def test_an_alert_carries_the_authority_of_its_device(self) -> None:
        (line,) = parse(run(lambda: writer((True, "advisory"))._alert_lines(alert())))
        assert line["metric"]["control_authority"] == "advisory"
        assert line["values"] == [1.0]

    def test_an_alert_carries_the_transport_of_its_device(self) -> None:
        """The axis that actually separates these episodes today.

        Every alert is sensor-sourced, so control_authority is truthfully
        not_applicable; transport is what distinguishes a probe measured on the
        board from a value relayed over a network. Both labels are on the series
        from the first sample so a future actuator-sourced alert class has a
        place to land without forking it.
        """
        (line,) = parse(run(lambda: writer((True, None), "local")._alert_lines(alert())))
        assert line["metric"]["transport"] == "local"
        assert line["metric"]["control_authority"] == "not_applicable"

        (relayed,) = parse(run(lambda: writer((True, None), "network")._alert_lines(alert())))
        assert relayed["metric"]["transport"] == "network"

    def test_a_cleared_alert_is_zero(self) -> None:
        (line,) = parse(
            run(lambda: writer((True, None))._alert_lines(alert(state="clear", value=26.0)))
        )
        assert line["values"] == [0.0]


class TestAdvisoryExtras:
    def test_an_authoritative_series_carries_no_ack_label(self) -> None:
        """§4 puts command_acked on advisory series specifically. On an
        authoritative one it would be noise — the command is verifiable at the
        electrical layer."""
        (line,) = parse(run(lambda: writer()._state_lines(state(PwmLevel(duty=0.6)))))
        assert "command_acked" not in line["metric"]

    def test_the_exchange_age_is_a_metric_not_a_label(self) -> None:
        """A label whose value changes every sample mints a new series every
        sample, and an age has to be charted to answer "when did we stop
        knowing" — which a label cannot do.

        Nothing produces these fields yet (there is no vendor-bridge), so this
        pins the shape rather than a live producer.
        """
        writer_ = writer((True, "advisory"))
        payload = state(
            PwmLevel(duty=0.4),
            actuator_id="kessil-1",
            source="vendor-bridge",
            command_acked=False,
            last_exchange_age_s=42.0,
        )
        lines = parse(run(lambda: writer_._state_lines(payload)))
        names = {line["metric"]["__name__"] for line in lines}
        assert "bellasreef_actuator_last_exchange_age_seconds" in names
        age = next(
            line
            for line in lines
            if line["metric"]["__name__"] == "bellasreef_actuator_last_exchange_age_seconds"
        )
        assert age["values"] == [42.0]
        level = next(
            line for line in lines if line["metric"]["__name__"] == "bellasreef_actuator_level"
        )
        assert level["metric"]["command_acked"] == "false"


@pytest.mark.skipif(not os.environ.get(_VM), reason=f"{_VM} not set; needs VictoriaMetrics")
def test_a_written_sample_can_be_read_back() -> None:
    """The whole point of the component, proven against a real store.

    Formatting a correct-looking line and never landing it is precisely the
    class of failure this project keeps finding, so the assertion is a query,
    not a serialisation.
    """

    async def scenario() -> float:
        vm = os.environ[_VM].rstrip("/")
        marker = f"probe-{uuid4().hex[:8]}"
        # Stamped *now*, not with the fixed AT the unit tests use. An instant
        # query looks back five minutes by default, so a sample written with a
        # historical timestamp lands correctly and is invisible to the
        # assertion — which would read as "the write failed".
        now = datetime.now(UTC)
        w = TelemetryWriter("nats://unused", vm, cast(Store, FakeStore((True, None))))
        # Only the HTTP half. `start()` would also launch the JetStream consumer
        # against a URL that does not resolve; what is under test here is the
        # write reaching VictoriaMetrics.
        w._http = httpx.AsyncClient(timeout=10.0)
        try:
            lines = await w._sensor_lines(reading(sensor_id=marker, value=21.25, emitted_at=now))
            await w._push(lines)
        finally:
            await w.close()

        async with httpx.AsyncClient(timeout=10) as c:
            # `/api/v1/export`, not `/api/v1/query`. An instant query applies
            # VictoriaMetrics' `-search.latencyOffset` (30s by default), which
            # hides the newest samples so a query cannot return a half-written
            # scrape interval. A freshly written point is therefore invisible to
            # `query` for half a minute — indistinguishable, from a test, from a
            # write that silently failed. Export reads what is actually stored.
            for _ in range(30):
                r = await c.get(
                    f"{vm}/api/v1/export",
                    params={"match[]": f'bellasreef_sensor_reading{{device_id="{marker}"}}'},
                )
                body = r.text.strip()
                if body:
                    return float(json.loads(body.splitlines()[0])["values"][0])
                await asyncio.sleep(0.5)
        raise AssertionError("the sample was never stored")

    assert run(scenario) == 21.25
