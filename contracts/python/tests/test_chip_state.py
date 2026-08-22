# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""ChipState contract tests."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from bellasreef_contracts import ChipState, subjects
from pydantic import ValidationError

_NOW = datetime(2026, 8, 22, 7, 0, tzinfo=UTC)


def _state(**overrides: object) -> ChipState:
    base: dict[str, object] = {
        "message_id": uuid4(),
        "emitted_at": _NOW,
        "source": "hardware-io",
        "hardware_source": "pca9685",
        "instance": "0x40@1",
        "initialised": True,
        "initialised_at": _NOW,
        "facts": {"pre_scale": 12, "frequency_hz": 502.7, "invrt": False, "address": "0x40"},
    }
    base.update(overrides)
    return ChipState.model_validate(base)


def test_round_trips_through_json() -> None:
    s = _state()
    assert ChipState.model_validate_json(s.model_dump_json()) == s


def test_never_initialised_has_no_timestamp() -> None:
    s = _state(initialised=False, initialised_at=None, facts={})
    assert s.initialised_at is None


def test_unknown_source_rejected() -> None:
    with pytest.raises(ValidationError):
        _state(hardware_source="esp32")


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        _state(bench_verified=True)


def test_chip_subject_sanitizes_dots() -> None:
    # NATS subject tokens cannot contain '.', but real instances do
    # ("1f00098000.pwm"). The subject swaps '.' for '-'; the message field
    # keeps the raw instance.
    assert subjects.chip("pi-pwm", "1f00098000.pwm") == "bellasreef.chip.pi-pwm.1f00098000-pwm"
    assert subjects.chip("pca9685", "0x40@1") == "bellasreef.chip.pca9685.0x40@1"
    assert subjects.ALL_CHIPS == "bellasreef.chip.>"


def test_chip_subject_rejects_empty() -> None:
    with pytest.raises(ValueError):
        subjects.chip("pca9685", "")


def test_chip_subject_validates_source() -> None:
    # Unlike instance, source is a real token — it must pass validate_token()
    # like every other subject builder's tokens (§1 of the subject spec).
    with pytest.raises(subjects.SubjectError):
        subjects.chip("Bad.Source", "0x40@1")
