# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Plumbing every service needs and none should reimplement.

CLAUDE.md requires structured JSON logs, a health endpoint and a metrics
endpoint from every service on day one. Two copies of that would drift, and
divergent log shapes defeat the point of structuring them — so it lives here.

Deliberately knows nothing about hardware or reef logic: control-engine must
never depend on hardware-io, so the shared parts cannot live in either.
"""

from bellasreef_service.clock import ASSUME_TRUSTED_ENV, clock_is_trusted
from bellasreef_service.httpd import Health, HealthProbe, MetricsServer, probe_once
from bellasreef_service.logging import JsonFormatter, configure_logging, get_logger
from bellasreef_service.watchdog import (
    LIVENESS_EXIT_CODE,
    LivenessGuard,
    SdNotifier,
    watchdog_interval_s,
)

__all__ = [
    "ASSUME_TRUSTED_ENV",
    "LIVENESS_EXIT_CODE",
    "Health",
    "HealthProbe",
    "JsonFormatter",
    "LivenessGuard",
    "MetricsServer",
    "SdNotifier",
    "clock_is_trusted",
    "configure_logging",
    "get_logger",
    "probe_once",
    "watchdog_interval_s",
]
