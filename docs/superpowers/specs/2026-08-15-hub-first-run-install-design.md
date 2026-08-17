# First-run install: from a cloned repo to a running hub

Drafted 2026-08-15 from a design conversation with David the same day. Not yet
approved.

Someone with a Pi finds this repo and clones it. There is currently nothing
they can run. This spec designs the one script that takes them from there to a
hub they can pair a phone with.

## The gap

Everything in `scripts/` today is operator tooling that assumes a prepared
machine and a second computer:

| | Assumes |
|---|---|
| `deploy-pi.sh` | a Mac with the repo, SSH keys, and a host already set up |
| `factory-reset-pi.sh` | an existing deployment |
| `drill-restart.sh` | an existing deployment |
| `check.sh`, `install-hooks.sh` | a development machine |

`host-setup.md` documents three pieces of host state a human establishes by
hand before any of that works: device tree overlays, `deploy/.env`, and the
ghcr.io pull credential. So there is a path from an *already prepared* Pi to a
running hub. There is no path from a fresh one, and the reason is that David
and Claude have been that human every time.

Ruled 2026-08-15: **there is no second machine.** The Mac exists only for
building this project. A hub owner has the hub, and this repo.

## Starting assumption

The repo is cloned on the machine that will be the hub, and the user runs the
script there. Getting the repo onto the machine is out of scope.

## Shape

`scripts/install-hub.sh`, run locally, no arguments required. Runs as a normal
user and calls `sudo` only for actions that need it.

Six phases, in this order. The order matters and phase 1 is first because it is
the cheapest and it short-circuits everything else.

### Phase 1. Already deployed?

Any of: a `bellasreef-*` container present, `bellasreef.service` enabled, or a
populated `deploy/.env`.

If found, report what was found and exit without touching anything. This tool
installs; it does not upgrade, repair, or reconfigure.

### Phase 2. Hard requirements

These are not optional. Nothing works without them, so a failure that the user
declines to fix stops the run.

| Check | Fix offered |
|---|---|
| Docker present, Compose v2 available, daemon reachable | install, or add to the docker group — see below |
| Architecture is arm64 or amd64 | none, report and stop |
| Kernel 6.x or newer | none, report and stop |
| RAM at or above floor | none, report and warn |
| Free disk at or above the 4 GB hard floor | none, report and stop |
| Free disk at or above the 16 GB practical minimum | none, report and warn |
| Clock synchronised, `chrony` + `fake-hwclock` installed and enabled | install and enable |
| `avahi-daemon` installed, running, and interface-allowlisted | install and configure |
| the `_bellasreef._tcp` service record installed | install |

Each install is offered individually and the user accepts or declines. Nothing
is installed silently.

Docker is unusable for three different reasons and only one of them is fixed by
installing Docker. Absent, or present without Compose v2, is the incomplete
install the convenience script answers. Present and complete but with the daemon
refusing this user, who is not in the `docker` group, is a `usermod` and a
re-login — no download. Present, in the group, and still refused is neither:
either `dockerd` is not running or the login predates the group being granted,
and there is nothing to offer. The M64 sat in the second state on 2026-08-17 and
was offered the first one's remedy, so `--yes` re-ran `get.docker.com` for five
minutes against a Docker that was already installed.

Group membership is read from the group database (`id -nG <user>`) rather than
from the current session. After a `usermod` the two disagree, and that
disagreement is exactly the third state — reading the session's groups would
call it the second and offer the `usermod` again.

The avahi work is two separate things and both are required.

The `allow-interfaces` allowlist comes first. Without it avahi advertises
Docker's bridge address, which is unreachable from the LAN, and the app resolves
the hub to an address that does not work. That failure has already happened once
on the reference host.

The service record is the second. A hostname A record is not enough: an app that
knows only `<host>.local` cannot tell a reef controller from anything else
answering to that name, and cannot learn the API port. The client browses for
`_bellasreef._tcp` (auth.md step 1), so `deploy/avahi/bellasreef.service` has to
be copied into `/etc/avahi/services/`. Its TXT records carry the API base path
and the contracts version, which is how a client refuses a hub it is too old to
talk to.

### Phase 3. Hardware inventory

**Reported, never blocking.** Ruled 2026-08-15: we do not assume anything is
missing. An owner may want temperature monitoring and no lights at all, or a
PCA9685 and no SoC PWM. A fixed list of required interfaces would be an
opinion about their tank.

