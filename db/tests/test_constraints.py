# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Storage-layer safety constraints, asserted against real Postgres.

The bug that motivated the first of these: a CHECK constraint that evaluates to
NULL **passes** in Postgres. `poll_interval_s > 0` with a NULL cadence is NULL,
`TRUE AND NULL` is NULL, `FALSE OR NULL` is NULL — so a sensor with no declared
polling cadence sailed straight in. Only the explicit `IS NOT NULL` closes it,
and only a real database can demonstrate that.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from helpers import engine, requires_postgres, run
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

pytestmark = requires_postgres


async def _insert(sql: str, **params: object) -> None:
    eng = engine()
    try:
        async with eng.begin() as conn:
            await conn.execute(text(sql), params)
    finally:
        await eng.dispose()


_DEVICE_COLS = (
    "id, device_id, kind, driver_id, sensor_type, poll_interval_s, "
    "actuator_class, role, control_authority, failsafe_capable, transport, "
    "safe_state, max_runtime_s, heartbeat_timeout_s"
)


def _device_sql() -> str:
    return (
        f"INSERT INTO devices ({_DEVICE_COLS}) VALUES "
        "(:id, :device_id, :kind, :driver_id, :sensor_type, :poll_interval_s, "
        ":actuator_class, :role, :control_authority, :failsafe_capable, :transport, "
        "CAST(:safe_state AS JSONB), :max_runtime_s, :heartbeat_timeout_s)"
    )


def _sensor(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": uuid.uuid4(),
        "device_id": f"probe-{uuid.uuid4().hex[:8]}",
        "kind": "sensor",
        "driver_id": "ds18b20",
        "sensor_type": "temp",
        "poll_interval_s": 1.0,
        "actuator_class": None,
        "role": None,
        "control_authority": None,
        "failsafe_capable": None,
        "transport": "local",
        "safe_state": None,
        "max_runtime_s": None,
        "heartbeat_timeout_s": None,
    }
    row.update(over)
    return row


def _actuator(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": uuid.uuid4(),
        "device_id": f"pump-{uuid.uuid4().hex[:8]}",
        "kind": "actuator",
        "driver_id": "gpio-relay",
        "sensor_type": None,
        "poll_interval_s": None,
        "actuator_class": "binary",
        "role": "outlet",
        "control_authority": "authoritative",
        "failsafe_capable": True,
        "transport": "local",
        "safe_state": '{"kind": "binary", "on": false}',
        "max_runtime_s": 3600.0,
        "heartbeat_timeout_s": 15.0,
    }
    row.update(over)
    return row


class TestSensorCadence:
    def test_valid_sensor_is_accepted(self) -> None:
        run(lambda: _insert(_device_sql(), **_sensor()))

    def test_null_cadence_sensor_is_rejected(self) -> None:
        """The regression test for the NULL hole.

        Before the fix this INSERT succeeded, because the CHECK evaluated to
        NULL and Postgres only rejects a CHECK that is FALSE.
        """
        with pytest.raises(IntegrityError, match="sensor_declares_type_cadence_and_transport"):
            run(lambda: _insert(_device_sql(), **_sensor(poll_interval_s=None)))

    def test_zero_cadence_sensor_is_rejected(self) -> None:
        with pytest.raises(IntegrityError, match="sensor_declares_type_cadence_and_transport"):
            run(lambda: _insert(_device_sql(), **_sensor(poll_interval_s=0.0)))

    def test_null_sensor_type_is_rejected(self) -> None:
        with pytest.raises(IntegrityError, match="sensor_declares_type_cadence_and_transport"):
            run(lambda: _insert(_device_sql(), **_sensor(sensor_type=None)))


