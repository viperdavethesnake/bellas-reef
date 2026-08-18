# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""WebSocket frame contract for `/api/v1/stream`.

These models exist so the stream has a **schema**, not just a shape someone
remembers. WebSockets cannot be described by OpenAPI 3.1, so PRD G3 is met a
different way (see the G3 footnote): the transport is hand-written, but every
frame is decoded into types generated from the JSON Schema exported here.

That is the property G3 exists to protect — a change to frame shape becomes a
compile error in the client rather than a runtime surprise on a tank. Clients
must not hand-write frame structs.

Layering note: these live in the API, not in `bellasreef_contracts`, because the
API *composes* them. A frame is a spine payload plus context the spine does not
have — hardware-io knows nothing about overrides. The exported JSON Schema is a
published artefact on the same footing as `openapi.json`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal
from uuid import UUID

from bellasreef_contracts import ActuatorState, SensorAlert, SensorReading
from bellasreef_db import Transition
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

__all__ = [
    "FRAME_SCHEMA_VERSION",
    "AlertFrame",
    "AnyFrame",
    "OverrideContext",
    "ReadyFrame",
    "SensorFrame",
    "StateFrame",
    "frame_json_schema",
]

#: Bumped when a frame's shape changes. Independent of the spine's
#: `schema_version`: a client can be current on frames and behind on the
#: contracts package, or the reverse.
FRAME_SCHEMA_VERSION: Final[Literal[1]] = 1


class _Frame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame_version: Literal[1] = FRAME_SCHEMA_VERSION
    received_at: datetime


class OverrideContext(BaseModel):
    """The active manual hold on an actuator, if any.

    Carried on every state frame because the time-and-scheduling contract
    requires override state to be loudly visible: a client showing a channel at
    0% must be able to say whether that is the schedule or a hold, and when the
    hold ends.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    duty: float = Field(ge=0.0, le=1.0)
    expires_at: datetime
    expires_in_s: float = Field(ge=0.0)
    #: "snap" or "ramp" — how the engine will move the light when this hold
    #: ends, as much as how it arrived. Shown on the active-hold row so what
    #: happens at expiry is legible (spec 2026-08-17).
    transition: Transition


class ReadyFrame(_Frame):
    """Sent once, after the socket authenticates. Nothing precedes it."""

    kind: Literal["ready"] = "ready"
    client_id: UUID


class StateFrame(_Frame):
    """An actuator state, plus the override context the spine cannot supply."""

    kind: Literal["state"] = "state"
    subject: str
    payload: ActuatorState
    override: OverrideContext | None = None


class SensorFrame(_Frame):
    """A sensor reading, forwarded unchanged."""

    kind: Literal["sensor"] = "sensor"
    subject: str
    payload: SensorReading


class AlertFrame(_Frame):
    """A threshold breach or clear, forwarded unchanged (PRD R12).

    A *new frame kind* rather than a field on the sensor frame. Adding a field
    to `SensorFrame` would be a MAJOR contract change under the `extra="forbid"`
    rule and would put alert state on every sample; a separate kind is additive
    and fires only on transitions.

    Clients must treat an unrecognised ``kind`` as skippable rather than fatal.
    On the spine, loud rejection of an unknown message is right — a misread dose
    is dangerous. On this stream it is not: refusing to render a temperature
    because the hub also sent a frame type this build predates is a worse
    outcome than ignoring it.
    """

    kind: Literal["alert"] = "alert"
    subject: str
    payload: SensorAlert


AnyFrame = Annotated[
    ReadyFrame | StateFrame | SensorFrame | AlertFrame, Field(discriminator="kind")
]

_ADAPTER: TypeAdapter[ReadyFrame | StateFrame | SensorFrame | AlertFrame] = TypeAdapter(AnyFrame)


def frame_json_schema(ref_template: str = "#/$defs/{model}") -> dict[str, object]:
    """JSON Schema for every frame a client can receive.

    Exported next to `openapi.json` so client Codable types are generated from
    it rather than typed by hand.

    ``ref_template`` exists because the same definitions are published twice, in
    two dialects. Standalone, internal references use JSON Schema's ``$defs``.
    Folded into ``openapi.json`` they must point at ``components/schemas``
    instead — OpenAPI cannot resolve a ``$defs`` pointer, and the generator
    fails with "JSONSchema reference points to this document" if it is left
    alone.
    """
    schema = _ADAPTER.json_schema(ref_template=ref_template)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Bella's Reef stream frames",
        "description": (
            "Frames delivered over the /api/v1/stream WebSocket. Generate client "
            "types from this; do not hand-write frame structs."
        ),
        "x-frame-schema-version": FRAME_SCHEMA_VERSION,
        **schema,
    }
