# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Host status: the reader against fixture files, the publisher's cadence
and best-effort contract.

The reader's fixture values are coco's, measured in-container 2026-08-31 —
the same numbers the contracts tests assert, so a drift between what the
host writes and what the wire carries shows up here first.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from bellasreef_contracts import HostStatus
from bellasreef_hardware_io.host import HostStatusPublisher, HostStatusReader


def run[T](scenario: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(scenario())


def write_proc_fixture(tmp_path: Path) -> Path:
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "loadavg").write_text("0.42 0.38 0.33 1/324 540\n")
    (proc / "meminfo").write_text(
        "MemTotal:        1014464 kB\nMemFree:          181000 kB\nMemAvailable:     445792 kB\n"
    )
    (proc / "uptime").write_text("1692.78 6468.85\n")
    return proc


def write_thermal_fixture(tmp_path: Path) -> Path:
    thermal = tmp_path / "thermal_zone0" / "temp"
    thermal.parent.mkdir(parents=True)
    thermal.write_text("46300\n")
    return thermal


class _RecordingSpine:
    def __init__(self, *, raises: bool = False) -> None:
        self.published: list[HostStatus] = []
        self._raises = raises

    async def publish_host_status(self, status: HostStatus) -> None:
        if self._raises:
            raise RuntimeError("spine unreachable")
        self.published.append(status)

    async def publish_heartbeat(self, heartbeat: object) -> None:
        # The wiring test runs the real _loop, which also beats.
        pass


def test_reader_reads_the_host_files(tmp_path: Path) -> None:
    reader = HostStatusReader(
        proc=write_proc_fixture(tmp_path), thermal=write_thermal_fixture(tmp_path)
    )
    status = reader.read()
    assert status.load_1m == 0.42
    assert status.load_5m == 0.38
    assert status.load_15m == 0.33
    assert status.mem_total_kb == 1014464
    assert status.mem_available_kb == 445792
    assert status.temp_c == 46.3
    assert status.uptime_s == 1692.78
    assert status.cpu_count >= 1
    assert status.source == "hardware-io"


def test_reader_reports_no_thermal_zone_as_none(tmp_path: Path) -> None:
    # A board with no thermal zone is a real state; None must never become a
    # fabricated 0.0 (the same rule the contract documents).
    reader = HostStatusReader(
        proc=write_proc_fixture(tmp_path), thermal=tmp_path / "absent" / "temp"
    )
    assert reader.read().temp_c is None


def test_publisher_honours_the_cadence(tmp_path: Path) -> None:
    spine = _RecordingSpine()
    reader = HostStatusReader(
        proc=write_proc_fixture(tmp_path), thermal=write_thermal_fixture(tmp_path)
    )
    publisher = HostStatusPublisher(lambda: spine, reader, interval_s=30.0)

    async def scenario() -> None:
        await publisher.maybe_publish(now=100.0)
        await publisher.maybe_publish(now=110.0)  # within the interval: no-op
        await publisher.maybe_publish(now=130.0)  # due again

    run(scenario)
    assert len(spine.published) == 2


def test_publisher_is_best_effort(tmp_path: Path) -> None:
    # A spine outage must not take the process down (the loop also drives
    # safety), and must not turn the next tick into a hammer: the failed
    # attempt still consumes its slot.
    spine = _RecordingSpine(raises=True)
    reader = HostStatusReader(
        proc=write_proc_fixture(tmp_path), thermal=write_thermal_fixture(tmp_path)
    )
    publisher = HostStatusPublisher(lambda: spine, reader, interval_s=30.0)

    async def scenario() -> None:
        await publisher.maybe_publish(now=100.0)  # raises inside, swallowed
        await publisher.maybe_publish(now=110.0)  # still within the interval

    run(scenario)
    assert spine.published == []


# ------------------------------------------------------------- spine leg


def _host_status() -> HostStatus:
    from datetime import UTC, datetime
    from uuid import uuid4

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


def test_br_host_stream_is_configured_as_retained_last_value() -> None:
    from bellasreef_hardware_io.spine import HOST_STREAM, STREAMS
    from nats.js.api import RetentionPolicy, StorageType

    by_name = {config.name: config for config in STREAMS}
    assert HOST_STREAM == "BR_HOST"
    host = by_name[HOST_STREAM]
    from bellasreef_contracts import subjects

    assert host.subjects == [subjects.ALL_HOSTS]
    assert host.retention == RetentionPolicy.LIMITS
    assert host.storage == StorageType.FILE
    assert host.max_msgs_per_subject == 1


def test_publish_host_status_uses_the_singleton_subject() -> None:
    from bellasreef_hardware_io.spine import Spine

    class _FakeNc:
        def __init__(self) -> None:
            self.published: list[tuple[str, bytes]] = []

        async def publish(self, subject: str, payload: bytes) -> None:
            self.published.append((subject, payload))

    async def scenario() -> tuple[str, bytes, HostStatus]:
        spine = Spine("nats://example.invalid:4222")
        fake_nc = _FakeNc()
        spine._nc = fake_nc  # type: ignore[assignment]
        status = _host_status()
        await spine.publish_host_status(status)
        subject, payload = fake_nc.published[0]
        return subject, payload, status

    subject, payload, status = run(scenario)
    assert subject == "bellasreef.host.status"
    assert HostStatus.model_validate_json(payload) == status


def test_publish_host_status_without_a_connection_raises() -> None:
    import pytest
    from bellasreef_hardware_io.spine import Spine

    async def scenario() -> None:
        spine = Spine("nats://example.invalid:4222")
        await spine.publish_host_status(_host_status())

    with pytest.raises(RuntimeError):
        run(scenario)


# ------------------------------------------------------------- app wiring


def test_the_main_loop_drives_the_host_status_publisher(tmp_path: Path) -> None:
    # The journey test for this feature's recurring defect class: a publisher
    # that exists, passes its unit tests, and is never called (the PCA9685's
    # initialise() shipped exactly that way). The real _loop must produce a
    # publish on a fake spine with no other help.
    from bellasreef_hardware_io.app import HardwareIO

    service = HardwareIO(loop_interval_s=0.01, metrics_port=0)
    spine = _RecordingSpine()
    service.spine = spine  # type: ignore[assignment]
    # Point the wired-in reader at fixtures; the wiring itself stays the
    # app's own.
    assert service._host_status is not None, "HardwareIO never built a host-status publisher"
    service._host_status._reader = HostStatusReader(
        proc=write_proc_fixture(tmp_path), thermal=write_thermal_fixture(tmp_path)
    )

    async def scenario() -> None:
        task = asyncio.create_task(service._loop())
        for _ in range(200):
            await asyncio.sleep(0.005)
            if spine.published:
                break
        service._stopping.set()
        await asyncio.wait_for(task, timeout=2.0)

    run(scenario)
    assert spine.published, "one pass of the main loop never published a host status"
