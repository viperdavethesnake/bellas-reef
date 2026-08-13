# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""Which devices an operator has adopted, per the retained assignment stream.

The engine's half of the contract hardware-io already honours: assignments are
tier two of the registry, on the wire (see DeviceAssignment in the contracts
package). hardware-io builds drivers from them; the engine consults them so it
never commands a channel nobody has adopted. Same stream, same tombstone
semantics, no database dependency.
"""

from __future__ import annotations

from collections.abc import Callable

from bellasreef_contracts import DeviceAssignment

__all__ = ["AssignmentLedger"]


class AssignmentLedger:
    """Pure state. Feeding it is the publisher's job; consulting it is the tick's."""

    def __init__(self) -> None:
        self._adopted: set[str] = set()
        #: Called with the device_id whenever apply() sees a tombstone
        #: (adopted=False), after this ledger's own state is updated. Plain
        #: attribute, settable any time after construction — deliberately not
        #: a constructor argument, since ControlEngine wires it to
        #: self.scheduler.forget only after both objects exist.
        #:
        #: Fires on *any* tombstone, including one for a device this ledger
        #: never had adopted (e.g. a duplicate unbind, or a tombstone learned
        #: for the first time via a re-drain). Forgetting an already-cold
        #: scheduler channel is idempotent and harmless, and trying to track
        #: "was it previously adopted" here would just be re-deriving what
        #: apply() itself already decided — badly, since this ledger has no
        #: visibility into what the scheduler currently believes.
        self.on_tombstone: Callable[[str], None] | None = None

    @property
    def adopted(self) -> frozenset[str]:
        return frozenset(self._adopted)

    def is_adopted(self, device_id: str) -> bool:
        return device_id in self._adopted

    def apply(self, assignment: DeviceAssignment) -> None:
        if assignment.adopted:
            self._adopted.add(assignment.device_id)
        else:
            # adopted=False is the tombstone: the channel is free again.
            self._adopted.discard(assignment.device_id)
            if self.on_tombstone is not None:
                self.on_tombstone(assignment.device_id)
