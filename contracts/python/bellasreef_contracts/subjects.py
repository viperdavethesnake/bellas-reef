# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""NATS subject taxonomy.

The subject strings themselves are part of the versioned contract — a phase-2
ESP32 spoke joins the running system by publishing on these subjects and nothing
else changes. Build and parse subjects only through this module; never format a
subject with an f-string at a call site.

See ``docs/contracts/nats-subjects.md`` for the full specification.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "ROOT",
    "SubjectError",
    "alert",
    "assignment",
    "audit",
    "capability",
    "chip",
    "cmd",
    "heartbeat",
    "parse_device_id",
    "registry",
    "sensor",
    "silence",
    "state",
    "validate_token",
]

ROOT: Final = "bellasreef"

# NATS treats '.', '*' and '>' as structural. A token that contains any of them
# would silently re-shape the subject tree, so ids are constrained to a safe,
# lowercase, DNS-ish alphabet.
_TOKEN_RE: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class SubjectError(ValueError):
    """Raised when a subject token or subject string is malformed."""


def validate_token(token: str, *, field: str = "token") -> str:
    """Return ``token`` unchanged, or raise :class:`SubjectError`.

    Tokens are lowercase ``[a-z0-9_-]``, 1–64 chars, not starting with a
    separator. This rejects wildcards (``*``, ``>``) and dots outright.
    """
    if not _TOKEN_RE.match(token):
        raise SubjectError(
            f"invalid {field} {token!r}: must match {_TOKEN_RE.pattern} "
            "(lowercase alphanumeric, '_' or '-', 1-64 chars, no dots or wildcards)"
        )
    return token


def sensor(sensor_type: str, sensor_id: str) -> str:
    """Telemetry from a sensor. Core pub/sub — deliberately not persisted."""
    validate_token(sensor_type, field="sensor_type")
    validate_token(sensor_id, field="sensor_id")
    return f"{ROOT}.sensor.{sensor_type}.{sensor_id}"


def alert(device_id: str) -> str:
    """A threshold breach or clear for one device.

    Core pub/sub, like sensor telemetry: an alert is a statement about *now*,
    and a consumer that reconnects to a queue of hours-old breaches would raise
    alarms about a tank that has long since recovered. Durability is the audit
    log's job, and that is written from the same event.
    """
    validate_token(device_id, field="device_id")
    return f"{ROOT}.alert.{device_id}"


def silence(device_id: str) -> str:
    """A probe went quiet, or came back.

    Its own root rather than a fourth token under ``bellasreef.alert.``.
    ``ALL_ALERTS`` is a ``>`` wildcard, so anything published beneath it reaches
    consumers that parse every message as :class:`SensorAlert` and are required
    to reject what they cannot parse. Sharing the root would make a new alert
    class a breaking change for every existing alert subscriber, which is the
    opposite of what a new class should cost.
    """
    validate_token(device_id, field="device_id")
    return f"{ROOT}.silence.{device_id}"


def capability(source: str) -> str:
    """What one hardware source can offer.

    Retained last-value per subject: a consumer that starts after hardware-io
    still learns the topology, rather than waiting for the next restart to find
    out what this hub is made of.
    """
    validate_token(source, field="source")
    return f"{ROOT}.capability.{source}"


def chip(source: str, instance: str) -> str:
    """Subject for one hardware source instance's retained ChipState.

    Instances carry characters NATS reserves ('.' in "1f00098000.pwm" would
    split the token), so the instance token swaps '.' for '-'. The MESSAGE's
    ``instance`` field keeps the raw string; the subject is an address, not
    the datum.
    """
    validate_token(source, field="source")
    if not instance:
        raise ValueError("chip subject needs a source and an instance")
    return f"{ROOT}.chip.{source}.{instance.replace('.', '-')}"


def assignment(device_id: str) -> str:
    """One device's binding, as the operator declared it.

    Retained last-value per subject: a hardware-io that restarts alone learns
    every assignment rather than waiting for someone to re-save each device.
    """
    validate_token(device_id, field="device_id")
    return f"{ROOT}.assignment.{device_id}"


def cmd(actuator_class: str, actuator_id: str) -> str:
    """A command addressed to one actuator. Durable via JetStream."""
    validate_token(actuator_class, field="actuator_class")
    validate_token(actuator_id, field="actuator_id")
    return f"{ROOT}.cmd.{actuator_class}.{actuator_id}"


def state(device_id: str) -> str:
    """Last-known actuator state. Retained one-per-subject in JetStream."""
    validate_token(device_id, field="device_id")
    return f"{ROOT}.state.{device_id}"


def heartbeat(component: str) -> str:
    """Liveness beacon from a component.

    Never persisted. A replayed heartbeat would make a dead controller look
    alive, which is precisely the failure the heartbeat exists to detect.
    """
    validate_token(component, field="component")
    return f"{ROOT}.heartbeat.{component}"


def audit(category: str) -> str:
    """Audit event. Durable in JetStream; Postgres is the system of record."""
    validate_token(category, field="category")
    return f"{ROOT}.audit.{category}"


def registry(device_id: str) -> str:
    """Device registration announcement."""
    validate_token(device_id, field="device_id")
    return f"{ROOT}.registry.{device_id}"


def parse_device_id(subject: str) -> str:
    """Extract the trailing device/sensor id token from a subject.

    Raises :class:`SubjectError` if the subject is not one of ours or carries a
    wildcard in the id position.
    """
    parts = subject.split(".")
    if len(parts) < 3 or parts[0] != ROOT:
        raise SubjectError(f"not a {ROOT} subject: {subject!r}")
    return validate_token(parts[-1], field="device_id")


# Wildcard subscriptions. Consumers filter with these; they are contract too.
ALL_ALERTS: Final = f"{ROOT}.alert.>"
ALL_CAPABILITIES: Final = f"{ROOT}.capability.>"
ALL_CHIPS: Final = f"{ROOT}.chip.>"
ALL_ASSIGNMENTS: Final = f"{ROOT}.assignment.>"
ALL_SILENCE: Final = f"{ROOT}.silence.>"
ALL_SENSORS: Final = f"{ROOT}.sensor.>"
ALL_COMMANDS: Final = f"{ROOT}.cmd.>"
ALL_STATE: Final = f"{ROOT}.state.>"
ALL_HEARTBEATS: Final = f"{ROOT}.heartbeat.>"
ALL_AUDIT: Final = f"{ROOT}.audit.>"
ALL_REGISTRY: Final = f"{ROOT}.registry.>"