```
I2C          enabled       PCA9685 and other I2C devices available
1-Wire       enabled       DS18B20 temperature probes available
SoC PWM      not enabled   no direct PWM channels; a PCA9685 still works over I2C
```

For anything not enabled, print the exact `config.txt` lines that would enable
it and what it would make possible. Then ask: proceed with what is available,
or stop so the user can edit and reboot.

**The script never writes boot config.** Ruled 2026-08-15. A bad overlay line
makes a headless machine unbootable with no remote recovery, and `config.txt`
edits need a reboot regardless, so the two-run flow is unavoidable rather than a
cost of this decision. If the user chooses to stop, they edit, reboot, and run
the script again.

This mirrors how `capabilities.py` already behaves: it announces what it can
prove and holds no opinion about what should be there.

Boot-config inspection is Pi-specific and runs only when a Pi is detected. On
any other board the script says so and skips it, which keeps the phase honest
rather than silently passing.

### Phase 4. Configuration

`deploy/.env` from `deploy/.env.example`, which ships with real working values
for everything that has a sensible default. The script walks the user through
them; pressing enter accepts the default.

Two values are carved out:

- **`I2C_GID` and `GPIO_GID` are read off the machine**, never defaulted.
  `getent group i2c`. On the reference Pi these are 988 and 986, but they are
  allocated by the OS when the package is installed and a wrong value means
  hardware-io cannot open `/dev/i2c-1`, failing with a permission error that
  reads like a hardware fault. If the group does not exist, that is a finding to
  report, not a value to guess.
- **`POSTGRES_PASSWORD` is generated**, and ships empty in the example with a
  comment saying so. A default password in a public repo means every hub shares
  one credential and most owners never change it.

**`BELLASREEF_TAG` is the checkout's own commit** — `git rev-parse HEAD` —
until there is a first tagged release for it to default to instead. Tracking
`latest` is possible and unsupported.

That is what makes the two halves of an install the same commit: the compose
file, the migrations and the contracts come from the clone while the images
come from the registry, and any gap between them surfaces as migrations from
one commit running against images built from another. So the script refuses to
write a tag it cannot stand behind — a dirty working tree, or a HEAD that is
not an ancestor of `origin/main`, stops phase 4 rather than pinning a tag no
image was ever built for. If `origin/main` is unknown to the clone, that is
recorded as unverified rather than assumed either way.

`deploy/.env` is never overwritten if it exists.

### Phase 5. Deploy

Pull images, apply migrations, install and enable `bellasreef.service`, bring
the stack up.

**Registry authentication is temporary scaffolding.** The three images are
private on ghcr today, so an anonymous pull returns 401. The script does not
manage credentials, install `gh`, or prompt for a token. On a 401 it prints the
one command that fixes it and stops. When the packages go public this path is
dead code and gets deleted. Building a credential flow we intend to remove would
be the wrong investment.

If the machine cannot run the stack, phase 5 must fail with a clear message
rather than surfacing a raw compose error. `compose.yaml` requires Pi-5-specific
device nodes (`/dev/gpiomem0` through `4`), a hard-coded RP1 sysfs path, and a
`gpio` group; on a machine lacking any of them compose refuses to start with an
error that does not name the cause.

### Phase 6. Verify and hand off

- all containers running and healthy
- `bellasreef.service` **enabled**, not merely active
- API answering on `/api/v1/info`
- avahi published the service, confirmed from the daemon's own log
- setup code minted, then printed

**How the mDNS check works, and why not by browsing.** `host-setup.md` §5 records
that avahi does not reliably reflect its own services back to a local browse, so
`avahi-browse` on the hub proves nothing. Measured 2026-08-15, it is worse than
unreliable: `avahi-browse` and `avahi-resolve` are not installed on the reference
host at all. They ship in `avahi-utils`, which `avahi-daemon` does not depend on.
A browse-based check would mean installing a package to run a test that cannot
answer the question.

Four local facts answer it properly, and the fourth is authoritative:

1. `/etc/avahi/services/bellasreef.service` is present
2. `avahi-daemon` is active
3. `allow-interfaces` names the real interfaces
4. the journal shows, after the most recent reload:
   `Service "Bella's Reef on <host>" (/services/bellasreef.service) successfully established.`

Item 4 is the daemon reporting that it parsed the XML and published the record.
A malformed file logs an error there instead, so this distinguishes "the file
exists" from "the file worked", which is the distinction that matters.

