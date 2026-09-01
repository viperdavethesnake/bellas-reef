# Draft issue: install-hub — missing compose-required device node must FAIL phase 2, not die mid-deploy

Ruled by David 2026-08-31 during the coco-bellasreef install: "leave it for
now, but it should be a fail. we can not assume everyone will have a Rpi with
exactly the same devices."

## Problem

`hub/deploy/compose.yaml` maps `/dev/i2c-1:/dev/i2c-1` into hardware-io
unconditionally (line 84). Docker refuses to create a container whose
`devices:` entry names a host node that does not exist, so a Pi with
`dtparam=i2c_arm` off (or any hardware profile lacking a node the manifest
requires) fails at phase 5 with a Docker device error instead of a phase-2
requirements FAIL that names the problem.

Hardware inventory (phase 3) is "reported, never required" — and should stay
that way for *optional* hardware. But a device node the shipped manifest
hard-requires is not optional: its absence is a guaranteed deploy failure and
must surface as a phase-2 FAIL with the config.txt remedy printed.

## Scope

- Phase 2 check: every host path named in a `devices:` entry of
  `deploy/compose.yaml` must exist, else FAIL with the dtparam/dtoverlay
  remedy.
- Design question — RESOLVED 2026-08-31 (David, option 1): the committed
  manifest stays canonical and hard-requires the full Pi 5 device set. The
  host contract is config.txt with I2C on, a PWM overlay (2chan or 4chan —
  soft: the stack starts without it, channels are just absent), and 1-Wire
  on; phase 2's host-path FAIL is the enforcement. An installer-generated
  override is the fallback IF a real non-Pi5 board ever lands on the bench
  (the 3B+), decided then, with the board in hand.

## Context

Found on coco-bellasreef (1 GB Pi 5 Rev 1.1, no PCA9685) when considering
disabling I2C since nothing will use it there. Left enabled for now so the
stack starts.

## Installer hand-off rewrite (same run, maiden real-Pi install, v0.2.0-rc.4)

David 2026-08-31: "this section is messy and confusing." Root causes found:

- **Doubled setup code label**: `install-hub.sh:1439` prints
  `Setup code:  %s` filled with the raw output of
  `bellasreef setup-code` — which is itself a labeled multi-line message
  ("Setup code: XXXX" + its own app instruction). Label-inside-label, the
  CLI's instruction line lands unindented mid-block, then the installer
  prints its own pairing instruction again in different words. Fix: CLI
  grows `setup-code --bare` (code only) for the installer to embed, or the
  installer stops re-labeling and uses the CLI output verbatim.
- **`ih_phase6_handoff` prescribes the wrong path**: it tells a fresh-hub
  owner to hand-edit devices.import.yaml and run the token-gated
  `docker compose exec … devices import`. That is the bulk-import/restore
  path (factory-wipe recovery, host-setup.md §10) — not the fresh-hub path.
  In-app adoption (System → Hardware → adopt) is the primary flow and was
  proven on coco the same hour: probe adopted from the app with no YAML and
  no token, telemetry on the wire inside a poll interval. Rewrite the block
  to point at the app's adopt flow, with the import mentioned only as the
  bulk/restore alternative.
- "This hub" summary lists Docker bridge addresses (172.17.0.1, 172.18.0.1)
  alongside the LAN address with no distinction. Ruled by David 2026-08-31:
  don't hide them — LABEL them. Print which addresses are external (LAN,
  what the app reaches) and which are internal Docker bridge networks,
  e.g.:
      addresses  192.168.33.105 (LAN)
                 172.17.0.1, 172.18.0.1 (internal — Docker bridges)
  The LAN/bridge split is derivable from the interface each address sits on
  (docker0/br-* vs eth0/wlan0 — the same interface set avahi
  allow-interfaces already names).
- `docker stats` reports 0B memory for every container on this OS build
  (memory cgroup accounting off) — relevant to the "measure RAM on coco"
  review item; per-container numbers need cgroup accounting enabled or
  another method.
