# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""HostStatus contract tests."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from bellasreef_contracts import HostStatus, subjects
from pydantic import ValidationError

_NOW = datetime(2026, 8, 31, 21, 0, tzinfo=UTC)


def _status(**overrides: object) -> HostStatus:
    # The base values are coco's, measured in-container 2026-08-31 — the
    # numbers this message exists to carry, not invented ones.
    base: dict[str, object] = {
        "message_id": uuid4(),
        "emitted_at": _NOW,
        "source": "hardware-io",
        "load_1m": 0.42,
        "load_5m": 0.38,
        "load_15m": 0.33,
        "cpu_count": 4,
        "mem_total_kb": 1014464,
        "mem_available_kb": 445792,
        "temp_c": 46.3,
        "uptime_s": 1692.78,
    }
    base.update(overrides)
    return HostStatus.model_validate(base)


def test_round_trips_through_json() -> None:
    s = _status()
    assert HostStatus.model_validate_json(s.model_dump_json()) == s


def test_temperature_is_optional_and_never_fabricated() -> None:
    # None means "unreadable on this host", which is a real state (a board
    # with no thermal zone) and must survive the wire as None, not as 0.0.
    s = _status(temp_c=None)
    assert s.temp_c is None
    assert HostStatus.model_validate_json(s.model_dump_json()).temp_c is None


def test_zero_cores_rejected() -> None:
    with pytest.raises(ValidationError):
        _status(cpu_count=0)


def test_negative_memory_rejected() -> None:
    with pytest.raises(ValidationError):
        _status(mem_available_kb=-1)


def test_negative_load_rejected() -> None:
    with pytest.raises(ValidationError):
        _status(load_1m=-0.1)


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        _status(hostname="coco-bellasreef")


def test_subject_is_the_singleton() -> None:
    # Phase 1 is one hub, so the subject is fixed; a phase-2 spoke carries
    # its identity in the envelope's `source`, not the subject.
    assert subjects.host_status() == "bellasreef.host.status"
    assert subjects.ALL_HOSTS == "bellasreef.host.>"
