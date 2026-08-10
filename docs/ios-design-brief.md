# iOS app — design brief v1

**Status:** direction settled, pre-implementation · **Owner:** David / Bella's Reef LLC
**Scope:** look/feel, palette, screen map, and the one contract change the app
design requires. The app is closed-source (see PRD Q3); this brief lives in the
public repo because it drives API and contract decisions, not app code.

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
| Red | Safety only | Interlock latched, fail-safe fired, clock untrusted. **Red appears nowhere else in the app, ever** — when it shows, it means something. |
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