class TestActuatorFailureBehaviour:
    def test_valid_actuator_is_accepted(self) -> None:
        run(lambda: _insert(_device_sql(), **_actuator()))

    # The constraint these assert on was renamed and narrowed by migration 0008:
    # the safety triple is now demanded of *authoritative* actuators rather than
    # every actuator (docs/device-classes.md §2.1). The default `_actuator()` is
    # authoritative, so the behaviour under test is unchanged for these rows —
    # only the constraint that enforces it has moved.
    @pytest.mark.parametrize("field", ["safe_state", "max_runtime_s", "heartbeat_timeout_s"])
    def test_authoritative_actuator_without_failure_behaviour_is_rejected(self, field: str) -> None:
        with pytest.raises(IntegrityError, match="authoritative_declares_failure_behaviour"):
            run(lambda: _insert(_device_sql(), **_actuator(**{field: None})))

    def test_an_actuator_without_a_class_is_rejected(self) -> None:
        """Electrical class is required of every actuator regardless of
        authority — it moved to the authority constraint, not out of the schema."""
        with pytest.raises(IntegrityError, match="actuator_declares_authority"):
            run(lambda: _insert(_device_sql(), **_actuator(actuator_class=None)))

    @pytest.mark.parametrize("field", ["max_runtime_s", "heartbeat_timeout_s"])
    def test_non_positive_timers_are_rejected(self, field: str) -> None:
        with pytest.raises(IntegrityError, match="authoritative_declares_failure_behaviour"):
            run(lambda: _insert(_device_sql(), **_actuator(**{field: 0.0})))


class TestAuditLogIsAppendOnly:
    """Append-only enforced by trigger, not by convention."""

    @staticmethod
    async def _seed() -> int:
        eng = engine()
        try:
            async with eng.begin() as conn:
                result = await conn.execute(
                    text(
                        "INSERT INTO audit_log "
                        "(message_id, occurred_at, category, actor, event) "
                        "VALUES (:mid, :at, 'safety', 'test', CAST(:event AS JSONB)) "
                        "RETURNING id"
                    ),
                    # Bound, not inlined: a ':1' inside a SQL string literal is
                    # parsed by SQLAlchemy as a bind parameter.
                    {
                        "mid": uuid.uuid4(),
                        "at": datetime.now(UTC),
                        "event": '{"k": 1}',
                    },
                )
                return int(result.scalar_one())
        finally:
            await eng.dispose()

    def test_insert_is_allowed(self) -> None:
        assert run(self._seed) > 0

    def test_update_raises(self) -> None:
        row_id = run(self._seed)

        async def attempt() -> None:
            eng = engine()
            try:
                async with eng.begin() as conn:
                    await conn.execute(
                        text("UPDATE audit_log SET actor = 'tampered' WHERE id = :id"),
                        {"id": row_id},
                    )
            finally:
                await eng.dispose()

        with pytest.raises(DBAPIError, match="append-only"):
            run(lambda: attempt())

    def test_delete_raises(self) -> None:
        row_id = run(self._seed)

        async def attempt() -> None:
            eng = engine()
            try:
                async with eng.begin() as conn:
                    await conn.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": row_id})
            finally:
                await eng.dispose()

        with pytest.raises(DBAPIError, match="append-only"):
            run(lambda: attempt())


class TestDosingJournalEvidence:
    """A row cannot claim to be confirmed without evidence of confirmation."""

    @staticmethod
    async def _device() -> uuid.UUID:
        row = _actuator(device_id=f"doser-{uuid.uuid4().hex[:8]}")
        await _insert(_device_sql(), **row)
        return uuid.UUID(str(row["id"]))

    def _dose_sql(self) -> str:
        return (
            "INSERT INTO dosing_journal "
            "(id, device_pk, idempotency_key, state, requested_ml, delivered_ml, "
            " intent_at, executed_at, confirmed_at, error) VALUES "
            "(:id, :device_pk, :idem, :state, :requested_ml, :delivered_ml, "
            " :intent_at, :executed_at, :confirmed_at, :error)"
        )

    def _row(self, device_pk: uuid.UUID, **over: object) -> dict[str, object]:
        now = datetime.now(UTC)
        row: dict[str, object] = {
            "id": uuid.uuid4(),
            "device_pk": device_pk,
            "idem": uuid.uuid4(),
            "state": "intent",
            "requested_ml": 5.0,
            "delivered_ml": None,
            "intent_at": now,
            "executed_at": None,
            "confirmed_at": None,
            "error": None,
        }
        row.update(over)
        return row

    def test_confirmed_without_evidence_is_rejected(self) -> None:
        device_pk = run(self._device)
        with pytest.raises(IntegrityError, match="confirmed_has_evidence"):
            run(lambda: _insert(self._dose_sql(), **self._row(device_pk, state="confirmed")))

    def test_confirmed_with_full_evidence_is_accepted(self) -> None:
        device_pk = run(self._device)
        now = datetime.now(UTC)
        run(
            lambda: _insert(
                self._dose_sql(),
                **self._row(
                    device_pk,
                    state="confirmed",
                    delivered_ml=5.0,
                    executed_at=now,
                    confirmed_at=now,
                ),
            )
        )

    def test_duplicate_idempotency_key_cannot_dose_twice(self) -> None:
        device_pk = run(self._device)
        key = uuid.uuid4()
        run(lambda: _insert(self._dose_sql(), **self._row(device_pk, idem=key)))
        with pytest.raises(IntegrityError, match="idempotency_key"):
            run(lambda: _insert(self._dose_sql(), **self._row(device_pk, idem=key)))

    def test_failed_state_requires_a_reason(self) -> None:
        device_pk = run(self._device)
        with pytest.raises(IntegrityError, match="failed_has_reason"):
            run(lambda: _insert(self._dose_sql(), **self._row(device_pk, state="failed")))


