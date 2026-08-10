# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Structured JSON logging.

One JSON object per line on stdout. Both deployment paths this service has —
`docker compose logs` and `journalctl` — treat stdout as the log stream, so
there is nothing to configure and no log file to rotate off a flash rootfs.

Timestamps are UTC and explicit. This host has no RTC battery, so a log line is
one of the few places you can later see that the clock was wrong.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

__all__ = ["JsonFormatter", "configure_logging", "get_logger"]

#: Attributes LogRecord always carries. Anything else a caller attached via
#: `extra=` is application context and gets emitted.
_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    """Renders a LogRecord as a single JSON object."""

    def __init__(self, *, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        # default=str so an unexpected object degrades to its repr instead of
        # taking down the logging call that was reporting a problem.
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(*, service: str, level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger.

    Replaces existing handlers rather than adding to them, so a library that
    called ``basicConfig`` cannot leave us emitting two formats.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service=service))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
