"""Subject taxonomy.

These strings are the phase-2 integration surface. A typo here is a breaking
change for every future spoke, so the shapes are pinned by test.
"""

from __future__ import annotations

import pytest
from bellasreef_contracts import subjects


def test_subject_shapes() -> None:
    assert subjects.sensor("temp", "display-tank") == "bellasreef.sensor.temp.display-tank"
    assert subjects.cmd("binary", "ato-pump") == "bellasreef.cmd.binary.ato-pump"
    assert subjects.state("ato-pump") == "bellasreef.state.ato-pump"
    assert subjects.heartbeat("control-engine") == "bellasreef.heartbeat.control-engine"
    assert subjects.audit("command") == "bellasreef.audit.command"
    assert subjects.registry("ato-pump") == "bellasreef.registry.ato-pump"


@pytest.mark.parametrize(
    "bad",
    [
        "has.dot",  # would silently re-shape the subject tree
        "wild*card",
        "greater>than",
        "Upper",
        "has space",
        "",
        "-leading-dash",
        "x" * 65,
    ],
)
def test_malformed_tokens_are_rejected(bad: str) -> None:
    with pytest.raises(subjects.SubjectError):
        subjects.sensor("temp", bad)


def test_wildcards_cannot_be_smuggled_into_a_built_subject() -> None:
    """A '>' in an id would turn one device's subject into a subtree subscription."""
    with pytest.raises(subjects.SubjectError):
        subjects.cmd("binary", ">")


def test_parse_device_id_roundtrips() -> None:
    subject = subjects.cmd("pwm", "led-channel-a")
    assert subjects.parse_device_id(subject) == "led-channel-a"


def test_parse_rejects_foreign_subjects() -> None:
    with pytest.raises(subjects.SubjectError):
        subjects.parse_device_id("someoneelse.cmd.binary.pump")


def test_wildcard_constants_are_stable() -> None:
    assert subjects.ALL_COMMANDS == "bellasreef.cmd.>"
    assert subjects.ALL_SENSORS == "bellasreef.sensor.>"
    assert subjects.ALL_STATE == "bellasreef.state.>"
