# iOS app — design brief v1.3

**Status:** active — v1.3 adds violet to §2 as the silence class's colour; v1.2 amends §2 (destructive controls vs safety red); v1.1 added §7 UX standards · **Owner:** David / Bella's Reef LLC
**Scope:** look/feel, palette, screen map, UX standards, and the one contract
change the app design requires. The app is closed-source (see PRD Q3); this
brief lives in the public repo because it drives API and contract decisions,
not app code.

---

## 1. Design language

**iOS 26+ Liquid Glass, applied by its own rule:** glass is exclusively for the
navigation layer floating above content — never applied to content itself
(lists, tables, readings, charts). The app is *content-first with glass
chrome*: tab bar, toolbars, and contextual action sheets are glass; sensor
values, curves, and states are solid content beneath. A temperature reading
never shimmers.

Modern, sleek, minimal. No borders where spacing will do, no visual noise
competing with the data. SwiftUI native materials only — no custom glass
imitations.

## 2. Palette

The tank carries the color; the UI stays nearly monochrome.

| Role | Color | Rule |
|---|---|---|
| Base (dark) | Deep blue-gray near-black — 20,000K water, not pure black | **Dark mode is primary.** This app opens at night next to an actinic-lit tank; a white screen is a flashbang. Light mode supported, secondary. |
| Accent | Single saturated electric teal/cyan | Interactive elements and the healthy state. One accent, everywhere. |
| Amber | Attention | Stale sensor, pending approval, override active. |
| Red | Safety only, in status and data | Interlock latched, fail-safe fired, clock untrusted. **Red never appears in status or data display for anything else** — when a reading or a status line goes red, it means something. |
| Violet | The silence alert class, and nothing else | Added v1.3. A probe has stopped reporting: *we no longer know*, as distinct from we know and it is bad. **This color has exactly one meaning and may never be borrowed for a second one.** Amber would file "nobody has any idea what the tank is doing" beside "the tank is slightly cold", and the first is strictly worse. Red would assert a certainty about the water that silence is precisely the absence of. Violet sits outside the temperature metaphor on purpose: this band is a statement about the instrumentation, not about the tank. Applies to the silence banner, its glyph, and its History band; it is not an accent, not a chart series color, and not available for a future feature that merely needs a fifth hue. |
| Destructive controls | Standard iOS red | Amended v1.2. Destructive *controls* — `.destructive` buttons, confirmation dialogs, swipe-to-delete — follow the platform convention, including its red. This is not a safety signal and does not weaken the rule above: safety-red governs what the app is *telling you about the tank*, while control-red is what iOS uses to say "this deletes something". Overloading a system-standard control colour would make unpair look like a hardware fault. |
| Channel colors | Inherited from fixture config | A royal-blue channel renders royal blue. The lighting screen's palette comes from the hardware, not the design system. |

## 3. Screen map — four tabs

1. **Tank** (home). One glance = is my tank okay. Temp as hero number with
   sparkline; current light state as a live per-channel spectrum bar; safety
   status line (teal dot "all clear" / amber / red exception). 90% of opens
   end here.
2. **Lighting.** The 24h day curve per channel with current-time cursor;
   drag-to-edit control points; tap a channel to solo. Manual override as a
   glass action (hold current / 50% / off, for N minutes) with the auto-revert
   timer **always loudly visible** — override state is never silent.
3. **History.** Charts off VictoriaMetrics via the API: temp, per-channel
   duty, later everything else. Time-range picker in the glass toolbar.
4. **System.** Paired devices (auth.md surface), hub health, audit log view,
   and operational config.

## 4. Config split

- **In-app (operational):** schedules and ramp curves, overrides, device
  names, alert thresholds. The daily-touch subset.
- **Web UI first (structural):** hardware registration, channel wiring,
  calibration. R17 assigns the full config surface to the web UI; the app
  adopts pieces later if usage justifies it.

## 5. Contract change required: `role`

PWM is lights today, possibly pumps and more later; 1-wire is temp and likely
only ever temp. The generalization is solved **in the model, not the UI**:

- Add `role` to actuator registration and the devices table
  (`light` now; `pump` etc. reserved). The wire contract stays
  class-based (`binary`/`pwm`); the app renders by role — lights get spectrum
  bars and day curves, a future pump gets a flow dial. Same contract,
  different presentation.
- Sensors need nothing: `sensor_type` already carries it.
- This lands **before the OpenAPI spec freezes**, because retrofitting it into
  a generated client is a breaking change for no reason.

## 6. Non-goals

Custom design systems, light-mode-first, per-user themes, widgets/watch/live
activities (later, on their own merits), any UI for hardware not yet in the
PRD. The ceiling is the PRD.

## 7. UX standards — interaction law

Added v1.1, after milestone 1 proved the vertical. These are review criteria,
not suggestions: a PR that violates one gets flagged in design review the same
way a failing test gets flagged in CI.

### 7.1 State completeness

Every view designs all five states explicitly: **loading, empty, populated,
error, reconnecting**. No spinner-forever, no blank panels, no view that only
looks right when everything works. The milestone-1 amber "Disconnected — could
not connect" (instead of red, instead of a stale number) is the canon example;
that instinct is now law.

### 7.2 Data honesty

Never present a stale value as current. A reading older than its expected
cadence dims and gains an age stamp ("78.3° · 2m ago"). A faulted sensor shows
*fault*, never its last good number. This is the UI twin of the driver rule
that a timeout yields `quality="fault"` rather than a stale value with a fresh
timestamp — the honesty chain runs probe → wire → glass unbroken.

### 7.3 Motion

One motion system. Values crossfade on change, never jump-cut. The spectrum
bar animates level changes at the engine's slew rate — the app *shows* the
ramp physics rather than teleporting between states. Reduce Motion is
respected everywhere: crossfades become instant swaps, nothing essential is
conveyed by animation alone.

### 7.4 Touch and haptics

44pt minimum touch targets, no exceptions. Overrides and approvals confirm
with haptics (success notification style). Destructive actions — revoke a
client, release an override early — use the standard iOS confirmation
pattern; nothing destructive fires on a single tap. Buttons that talk to the
hub show their in-flight state; a tap that silently does nothing for 800ms is
a bug.

### 7.5 Dynamic Type and accessibility

Hero numbers and body text scale with Dynamic Type; layouts reflow rather
than truncate at accessibility sizes. VoiceOver labels carry meaning, not
widget names: the spectrum bar reads "Royal blue, ninety percent," the status
line reads its actual state. Contrast for all text against the dark palette
meets WCAG AA; the dimmed stale-data treatment (§7.2) must remain readable,
not decorative-gray.

### 7.6 Glass discipline (enforcement of §1)

Glass appears on exactly: tab bar, toolbars, contextual action sheets.
Content — readings, charts, lists, banners — is solid. A PR that applies
glass/material to a content surface fails design review. When in doubt: if
it displays data, it is not glass.

### 7.7 Alert presentation

Alerts follow the palette's severity law (§2). Amber banners state the
reading, the threshold, and the age ("78.9° — above 78.5° max · now"), not
just "alert." Red is reserved for the safety class and always names what
latched. Banners never cover the data they describe. Clearing an alert in the
UI never clears the condition — acknowledgment and resolution are visibly
different things.
