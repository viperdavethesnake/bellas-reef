# Hub status — host metrics on the System tab

Requested by David 2026-08-31 ("brainstorm and add a hub status page, show
cpu, ram, temp, etc, etc. for the Rpi5 itself. maybe it should be under
system. you decide. build it deploy it, verify it"). Design decisions
delegated; this spec records what was chosen and why so any of it can be
overruled cheaply.

## What ships

A read-only "Hub status" leaf under the iOS System tab showing, for the hub
machine itself:

| Row | Source | Verified on coco in-container 2026-08-31 |
|---|---|---|
| CPU load (1/5/15 min) + core count | `/proc/loadavg`, `os.cpu_count()` | `0.42 0.38 0.33` |
| Memory used / total | `/proc/meminfo` MemTotal − MemAvailable | 1014464 / 445792 kB |
| SoC temperature | `/sys/class/thermal/thermal_zone0/temp` (type `cpu-thermal`) | 46.3 °C |
| Uptime | `/proc/uptime` | host-global, confirmed |
| Last updated | message `emitted_at` | — |

**The whole read set is visible inside the existing hardware-io container
with no compose change and no new privilege** — `/proc` loadavg/meminfo/
uptime are system-wide (not namespaced) and `/sys` is the host's sysfs.
Measured in `bellasreef-hardware-io-1` on coco (values above), not assumed.

## What deliberately does NOT ship (v1)

- **Disk usage.** A container's statvfs sees its own filesystem; honest host
  rootfs numbers need a rootfs-backed bind mount, which is a new grant that
  wants David's ruling, not a default. Recorded as the one open design
  question. (The installer already prints free disk at install time.)
- **Throttle/undervoltage flags.** `vcgencmd` is not in the container and the
  sysfs path for it on this board is unverified. Never guess — add later if
  measured.
- **History/VM.** Snapshot only. The telemetry bridge filters subjects
  explicitly, so nothing lands in VM for free; wiring host metrics into VM is
  a separate decision (charts, retention, naming) not needed for a status
  page. The retained-stream design below leaves the door open.

## Architecture — mirrors ChipState (#61/#62) exactly

The shipped chip-state flow is the template; every choice below is "what
ChipState did", except storage, which is lighter.

1. **Contracts 4.3.0 (MINOR: new message + new subject).**
   `HostStatus` message: `load_1m/load_5m/load_15m: float`, `cpu_count: int`,
   `mem_total_kb: int`, `mem_available_kb: int`, `temp_c: float | None`
   (None = unreadable, never fabricated), `uptime_s: float`, on the standard
   envelope. Subject `bellasreef.host.status` (singleton — phase-1 is one
   hub; a phase-2 spoke carries its own identity in `source`). New retained
   stream `BR_HOST` over `bellasreef.host.>`, LIMITS retention,
   `max_msgs_per_subject=1`, same shape as `BR_CHIP`. No overlap with any
   existing stream's subjects.
   No `hostname` field: in-container hostname is the container's, and
   clients already get the hub's name from `/api/v1/info`.

2. **hardware-io publishes.** `HostStatusReader` (injectable `/proc` and
   thermal paths for tests) + a 30 s asyncio publish loop in `app.py`,
   best-effort like chip state (a failed publish logs, never kills the
   service). Why hardware-io: it is the "hardware truth" service, already
   publishes chip state, and already has the mounts. control-engine never
   touches it; the API never reads the host directly (it may not even be on
   the same machine in phase 2).

3. **API consumes, in memory only.** `HostStatusConsumer` mirroring
   `ChipConsumer` (ephemeral push sub, `DeliverPolicy.LAST_PER_SUBJECT`,
   5 s retry) but storing the latest message in process memory — **no
   Postgres table, no migration**. A 30 s heartbeat snapshot is not config
   and not history; the retained stream replays the last value on API
   restart, which is exactly the durability it deserves.
   Endpoint `GET /api/v1/hub-status`, `operation_id="getHubStatus"`,
   auth `Depends(current_client)` like everything else; 404 with a typed
   detail while no message has arrived yet (fresh boot, pre-4.3.0
   hardware-io).

4. **iOS.** New `NavigationLink` row on the System index ("Hub status",
   accessibility id `system-hub-status`) → leaf in `SystemView.swift`
   following the existing leaf idiom (List + LabeledContent, `Theme.value`
   monospaced digits, capsule bar for the memory fraction per
   `TankView.swift:686`, `RelativeAge` for uptime/updated). Fetched in its
   own do/catch like `hardware()` so a pre-4.3.0 hub degrades to "—", never
   a failure row. Requires a contracts re-pin (`scripts/pin-contracts.sh`)
   after the backend merges.

## Deploy vehicle

`update-hub.sh` gets implemented per the ruled design in its own header
(fetch tags → checkout newest `v*` → re-exec --stage2 → backup → pull →
migrate → `up -d --wait` app services only → rewrite BELLASREEF_TAG →
verify), because the documented "manual steps" for updating an installed hub
turned out not to exist (the docs' anchor points at first-bring-up steps) and
hand-applied sequences are banned. Coco then moves to the new tag by running
it — the script's maiden run, mirroring install-hub.sh's on the same day.

## Testing

Each leg mirrors its ChipState twin: `contracts/python/tests/
test_host_status.py`; `services/hardware_io/tests/test_host_status.py`
(reader against fixture files, publisher against the recording-spine fake,
cadence + best-effort); `services/api/tests/test_host_status_consumer.py`
and `test_hub_status_api.py` (404-before-first-message, 200-after);
`tests/test_update_hub.py` grows the real suite alongside the
implementation. iOS: `HardwareSectionsTests`-style tests for the new leaf's
formatting.

## Open question — RESOLVED 2026-08-31 (David: "for a hobbyist app do we
really care?")

Disk usage is SKIPPED by scope ruling, not deferred. The failure mode it
would guard (slow storage exhaustion) is years out at this telemetry volume
and is an alert's job when it ever matters, not a status row's. The
installer prints free disk at install time. Do not re-open without a
ruling.
