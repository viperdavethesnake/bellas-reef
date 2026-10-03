# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""The hub's vitals ride the stream as a `host` frame (contracts 4.5.0).

Found on coco 2026-10-02: an app can hold a socket the API answers while
nothing behind the API is flowing — the ping proves uvicorn is up, not that
hardware-io and NATS are. HostStatus is published by hardware-io every 30 s,
so forwarding it gives every client a heartbeat that has crossed the whole
pipeline, and the System tab a value that is live instead of a one-off fetch.

These need no broker: `_encode` and the subscription list are exercised
directly. The end-to-end delivery test lives with the other bridge tests in
test_stream_and_overrides.py, which needs NATS.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from bellasreef_api.frames import frame_json_schema
from bellasreef_api.stream import StreamBridge
from bellasreef_contracts import HostStatus, subjects


def _status() -> HostStatus:
    # coco's values, as measured in-container 2026-08-31 (test_host_status.py).
    return HostStatus(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="hardware-io",
        load_1m=0.42,
        load_5m=0.38,
        load_15m=0.33,
        cpu_count=4,
        mem_total_kb=1014464,
        mem_available_kb=445792,
        temp_c=46.3,
        uptime_s=1692.78,
    )


def test_a_host_status_becomes_a_host_frame() -> None:
    bridge = StreamBridge("nats://unused")
    status = _status()

    encoded = asyncio.run(bridge._encode(subjects.host_status(), status.model_dump_json().encode()))

    assert encoded is not None
    frame = json.loads(encoded)
    assert frame["kind"] == "host"
    assert frame["subject"] == subjects.host_status()
    assert frame["payload"]["temp_c"] == pytest.approx(46.3)
    assert frame["payload"]["load_1m"] == pytest.approx(0.42)


def test_a_host_status_that_breaks_the_contract_is_dropped() -> None:
    """Same rule as every other kind: a producer bug must not reach clients."""
    bridge = StreamBridge("nats://unused")
    bad = json.loads(_status().model_dump_json())
    bad["cpu_count"] = 0  # ge=1

    assert asyncio.run(bridge._encode(subjects.host_status(), json.dumps(bad).encode())) is None


def test_the_published_frame_schema_includes_host() -> None:
    schema = frame_json_schema()
    defs: dict[str, Any] = schema["$defs"]  # type: ignore[assignment]
    assert "HostFrame" in defs
    assert defs["HostFrame"]["properties"]["kind"]["const"] == "host"


class _FakeNats:
    def __init__(self) -> None:
        self.subjects: list[str] = []
        self.is_connected = True

    async def subscribe(self, subject: str, **_: object) -> None:
        self.subjects.append(subject)


def test_the_bridge_subscribes_to_host_status(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeNats()

    async def connect(_url: str) -> _FakeNats:
        return fake

    monkeypatch.setattr("bellasreef_api.stream.nats.connect", connect)
    asyncio.run(StreamBridge("nats://unused")._ensure_connected())

    assert subjects.ALL_HOSTS in fake.subjects