What remains genuinely unverifiable from the hub is whether the **network path**
works: multicast blocked by a router, or a client on another subnet. No local
check can establish that, and the script says so rather than implying otherwise.
The phone finding the hub is the proof, and it is the next thing the owner does.

The boot-unit check earns its place separately from the container check: the
others prove the hub works now, and only this one proves it comes back after a
power cut. For a tank controller that is the failure you find at the worst
possible time.

Telemetry is deliberately not verified. A fresh hub has no adopted devices, so
there is nothing on the wire yet, and the gate `deploy-pi.sh` uses cannot apply.

The setup code needs no design work: setup mode is entered automatically when no
client has ever paired, and `bellasreef setup-code` prints it. The script
displays it and exits.

## Failure and resume

Every phase is idempotent and re-running is always safe. Nothing is written
twice, `deploy/.env` is never overwritten, and phase 1 catches a completed
install.

There is no state file and no resume token. The checks themselves are the state,
which means a machine that was rebooted, power-cycled, or half-configured by
hand converges the same way as one that failed mid-run. A failure prints what
failed, what it was attempting, and the command to retry.

## Flags

| Flag | Effect |
|---|---|
| `--dry-run` | report every action it would take, mutate nothing |
| `--check-only` | phases 1 through 3, then stop |

`--dry-run` exists for testing as much as for caution: it lets the script be
exercised against a machine nobody wants changed.

## Testing

Logic is factored into functions that take machine facts as arguments rather
than reading the system inline, so phases 2 through 4 can be exercised against
fixtures with no hardware.

Two physical targets, and they cover different halves:

**Raspberry Pi 5 (reference).** The happy path end to end. It cannot test
anything in phase 2, because everything is already installed and correct there.

**Banana Pi M64 (mule).** Explicitly not a supported platform and nothing is
being built for it. It is useful precisely because it fails checks the Pi
cannot:

| Phase | Value there |
|---|---|
| 1 | nothing deployed, exercises the clean path |
| 2 | **Docker genuinely absent**, so the detect-and-offer-install path runs for real |
| 3 | I2C and 1-Wire present, no Pi `config.txt`, exercises the non-Pi branch |
| 4 | `i2c` GID is 108 and **there is no `gpio` group**, testing both read-off-the-machine and report-do-not-guess |
| 5 | fails at compose, which is the clear-error requirement above |
| 6 | not reachable |

Phases 1 through 4 are where the interesting logic is, and the Pi can test none
of it.

## Non-goals

- **Upgrades.** This installs. Getting a running hub to a newer version is a
  separate problem and a separate tool.
- **Drift detection.** Phase 1 exits on an existing deployment, so this can
  never diagnose a hub that has been running for a year. Accepted deliberately
  for simplicity; it may want its own tool later.
- **Writing boot config.** Reported, never written.
- **Supporting non-Pi hardware.** The M64 is a test target, not a platform.
- **Replacing `deploy-pi.sh`.** It keeps working from the Mac while the Mac
  exists.

## Open questions

1. **The images must go public before anyone outside this project can install.**
   Not a blocker for development, and recorded as a launch item.
2. **RAM and disk floors need real numbers.** Disk is settled; RAM is not.

   Measured on the reference Pi 2026-08-17: one generation of images is 1.69 GB
   (api 482, control-engine 353, hardware-io 348, postgres 415, nats 38,
   victoria-metrics 52 MB), Docker Engine itself is about 0.4 GB, and the data
   volumes start at roughly 60 MB. A single hard 16 GB stop was therefore
   wrong in both directions: it refused machines that can complete an install,
   and it said nothing about the machines that complete one and fill up later.

   Ruled two-tier. **4 GB is the hard floor** — a shade over 2 GB has to land
   before a hub exists at all, so below it the run stops. **16 GB stays the
   practical minimum** (docs/hub-platform-requirements.md) and between the two
   the run warns and continues: retention, a growing Postgres, and the second
   image generation an upgrade pulls before dropping the first will not fit.
   The warn is a WARN and not an UNVERIFIED — the check ran and the machine is
   knowingly degraded, which is not the same as a measurement nobody took.

   This is what made the Banana Pi M64 evaluation reachable at all: 4.9 GB free
   could never clear a hard 16 GB stop, so no run of the installer on that board
   ever got as far as the hardware inventory it was being used to collect.

   The RAM floor is still not measured under load.
3. **Whether the two-run reboot flow needs anything beyond a printed
   instruction.** Idempotency means re-running works, but nobody has watched a
   new owner do it.