class TestActuatorRole:
    """contracts 2.0.0 requires a role; the storage layer agrees."""

    def test_actuator_without_a_role_is_rejected(self) -> None:
        """The NULL case specifically.

        `role IN (...)` with role NULL evaluates to NULL, and a CHECK that is
        NULL PASSES in Postgres. Only the explicit IS NOT NULL closes it — the
        same trap as the sensor cadence constraint in 0001.
        """
        with pytest.raises(IntegrityError, match="actuator_declares_role"):
            run(lambda: _insert(_device_sql(), **_actuator(role=None)))

    def test_an_unknown_role_is_rejected(self) -> None:
        with pytest.raises(IntegrityError, match="actuator_declares_role"):
            run(lambda: _insert(_device_sql(), **_actuator(role="disco-ball")))

    @pytest.mark.parametrize("role", ["light", "heater", "pump", "doser", "outlet"])
    def test_reserved_roles_are_accepted(self, role: str) -> None:
        run(lambda: _insert(_device_sql(), **_actuator(role=role)))

    def test_sensors_need_no_role(self) -> None:
        """sensor_type already carries it; a role on a probe would be noise."""
        run(lambda: _insert(_device_sql(), **_sensor(role=None)))


class TestPairedClients:
    def _client_sql(self) -> str:
        return (
            "INSERT INTO paired_clients (id, name, refresh_token_hash, revoked_at) "
            "VALUES (:id, :name, :hash, :revoked)"
        )

    def test_a_live_client_has_a_hash(self) -> None:
        run(
            lambda: _insert(
                self._client_sql(),
                id=uuid.uuid4(),
                name="David's iPad",
                hash=uuid.uuid4().hex + uuid.uuid4().hex,
                revoked=None,
            )
        )

    def test_a_revoked_client_must_not_keep_a_usable_hash(self) -> None:
        """Revocation deletes the hash (auth.md §3). Keeping both states in
        sync is what stops a 'revoked' client still minting JWTs."""
        with pytest.raises(IntegrityError, match="revoked_iff_hash_cleared"):
            run(
                lambda: _insert(
                    self._client_sql(),
                    id=uuid.uuid4(),
                    name="lost phone",
                    hash=uuid.uuid4().hex + uuid.uuid4().hex,
                    revoked=datetime.now(UTC),
                )
            )

    def test_a_live_client_without_a_hash_is_rejected(self) -> None:
        with pytest.raises(IntegrityError, match="revoked_iff_hash_cleared"):
            run(
                lambda: _insert(
                    self._client_sql(),
                    id=uuid.uuid4(),
                    name="ghost",
                    hash=None,
                    revoked=None,
                )
            )

    def test_a_blank_name_is_rejected(self) -> None:
        with pytest.raises(IntegrityError, match="name_not_blank"):
            run(
                lambda: _insert(
                    self._client_sql(),
                    id=uuid.uuid4(),
                    name="   ",
                    hash=uuid.uuid4().hex + uuid.uuid4().hex,
                    revoked=None,
                )
            )


class TestPairingRequests:
    def test_approved_without_a_client_is_rejected(self) -> None:
        """An approved request with no client would mint tokens for nobody."""
        with pytest.raises(IntegrityError, match="approved_has_client"):
            run(
                lambda: _insert(
                    "INSERT INTO pairing_requests (id, client_name, state, expires_at) "
                    "VALUES (:id, 'phone', 'approved', :exp)",
                    id=uuid.uuid4(),
                    exp=datetime.now(UTC),
                )
            )

    def test_pending_is_fine_without_a_client(self) -> None:
        run(
            lambda: _insert(
                "INSERT INTO pairing_requests (id, client_name, state, expires_at) "
                "VALUES (:id, 'phone', 'pending', :exp)",
                id=uuid.uuid4(),
                exp=datetime.now(UTC),
            )
        )


