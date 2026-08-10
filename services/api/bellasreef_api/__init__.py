# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""API service — stateless HTTP front door."""

from bellasreef_api.app import build_app, create_app

__all__ = ["build_app", "create_app"]
