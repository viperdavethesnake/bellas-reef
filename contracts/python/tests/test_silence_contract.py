# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""A probe going quiet is its own alert class.

A dead probe and a probe reading 40 °C are different emergencies, and the
difference has to survive the wire. Collapsing them into one message type was
the tempting move: add `alert_class` to `SensorAlert` and make `value`,
`threshold` and `bound` optional. That is rejected twice over.

It is a MAJOR contract change by this repo's own versioning table ("adding a
field to an existing message is a MAJOR change"), and it would make every
threshold field optional in every generated client for the benefit of a class
that never carries them. A new message type on a new subject is the documented
MINOR path, and it lets both models stay strict.

`ALL_ALERTS` is a `>` wildcard, so publishing silence on `bellasreef.alert.*`
would hand existing subscribers a payload they are contractually obliged to
reject loudly. Hence its own root.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from bellasreef_contracts import SensorSilence, subjects
from pydantic import ValidationError


def _silence(**kw: object) -> SensorSilence:
    base: dict[str, object] = {
        "message_id": uuid4(),
        "emitted_at": datetime.now(UTC),
        "source": "control-engine",
        "device_id": "ds18b20-28-000000bfe244",
        "sensor_type": "temp",
        "state": "breach",
        "silent_for_s": 45.0,
        "silence_threshold_s": 33.6,
        "last_reading_at": datetime.now(UTC) - timedelta(seconds=45),
    }
    base.update(kw)
    return SensorSilence(**base)  # type: ignore[arg-type]


def test_silence_has_its_own_subject_root() -> None:
    """Not under bellasreef.alert.>, which existing consumers parse as SensorAlert."""
    subject = subjects.silence("ds18b20-28-000000bfe244")
    assert subject == "bellasreef.silence.ds18b20-28-000000bfe244"
    assert not subject.startswith("bellasreef.alert.")
    assert subjects.ALL_SILENCE == "bellasreef.silence.>"


def test_a_probe_that_went_quiet_is_a_breach() -> None:
    alert = _silence()
    assert alert.state == "breach"
    assert alert.silent_for_s == 45.0


def test_a_probe_that_never_reported_has_no_last_reading() -> None:
    """Nullable on purpose: a probe absent since boot has nothing to point at."""
    assert _silence(last_reading_at=None).last_reading_at is None


def test_silence_shorter_than_its_own_threshold_is_rejected() -> None:
    """Catches an evaluator comparing against the wrong side of the deadline.

    The same shape as SensorAlert's inverted-comparison guard: a raise that does
    not satisfy its own trigger is a bug no downstream rendering can detect.
    """
    with pytest.raises(ValidationError):
        _silence(silent_for_s=10.0, silence_threshold_s=33.6)


def test_a_clear_may_be_shorter_than_the_threshold() -> None:
    """On a clear the probe is reporting again; the duration is history, not a trigger."""
    assert _silence(state="clear", silent_for_s=10.0, silence_threshold_s=33.6).state == "clear"


def test_the_threshold_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        _silence(silence_threshold_s=0.0)


def test_silence_carries_no_threshold_fields() -> None:
    """Strictness both ways: a silence message must not smuggle a reading."""
    with pytest.raises(ValidationError):
        _silence(value=23.9)