class TestControlAuthority:
    """docs/device-classes.md §2, at rest."""

    def test_an_actuator_must_declare_its_authority(self) -> None:
        with pytest.raises(IntegrityError, match="actuator_declares_authority"):
            run(lambda: _insert(_device_sql(), **_actuator(control_authority=None)))

    def test_an_unknown_authority_is_rejected(self) -> None:
        with pytest.raises(IntegrityError, match="actuator_declares_authority"):
            run(lambda: _insert(_device_sql(), **_actuator(control_authority="best_effort")))

    @pytest.mark.parametrize("field", ["safe_state", "max_runtime_s", "heartbeat_timeout_s"])
    def test_authoritative_needs_the_full_triple(self, field: str) -> None:
        """R1 at rest, now scoped to the authority that can honour it."""
        with pytest.raises(IntegrityError, match="authoritative_declares_failure_behaviour"):
            run(lambda: _insert(_device_sql(), **_actuator(**{field: None})))

    def test_authoritative_must_be_failsafe_capable(self) -> None:
        with pytest.raises(IntegrityError, match="authoritative_declares_failure_behaviour"):
            run(lambda: _insert(_device_sql(), **_actuator(failsafe_capable=False)))

    def test_authoritative_must_be_local(self) -> None:
        with pytest.raises(IntegrityError, match="authoritative_declares_failure_behaviour"):
            run(lambda: _insert(_device_sql(), **_actuator(transport="network")))

    def test_advisory_stores_without_a_triple(self) -> None:
        run(
            lambda: _insert(
                _device_sql(),
                **_actuator(
                    control_authority="advisory",
                    failsafe_capable=False,
                    transport="network",
                    safe_state=None,
                    max_runtime_s=None,
                    heartbeat_timeout_s=None,
                ),
            )
        )

    @pytest.mark.parametrize("authority", ["advisory", "observe_only"])
    def test_an_uncommandable_device_may_not_carry_a_safe_state(self, authority: str) -> None:
        """Rejected, not ignored. A stored safe_state is indistinguishable from
        an enforced one to every reader of this table — and for observe_only
        there is no command path that could ever apply it."""
        with pytest.raises(IntegrityError, match="unenforceable_authority_declares_no_safe_state"):
            run(
                lambda: _insert(
                    _device_sql(),
                    **_actuator(
                        control_authority=authority,
                        failsafe_capable=False,
                        transport="network",
                        max_runtime_s=None,
                        heartbeat_timeout_s=None,
                    ),
                )
            )

    def test_observe_only_stores_bare(self) -> None:
        run(
            lambda: _insert(
                _device_sql(),
                **_actuator(
                    control_authority="observe_only",
                    failsafe_capable=False,
                    transport="network",
                    safe_state=None,
                    max_runtime_s=None,
                    heartbeat_timeout_s=None,
                ),
            )
        )

    def test_a_sensor_carries_no_control_authority(self) -> None:
        # `observe_only` rather than `authoritative`: an authoritative sensor
        # trips the failure-behaviour constraint first, which is also a
        # rejection but not the one under test here.
        with pytest.raises(IntegrityError, match="sensors_carry_no_control_authority"):
            run(lambda: _insert(_device_sql(), **_sensor(control_authority="observe_only")))

    def test_a_sensor_must_declare_a_transport(self) -> None:
        """Provenance, not authority. An alert episode is labelled with it, so a
        sensor without one would produce a series that cannot say whether the
        reading behind it was measured locally or relayed."""
        with pytest.raises(IntegrityError, match="sensor_declares_type_cadence_and_transport"):
            run(lambda: _insert(_device_sql(), **_sensor(transport=None)))


class TestOverrideTransition:
    """Migration 0018 (docs/superpowers/specs/2026-08-17-hold-transition-design.md):
    overrides.transition is CHECK-constrained to 'snap' or 'ramp'."""

    def _override_sql(self) -> str:
        return (
            "INSERT INTO overrides (id, target, level, created_at, expires_at, transition) "
            "VALUES (:id, :target, CAST(:level AS JSONB), :created, :expires, :transition)"
        )

    def _row(self, **over: object) -> dict[str, object]:
        now = datetime.now(UTC)
        row: dict[str, object] = {
            "id": uuid.uuid4(),
            "target": f"light-{uuid.uuid4().hex[:8]}",
            "level": '{"kind": "pwm", "duty": 0.0}',
            "created": now,
            "expires": now + timedelta(minutes=30),
            "transition": "snap",
        }
        row.update(over)
        return row

    def test_an_unknown_transition_is_rejected(self) -> None:
        with pytest.raises(IntegrityError, match="override_transition_valid"):
            run(lambda: _insert(self._override_sql(), **self._row(transition="fade")))

    def test_snap_is_accepted(self) -> None:
        run(lambda: _insert(self._override_sql(), **self._row(transition="snap")))


