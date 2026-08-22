# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""ScheduleDefinition / SchedulePoint contract tests.

Mirrors the validation ChannelProfile/RampPoint already exercise in
services/control_engine — this is the shared model both API writes and the
engine's reads will validate against.
"""

from datetime import time

import pytest
from bellasreef_contracts.schedules import ScheduleDefinition, SchedulePoint
from pydantic import ValidationError


def _points(*pairs: tuple[str, float]) -> list[dict[str, object]]:
    return [{"at": at, "duty": duty} for at, duty in pairs]


def test_valid_definition_round_trips() -> None:
    d = ScheduleDefinition.model_validate(
        {"name": "Bobs French Fries", "points": _points(("08:00", 0.0), ("13:00", 1.0))}
    )
    assert d.zone == "UTC"
    assert d.anchor == "clock"
    assert d.points[0].seconds == 8 * 3600


def test_fewer_than_two_points_rejected() -> None:
    with pytest.raises(ValidationError):
        ScheduleDefinition.model_validate({"name": "x", "points": _points(("08:00", 0.5))})


def test_unordered_and_duplicate_times_rejected() -> None:
    with pytest.raises(ValidationError, match="ascending"):
        ScheduleDefinition.model_validate(
            {"name": "x", "points": _points(("13:00", 1.0), ("08:00", 0.0))}
        )
    with pytest.raises(ValidationError, match="same time"):
        ScheduleDefinition.model_validate(
            {"name": "x", "points": _points(("08:00", 0.0), ("08:00", 1.0))}
        )


def test_solar_anchor_rejected_until_v2() -> None:
    with pytest.raises(ValidationError, match="solar"):
        ScheduleDefinition.model_validate(
            {
                "name": "x",
                "anchor": "solar_natural",
                "locale": {"name": "Bora Bora", "lat": -16.5, "lon": -151.74},
                "points": _points(("08:00", 0.0), ("13:00", 1.0)),
            }
        )


def test_locale_on_clock_anchor_rejected() -> None:
    with pytest.raises(ValidationError, match="locale"):
        ScheduleDefinition.model_validate(
            {
                "name": "x",
                "locale": {"name": "Bora Bora", "lat": -16.5, "lon": -151.74},
                "points": _points(("08:00", 0.0), ("13:00", 1.0)),
            }
        )


def test_unknown_zone_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        ScheduleDefinition.model_validate(
            {"name": "x", "zone": "Mars/Olympus", "points": _points(("08:00", 0.0), ("13:00", 1.0))}
        )


def test_microseconds_stripped() -> None:
    p = SchedulePoint(at=time(8, 0, 0, 123456), duty=0.5)
    assert p.at.microsecond == 0
