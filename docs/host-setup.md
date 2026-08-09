# Host setup

The **only** mutations made to the Pi outside containers. Everything else the
platform needs runs in Docker with specific device nodes passed to `hardware-io`
and nothing privileged.

Keep this file exhaustive. If a host change is needed and it is not written
here, that is a bug in this document, not a licence to make the change quietly.

Values below are the measured state of the reference host as of 2026-08-09; see
"Verified host facts" in CLAUDE.md for the full inventory.

---

## 1. Device tree overlays

In `/boot/firmware/config.txt`:

```ini
dtparam=i2c_arm=on

[pi5]
dtoverlay=w1-gpio-pi5,gpiopin=4
```

**The `-pi5` suffix is required.** Plain `dtoverlay=w1-gpio` is the pre-Pi-5
variant; it loads without error and silently fails to bring up the bus. This
costs a reboot cycle to discover.

Never put a trailing `#` comment on a `dtparam=` or `dtoverlay=` line — the
firmware device-tree parser can fold the comment into the value.

After changing this file, reboot. Verify:

```bash
ls /sys/bus/w1/devices/     # expect w1_bus_master1
ls /dev/i2c-1
gpiodetect | grep rp1       # expect gpiochip0 [pinctrl-rp1] (54 lines)
```

## 2. Headless stripping

The reference host runs with no display stack: `vc4-kms-v3d` removed, audio off,
camera and DSI auto-detect off. This drops all HDMI audio modules with it
(`snd` module count 0).

Consequence worth knowing before you need it: **the serial console is the only
live out-of-band console.** `serial-getty@ttyAMA10` runs and
`console=serial0,115200` is in `cmdline.txt`, so a USB-TTL adapter on the 3-pin
debug connector gives a real login. Offline recovery is to power down, pull the
boot drive, and mount the vfat boot partition on another machine to fix
`config.txt`.

Timestamped backups of every edit sit beside the file as `config.txt.bak-*`.

## 3. Clock — mandatory, this host has no RTC battery

A power cut loses the time, and a scheduler acting on a wrong clock doses at the
wrong hour. Three packages, all enabled:

```bash
sudo apt-get install -y chrony fake-hwclock
sudo systemctl enable chrony chrony-wait fake-hwclock-load fake-hwclock-save
```

| Unit | Role |
|---|---|
| `fake-hwclock-load` / `-save` | restores the saved time at boot, before any network |
| `chrony` | corrects once the network is up; `makestep 1 3` steps rather than slews |
| `chrony-wait` | makes `time-sync.target` mean *actually synchronised* |

`chrony` replaces `systemd-timesyncd` (apt removes it automatically). It is
chosen over timesyncd because timesyncd is a plain SNTP client that slews — on a
box that boots with a wrong clock you need a step, not a crawl.

`fake-hwclock.service` and `hwclock.service` show as **masked**. That is the
package masking its own legacy sysvinit units, not a fault. Do not "fix" it.

**Every service that acts on time must be ordered against this:**

```ini
[Unit]
After=time-sync.target
Wants=time-sync.target
```

An unsynchronised clock is a fault state that holds actuators at safe state — it
is not a reason to guess.

## 4. Container hardware access

`hardware-io` is the only container with device access. Specific nodes, never
`privileged: true`:

```yaml
devices:
  - /dev/i2c-1:/dev/i2c-1
  - /dev/gpiochip0:/dev/gpiochip0
volumes:
  - /sys/bus/w1:/sys/bus/w1:ro        # DS18B20 read path
  - /sys/devices/w1_bus_master1:/sys/devices/w1_bus_master1:ro
```

The 1-Wire mount is read-only and sysfs-based because `w1-therm` exposes no
character device. `/dev/gpiochip0` is passed by path, but the driver still
resolves the chip **by label** inside the container — the index is not
guaranteed stable across kernels.

The container user needs the host's `i2c` and `gpio` group GIDs. Nothing needs
root.

## 5. Networking

The host is reached by mDNS name, never by IP — the WiFi lease changes.

`/etc/avahi/avahi-daemon.conf` must pin the real interfaces:

```ini
allow-interfaces=eth0,wlan0
```

Without this, avahi also advertises Docker's `docker0` bridge (`172.17.0.1`),
which is unreachable from the LAN — clients then intermittently resolve the host
to an address that does not work. An allowlist is used rather than denying
`docker0` because Docker also creates `br-*` bridges for user-defined networks
and avahi has no wildcard syntax.

WiFi power save is disabled and persisted in the NetworkManager connection
profile:

```bash
sudo nmcli con modify "<ssid>" 802-11-wireless.powersave 2
```

## 6. Not configured yet

- **Firewall.** Nothing listens yet. Must be revisited before the API is
  exposed. Note Docker writes its own nftables rules and will conflict with a
  naive host firewall config.
- **Unattended upgrades.** Deliberately absent so nothing reboots mid-dose. The
  trade is that patching must become a scheduled, supervised action.
