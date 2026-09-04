# Identify before adopt (C4)

Ruled by David 2026-09-02: take the **provisional-adopt** side of the C4 fork
(`docs/drafts/2026-09-01-ux-items-plan.md` §C4). Adopt the channel with no name,
pulse it through the override path that already exists, then confirm or rename.
No new hardware-io command, and no pulse on an unadopted channel: an unadopted
channel has no declared safe state, so nothing may drive it.

The problem this closes is the one the UX review named as the most expensive
mistake the app allows (`docs/bellas-reef-ios-ux-review.md` §C4): sixteen
PCA9685 rows look identical, the operator picks one from memory, and the wrong
physical fixture ends up wired to a schedule.

What ships is an Identify step inside the existing adopt sheet, one line of API
change, and nothing in hardware-io or control-engine. The pulse is an ordinary
manual hold: `POST /api/v1/overrides` at duty 0.50, transition `snap`, duration
5 s, released by its own expiry. (Amended 2026-09-03 by David's ruling from the
draft's 0.30 / 3 s; see open question 1.)

## The flow

1. **Pick the channel.** Unchanged. The Available channels list on the System
   tab's Hardware section, adopt sheet opens
   (`docs/superpowers/specs/2026-08-13-adoption-ui-design.md`).
2. **Provisional adopt.** `POST /api/v1/devices` with the channel, driver type,
   role `light`, the `device_id` the sheet proposes today (`<source>-<channel>`,
   the form the hub's rows already use: `pi-pwm-0`, `pca9685-0`), and
   **`display_name` omitted**. The adopt sheet's existing safety confirm still
   stands in front of this: adopting starts real output.
3. **Wait for the rebuild.** Adoption restarts hardware-io; the client waits for
   proof the channel is built (see "Waiting for the rebuild").
4. **Pulse.** One override, parameters below. The operator watches the tank.
5. **Answer.** Three endings:
   - *Yes, name it*: `PATCH /api/v1/devices/{device_id}` with the typed name.
     Done, the device is an ordinary adopted light.
   - *Pulse again*: repeat step 4. No restart, the device is already adopted.
   - *Not this one*: `DELETE /api/v1/devices/{device_id}` (unbind), then
     `POST /api/v1/devices/{device_id}/forget` **only if** the bind returned
     `created: true`. Back to the channel list.

The forget guard is load-bearing. `bind_device` matches before it creates
(`services/api/bellasreef_api/store.py:Store.bind_device`), so a channel adopted
and released in an earlier session comes back as the same row, with its old
name, thresholds and history, and `created` is `false`. Forgetting that row
would delete a device the operator built. A `created: true` row is seconds old
and holds nothing, so unbind plus forget leaves no detached litter behind a
sweep through sixteen channels.

## Provisional adoption is a name, not a state

**Decision: no flag, no column, no contracts bump.** A provisionally adopted
device is an adopted device whose `display_name` is NULL. The registry already
models that: `display_name` is `str | None`
(`services/api/bellasreef_api/app.py:DeviceView`), `DeviceName._blank_is_not_a_name`
normalises whitespace to NULL so "no name" is one state rather than two, and the
app already renders a nameless row as its id, `displayName ?? deviceId`
(BellasReefKit `EquipmentRows.swift:76`, and the same expression in
`LightingCards.swift`, `DeviceCatalog.swift` and `SystemView.swift`).
`DeviceView.name` in the API is a plain `@property` and is not serialised, so
the client fallback is what satisfies the placeholder requirement.

Nothing stops an unnamed device living forever, and nothing should: it is a
missing name, not a hole. The device is adopted, authoritative, carries the full
safety triple and runs schedules like any other, and it renders as `pi-pwm-0`
instead of "Left fixture". At one operator and a dozen channels that costs
legibility, not safety.

A flag would cost a migration, an API field, a contracts MINOR, and a second
class of adopted device that hardware-io and control-engine would both have to
ignore. `docs/device-classes.md` §2 is the reason not to invent one: the axis
that decides what a registration promises is control authority, and a
provisional adoption promises exactly what every other one does.
`Store.bind_device` writes `control_authority='authoritative'`,
`failsafe_capable=true`, `safe_state={"kind":"pwm","duty":0.0}`,
`max_runtime_s=LIGHT_MAX_RUNTIME_S` (18 h) and
`heartbeat_timeout_s=LIGHT_HEARTBEAT_TIMEOUT_S` (30 s, both from
`contracts/python/bellasreef_contracts/messages.py`). There is no lesser
adoption to declare.

## The pulse

`target` the new `device_id`, `duty` 0.50, `transition` `"snap"`, `duration_s`
5.0, `reason` `"identify"`. Duty 0.50 is clear of the undefined band, plainly
visible in a lit room, and not a full-power flash. Snap because identify is an
operator standing at the tank: a ramp at 0.05/s would take ten seconds to
arrive (spec 2026-08-17).

**5 s is legal, and there is no minimum to work around.**
`OverrideRequest.duration_s` is `Field(gt=0.0, le=86400.0)`
(`services/api/bellasreef_api/app.py`) and `OverrideStore.create` refuses only
`duration_s <= 0` (`db/bellasreef_db/overrides.py`). The real floor is the
engine tick, 1 s (`ControlEngine.__init__`, `loop_interval_s: float = 1.0`), so
a 5 s hold is four to five ticks: arrival within a tick of creation, release
within a tick of expiry.

**0.50 is above the 8 % floor.** `snap_duty` snaps anything below
`MIN_USABLE_DUTY = 0.08` to zero
(`services/hardware_io/bellasreef_hardware_io/drivers/dimming.py`), so the pulse
reaches the pin at 1.654 V of the 3.31 V full scale, the 50 % row measured on
both silicons (CLAUDE.md Stage 1 and Stage 2). A duty inside the band would
measure dark and read as "not this one" on a channel that was right.

**The server ends it, not the client.** No client timer, no `DELETE` on the
happy path: the hold expires 5 s after creation and `ControlEngine`
`_expire_overrides` closes the row against its monotonic deadline. A phone
backgrounded or killed mid-pulse leaves nothing behind.
`DELETE /api/v1/overrides/{id}` stays available for a cancel taken inside the
five seconds, tolerating 404.

**Audit.** Two existing rows, both category `command`: `override.created` from
`create_override`, and `override.released` with reason `expired` from the
engine's `_audit_override_release`. One backend change, the only one in this
spec: `create_override`'s `override.created` detail does not include the
request's `reason` today (`services/api/bellasreef_api/app.py`), so an identify
pulse is indistinguishable in the trail from a manual 50 % hold. Add
`"reason": body.reason` to that detail dict, taking it from the request because
`ActiveOverride` (`db/bellasreef_db/overrides.py`) does not carry one.
`AuditEvent.event` is `dict[str, Any]`, so this is not an OpenAPI change and
needs no contracts bump.

## Waiting for the rebuild

Adopting restarts hardware-io, every time: a `DeviceAssignment` for a
`device_id` this process never built is news, and `_on_assignment_message` falls
through to `_on_assignment_changed`, which exits so the restart policy rebuilds
from the registry (`services/hardware_io/bellasreef_hardware_io/app.py`,
event `assignment_restart`). Measured recovery is about 15 s (CLAUDE.md).

A pulse issued into that window is not merely slow, it is silently wrong. The
API accepts the override (it gates on authority and the clock, not on whether
hardware-io is up), the engine sees the
channel adopted through its `AssignmentLedger` and publishes a command with a
30 s TTL (`DEFAULT_COMMAND_TTL_S = 30.0`, engine `publisher.py`), and the
command waits in the BR_CMD workqueue with nobody bound. Three seconds later the
hold expires and the release queues behind it. hardware-io comes back, drains
both in order, and the fixture flashes for milliseconds or not at all. The
operator sees nothing and answers "not this one" about the right channel.

**The client waits for a `StateFrame` for the new `device_id` on the existing
`/api/v1/stream` socket, whose `payload.emitted_at` is newer than any frame it
had for that id before adopting.** That frame is hardware-io's per-actuator
startup publish, and it is the last thing the rebuild does:
`_connect_spine` reads the registry and opens each actuator, watches
assignments, announces capabilities, publishes registrations, then calls
`_publish_startup_states`, which emits one `ActuatorState` per registered
actuator with `reason="startup"`. A frame for that id therefore proves the
channel was built, opened and registered in the supervisor, not merely that a
process restarted.

The `emitted_at` comparison is needed because BR_STATE is retained
last-value-per-subject and the API replays it on socket open
(`stream.py:_retained_state`), so a re-adopted channel can produce a frame from
its previous life. Both timestamps come from the hub, so this is one clock. The
startup publish precedes hardware-io's `CommandConsumer` subscription, which is
harmless: BR_CMD is a durable workqueue, so a command published in that gap is
delivered when the consumer binds.

Rejected: polling `GET /api/v1/capabilities` for a moved `announced_at`. It is
ordered after the build too, but it is per process rather than per channel, and
an actuator whose `open()` fails is skipped while its capability is still
announced, so it would call a channel ready that has no driver. `bound_to` is
worse: `Store.list_capabilities` computes it by joining `devices.adopted`, true
the moment the API writes the row.

Timeout: 45 s, three times the measured restart. On timeout the sheet says the
hub is still restarting and offers Retry, which waits again. The adoption
stands either way.

## What hardware-io and control-engine need

Nothing. No new subject, no new command, no new field, no new code. hardware-io
already builds a driver for any adopted assignment (`_on_assignment_message`),
publishes the startup state the client waits on (`_publish_startup_states`) and
applies the 8 % rule to whatever duty arrives (`snap_duty`). control-engine
already honours a manual hold on any adopted channel: `_tick` reloads overrides
every second, gates on `self.assignments.is_adopted`, and emits a snap hold in
one step per the 2026-08-17 transition rules; an unprofiled channel rests at
`SAFE_DUTY`, so the release returns it to dark with no schedule involved.

The API change is the one audit line above. Everything else in the flow is calls
that exist: `bindDevice`, `createOverride`, `renameDevice`, `unbindDevice`,
`forgetDevice`.

## Failure paths

| Case | Behaviour |
|---|---|
| Hub unreachable after the adopt succeeded | The channel is adopted and unnamed. Nothing is stuck: the Hardware section shows it as an adopted row rendered by `device_id`, and the operator renames or unadopts it there. A second pulse means unadopt and start again (identify from a device row is out of scope). |
| Adopt refused, or the request never lands | Nothing was adopted. The 404/409/422 endings the adopt sheet already renders verbatim; a transport failure is the sheet's existing error state. |
| Override refused 503 | The clock is not synchronised (`OverrideStore.create` clock gate). Copy: the hub's clock is still syncing, try Identify again in a moment. The adoption stands. |
| Override refused 409 or 422 | 409 is an `observe_only` target and 422 comes from the store's `ValueError` on duty, duration or transition. Neither is reachable here (`Store.bind_device` writes `authoritative`; the three values are constants), so both render as the generic failure with Retry rather than being special-cased. |
| App backgrounded mid-pulse | Nothing to clean up. The hold expires server-side; see "The server ends it". On return the sheet is at the answer step, and Pulse again is one tap. |
| Duty below the 8 % floor | Not reachable. 0.50 is the constant, not an operator input. |
| Wrong fixture lights | The Not this one path above. Costs a second restart of hardware-io for the tombstone. |
| Channel already carries a schedule assignment | Legal (schedule-before-adoption is allowed by the engine's gate). The channel starts converging to the curve the moment it is adopted, the pulse overrides it for 5 s, and the release returns it to the curve rather than to dark. Nothing special to do, but the copy should not promise the channel goes dark afterwards. |

Cost, stated plainly: a confirmed identify is one hardware-io restart and a
rejected one is two, each pausing sensor telemetry for about 15 s. No alert
fires: the silence deadline is `max(cadence x 6, 30 s)`
(`services/control_engine/bellasreef_control_engine/alerts.py`,
`SILENCE_FLOOR_S = 30.0`), and the 30 s floor covers a restart at any cadence.
It is the price of the ruling that nothing drives an unadopted channel.

## iOS surface

The adopt sheet, unchanged in placement and in its safety confirm. It gains a
phase machine rather than a second screen.

| Phase | Copy and controls |
|---|---|
| Choose | Channel and driver fixed, role picker as today. Primary button **Identify this channel**. Secondary **Adopt without identifying** (today's path, name required). |
| Adopting | "Adopting the channel. The hub restarts to pick it up, about 15 seconds." Progress, plus **Cancel**, which unadopts and forgets exactly like Not this one. |
| Pulsing | "Watch your fixtures. PWM ch 3 is at 50 percent for 5 seconds." |
| Answer | "Did the right fixture light up?" Buttons **Yes, name it**, **Pulse again**, **Not this one**. |
| Naming | Name field, Save calls `renameDevice`. Prefilled with the existing name when the bind matched a detached row (`created: false`), empty otherwise. |
| Failed | The reason, plus **Retry** and **Not this one**. |

Vocabulary: "PWM ch n", never "LED n", never "dimmer" (David, repeated). The
channel number shown is the one the row the operator tapped shows.

**Accessibility.** The pulse is visual and there is no substitute for it, so
the flow never depends on seeing it. Every phase change posts a VoiceOver
announcement ("Pulsing PWM ch 3", "Pulse finished"), the three answer buttons
are reachable and labelled without having seen anything, Pulse again lets
someone repeat it for a helper in the room, and the naming step is identical to
the non-identify path. An operator who cannot use the pulse loses the
confirmation, never the ability to adopt and name a channel.

## Testing

**API.** One new case in `services/api/tests/test_stream_and_overrides.py`: a
`createOverride` carrying `reason` writes it into the `override.created` audit
detail, and omitting it leaves the key absent or null. Nothing else is new
because nothing else changed; `test_device_binding.py` already covers the bind,
rename, unbind and forget calls this flow chains.

**iOS kit.** Tests for the flow state machine against `StubTransport`, one per
transition: adopt then wait then pulse then name (happy path); adopt matching a
detached row (`created: false`) then Not this one issues unbind and **no**
forget; adopt creating a row then Not this one issues unbind **and** forget;
override 503 lands in Failed with the adoption intact; no state frame within
45 s lands in Failed with Retry; a retained frame older than the adopt does not
satisfy the wait.

**Bench acceptance, Stage 2 method** (David's meter, same probe point as that
leg's Stage 1). One row on `pi-pwm-0`, pin 32: 0 V before Identify, 1.654 V
held for about 5 s during the pulse (0.50 x the 3.309 V measured full scale,
the Stage 1 and Stage 2 50 % row), 0 V after. Then the deploy gate as always:
CI green, `v*` tag, release workflow green, `update-hub.sh` on the hub, telemetry
verified on the wire.

## Out of scope, named

- Blink patterns and N-pulse identify: more code, no more certainty on a hub
  with two to sixteen channels.
- Identify from the System tab or from an adopted device row: the moment it is
  needed is mid-adoption.
- Any pulse on an unadopted channel, and so any new hardware-io command surface.
  This is the ruled side of the fork, not an omission.
- Identify for sensors: a DS18B20 has nothing to pulse and its ROM is already
  its identity.
- Changing the 8 % floor, the 0.05/s slew, or restart-on-adopt.
- A sweep mode that pulses every unadopted channel in turn.

## Open questions

None block implementation. Two decisions are cheap to overrule, both made
above:

1. **0.50 and 5 s.** RULED 2026-09-03 by David: the draft's 0.30 for 3 s is
   raised to 0.50 for 5 s. The 50 % point is one both silicons have been
   metered at (1.654 V), so the bench row needs no new prediction. If the
   bench still finds it hard to see, raise the duty rather than lengthen the
   hold: brightness is what identifies, and a longer hold only widens the
   window in which a schedule is being overridden.
2. **Not this one forgets the row it created.** The alternative is unbind only,
   which leaves a nameless detached row per rejected channel. Recommended
   answer stands: forget, guarded by `created: true`.
