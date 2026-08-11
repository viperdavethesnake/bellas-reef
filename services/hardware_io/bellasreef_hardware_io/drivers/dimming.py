# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Rules every dimming driver obeys, stated once.

The hub has two PWM sources — RP1 hardware PWM on the Pi itself, and the
PCA9685 over I²C — selected per channel by config and indistinguishable above
hardware-io. That is the whole point of the driver contract, and it is also
exactly the situation in which two implementations quietly grow two different
opinions about what 3% duty means.

So the safety-relevant rules live here and both drivers import them. A future
third source inherits them by construction rather than by whoever writes it
remembering to read the other one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final
from uuid import uuid4

from bellasreef_contracts import ActuatorLevel, ActuatorRegistration, PwmLevel

__all__ = ["MIN_USABLE_DUTY", "light_registration", "snap_duty"]

#: Below this the XLG output is undefined — it may flicker, sit dark, or go to
#: full. A property of the LED driver, not something software can smooth over.
#:
#: Ruled in session-4 planning, recorded in CLAUDE.md "Verified host facts":
#: **anything under 8% snaps to 0.** Not clamped up to 0.08.
#:
#: A diurnal ramp crosses this band twice every day, so dawn and dusk are the
#: normal path through it, not an edge case. Of the two options, snapping down
#: is the one that cannot leave a channel emitting light at a duty the hardware
#: refuses to define.
MIN_USABLE_DUTY: Final = 0.08


def snap_duty(duty: float) -> float:
    """Apply the undefined-band rule. Returns a duty safe to hand to hardware.

    Total, not partial: every duty a driver is asked for goes through here, so
    there is no path by which a value inside the band reaches a pin.
    """
    if duty < MIN_USABLE_DUTY:
        return 0.0
    return min(duty, 1.0)


def light_registration(
    *,
    actuator_id: str,
    driver_id: str,
    safe_state: ActuatorLevel | None = None,
    max_runtime_s: float = 18 * 3600.0,
    heartbeat_timeout_s: float = 30.0,
) -> ActuatorRegistration:
    """Registration for one dimmable light channel, with the full R1 triple.

    Shared so both PWM sources register identically. A channel's safety
    contract must not depend on which silicon happens to be driving it — an
    operator swapping a light from the Pi's own PWM to a PCA9685 board is
    changing wiring, not changing what happens when the controller dies.

    ``authoritative`` because hardware-io handles nothing else
    (device-classes.md §3): both transports are local, synchronous and
    deterministic, and a write either lands or raises. That declaration is what
    obliges the other three fields; PRD R1 rejects the registration without
    them.

    ``max_runtime_s`` defaults to 18 hours: a *runaway* bound, not a
    photoperiod. A reef light legitimately runs 10–12 hours a day, so a cap near
    that trips on an ordinary Tuesday and teaches the operator to ignore it.

    ``heartbeat_timeout_s`` is 30s. Lose the control engine for half a minute
    and the channel goes dark — a visible, survivable failure, which is why
    lighting was chosen as the first actuator at all.
    """
    return ActuatorRegistration(
        message_id=uuid4(),
        emitted_at=datetime.now(UTC),
        source="hardware-io",
        actuator_id=actuator_id,
        actuator_class="pwm",
        role="light",
        driver_id=driver_id,
        control_authority="authoritative",
        failsafe_capable=True,
        transport="local",
        safe_state=safe_state if safe_state is not None else PwmLevel(duty=0.0),
        max_runtime_s=max_runtime_s,
        heartbeat_timeout_s=heartbeat_timeout_s,
    )
