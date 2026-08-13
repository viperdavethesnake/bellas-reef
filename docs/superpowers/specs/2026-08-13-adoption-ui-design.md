# Adoption UI — design

2026-08-13. Approved by David in session; implements the app half of the
capability registry (backend shipped 2026-08-12, "adoption-UI gap" since).

## Purpose

The hub announces what its hardware can offer (`/api/v1/capabilities`, tier
one of the registry) and the API can bind a channel to a device
(`bindDevice`) and release it (`unbindDevice`) — but the app has no screens
for any of it. Adopting hardware today means the CLI import file. This unit
gives the operator the registry in the app: see what the hub can drive, adopt
a channel as a device, release one, with a safety confirm standing between a
tap and real actuator output.

## Placement ruling (David, 2026-08-13): System is never a junk drawer

**System tab = inventory and lifecycle.** A "Hardware" section on the System
page — adopted devices and unclaimed channels, adopt/unadopt. It grows by
listing more sources, never by acquiring controls.

**Function tabs = operation.** An adopted PWM channel is *operated* on the
Lighting tab; sensors keep their detail sheet off Tank. Future roles (pump,
heater) get their operational surface where they are used, never in System.

Functionality alignment is the invariant to preserve in every later addition.

## The screens

**Hardware section** on SystemView, below "Paired devices":

- *Adopted* rows first: display name (fallback `device_id`), channel +
  driver, role badge when present. Each row has an **Unadopt** action behind
  a standard confirmation dialog. Unadopt copy states the safe direction: the
  engine stops commanding the channel and history is kept — the hub's
  `unbindDevice` is soft-delete by design, so re-adopting the same hardware
  later reattaches its history rather than forking it.
- *Available channels* below: announced-but-unclaimed capabilities
  (`bound_to == nil`), rendered as `source · channel` with the useful bits of
  `detail` (e.g. the I2C address or GPIO). Tapping one opens the adopt sheet.
- Loading/failure states mirror the paired-devices list exactly, including
  the stale-list banner; the section refreshes via SystemView's existing
  `loadEverything()` / `.refreshable` path.

**Adopt sheet** (per unclaimed channel):

- Channel and driver shown fixed (derived from the capability row; the
  operator never types them).
- Name field, seeded with a sensible default from source/channel; required
  non-empty.
- Role picker. Sources map to what the contract allows: a `w1-bus` probe is
  a sensor and carries no role; PWM sources require `light` (the only
  implemented role). Rendered as a picker with one legal choice so future
  roles have a home, disabled rather than hidden when only one is legal.
- **Adopt** button, enabled only when the form is valid, gated by the safety
  confirm (below).
- The three refusal endings the hub documents render verbatim as inline
  errors: 404 (channel no longer announced), 409 (already bound — someone
  else claimed it since the list loaded), 422 (role not legal).

**Safety confirm** (actuator sources only — not shown for `w1-bus`):

> Adopting starts real output on this channel as soon as the engine's
> schedule runs. Only adopt hardware you have bench-verified.

Confirmation dialog with a destructive-styled "Adopt" and a Cancel. This is
the guardrail that lets the screens ship while led-blue adoption stays
bench-gated: the operator decides, with the consequence stated at the moment
of decision. Sensors adopt without it — a probe read has no failure mode
worth the friction.

## Contract facts (pinned from the deployed API, contracts 3.5.0)

- `listCapabilities` → `[CapabilityView]`: `source` (`"pi-pwm" | "pca9685" |
  "w1-bus"`), `channel` (string; PWM channel number or 1-Wire ROM), `detail`
  (free dict, render what's useful), `announced_at`, `bound_to`
  (device_id or null).
- `bindDevice` (POST `/api/v1/devices`): `device_id` (proposed,
  pattern `^[a-z0-9][a-z0-9_-]{0,63}$`; ignored when the channel already has
  a device — match-before-create), `driver_type` (`"pi-pwm" | "pca9685" |
  "ds18b20"`), `channel`, `role` (`"light"` or absent), `display_name`,
  optional `location`, `poll_interval_s`. Driver for a capability:
  `w1-bus → ds18b20`, PWM sources map to themselves. Returns `BoundDevice`
  (`device_id`, `created`, `driver_type`, `channel`). Endings: 404/409/422
  as above.
- `unbindDevice` (DELETE `/api/v1/devices/{device_id}`): 204 on success,
  404 unknown-or-already-unbound. Soft under the hood; the row and history
  survive.
- `listDevices` → `[DeviceView]` (already wrapped as `HubClient.devices()`).

All four operations already exist in the generated `BellasReefAPI` client
(spec 3.5); this unit adds kit wrappers and screens, no contract change.

## Plumbing

`HubClient` gains three wrappers in the kit's typed-outcome idiom:

- `capabilities() async throws -> [Components.Schemas.CapabilityView]`
- `bind(_:) async throws -> BindOutcome` — an enum with one case per
  documented ending (`bound(created:deviceId:)`, `channelGone`,
  `alreadyBound`, `roleNotLegal(String)`), because each needs different
  words and a different way out.
- `unbind(deviceId:) async throws -> UnbindOutcome` (`unbound`,
  `alreadyUnbound`).

401s flow through the existing middleware/retry/rejection path untouched.

## Testing

- Kit tests (`StubTransport`): every documented ending of the three
  wrappers, including 409-after-list-staleness and the match-before-create
  `created: false` case.
- UI test: reach the Hardware section, open the adopt sheet for a fake-free
  channel — asserts the safety confirm exists for an actuator source and
  that Adopt is disabled with an empty name. Stops short of adopting
  anything real (same philosophy as the approver-screen test).
- The live adopt of led-blue is **not** part of this unit's verification —
  it stays bench-gated until Stage 1/2 prove duty 0.0 dark at the FET drain.

## Out of scope

Operating adopted devices (Lighting tab work), new roles, capability
re-announcement UX, and any backend change. The unit is app-only.
