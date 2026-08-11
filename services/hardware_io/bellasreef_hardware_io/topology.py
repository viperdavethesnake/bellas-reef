# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 Bella's Reef LLC
"""What this hub is wired to, declared rather than compiled in.

``/etc/bellasreef/devices.yaml`` is host-owned, like the service env files: the
unit files and the code are the same on every hub, and what differs between two
tanks is a file on the box. Adding a second temperature probe or moving a light
from the Pi's own PWM to a PCA9685 board is an edit and a restart, not a commit.

Two properties are load-bearing.

**Identity is independent of binding.** A device's ``id`` is what the rest of
the system knows it by — the NATS subject token, the row in ``devices``, the
name on the operator's phone, the history behind it in VictoriaMetrics. The
``binding`` is merely where it happens to be plugged in this week. Rebinding a
light from ``pi-pwm`` channel 0 to a ``pca9685`` channel keeps every one of
those, because nothing downstream of this file ever learns which it was.

**A bad entry stops the service.** An unknown driver type or a malformed
binding raises at startup and names the offending entry. The alternative — skip
it and carry on — produces a hub that comes up healthy with a light missing,
which is the failure this project keeps meeting in different costumes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from bellasreef_contracts import DeviceId
from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "DEVICES_PATH",
    "ActuatorEntry",
    "Ds18b20Binding",
    "Pca9685Binding",
    "PiPwmBinding",
    "SensorEntry",
    "Topology",
    "TopologyError",
    "load_topology",
]

#: Host-owned, beside the service env files, for the same reason: this is the
#: part that differs between two hubs running identical code.
DEVICES_PATH = Path("/etc/bellasreef/devices.yaml")


class TopologyError(RuntimeError):
    """The device file is unusable. Always names the entry at fault."""


class _Strict(BaseModel):
    # Unknown keys are rejected, not ignored. A typo'd binding field would
    # otherwise silently take the default — `chanel: 1` quietly driving channel
    # 0 is exactly the class of bug a device file makes easy to write.
    model_config = ConfigDict(extra="forbid", frozen=True)


class PiPwmBinding(_Strict):
    """RP1 hardware PWM on the Pi itself."""

    driver: Literal["pi-pwm"]
    channel: int = Field(ge=0, le=3, description="pwmchip channel; npwm is 4 on RP1")
    period_ns: int = Field(default=2_000_000, gt=0)
    #: Set from bench measurement, never from argument. See the bench boundary.
    inverted: bool = False


class Pca9685Binding(_Strict):
    """A channel on a PCA9685 board over I²C."""

    driver: Literal["pca9685"]
    channel: int = Field(ge=0, le=15)
    bus: int = Field(default=1, ge=0)
    address: int = Field(default=0x40, ge=0x03, le=0x77)


class Ds18b20Binding(_Strict):
    """A 1-Wire temperature probe, addressed by its ROM code.

    The ROM is the probe's own identity, burned in at manufacture, so it
    survives being moved to a different port on the bus. It is deliberately not
    the device id: two hubs both have a probe called ``display-tank``, and
    replacing a failed probe should change one line here rather than rewriting
    the history of the tank.
    """

    driver: Literal["ds18b20"]
    rom: str = Field(pattern=r"^28-[0-9a-f]{12}$")
    poll_interval_s: float = Field(default=5.0, gt=0)
    offset_c: float = 0.0


ActuatorBinding = Annotated[PiPwmBinding | Pca9685Binding, Field(discriminator="driver")]
SensorBinding = Annotated[Ds18b20Binding, Field(discriminator="driver")]


class ActuatorEntry(_Strict):
    id: DeviceId
    #: Only ``light`` is implemented. The rest of the contract's roles are
    #: reserved rather than accepted, so a config naming one fails here instead
    #: of registering a device nothing knows how to drive.
    role: Literal["light"] = "light"
    binding: ActuatorBinding


class SensorEntry(_Strict):
    id: DeviceId
    binding: SensorBinding


class Topology(_Strict):
    """Every device this hub owns."""

    #: Bumped when the file's shape changes incompatibly. A hub reading a
    #: version it does not know refuses to start rather than guessing which
    #: fields moved.
    version: Literal[1] = 1
    actuators: list[ActuatorEntry] = Field(default_factory=list)
    sensors: list[SensorEntry] = Field(default_factory=list)

    def device_ids(self) -> list[str]:
        return [e.id for e in self.actuators] + [e.id for e in self.sensors]


def load_topology(path: Path = DEVICES_PATH) -> Topology:
    """Read and validate the device file, or raise :class:`TopologyError`.

    Every failure names what is wrong and where. An operator reading this at
    the end of a deploy has a file open in front of them; "validation error"
    with a pydantic traceback is not what helps them.
    """
    if not path.is_file():
        raise TopologyError(
            f"no device file at {path}. hardware-io is topology-driven: it does not "
            "guess what is plugged in. See docs/host-setup.md."
        )

    try:
        raw: Any = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise TopologyError(f"{path} is not valid YAML: {exc}") from exc

    if raw is None:
        raise TopologyError(f"{path} is empty")
    if not isinstance(raw, dict):
        raise TopologyError(f"{path} must be a mapping at the top level, got {type(raw).__name__}")

    try:
        topology = Topology.model_validate(raw)
    except ValidationError as exc:
        raise TopologyError(_explain(path, raw, exc)) from exc

    _reject_duplicate_ids(path, topology)
    return topology


def _explain(path: Path, raw: dict[str, Any], exc: ValidationError) -> str:
    """Turn a pydantic error into a sentence naming the offending entry.

    The default rendering points at ``actuators.0.binding.PiPwmBinding.channel``,
    which is precise and tells the operator nothing about which of their lights
    is broken. This resolves the index back to the device id they wrote.
    """
    lines = [f"{path} is not a usable device file:"]
    for error in exc.errors():
        loc = error["loc"]
        entry = _entry_id(raw, loc)
        where = ".".join(str(part) for part in loc)
        subject = f"device {entry!r}" if entry else "the file"
        lines.append(f"  - {subject}: {error['msg']} (at {where})")

        # The discriminated union's own message is unhelpfully generic, so name
        # the drivers this build actually knows.
        if error["type"] in {"union_tag_invalid", "union_tag_not_found"}:
            lines.append("    known drivers: pi-pwm, pca9685 (actuators); ds18b20 (sensors)")
    return "\n".join(lines)


def _entry_id(raw: dict[str, Any], loc: tuple[Any, ...]) -> str | None:
    if len(loc) < 2 or loc[0] not in {"actuators", "sensors"}:
        return None
    try:
        entry = raw[loc[0]][loc[1]]
    except (KeyError, IndexError, TypeError):
        return None
    return entry.get("id") if isinstance(entry, dict) else None


def _reject_duplicate_ids(path: Path, topology: Topology) -> None:
    """Two devices with one id is an ambiguity nothing downstream can resolve.

    Subjects, database rows and alert episodes are all keyed on the id, so a
    duplicate does not produce two devices — it produces one device with two
    drivers writing to it, intermittently.
    """
    seen: set[str] = set()
    duplicates: set[str] = set()
    for device_id in topology.device_ids():
        if device_id in seen:
            duplicates.add(device_id)
        seen.add(device_id)
    if duplicates:
        raise TopologyError(
            f"{path} declares duplicate device id(s): {', '.join(sorted(duplicates))}. "
            "Ids key the NATS subject, the devices row and the alert history; two "
            "devices sharing one would interleave rather than coexist."
        )
