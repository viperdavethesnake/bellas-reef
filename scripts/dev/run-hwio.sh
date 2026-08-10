#!/usr/bin/env bash
# Dev launcher for hardware-io on the Pi.
#
# The spine URL is the whole point. Without BELLASREEF_NATS_URL the service
# reads the probe, serves metrics, logs "starting" and publishes nothing — it
# looks healthy from every angle except the one that matters, and the only
# symptom is an app that never shows a temperature.
set -euo pipefail
cd "$(dirname "$0")/../.."
export BELLASREEF_NATS_URL="${BELLASREEF_NATS_URL:-nats://localhost:4222}"
export BELLASREEF_DATABASE_URL="${BELLASREEF_DATABASE_URL:-postgresql+asyncpg://bellasreef:bellasreef@localhost:5432/bellasreef}"
export BELLASREEF_ASSUME_CLOCK_TRUSTED="${BELLASREEF_ASSUME_CLOCK_TRUSTED:-1}"
exec uv run python -m bellasreef_hardware_io.app
