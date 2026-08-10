#!/usr/bin/env bash
# Dev launcher for the API on the Pi. Without BELLASREEF_NATS_URL the WebSocket
# closes with 1011 "stream unavailable: no spine" and auth events never reach
# bellasreef.audit.auth.
set -euo pipefail
cd "$(dirname "$0")/../.."
export BELLASREEF_NATS_URL="${BELLASREEF_NATS_URL:-nats://localhost:4222}"
export BELLASREEF_DATABASE_URL="${BELLASREEF_DATABASE_URL:-postgresql+asyncpg://bellasreef:bellasreef@localhost:5432/bellasreef}"
export BELLASREEF_ASSUME_CLOCK_TRUSTED="${BELLASREEF_ASSUME_CLOCK_TRUSTED:-1}"
exec uv run uvicorn bellasreef_api.app:create_app --factory --host 0.0.0.0 --port 8000
