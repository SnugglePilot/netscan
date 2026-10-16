# netscan

Records this Mac's network quality around the clock and serves a dashboard at
**http://127.0.0.1:2325/**. Built to pin down where an internet connection fails:
on the laptop's Wi-Fi, at the home router, on the ISP's line, or further upstream.

Stdlib-only Python (runs on macOS's `/usr/bin/python3`), plus a tiny Swift helper for
Wi-Fi signal readings and a vendored copy of uPlot for charts. Nothing is sent anywhere
except the probes themselves.

## Install on a Mac

Requires macOS (it uses launchd, CoreWLAN, `pmset`, `networkQuality`) and Apple's
Command Line Tools, which provide `/usr/bin/python3`, `swiftc` and `git`. On a fresh Mac:

```sh
xcode-select --install                        # once; accept the dialog (skip if already installed)
git clone https://github.com/SnugglePilot/netscan.git ~/netscan-src
cd ~/netscan-src
python3 netscan.py install                    # copies to ~/netscan/app, builds the Wi-Fi helper, starts a LaunchAgent
open http://127.0.0.1:2325/
```

No sudo and no packages needed. It starts at login, restarts if it crashes, and records
whenever the Mac is awake. It does not keep the Mac awake: for round-the-clock data on a
laptop, keep it plugged in and set *System Settings > Battery > Options > Prevent automatic
sleeping when the display is off*, or run `caffeinate -s &` in a terminal.

## Operating it

```sh
python3 netscan.py status      # is the service running, latest window
python3 netscan.py path        # show the current route to 1.1.1.1
python3 netscan.py uninstall   # stops it; data stays in ~/netscan/data
python3 -m unittest test_netscan
tail -f ~/netscan/logs/netscan.log
launchctl kickstart -k gui/$(id -u)/com.andymoore.netscan   # restart (e.g. after editing config.json)
```

To update: `git pull` in the source folder, then `python3 netscan.py install` again. The
service runs from the `~/netscan/app` copy, not the clone, because launchd jobs that read
`~/Documents` and similar folders run into macOS privacy (TCC) blocks.

Files: `~/netscan/data/netscan.db` (SQLite; about 4 MB per day), `~/netscan/logs/`,
optional `~/netscan/config.json`.

### Viewing another Mac's dashboard

The dashboard only listens on 127.0.0.1, so it isn't exposed to the network. To look at
it from a different computer, turn on *System Settings > General > Sharing > Remote Login*
on the monitored Mac and tunnel the port over SSH:

```sh
ssh -N -L 2326:127.0.0.1:2325 user@other-mac.local    # then open http://127.0.0.1:2326/
```

Or copy the data back for a closer look: `scp user@other-mac.local:netscan/data/netscan.db .`
(then query it with `sqlite3`). Export CSV on the dashboard works too.

### Diagnosing one problem computer

Install it on the computer with the problem and leave it running through a few bad
episodes. Ask its user to note the time of each one (to the minute, if possible). Then
check those minutes on the dashboard:

- **Router loss or blackout seconds blamed on *LAN / Wi-Fi*, weak RSSI (below about −70
  dBm), a dropping link rate, or channel changes**: the problem is this computer's Wi-Fi
  link. Try moving closer, a 5 GHz/6 GHz band, or a wired connection.
- **Router fine, hop 2 or *ISP access* bad**: the modem, the line, or the ISP.
- **Everything fine but latency at the router and hop 2 jumps to 100 ms+**: the
  connection is saturated (someone else downloading, backups, or a speed test).
  Bufferbloat; enable SQM / Smart Queue / adaptive QoS on the router.
- **Everything fine on this computer**: the problem is the far end of the call or the
  calling service.

After editing the code here, run `install` again; the service runs from the
`~/netscan/app` copy, because launchd jobs that read `~/Documents` run into macOS
privacy (TCC) blocks. It starts at login, restarts if it crashes, and records
whenever the Mac is awake. It does not keep the Mac awake.

Files: `~/netscan/data/netscan.db` (SQLite), `~/netscan/logs/`, optional `~/netscan/config.json`.

## What it measures

**Every second** (unprivileged ICMP, one socket, ~7 packets/s):
- your router (direct ping)
- every hop that responds on the path to 1.1.1.1, using TTL-limited probes (the `mtr`
  technique). Hop 2 is the first device past your router: the ISP's edge, or the
  ISP modem if the router sits behind one.
- 1.1.1.1, 8.8.8.8 and 9.9.9.9 (three unrelated providers)

**Every 30 seconds** these are folded into one window per hop: loss %, average, minimum
and maximum RTT, jitter, and the longest run of lost packets. The window also gets:
- **Blackout seconds**: seconds in which all three internet hosts were silent. Each is
  blamed on the furthest point that still answered: *LAN/Wi-Fi* (router silent too),
  *ISP access* (router answered, hop 2 didn't), or *upstream* (hop 2 answered).
- DNS query time to the resolver the system uses (sent direct, no local cache)
- HTTPS request to Cloudflare with DNS/connect/TLS/first-byte timings, plus the public
  IP. A changed public IP is logged, since it often means the modem reconnected.
- Wi-Fi RSSI, noise, link rate, channel/band; the network's identity; lid open or
  closed; AC or battery
- status: `ok`, `degraded` (≥3% loss, ≥3 blackout seconds, DNS/HTTPS failure or
  ≥150 ms), `offline`, `no-network`

**Speed tests (off by default)**: `networkQuality` measures download, upload and
responsiveness (RPM). Click *Run speed test* on the dashboard for one now, or set
`speedtest_interval_min` to schedule them. They are off by default because a test fills
the whole connection for 15–25 s: on a line with bufferbloat, every other device in the
house sees 200–300 ms of lag, enough to make video calls choppy. Each test also moves a
few hundred MB to 1 GB. Windows during a test are flagged and left out of the per-hop
analysis.

**Sleep**: when the Mac sleeps, the gap is logged along with the reason from
`pmset -g log` (for example "Clamshell Sleep" or "woke: lid"). Pings caught in flight
across a sleep are discarded, so they don't show up as loss.

## Reading the dashboard

- **Where is the problem?** shows the path with loss per hop over the selected range,
  and a verdict naming the first hop from which loss continues all the way to the
  internet. Routers often rate-limit replies to probes aimed at them, so a hop showing
  loss that later hops don't share is marked *rate-limited* and ignored (hidden by
  default in the per-hop loss chart). Internet exchanges such as VANIX commonly do this,
  showing 60–70% "loss" that isn't real.
- **Blackout seconds** split by location is the clearest evidence to give an ISP:
  "N seconds where my router answered but your network didn't".
- The day × hour heatmap shows whether problems cluster at certain times (evenings
  suggest congestion).
- Drag on any chart to zoom in; double-click to zoom out. Export CSV covers the
  selected range.

## Networks and Wi-Fi names

macOS hides the Wi-Fi name (SSID) from background tools without Location permission,
so networks are identified by the router's hardware (MAC) address and can be renamed
in the dashboard. To record real SSIDs, run once: `sudo ipconfig setverbose 1`.

## Config (`~/netscan/config.json`, then restart)

Only the keys you want to change are needed; these are the defaults:

```json
{"interval_s": 30, "speedtest_interval_min": 0, "speedtest_on_battery": true,
 "internet_targets": ["1.1.1.1", "8.8.8.8", "9.9.9.9"], "path_dest": "1.1.1.1",
 "degraded_loss_pct": 3, "degraded_dead_s": 3, "degraded_rtt_ms": 150, "port": 2325}
```
