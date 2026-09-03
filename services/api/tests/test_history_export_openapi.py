# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""`GET /api/v1/history/export` declares its `Content-Disposition` header.

The route sets the header on every response (`app.py`'s `attachment()`
helper) but the OpenAPI `responses={200: ...}` block did not say so, which
meant swift-openapi-generator emitted no `Ok.Headers` and a generated client
had no way to read the filename without hand-written middleware.

Postgres-free and NATS-free: building the `AsyncEngine` object opens no
connection (SQLAlchemy connects lazily on first use, same reasoning as
`test_audit_writer.py`'s `_writer()`), and `app.openapi()` only walks the
route table — it never calls a handler or touches the database. No
`BELLASREEF_TEST_DATABASE_URL` needed, no skip.
"""

from __future__ import annotations

from typing import Any

from bellasreef_api.app import build_app
from sqlalchemy.ext.asyncio import create_async_engine


def _schema() -> dict[str, Any]:
    engine = create_async_engine("postgresql+asyncpg://unused/unused")
    app = build_app(engine, nats_url=None, vm_url=None)
    schema: dict[str, Any] = app.openapi()
    return schema


def test_history_export_declares_content_disposition_header() -> None:
    schema = _schema()
    response_200 = schema["paths"]["/api/v1/history/export"]["get"]["responses"]["200"]
    headers = response_200["headers"]
    header = headers["Content-Disposition"]
    assert header["schema"]["type"] == "string"
    assert header["description"]
