# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""An assignment event after startup must stop the service so the restart
policy rebuilds it from the retained registry. One topology path, by ruling."""

from bellasreef_hardware_io.app import HardwareIO


def test_assignment_event_requests_stop() -> None:
    service = HardwareIO(metrics_port=0)
    assert not service._stopping.is_set()
    service._on_assignment_changed()
    assert service._stopping.is_set()


def test_second_event_is_harmless() -> None:
    service = HardwareIO(metrics_port=0)
    service._on_assignment_changed()
    service._on_assignment_changed()  # burst of adoptions: one restart, no error
    assert service._stopping.is_set()
