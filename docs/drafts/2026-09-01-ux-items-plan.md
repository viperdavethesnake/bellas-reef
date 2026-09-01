# UX open items — brainstorm and proposed plan (2026-09-01)

The four items the 2026-08-18 UX review left as design conversations (B3, C4,
C5, Tier D), each with options, a recommendation, and what it costs. Nothing
here is built or committed to — every item ends in a decision that is David's.
Scope rule applied throughout: one operator, paired devices, private LAN
(`scope-is-home-hobbyist`). Source: `docs/bellas-reef-ios-ux-review.md`,
`…-proposals.md`, `docs/drafts/2026-08-23-ios-ux-review.md`.

## B3 · Status accessory strip (`tabViewBottomAccessory`)

**Problem.** Connection and staleness are only visible on the Tank tab; on
Lighting/History/System a dead hub looks healthy until you tab back. The
review called this its strongest suggestion and explicitly said "do not build
from the paragraph."

**Options.**
1. **Three-state strip, no content beyond status** — `● 78.7 °F · live` /
   `⚠ No data for a minute` / `⚠ Hub unreachable · 4m`, teal/amber, native
   `tabViewBottomAccessory`, reading state the model already publishes
   (`connection`, `lastFrameAt`, primary probe). No taps, no menu, no glass.
2. Same strip, tappable → jumps to Tank. Slightly more useful, slightly more
   design surface (what does tapping mean on Tank itself?).
3. Do nothing; rely on per-tab affordances. Costs nothing, keeps the
   silent-dead-hub gap the review (and today's bench capture) showed.

**Recommendation: option 1, built behind a branch so you see it before
ruling.** The code audit already found every input exists; the cost is the
look, and a look is judged on a screen, not in prose. One component, S–M.
Today's unreachable-hub capture is the concrete argument: the banner only
helped because we were on the Tank tab. **Decision needed: yes/no to a
prototype on a branch.**

## C4 · Identify flow for adoption

**Problem.** Adopting a channel means picking one near-identical row from
memory. Wrong pick = wrong physical light wired to a schedule — the most
expensive mistake the app allows.

**Options.**
1. **Identify step inside the adopt sheet** — on an unadopted channel:
   *Identify* pulses it (3 s snap hold at ~30 % via the existing
   `POST /overrides` + release), you watch which fixture lights, then name
   and adopt. Engine-side nothing new since #42; this is exactly what the
   Lighting tab already does, pointed at a different moment.
2. Identify as a separate System-tab action, decoupled from adoption.
   Weaker: the moment you need it is mid-adoption.
3. Blink-pattern identify (N pulses) instead of a hold. More code for no
   added certainty on a 2–16 channel hobby hub.

**Recommendation: option 1, M.** One caveat to settle in the design pass:
overrides today target *adopted* actuators — identify wants to pulse an
**unadopted** channel, so either adoption order changes (adopt provisionally,
identify, confirm/rename) or hardware-io grows a narrow bench-pulse path for
discovered-but-unadopted channels (new command surface, safety-relevant, needs
its own ruling — an unadopted channel has no declared safe state). That fork
is the real design conversation; the UI around it is small. **Decision
needed: which side of that fork, then a spec.**

## C5 · Alerts home

**Problem.** Thresholds are per-sensor (right); mute/routing/quiet-hours have
no global location. The review's point is *establish the location before
users have eight probes*, not build features.

**Options.**
1. **An Alerts leaf on the System tab index now** — showing what exists
   today: active alerts, silences with remaining time, and a line stating the
   alerting tier (see D1). No routing, no quiet hours, no escalation — those
   are features for when they're real.
2. Wait for D1 to be decided and build the home with the first real routing
   feature. Cheaper now, and the retrofit cost the review warns about is low
   at one-operator scale.
3. Never — keep alerts purely per-sensor + Tank banner. Rejects the review's
   premise; defensible for a hobby hub with one operator who sees the tank.

**Recommendation: option 2 — explicitly deferred until D1 is ruled.** At
hobbyist scale with silences already visible per-sensor and on Tank, an empty
"location" leaf is furniture. C1's Hardware leaf shipped because it had
content on day one; an Alerts leaf before D1 wouldn't. This deliberately
softens the review's "establish now" — the retrofit risk it names is an
eight-probe multi-user scenario we ruled out of scope. **Decision needed:
agree to fold C5 into D1's design, or overrule and take option 1 (S).**

## D1 · Alerting architecture — the gating decision

**Problem.** With the app closed, nothing alerts. The hub has outbound
internet and no inbound; another operator's hub may have none. AlarmKit
doesn't cover it (E4); critical-alert push needs an Apple entitlement with
lead time.

**Options.**
1. **Push-out via APNs with `.critical` interruption**, degrading to
   in-app-only when the hub has no egress, the app always stating which tier
   it's in. Full answer; carries an entitlement application, a token-registry
   on the hub, and the first piece of infrastructure that talks to the
   internet — a real scope expansion for a LAN-only product.
2. **LAN-tier only, stated honestly** — no push; the app (foreground /
   background-refresh) and the tank's own operator are the alarm. One line of
   UI ("Alerts reach you only while the app is open — this hub doesn't send
   push"), zero infrastructure. What we already are, made explicit.
3. Local-network substitute (e.g. a paired iPad as always-on display).
   Recorded as a pattern, not software work.

**Recommendation: option 2 now, revisit option 1 at livestock.** The no-tank
memory cuts both ways: severity today is low, but livestock is the stated
future. The honest-tier line is S, ships anytime, and is required under
*every* option (the review: "an air-gapped hub that silently cannot alert is
the same class of failure as A3"). Option 1 should be a deliberate project
with its own spec when a heater and living animals raise the stakes — not
before. **Decision needed: accept the two-step, and whether to ship the
tier-statement line (S) in the next iOS batch.**

## D2 · Live Activity for holds — flagged ready, not urgent

The proposals doc says D2 earned a design session once holds had transition +
countdown; both shipped (#42, PR #27). A Lock-Screen `Light 1 — 50 %, 8 min
remaining · Release` fits the existing primitive. **Recommendation: keep in
Tier D; next candidate after B3/C4 land, its own session.** No decision
needed now.

## D3–D7 · unchanged

Recorded capabilities (App Intents, Control Center, widgets, correlation
view, export). No decisions needed; D6 (correlation) is the one with data
already in VictoriaMetrics and would be my first pick if any is pulled
forward.

## Proposed order (if the recommendations stand)

1. **D1 tier-statement line** (S) — next iOS batch, closes the honesty gap.
2. **B3 prototype on a branch** (S–M) — you rule on the look.
3. **C4 spec** — after ruling on the unadopted-pulse fork (the only piece
   that touches safety architecture).
4. **C5** — inside D1's eventual design; nothing now.
5. D2, then D3–D7 as pulled.

Every item above stops at a decision of yours; none proceeds on this
document alone.
