#!/usr/bin/env bash
# Dev launcher for control-engine on the Pi.
#
# Threshold alerting needs BOTH a database (bands + episodes) and the spine
# (readings in, alerts out). With either missing the engine still schedules
# lighting and logs that alerting is disabled — see ControlEngine._start_alerting.
set -euo pipefail
cd "$(dirname "$0")/../.."
export BELLASREEF_NATS_URL="${BELLASREEF_NATS_URL:-nats://localhost:4222}"
export BELLASREEF_DATABASE_URL="${BELLASREEF_DATABASE_URL:-postgresql+asyncpg://bellasreef:bellasreef@localhost:5432/bellasreef}"
export BELLASREEF_ASSUME_CLOCK_TRUSTED="${BELLASREEF_ASSUME_CLOCK_TRUSTED:-1}"
exec uv run python -m bellasreef_control_engine.app
