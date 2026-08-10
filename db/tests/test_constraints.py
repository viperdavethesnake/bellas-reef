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
from datetime import UTC, datetime

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
    "actuator_class, role, safe_state, max_runtime_s, heartbeat_timeout_s"
)


def _device_sql() -> str:
    return (
        f"INSERT INTO devices ({_DEVICE_COLS}) VALUES "
        "(:id, :device_id, :kind, :driver_id, :sensor_type, :poll_interval_s, "
        ":actuator_class, :role, CAST(:safe_state AS JSONB), :max_runtime_s, "
        ":heartbeat_timeout_s)"
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
        with pytest.raises(IntegrityError, match="sensor_declares_type_and_cadence"):
            run(lambda: _insert(_device_sql(), **_sensor(poll_interval_s=None)))

    def test_zero_cadence_sensor_is_rejected(self) -> None:
        with pytest.raises(IntegrityError, match="sensor_declares_type_and_cadence"):
            run(lambda: _insert(_device_sql(), **_sensor(poll_interval_s=0.0)))

    def test_null_sensor_type_is_rejected(self) -> None:
        with pytest.raises(IntegrityError, match="sensor_declares_type_and_cadence"):
            run(lambda: _insert(_device_sql(), **_sensor(sensor_type=None)))


class TestActuatorFailureBehaviour:
    def test_valid_actuator_is_accepted(self) -> None:
        run(lambda: _insert(_device_sql(), **_actuator()))

    @pytest.mark.parametrize(
        "field", ["safe_state", "max_runtime_s", "heartbeat_timeout_s", "actuator_class"]
    )
    def test_actuator_without_declared_failure_behaviour_is_rejected(self, field: str) -> None:
        with pytest.raises(IntegrityError, match="actuator_declares_failure_behaviour"):
            run(lambda: _insert(_device_sql(), **_actuator(**{field: None})))

    @pytest.mark.parametrize("field", ["max_runtime_s", "heartbeat_timeout_s"])
    def test_non_positive_timers_are_rejected(self, field: str) -> None:
        with pytest.raises(IntegrityError, match="actuator_declares_failure_behaviour"):
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