class TestLightingSchedules:
    """Migration 0019
    (docs/superpowers/specs/2026-08-19-lighting-schedules-design.md
    §Data model): ``lighting_schedules`` / ``schedule_assignments``.
    """

    @staticmethod
    def _points() -> str:
        # Minimal valid curve: two points, ascending, midnight-to-midnight.
        return '[{"at": "08:00:00", "duty": 0.2}, {"at": "20:00:00", "duty": 0.0}]'

    @staticmethod
    async def _insert_schedule(name: str) -> uuid.UUID:
        eng = engine()
        try:
            async with eng.begin() as conn:
                result = await conn.execute(
                    text(
                        "INSERT INTO lighting_schedules (id, name, points) VALUES "
                        "(:id, :name, CAST(:points AS JSONB)) RETURNING id"
                    ),
                    {"id": uuid.uuid4(), "name": name, "points": TestLightingSchedules._points()},
                )
                return uuid.UUID(str(result.scalar_one()))
        finally:
            await eng.dispose()

    @staticmethod
    async def _insert_assignment(channel_id: str, schedule_id: uuid.UUID) -> None:
        await _insert(
            "INSERT INTO schedule_assignments (channel_id, schedule_id) "
            "VALUES (:channel_id, :schedule_id)",
            channel_id=channel_id,
            schedule_id=schedule_id,
        )

    def test_schedule_name_unique(self) -> None:
        run(lambda: self._insert_schedule("This One"))
        with pytest.raises(IntegrityError, match="uq_lighting_schedules_name"):
            run(lambda: self._insert_schedule("This One"))

    def test_deleting_assigned_schedule_restricted(self) -> None:
        sid = run(lambda: self._insert_schedule("That One"))
        run(lambda: self._insert_assignment("pi-pwm-0", sid))
        # ON DELETE RESTRICT — the forgetDevice lesson, pre-applied here.
        with pytest.raises(
            IntegrityError, match="fk_schedule_assignments_schedule_id_lighting_schedules"
        ):
            run(lambda: _insert("DELETE FROM lighting_schedules WHERE id = :id", id=sid))

    def test_one_schedule_per_channel(self) -> None:
        a = run(lambda: self._insert_schedule("A"))
        b = run(lambda: self._insert_schedule("B"))
        run(lambda: self._insert_assignment("pi-pwm-1", a))
        # channel_id is the PK; assign-replace is an upsert, not a second row.
        with pytest.raises(IntegrityError, match="pk_schedule_assignments"):
            run(lambda: self._insert_assignment("pi-pwm-1", b))


class TestChipState:
    """Migration 0020
    (docs/superpowers/specs/2026-08-19-chip-state-on-the-wire-design.md
    §API, option A ruled 2026-08-18): ``chip_state`` — one row per hardware
    source instance, upserted on ``(source, instance)``.
    """

    @staticmethod
    def _chip_sql() -> str:
        return (
            "INSERT INTO chip_state "
            "(id, source, instance, initialised, initialised_at, facts, announced_at) "
            "VALUES (:id, :source, :instance, :initialised, :initialised_at, "
            "CAST(:facts AS JSONB), :announced_at)"
        )

    @staticmethod
    def _row(**over: object) -> dict[str, object]:
        now = datetime.now(UTC)
        row: dict[str, object] = {
            "id": uuid.uuid4(),
            "source": "pca9685",
            "instance": "0x40@1",
            "initialised": True,
            "initialised_at": now,
            "facts": '{"address": "0x40", "bus": 1, "pre_scale": 12}',
            "announced_at": now,
        }
        row.update(over)
        return row

    def test_one_chip_state_row_per_source_instance(self) -> None:
        run(lambda: _insert(self._chip_sql(), **self._row()))
        with pytest.raises(IntegrityError, match="uq_chip_state_source_instance"):
            run(lambda: _insert(self._chip_sql(), **self._row(id=uuid.uuid4())))
