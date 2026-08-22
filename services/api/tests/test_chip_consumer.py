# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""``ChipConsumer._on_message``: retained ChipState into ``chip_state``.

Unit-level only, the same way ``TelemetryWriter``'s label logic is tested in
``test_telemetry.py`` — a fake store records what it was asked to do, and a
fake NATS message stands in for the wire. No NATS or Postgres needed: this is
pure message handling, and the JetStream subscribe/retry plumbing is a verbatim
copy of ``CapabilityConsumer``'s, which is exercised for real by
``test_background_components.py``'s env-gated lifespan test.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from bellasreef_api.registry import ChipConsumer
from bellasreef_api.store import Store
from bellasreef_contracts import ChipState


class FakeStore:
    """Records every ``upsert_chip_state`` call, does nothing else."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def upsert_chip_state(
        self,
        *,
        source: str,
        instance: str,
        initialised: bool,
        initialised_at: datetime | None,
        facts: dict[str, Any],
        announced_at: datetime,
    ) -> None:
        self.calls.append(
            {
                "source": source,
                "instance": instance,
                "initialised": initialised,
                "initialised_at": initialised_at,
                "facts": facts,
                "announced_at": announced_at,
            }
        )


class _FakeMsg:
    def __init__(self, payload: bytes, subject: str) -> None:
        self.data = payload
        self.subject = subject
        self.acked = False

    async def ack(self) -> None:
        self.acked = True


def _chip_state(**over: Any) -> ChipState:
    fields: dict[str, Any] = {
        "message_id": uuid4(),
        "emitted_at": datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC),
        "source": "hardware-io",
        "hardware_source": "pca9685",
        "instance": "0x40",
        "initialised": True,
        "initialised_at": datetime(2026, 8, 22, 11, 59, 0, tzinfo=UTC),
        "facts": {"pre_scale": 12, "invrt": False},
    }
    fields.update(over)
    return ChipState(**fields)


def test_valid_message_upserts_with_the_message_fields() -> None:
    store = FakeStore()
    consumer = ChipConsumer("nats://unused", cast(Store, store))
    state = _chip_state()
    msg = _FakeMsg(state.model_dump_json().encode(), "bellasreef.chip.pca9685.0x40")

    asyncio.run(consumer._on_message(cast(Any, msg)))

    assert len(store.calls) == 1
    call = store.calls[0]
    assert call["source"] == "pca9685"
    assert call["instance"] == "0x40"
    assert call["initialised"] is True
    assert call["initialised_at"] == state.initialised_at
    assert call["facts"] == {"pre_scale": 12, "invrt": False}
    # announced_at is the message's own emitted_at, not receipt time: LAST_PER_SUBJECT
    # redelivers every retained message on API restart, so receipt time would claim
    # hardware-io "announced" at every API restart.
    assert call["announced_at"] == state.emitted_at
    assert call["announced_at"].tzinfo is not None
    assert msg.acked


def test_garbage_bytes_warns_and_does_not_upsert(caplog: Any) -> None:
    store = FakeStore()
    consumer = ChipConsumer("nats://unused", cast(Store, store))
    msg = _FakeMsg(b"not json at all", "bellasreef.chip.pca9685.0x40")

    with caplog.at_level(logging.WARNING):
        asyncio.run(consumer._on_message(cast(Any, msg)))

    assert store.calls == []
    assert any("did not validate" in record.message for record in caplog.records)
