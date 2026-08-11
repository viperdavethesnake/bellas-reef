# Prior-art review — bellasreef-archived → current design gaps

> The archived projects died of complexity accretion — features, gates, and
> process added by AI agents faster than they were needed. This ledger exists so
> features are looked up when their area is being built, never imported in
> advance. The dispositions here are notes, not commitments; nothing enters the
> PRD from this document without David deciding to build it.

**Status:** review complete 2026-08-11 · **Owner:** David / Bella's Reef LLC
**Purpose:** full-archive review of `reference/bellasreef-archived` (all services,
not just HAL). Records every design concept the archive carries that the current
system lacks, with a disposition for each. This is a lookup table, not a
backlog: consult it when building in an area, and leave the rest alone.

Dispositions: **ADOPT-NOW** (current/next pass) · **P1** (fast-follow, PRD §8)
· **P2** (design-for, build later) · **SUPERSEDED** (current design already
covers it better) · **REJECTED** (deliberately not carried forward).

---

## 1. HAL service — mined previously, restated

Two-tier controller→channel registry, discover-then-register operator flow,
role at registration, 2-channel GPIO12/13 pwmchip0 hardware reality, sysfs race
defenses, universal PWM overlay. **ADOPT-NOW** (in-flight pass). Additional
items found on the full read:

- **Ramping at the control API** (`intensity + duration_ms + curve +
  step_interval_ms` per command): richer than the engine's single global slew
  knob — per-command ramps with curve selection (linear/exponential/sigmoid
  existed in behavior points too). **P1**: engine-side per-command ramp
  parameters; the global slew limit remains the safety floor either way.
- **Resource manager** (channel conflict prevention): **SUPERSEDED** — the
  registry's not-double-bound validation covers it at the right layer.
- **50+ error-code taxonomy**: **REJECTED** — typed responses + named refusal
  reasons already serve this without the catalog maintenance.

## 2. Lighting service — the deepest feature archive

- **Acclimation periods** — gradually introduce new coral/livestock to full
  lighting: an intensity *cap* that relaxes over a configured span (days/weeks),
  applied over whatever the schedule computes, with status tracking and
  automatic expiry. Reef-specific, high-value, cheap on our engine (a per-
  channel output limiter with a time-based release). **P1** — strong candidate
  for the first post-bench lighting increment; belongs in PRD §8 P1 list.
- **Behaviors / assignments / groups** — define a light behavior once, assign
  to channels or groups. **P2** (recorded previously; lighting v2).
- **Location presets + astronomy service** — **P2** (already in
  time-and-scheduling.md as the locale/anchor model).
- **Moonlight / lunar cycle** (dedicated user guide, lunar intensity fix
  history) — moon-phase-tracked night lighting. **P2**, same v2 package as
  solar anchors.
- **Weather effects** (OWM-driven cloud simulation, weather-triggered
  effects) — **P2+**, explicitly last of the lighting-v2 set.
- **Custom schedules** (user-defined point schedules with a full test-plan
  history) — **SUPERSEDED**: current ChannelProfile *is* this, with the wrap
  and DST work done properly.
- **Calculation cache / queue compactor / override queue** — performance
  machinery for a chattier architecture. **SUPERSEDED** by the spine +
  deadline-driven engine; revisit only if profiling ever says so.

## 3. Flow service — R8's requirements document

- **Feed mode** — one-touch stop of flow devices, automatic state save,
  configurable duration (1–60 min), automatic restoration, extend, manual
  early stop, **emergency stop**, and *profiles* (which devices participate).
  Maps directly onto the override machinery (grouped overrides + lapse
  semantics already exist). **P1** — this is the PRD's "maintenance/feed mode"
  made concrete; the profile concept (named device-sets) is the piece our
  design didn't have.
- **Device groups** (flow groups, also lighting groups) — named sets of
  devices targeted together by schedules/overrides/feed profiles. **P1**
  alongside feed mode; schema is a join table + group_id on override targets.
- **Flow schedules/behaviors** — **P2** with R8 proper (wavemaker patterns).

## 4. SmartOutlets service — the advisory-class prior art

This is the vendor-bridge (device-classes.md §5) already built once:

- **Multi-vendor drivers** (Kasa, Shelly local; VeSync cloud) with a common
  outlet abstraction — the advisory/observe-only device landscape. **P2**:
  requirements source for vendor-bridge; driver list is the market-validated
  starting set.
- **Local network discovery** (Kasa/Shelly scan) as an async task with
  task_id + progress polling — the "find devices" pattern extended to network
  hardware. **P2** with vendor-bridge.
- **Encrypted credential storage** (`crypto_utils`, `db_encryption`) for cloud
  accounts — **P2 requirement, flag now**: the moment vendor-bridge stores a
  VeSync/Kessil account, secrets-at-rest encryption is mandatory. Current
  schema stores no third-party credentials, so nothing to do yet — but the
  identity/signing-key work should not be extended to vendor creds without
  this.
- **Operation progress tracking** (long-running control ops return task
  status) — **P2**; advisory devices over cloud APIs need it, authoritative
  local devices don't.
- **Identifier discipline warning** (their README's ⚠️ section: three ID
  types confused users) — **ADOPTED BY CONTRAST**: current design's single
  device_id + capability/channel binding exists precisely because of this
  lesson; noted so it stays deliberate.

## 5. Temp service

- **Probe management endpoints** (per-probe resolution config, discovery,
  guides) — largely **SUPERSEDED** by announce-adopt + detail sheet. One gap:
  **per-probe resolution setting** (9–12 bit trades precision vs 94–750 ms
  conversion). **P2**, only if a real multi-probe bus ever needs the speed.

## 6. Telemetry service

- Rollup worker / aggregator / history API — **SUPERSEDED** by
  VictoriaMetrics + the envelope-preserving history endpoint (better than the
  archive's design).

## 7. Core service

- **System info API** (host stats: CPU, memory, disk, temperature of the Pi
  itself) — **P1, small**: the System tab's "Hub health" row wants this; the
  hub's own vitals (SoC temp, disk %, load) are one endpoint + a few psutil
  reads, and hub SoC temperature belongs in VM series (a cooked Pi in a
  canopy is a real failure mode).
- **User management / RBAC** — **REJECTED for v1** (PRD non-goal; paired
  clients are the trust model).

## 8. mydocs — the meta-archive

- **SwiftUI design docs** (theme/design system, per-screen UI designs,
  dashboard/lighting/settings/outlets/telemetry screens, token management) —
  **reference input** for the app's remaining screens; the current design
  brief supersedes the theme but the *screen inventories* are checklists of
  what a complete product covered.
- **Timezone architecture reports** (multiple fix summaries, final
  architecture doc) — validation that per-profile IANA + solar anchors was
  the hard-won conclusion last time too. **SUPERSEDED**, comfortingly.
- **Audit/QA process docs** — historical; the session-log + ruling process
  replaces them.

---

## Summary of dispositions requiring action

| Item | Disposition | Where |
|---|---|---|
| Two-tier registry + find/assign flow | ADOPT-NOW | in-flight pass |
| Feed mode w/ profiles + emergency stop | P1 | PRD §8 P1 (extends existing feed-mode line) |
| Device groups | P1 | PRD §8 P1 (new line) |
| Acclimation periods | P1 | PRD §8 P1 (new line) |
| Per-command ramp (duration + curve) | P1 | PRD §8 P1 (new line) |
| Hub system-info endpoint + SoC temp series | P1 | PRD §8 P1 (new line) |
| Vendor-bridge driver set (Kasa/Shelly/VeSync/Kessil) | P2 | device-classes.md §5 note |
| Encrypted vendor credentials at rest | P2 (mandatory with bridge) | device-classes.md §5 note |
| Network discovery as task + progress | P2 | device-classes.md §5 note |
| Lunar/moonlight | P2 | time-and-scheduling.md v2 set |
| Per-probe resolution config | P2 | noted here only |
