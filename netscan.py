#!/usr/bin/env python3
"""netscan - records this Mac's network quality and serves a dashboard.

One process does three jobs:
  * a continuous pinger: one ICMP echo per second to the router, to every
    responding hop on the path to the internet (mtr-style, TTL-limited probes
    toward 1.1.1.1) and to several public anycast hosts;
  * a collector that every `interval_s` seconds folds those pings into one
    window per hop, runs DNS and HTTPS probes, and records the Wi-Fi link,
    network identity, lid and power state; plus a periodic `networkQuality`
    speed test;
  * a web server on 127.0.0.1 with the dashboard and a JSON API.

stdlib only, targets /usr/bin/python3 (3.9). ICMP uses an unprivileged
SOCK_DGRAM socket, which macOS allows without root.

  python3 netscan.py run        # everything, in the foreground
  python3 netscan.py once       # one window (~interval_s), printed as JSON
  python3 netscan.py path       # discover the path to the internet, print it
  python3 netscan.py install    # copy to ~/netscan/app and start a LaunchAgent
  python3 netscan.py uninstall  # stop the LaunchAgent (data is kept)
  python3 netscan.py status
"""
import argparse
import csv
import io
import json
import os
import random
import re
import shutil
import socket
import sqlite3
import ssl
import statistics
import struct
import subprocess
import sys
import threading
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
HOME = os.environ.get("NETSCAN_HOME", os.path.expanduser("~/netscan"))
LABEL = "com.andymoore.netscan"
PLIST = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % LABEL)

DEFAULTS = {
    "port": 2325,
    "interval_s": 30,                       # one stored window per this many seconds
    "internet_targets": ["1.1.1.1", "8.8.8.8", "9.9.9.9"],
    "path_dest": "1.1.1.1",                 # hops on the way here are probed per-second
    "path_rediscover_min": 10,
    "max_path_hops": 6,                     # intermediate hops probed (beyond the router)
    "dns_query_name": "www.google.com",
    "https_url": "https://www.cloudflare.com/cdn-cgi/trace",
    "speedtest_interval_min": 0,            # minutes between scheduled speed tests; 0 = off (they make calls on the same line choppy)
    "speedtest_on_battery": True,
    "speedtest_max_s": 25,
    "degraded_loss_pct": 3,
    "degraded_rtt_ms": 150,
    "degraded_dead_s": 3,
}

CFG = dict(DEFAULTS)


def load_config():
    path = os.path.join(HOME, "config.json")
    if os.path.exists(path):
        with open(path) as f:
            CFG.update(json.load(f))
    return CFG


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def sh(args, timeout=10):
    """Run a command, return stdout ('' on any failure)."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except Exception:
        return ""


# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  ts REAL NOT NULL,
  window_s REAL, cycle_ms REAL,
  status TEXT,
  iface TEXT, iface_type TEXT, vpn INTEGER,
  net_key TEXT, ssid TEXT, bssid TEXT, gateway TEXT, router_mac TEXT, local_ip TEXT,
  rssi INTEGER, noise INTEGER, tx_rate REAL, channel INTEGER, band TEXT, width INTEGER, phy TEXT,
  lid_closed INTEGER, on_ac INTEGER, battery INTEGER,
  gw_sent INTEGER, gw_loss REAL, gw_rtt REAL, gw_max REAL, gw_jitter REAL, gw_burst INTEGER,
  isp_hop INTEGER, isp_ip TEXT, isp_sent INTEGER, isp_loss REAL, isp_rtt REAL, isp_max REAL,
  isp_jitter REAL, isp_burst INTEGER,
  inet_sent INTEGER, inet_loss REAL, inet_rtt REAL, inet_max REAL, inet_jitter REAL, inet_burst INTEGER,
  rounds INTEGER, dead_s INTEGER, dead_lan_s INTEGER, dead_access_s INTEGER, dead_upstream_s INTEGER,
  dns_server TEXT, dns_ok INTEGER, dns_ms REAL,
  http_ok INTEGER, http_dns_ms REAL, http_connect_ms REAL, http_tls_ms REAL,
  http_ttfb_ms REAL, http_total_ms REAL, http_err TEXT,
  public_ip TEXT, cf_colo TEXT,
  speedtest_running INTEGER
);
CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);
CREATE INDEX IF NOT EXISTS samples_net_ts ON samples(net_key, ts);
CREATE TABLE IF NOT EXISTS hops (
  ts REAL NOT NULL, net_key TEXT,
  key TEXT NOT NULL, hop INTEGER, ip TEXT,
  sent INTEGER, recv INTEGER, loss REAL, rtt REAL, rtt_min REAL, rtt_max REAL, jitter REAL,
  burst INTEGER, speedtest INTEGER
);
CREATE INDEX IF NOT EXISTS hops_ts ON hops(ts);
CREATE TABLE IF NOT EXISTS speedtests (
  ts REAL NOT NULL,
  net_key TEXT, iface TEXT, ssid TEXT, lid_closed INTEGER, on_ac INTEGER,
  ok INTEGER, error TEXT,
  dl_mbps REAL, ul_mbps REAL, rpm REAL, base_rtt_ms REAL, duration_s REAL,
  manual INTEGER, raw TEXT
);
CREATE INDEX IF NOT EXISTS speedtests_ts ON speedtests(ts);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL, end_ts REAL,
  kind TEXT NOT NULL, net_key TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS labels (net_key TEXT PRIMARY KEY, label TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hostnames (ip TEXT PRIMARY KEY, name TEXT, ts REAL);
"""


def db_path():
    return os.path.join(HOME, "data", "netscan.db")


def connect():
    os.makedirs(os.path.dirname(db_path()), exist_ok=True)
    c = sqlite3.connect(db_path(), timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def init_db():
    c = connect()
    c.executescript(SCHEMA)
    c.commit()
    c.close()


def insert(c, table, row, commit=True):
    keys = list(row)
    c.execute("INSERT INTO %s (%s) VALUES (%s)" % (table, ",".join(keys), ",".join("?" * len(keys))),
              [row[k] for k in keys])
    if commit:
        c.commit()


def add_event(c, kind, detail, net_key=None, ts=None, end_ts=None):
    insert(c, "events", {"ts": ts or time.time(), "end_ts": end_ts, "kind": kind,
                         "net_key": net_key, "detail": detail})
    log("event", kind, detail)


# ---------------------------------------------------------------- ICMP

def icmp_checksum(b):
    if len(b) % 2:
        b += b"\0"
    t = sum(struct.unpack("!%dH" % (len(b) // 2), b))
    t = (t >> 16) + (t & 0xFFFF)
    t += t >> 16
    return ~t & 0xFFFF


def icmp_echo(ident, seq, payload=b"netscan-probe-0123456789abcdef"):
    h = struct.pack("!BBHHH", 8, 0, 0, ident, seq)
    return struct.pack("!BBHHH", 8, 0, icmp_checksum(h + payload), ident, seq) + payload


def icmp_parse(d):
    """-> (type, ident, seq) for echo replies and for errors quoting our echo; None otherwise.
    macOS delivers the IP header on SOCK_DGRAM ICMP sockets."""
    try:
        ihl = (d[0] & 0x0F) * 4 if d[0] >> 4 == 4 else 0
        typ = d[ihl]
        if typ == 0:
            rid, rseq = struct.unpack("!HH", d[ihl + 4:ihl + 8])
        elif typ in (3, 11):
            inner = d[ihl + 8:]
            iihl = (inner[0] & 0x0F) * 4
            if inner[iihl] != 8:
                return None
            rid, rseq = struct.unpack("!HH", inner[iihl + 4:iihl + 8])
        else:
            return None
        return typ, rid, rseq
    except (IndexError, struct.error):
        return None


def icmp_socket():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    s.settimeout(0.5)
    return s


def discover_path(dest, max_ttl=16, tries=3):
    """mtr-style discovery with ICMP: -> (hops {ttl: ip or None}, dest_ttl or None)."""
    s = icmp_socket()
    ident = random.randint(1, 0xFFFF)
    sent = {}
    seen = defaultdict(Counter)
    reached = []
    try:
        for t in range(tries):
            for ttl in range(1, max_ttl + 1):
                seq = t * 64 + ttl
                s.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
                try:
                    s.sendto(icmp_echo(ident, seq), (dest, 0))
                except OSError:
                    pass
                sent[seq] = ttl
            end = time.time() + 1.2
            while time.time() < end:
                try:
                    d, a = s.recvfrom(2048)
                except socket.timeout:
                    continue
                p = icmp_parse(d)
                if not p or p[1] != ident or p[2] not in sent:
                    continue
                ttl = sent[p[2]]
                if p[0] == 0:
                    reached.append(ttl)
                elif p[0] == 11:
                    seen[ttl][a[0]] += 1
    except OSError as e:
        log("path discovery failed:", e)
    finally:
        s.close()
    dest_ttl = min(reached) if reached else None
    top = (dest_ttl - 1) if dest_ttl else max(list(seen) or [0])
    hops = {ttl: (seen[ttl].most_common(1)[0][0] if seen[ttl] else None) for ttl in range(1, top + 1)}
    return hops, dest_ttl


class Pinger:
    """One echo per target per second on a single socket; harvested into windows."""
    TIMEOUT = 2.5

    def __init__(self):
        self.sock = icmp_socket()
        self.ident = random.randint(1, 0xFFFF)
        self.lock = threading.Lock()
        self.targets = []
        self.inflight = {}
        self.records = []
        self.seq = 0
        self.round = 0
        self.stop = threading.Event()
        self.last_wall = None

    def start(self):
        threading.Thread(target=self.send_loop, daemon=True, name="ping-send").start()
        threading.Thread(target=self.recv_loop, daemon=True, name="ping-recv").start()

    def set_targets(self, targets):
        with self.lock:
            self.targets = list(targets)

    def void(self):
        """Forget packets in flight (the Mac slept; they were never really lost)."""
        with self.lock:
            self.records = []
            self.inflight = {}

    def send_loop(self):
        while not self.stop.is_set():
            t0 = time.time()
            if self.last_wall and t0 - self.last_wall > 5:
                self.void()
            self.last_wall = t0
            with self.lock:
                targets = list(self.targets)
                self.round += 1
                rnd = self.round
            for t in targets:
                self.send(t, rnd)
                time.sleep(0.005)
            self.stop.wait(max(0.0, 1.0 - (time.time() - t0)))

    def send(self, t, rnd):
        with self.lock:
            self.seq = (self.seq + 1) & 0xFFFF
            seq = self.seq
            rec = {"key": t["key"], "hop": t.get("hop"), "round": rnd, "seq": seq, "t": time.time(),
                   "p": time.perf_counter(), "rtt": None, "ip": None, "err": None}
            self.inflight[seq] = rec
            self.records.append(rec)
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, t.get("ttl", 64))
            self.sock.sendto(icmp_echo(self.ident, seq), (t["dest"], 0))
        except OSError as e:
            rec["err"] = e.strerror or str(e)
            with self.lock:
                self.inflight.pop(seq, None)

    def recv_loop(self):
        while not self.stop.is_set():
            try:
                d, a = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                time.sleep(0.5)
                continue
            now = time.perf_counter()
            p = icmp_parse(d)
            if not p or p[1] != self.ident:
                continue
            with self.lock:
                rec = self.inflight.pop(p[2], None)
            if not rec or now - rec["p"] > self.TIMEOUT:
                continue
            rec["ip"] = a[0]
            if p[0] in (0, 11):
                rec["rtt"] = (now - rec["p"]) * 1000
            else:
                rec["err"] = "unreachable"

    def harvest(self):
        """Records old enough to be final (answered, or lost after TIMEOUT)."""
        cut = time.time() - self.TIMEOUT
        with self.lock:
            ready = [r for r in self.records if r["t"] < cut]
            self.records = [r for r in self.records if r["t"] >= cut]
            for r in ready:
                if self.inflight.get(r["seq"]) is r:
                    del self.inflight[r["seq"]]
        return ready


def stats(recs):
    """loss %, rtt avg/min/max, jitter (mean |delta| of consecutive replies), longest loss run."""
    recs = sorted(recs, key=lambda r: r["round"])
    sent = len(recs)
    rtts = [r["rtt"] for r in recs if r["rtt"] is not None]
    burst = run = 0
    for r in recs:
        run = run + 1 if r["rtt"] is None else 0
        burst = max(burst, run)
    ips = Counter(r["ip"] for r in recs if r["ip"] and r["rtt"] is not None)
    res = {"sent": sent, "recv": len(rtts),
           "loss": round(100.0 * (sent - len(rtts)) / sent, 2) if sent else None,
           "rtt": round(statistics.mean(rtts), 2) if rtts else None,
           "min": round(min(rtts), 2) if rtts else None,
           "max": round(max(rtts), 2) if rtts else None,
           "jitter": round(statistics.mean(abs(a - b) for a, b in zip(rtts, rtts[1:])), 2) if len(rtts) > 1 else None,
           "burst": burst,
           "ip": ips.most_common(1)[0][0] if ips else None}
    return res


def fold_window(recs):
    """Per-target stats plus the internet aggregate and blackout-second attribution."""
    by_key = defaultdict(list)
    for r in recs:
        by_key[r["key"]].append(r)
    per = {k: stats(v) for k, v in by_key.items()}
    for k, v in by_key.items():
        per[k]["hop"] = v[0]["hop"]
    inet_recs = [r for r in recs if r["key"].startswith("inet:")]
    hop_keys = sorted((k for k in by_key if k.startswith("hop")), key=lambda k: by_key[k][0]["hop"])
    isp_key = hop_keys[0] if hop_keys else None
    rounds = defaultdict(dict)
    for r in recs:
        rounds[r["round"]].setdefault(r["key"], []).append(r)
    dead = {"lan": 0, "access": 0, "upstream": 0}
    nrounds = run = longest = 0
    for rnd in sorted(rounds):
        ks = rounds[rnd]
        inet = [x for k, v in ks.items() if k.startswith("inet:") for x in v]
        if not inet:
            continue
        nrounds += 1
        if any(x["rtt"] is not None for x in inet):
            run = 0
            continue
        run += 1
        longest = max(longest, run)
        gw = ks.get("gw")
        isp = ks.get(isp_key) if isp_key else None
        if not gw or gw[0]["rtt"] is None:
            dead["lan"] += 1
        elif not isp or isp[0]["rtt"] is None:
            dead["access"] += 1
        else:
            dead["upstream"] += 1
    agg = stats(inet_recs) if inet_recs else None
    if agg:
        # jitter per target, averaged: interleaved targets would otherwise look jittery
        js = [per[k]["jitter"] for k in by_key if k.startswith("inet:") and per[k]["jitter"] is not None]
        agg["jitter"] = round(statistics.mean(js), 2) if js else None
        agg["burst"] = longest  # longest run of seconds with every internet host silent
    return {"per": per, "inet": agg, "isp_key": isp_key, "rounds": nrounds,
            "dead": dead, "dead_s": sum(dead.values())}


# ---------------------------------------------------------------- DNS / HTTPS

def nameserver():
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                p = line.split()
                if len(p) >= 2 and p[0] == "nameserver":
                    return p[1]
    except OSError:
        pass
    return None


def dns_probe(server, name):
    """Time one A query sent straight to the resolver the system uses (no local cache)."""
    if not server:
        return {"dns_server": None, "dns_ok": 0, "dns_ms": None}
    qid = random.randint(0, 0xFFFF)
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    q += struct.pack(">HH", 1, 1)
    fam = socket.AF_INET6 if ":" in server else socket.AF_INET
    for attempt in range(2):
        s = socket.socket(fam, socket.SOCK_DGRAM)
        s.settimeout(2.5)
        try:
            t0 = time.perf_counter()
            s.sendto(q, (server.split("%")[0], 53))
            while True:
                data, _ = s.recvfrom(2048)
                if len(data) >= 4 and struct.unpack(">H", data[:2])[0] == qid:
                    break
            ms = (time.perf_counter() - t0) * 1000
            return {"dns_server": server, "dns_ok": int(data[3] & 0x0F == 0), "dns_ms": round(ms, 2)}
        except OSError:
            pass
        finally:
            s.close()
    return {"dns_server": server, "dns_ok": 0, "dns_ms": None}


def https_probe(url):
    """GET url over IPv4 with per-phase timings. cdn-cgi/trace also reports our public IP."""
    u = urlparse(url)
    host, port, path = u.hostname, u.port or 443, u.path or "/"
    r = {"http_ok": 0, "http_dns_ms": None, "http_connect_ms": None, "http_tls_ms": None,
         "http_ttfb_ms": None, "http_total_ms": None, "http_err": None, "public_ip": None, "cf_colo": None}
    t0 = time.perf_counter()
    stage, sock = "dns", None
    try:
        ip = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
        t1 = time.perf_counter()
        r["http_dns_ms"] = round((t1 - t0) * 1000, 2)
        stage = "connect"
        sock = socket.create_connection((ip, port), timeout=8)
        t2 = time.perf_counter()
        r["http_connect_ms"] = round((t2 - t1) * 1000, 2)
        stage = "tls"
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        t3 = time.perf_counter()
        r["http_tls_ms"] = round((t3 - t2) * 1000, 2)
        stage = "response"
        sock.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: netscan/1\r\n"
                      "Connection: close\r\n\r\n" % (path, host)).encode())
        buf = sock.recv(1)
        r["http_ttfb_ms"] = round((time.perf_counter() - t3) * 1000, 2)
        while len(buf) < 1 << 20:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        r["http_total_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        text = buf.decode("latin-1")
        status = text.split(" ", 2)[1] if " " in text else "?"
        r["http_ok"] = int(status.startswith("2"))
        if not r["http_ok"]:
            r["http_err"] = "HTTP " + status
        m = re.search(r"^ip=(\S+)", text, re.M)
        r["public_ip"] = m.group(1) if m else None
        m = re.search(r"^colo=(\S+)", text, re.M)
        r["cf_colo"] = m.group(1) if m else None
    except Exception as e:
        r["http_err"] = "%s: %s" % (stage, (str(e) or type(e).__name__)[:120])
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
    return r


# ---------------------------------------------------------------- host state

_ports = {"t": 0, "map": {}}


def hardware_ports():
    """device -> hardware port name ('Wi-Fi', 'Ethernet Adapter (en4)', ...), cached 10 min."""
    if time.time() - _ports["t"] > 600:
        m, port = {}, None
        for line in sh(["/usr/sbin/networksetup", "-listallhardwareports"]).splitlines():
            if line.startswith("Hardware Port:"):
                port = line.split(":", 1)[1].strip()
            elif line.startswith("Device:") and port:
                m[line.split(":", 1)[1].strip()] = port
        _ports.update(t=time.time(), map=m)
    return _ports["map"]


def wifi_device():
    for dev, name in hardware_ports().items():
        if name in ("Wi-Fi", "AirPort"):
            return dev
    return None


def default_route():
    out = sh(["/sbin/route", "-n", "get", "default"], timeout=5)
    gw = re.search(r"gateway: (\S+)", out)
    iface = re.search(r"interface: (\S+)", out)
    return (gw.group(1) if gw else None), (iface.group(1) if iface else None)


def wifi_info():
    helper = os.path.join(APP_DIR, "bin", "wifiinfo")
    if os.access(helper, os.X_OK):
        try:
            return json.loads(sh([helper], timeout=5) or "{}")
        except ValueError:
            return {}
    # Fallback: system_profiler is slow (~8s) but needs nothing compiled.
    try:
        d = json.loads(sh(["/usr/sbin/system_profiler", "SPAirPortDataType", "-json"], timeout=30))
        for i in d["SPAirPortDataType"][0]["spairport_airport_interfaces"]:
            cur = i.get("spairport_current_network_information")
            if not cur:
                continue
            res = {"iface": i.get("_name")}
            m = re.match(r"(-?\d+) dBm / (-?\d+) dBm", cur.get("spairport_signal_noise", ""))
            if m:
                res["rssi"], res["noise"] = int(m.group(1)), int(m.group(2))
            res["tx_rate"] = cur.get("spairport_network_rate")
            m = re.match(r"(\d+) \((\S+), (\d+)MHz\)", cur.get("spairport_network_channel", ""))
            if m:
                res["channel"], res["band"], res["width"] = int(m.group(1)), m.group(2), int(m.group(3))
            res["phy"] = cur.get("spairport_network_phymode")
            name = cur.get("_name")
            res["ssid"] = None if not name or "redacted" in name else name
            return res
    except Exception:
        pass
    return {}


def ipconfig_summary(dev):
    """SSID/BSSID from ipconfig; macOS redacts them unless `sudo ipconfig setverbose 1` was run."""
    out = sh(["/usr/sbin/ipconfig", "getsummary", dev], timeout=5)
    res = {}
    for key in ("SSID", "BSSID"):
        m = re.search(r"^\s*%s : (.+)$" % key, out, re.M)
        if m and "redacted" not in m.group(1):
            res[key.lower()] = m.group(1).strip()
    return res


def router_mac(gw):
    if not gw:
        return None
    m = re.search(r"at ([0-9a-f:]+) ", sh(["/usr/sbin/arp", "-n", gw], timeout=5))
    if not m:
        return None
    return ":".join(x.zfill(2) for x in m.group(1).split(":"))


def local_ip(dev):
    if not dev:
        return None
    return sh(["/usr/sbin/ipconfig", "getifaddr", dev], timeout=5).strip() or None


def lid_closed():
    out = sh(["/usr/sbin/ioreg", "-r", "-k", "AppleClamshellState", "-d", "1"], timeout=5)
    m = re.search(r'"AppleClamshellState" = (Yes|No)', out)
    return None if not m else int(m.group(1) == "Yes")


def power():
    out = sh(["/usr/bin/pmset", "-g", "batt"], timeout=5)
    on_ac = 1 if "AC Power" in out else 0 if "Battery Power" in out else None
    m = re.search(r"(\d+)%", out)
    return on_ac, (int(m.group(1)) if m else None)


def iface_type(dev):
    if not dev:
        return "none"
    name = hardware_ports().get(dev)
    if name in ("Wi-Fi", "AirPort"):
        return "wifi"
    if name and "iPhone" in name:
        return "hotspot"
    if name and ("Ethernet" in name or "LAN" in name or "Thunderbolt" in name):
        return "ethernet"
    if dev.startswith(("utun", "ipsec", "ppp", "tun", "tap")):
        return "vpn"
    return name or "other"


def reverse_dns(ip, timeout=2.0):
    res = [None]

    def look():
        try:
            res[0] = socket.gethostbyaddr(ip)[0]
        except Exception:
            pass
    t = threading.Thread(target=look, daemon=True)
    t.start()
    t.join(timeout)
    return res[0]


PM_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}) (Sleep|Wake|DarkWake)\s+\t(.*)$")


def sleep_reasons(start, end):
    """Read pmset's log for why the Mac slept/woke between start and end."""
    out = sh(["/usr/bin/pmset", "-g", "log"], timeout=30)
    first_sleep, last_wake, dark = None, None, 0
    for line in out.splitlines():
        m = PM_LINE.match(line)
        if not m:
            continue
        try:
            t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S %z").timestamp()
        except ValueError:
            continue
        if t < start - 60 or t > end + 60:
            continue
        kind, text = m.group(2), m.group(3)
        if kind == "Sleep" and first_sleep is None:
            r = re.search(r"due to '([^']+)'", text)
            first_sleep = r.group(1) if r else text[:60].strip()
        elif kind == "DarkWake":
            dark += 1
        elif kind == "Wake":
            r = re.search(r"due to (.+?)(?: Using| \d+ secs|$)", text)
            last_wake = r.group(1).strip() if r else text[:60].strip()
    res = {}
    if first_sleep:
        res["sleep"] = first_sleep
    if last_wake:
        res["wake"] = last_wake
    if dark:
        res["dark_wakes"] = dark
    return res


# ---------------------------------------------------------------- collector

class Collector:
    def __init__(self):
        self.stop = threading.Event()
        self.pinger = Pinger()
        self.speedtest_now = threading.Event()
        self.speedtest_running = False
        self.next_speedtest = None
        self.path = {"gw": None, "hops": {}, "dest_ttl": None, "t": 0, "busy": False}
        self.prev = {}
        self.last_cycle = None
        self.window_start = time.time()
        self.started = time.time()

    # -- path / targets
    def ensure_path(self, gw, dev):
        p = self.path
        stale = time.time() - p["t"] > CFG["path_rediscover_min"] * 60
        empty = not p["hops"] and time.time() - p["t"] > 60
        if (p["gw"] != gw or stale or empty) and not p["busy"] and dev:
            if p["gw"] != gw:
                # new network: stop probing the old path right away
                p.update(gw=gw, hops={}, dest_ttl=None)
                self.set_targets()
            p["busy"] = True
            threading.Thread(target=self._discover, args=(gw,), daemon=True).start()
        elif not dev and p["gw"] is not None:
            p.update(gw=None, hops={}, dest_ttl=None)
            self.set_targets()

    def _discover(self, gw):
        try:
            hops, dest_ttl = discover_path(CFG["path_dest"])
            if self.path["gw"] != gw:
                return
            old = [self.path["hops"].get(k) for k in sorted(self.path["hops"])]
            self.path.update(hops=hops, dest_ttl=dest_ttl, t=time.time())
            new = [hops.get(k) for k in sorted(hops)]
            if new != old and any(new):
                names = {}
                c = connect()
                for ip in set(filter(None, new)):
                    row = c.execute("SELECT name FROM hostnames WHERE ip=?", (ip,)).fetchone()
                    if row is None:
                        name = reverse_dns(ip)
                        c.execute("INSERT OR REPLACE INTO hostnames VALUES (?,?,?)", (ip, name, time.time()))
                        c.commit()
                    else:
                        name = row["name"]
                    names[ip] = name
                add_event(c, "path", json.dumps({"dest": CFG["path_dest"], "dest_ttl": dest_ttl,
                                                 "hops": [[k, hops[k], names.get(hops[k])] for k in sorted(hops)]}),
                          self.prev.get("net_key"))
                c.close()
            self.set_targets()
        except Exception:
            log("discover failed:", traceback.format_exc())
        finally:
            self.path["t"] = time.time()
            self.path["busy"] = False

    def set_targets(self):
        gw = self.path["gw"]
        targets = []
        if gw:
            targets.append({"key": "gw", "dest": gw, "ttl": 64, "hop": 1})
        n = 0
        for ttl in sorted(self.path["hops"]):
            ip = self.path["hops"][ttl]
            if ttl < 2 or not ip or ip == gw or n >= CFG["max_path_hops"]:
                continue
            targets.append({"key": "hop%d" % ttl, "dest": CFG["path_dest"], "ttl": ttl, "hop": ttl})
            n += 1
        if gw or self.path["hops"]:
            for ip in CFG["internet_targets"]:
                targets.append({"key": "inet:" + ip, "dest": ip, "ttl": 64, "hop": None})
        self.pinger.set_targets(targets)

    # -- one window
    def sample(self, c=None):
        t_start = time.perf_counter()
        ts = time.time()
        gw, dev = default_route()
        wdev = wifi_device()
        vpn = int(bool(dev) and iface_type(dev) == "vpn")
        phys = dev
        if vpn and wdev:
            phys = wdev
            gw = sh(["/usr/sbin/ipconfig", "getoption", wdev, "router"], timeout=5).strip() or None
        self.ensure_path(gw, dev)
        itype = iface_type(phys)
        on_ac, batt = power()
        s = {"ts": ts, "window_s": round(ts - self.window_start, 1), "iface": phys, "iface_type": itype,
             "vpn": vpn, "gateway": gw, "speedtest_running": int(self.speedtest_running),
             "lid_closed": lid_closed(), "on_ac": on_ac, "battery": batt}
        self.window_start = ts

        jobs = {}
        if dev:
            jobs["dns"] = lambda: dns_probe(nameserver(), CFG["dns_query_name"])
            jobs["http"] = lambda: https_probe(CFG["https_url"])
        if wdev:
            jobs["wifi"] = wifi_info
        res = {}

        def run(k, f):
            try:
                res[k] = f()
            except Exception:
                log("probe %s failed: %s" % (k, traceback.format_exc(limit=2)))
        threads = [threading.Thread(target=run, args=kv, daemon=True) for kv in jobs.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=40)
        s.update(res.get("dns", {}))
        s.update(res.get("http", {}))

        w = res.get("wifi") or {}
        if itype == "wifi" or vpn:
            if w.get("rssi"):  # 0 means not associated
                s.update(rssi=w["rssi"], noise=w.get("noise"), tx_rate=w.get("tx_rate"),
                         channel=w.get("channel"), band=w.get("band"), width=w.get("width"), phy=w.get("phy"))
            idn = ipconfig_summary(wdev) if wdev else {}
            s["ssid"] = w.get("ssid") or idn.get("ssid")
            s["bssid"] = w.get("bssid") or idn.get("bssid")
        s["router_mac"] = router_mac(gw)
        s["local_ip"] = local_ip(phys)
        if s.get("ssid"):
            s["net_key"] = "wifi:" + s["ssid"]
        elif s["router_mac"]:
            s["net_key"] = "%s:%s" % (itype, s["router_mac"])
        elif phys:
            s["net_key"] = "%s:%s" % (itype, phys)
        else:
            s["net_key"] = "none"

        win = fold_window(self.pinger.harvest())
        hop_rows = []
        for key, st in win["per"].items():
            hop_rows.append({"ts": ts, "net_key": s["net_key"], "key": key, "hop": st["hop"], "ip": st["ip"],
                             "sent": st["sent"], "recv": st["recv"], "loss": st["loss"], "rtt": st["rtt"],
                             "rtt_min": st["min"], "rtt_max": st["max"], "jitter": st["jitter"],
                             "burst": st["burst"], "speedtest": int(self.speedtest_running)})
        for prefix, st in (("gw", win["per"].get("gw")), ("isp", win["per"].get(win["isp_key"])),
                           ("inet", win["inet"])):
            if st:
                for f in ("sent", "loss", "rtt", "max", "jitter", "burst"):
                    s["%s_%s" % (prefix, f)] = st[f]
        if win["isp_key"]:
            s["isp_hop"] = win["per"][win["isp_key"]]["hop"]
            s["isp_ip"] = win["per"][win["isp_key"]]["ip"]
        s.update(rounds=win["rounds"], dead_s=win["dead_s"], dead_lan_s=win["dead"]["lan"],
                 dead_access_s=win["dead"]["access"], dead_upstream_s=win["dead"]["upstream"])
        s["status"] = self.classify(s, dev)
        s["cycle_ms"] = round((time.perf_counter() - t_start) * 1000, 1)
        if c is not None:
            insert(c, "samples", s, commit=False)
            for h in hop_rows:
                insert(c, "hops", h, commit=False)
            c.commit()
        s["_hops"] = hop_rows
        return s

    def classify(self, s, dev):
        if not dev:
            return "no-network"
        sent = s.get("inet_sent") or 0
        inet_dead = sent > 0 and s.get("inet_loss") == 100
        if (inet_dead or not sent) and not s.get("http_ok"):
            return "offline"
        if (inet_dead or (s.get("inet_loss") or 0) >= CFG["degraded_loss_pct"]
                or (s.get("dead_s") or 0) >= CFG["degraded_dead_s"]
                or not s.get("http_ok") or not s.get("dns_ok")
                or (s.get("inet_rtt") or 0) >= CFG["degraded_rtt_ms"]):
            return "degraded"
        return "ok"

    # -- change tracking
    def note_changes(self, c, s):
        p = self.prev
        same = s["net_key"] == p.get("net_key")
        if p:
            if not same:
                add_event(c, "network", "%s -> %s" % (p.get("net_key"), s["net_key"]), s["net_key"], s["ts"])
            if same and s.get("public_ip") and p.get("public_ip") and s["public_ip"] != p["public_ip"]:
                add_event(c, "public-ip", "%s -> %s" % (p["public_ip"], s["public_ip"]), s["net_key"], s["ts"])
            if s.get("lid_closed") is not None and p.get("lid_closed") is not None \
                    and s["lid_closed"] != p["lid_closed"]:
                add_event(c, "lid", "closed" if s["lid_closed"] else "opened", s["net_key"], s["ts"])
            if s.get("on_ac") is not None and p.get("on_ac") is not None and s["on_ac"] != p["on_ac"]:
                add_event(c, "power", "AC power" if s["on_ac"] else "battery", s["net_key"], s["ts"])
        pub = s.get("public_ip") or (p.get("public_ip") if same else None)
        self.prev = {"net_key": s["net_key"], "lid_closed": s.get("lid_closed"), "on_ac": s.get("on_ac"),
                     "public_ip": pub}

    # -- loops
    def run(self):
        c = connect()
        last = c.execute("SELECT MAX(ts) FROM samples").fetchone()[0]
        add_event(c, "start", "collector started, window %ss" % CFG["interval_s"])
        gw, dev = default_route()
        self.ensure_path(gw, dev)
        self.pinger.start()
        threading.Thread(target=self.speedtest_loop, daemon=True).start()
        interval = CFG["interval_s"]
        gap_limit = max(3 * interval, 120)
        self.window_start = time.time()
        self.stop.wait(interval)
        while not self.stop.is_set():
            now = time.time()
            if last and now - last > gap_limit:
                # Asleep or not running: log the gap, drop pings that straddled it, let Wi-Fi rejoin.
                detail = {"minutes": round((now - last) / 60, 1)}
                detail.update(sleep_reasons(last, now))
                add_event(c, "gap", json.dumps(detail), self.prev.get("net_key"), last, now)
                self.prev.pop("lid_closed", None)
                self.pinger.void()
                self.stop.wait(15)
                self.pinger.void()
                self.window_start = time.time()
                self.stop.wait(interval)
            start = time.time()
            try:
                s = self.sample(c)
                self.note_changes(c, s)
                self.last_cycle = s
                last = s["ts"]
            except Exception:
                log("cycle failed:", traceback.format_exc())
                last = time.time()
            # Wait on the wall clock in short steps so a wake from sleep is noticed promptly.
            while not self.stop.is_set() and time.time() < start + interval:
                if time.time() - start > gap_limit:
                    break
                self.stop.wait(1)

    def speedtest_loop(self):
        c = connect()
        mins = CFG["speedtest_interval_min"]
        row = c.execute("SELECT MAX(ts) FROM speedtests WHERE manual=0").fetchone()[0]
        if mins:
            self.next_speedtest = max(row + mins * 60, time.time() + 120) if row else time.time() + 120
        while not self.stop.is_set():
            manual = self.speedtest_now.wait(10)
            due = self.next_speedtest and time.time() >= self.next_speedtest
            if not (manual or due):
                continue
            self.speedtest_now.clear()
            if mins:
                self.next_speedtest = time.time() + mins * 60
            s = self.last_cycle or {}
            if not manual and s.get("on_ac") == 0 and not CFG["speedtest_on_battery"]:
                continue
            if not manual and s.get("status") in ("no-network", "offline"):
                continue
            self.run_speedtest(c, manual)

    def run_speedtest(self, c, manual):
        s = self.last_cycle or {}
        self.speedtest_running = True
        t0 = time.time()
        row = {"ts": t0, "net_key": s.get("net_key"), "iface": s.get("iface"), "ssid": s.get("ssid"),
               "lid_closed": lid_closed(), "on_ac": power()[0], "manual": int(bool(manual)), "ok": 0,
               "error": None}
        try:
            out = sh(["/usr/bin/networkQuality", "-c", "-M", str(CFG["speedtest_max_s"])],
                     timeout=CFG["speedtest_max_s"] + 60)
            d = json.loads(out) if out.strip() else {}
            dl, ul = d.get("dl_throughput"), d.get("ul_throughput")
            rpm = d.get("responsiveness")
            if isinstance(rpm, dict):
                rpm = rpm.get("value")
            row.update(dl_mbps=round(dl / 1e6, 2) if dl else None, ul_mbps=round(ul / 1e6, 2) if ul else None,
                       rpm=rpm, base_rtt_ms=d.get("base_rtt"),
                       raw=json.dumps({k: v for k, v in d.items() if not isinstance(v, (list, dict))}))
            row["ok"] = int(bool(dl or ul))
            if not row["ok"]:
                row["error"] = str(d.get("error_description") or d.get("error_code") or "no result")[:200]
        except Exception as e:
            row["error"] = str(e)[:200]
        row["duration_s"] = round(time.time() - t0, 1)
        self.speedtest_running = False
        insert(c, "speedtests", row)
        log("speedtest", "manual" if manual else "scheduled", row.get("dl_mbps"), row.get("ul_mbps"), row["error"])


# ---------------------------------------------------------------- queries

def net_filter(q, alias=""):
    keys = [k for k in (q.get("net") or "").split(",") if k]
    if not keys:
        return "", []
    return " AND %snet_key IN (%s)" % (alias, ",".join("?" * len(keys))), keys


def time_range(q):
    to = float(q.get("to") or time.time())
    frm = float(q.get("from") or to - 86400)
    return frm, to


def pct(vals, p):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(vals) - 1)
    return round(vals[f] + (vals[c] - vals[f]) * (k - f), 2)


def bucket_for(frm, to, q):
    points = min(int(q.get("points") or 800), 5000)
    return max(CFG["interval_s"], (to - frm) / points)


def weighted(expr, w):
    return "SUM((%s)*%s)/NULLIF(SUM(CASE WHEN %s IS NULL THEN 0 ELSE %s END),0)" % (expr, w, expr, w)


SERIES_COLS = [
    ("gw_rtt", weighted("gw_rtt", "gw_sent")), ("gw_max", "MAX(gw_max)"),
    ("gw_loss", weighted("gw_loss", "gw_sent")), ("gw_jitter", "AVG(gw_jitter)"),
    ("isp_rtt", weighted("isp_rtt", "isp_sent")), ("isp_loss", weighted("isp_loss", "isp_sent")),
    ("isp_jitter", "AVG(isp_jitter)"),
    ("inet_rtt", weighted("inet_rtt", "inet_sent")), ("inet_max", "MAX(inet_max)"),
    ("inet_loss", weighted("inet_loss", "inet_sent")), ("inet_jitter", "AVG(inet_jitter)"),
    ("dead_lan_s", "SUM(dead_lan_s)"), ("dead_access_s", "SUM(dead_access_s)"),
    ("dead_upstream_s", "SUM(dead_upstream_s)"),
    ("dns_ms", "AVG(dns_ms)"), ("dns_fail", "AVG(1-COALESCE(dns_ok,0))*100"),
    ("http_connect_ms", "AVG(http_connect_ms)"), ("http_tls_ms", "AVG(http_tls_ms)"),
    ("http_ttfb_ms", "AVG(http_ttfb_ms)"), ("http_total_ms", "AVG(http_total_ms)"),
    ("rssi", "AVG(rssi)"), ("noise", "AVG(noise)"), ("tx_rate", "AVG(tx_rate)"),
    ("lid_closed", "AVG(lid_closed)"), ("on_ac", "AVG(on_ac)"),
    ("offline", "AVG(status='offline')"), ("degraded", "AVG(status='degraded')"),
    ("nonet", "AVG(status='no-network')"), ("speedtest", "MAX(speedtest_running)"),
    ("net_key", "MAX(net_key)"), ("n", "COUNT(*)"),
]


def gapped(rows, bucket, names, tfn):
    """Column arrays; a null row breaks lines where the collector wasn't running."""
    out = {"t": []}
    out.update({n: [] for n in names})
    gap = max(bucket * 2.5, CFG["interval_s"] * 4)
    prev = None
    for r in rows:
        t = tfn(r)
        if prev is not None and t - prev > gap:
            out["t"].append(prev + bucket)
            for n in names:
                out[n].append(None)
        out["t"].append(round(t, 1))
        for n in names:
            v = r[n]
            out[n].append(round(v, 3) if isinstance(v, float) else v)
        prev = t
    return out


def q_series(c, q):
    frm, to = time_range(q)
    bucket = bucket_for(frm, to, q)
    nf, args = net_filter(q)
    sql = ("SELECT CAST(ts/? AS INTEGER) AS k, MIN(ts) AS t, %s FROM samples WHERE ts>=? AND ts<=?%s "
           "GROUP BY k ORDER BY k") % (",".join("%s AS %s" % (e, n) for n, e in SERIES_COLS), nf)
    rows = c.execute(sql, [bucket, frm, to] + args).fetchall()
    fine = bucket <= CFG["interval_s"]
    out = gapped(rows, bucket, [n for n, _ in SERIES_COLS], lambda r: r["t"] if fine else r["k"] * bucket)
    return {"from": frm, "to": to, "bucket": bucket, "interval": CFG["interval_s"], "series": out}


def hostnames(c):
    return {r["ip"]: r["name"] for r in c.execute("SELECT ip, name FROM hostnames")}


def q_path(c, q):
    """Per-hop loss/latency over the range, a loss-over-time series per hop, and a verdict."""
    frm, to = time_range(q)
    nf, args = net_filter(q)
    excl = "" if q.get("include_speedtest") else " AND COALESCE(speedtest,0)=0"
    names = hostnames(c)
    rows = c.execute(("SELECT key, MIN(hop) hop, SUM(sent) sent, SUM(recv) recv, %s rtt, MAX(burst) burst, "
                      "GROUP_CONCAT(DISTINCT ip) ips FROM hops WHERE ts>=? AND ts<=?%s%s GROUP BY key")
                     % (weighted("rtt", "recv"), nf, excl), [frm, to] + args).fetchall()
    rtts = defaultdict(list)
    for r in c.execute("SELECT key, rtt FROM hops WHERE ts>=? AND ts<=? AND rtt IS NOT NULL%s%s" % (nf, excl),
                       [frm, to] + args):
        rtts[r["key"]].append(r["rtt"])
    hops = []
    inet = {"key": "inet", "label": "Internet (%s)" % ", ".join(CFG["internet_targets"]), "sent": 0, "recv": 0,
            "burst": 0, "role": "internet", "hop": 99}
    inet_rtts = []
    for r in rows:
        d = dict(r)
        d["loss"] = round(100.0 * (d["sent"] - d["recv"]) / d["sent"], 3) if d["sent"] else None
        d["rtt"] = round(d["rtt"], 2) if d["rtt"] is not None else None
        d["rtt_p50"] = pct(rtts[r["key"]], 50)
        d["rtt_p95"] = pct(rtts[r["key"]], 95)
        ips = [i for i in (d.pop("ips") or "").split(",") if i]
        d["ips"] = ips
        d["names"] = [names.get(i) for i in ips]
        if r["key"].startswith("inet:"):
            ip = r["key"][5:]
            d.update(role="target", label=ip, hop=None)
            inet["sent"] += d["sent"]
            inet["recv"] += d["recv"]
            inet_rtts += rtts[r["key"]]
            if ip not in CFG["internet_targets"]:
                continue
        elif r["key"] == "gw":
            d.update(role="router", label="Your router")
        else:
            d.update(role="hop", label="Hop %d" % d["hop"])
        hops.append(d)
    hops.sort(key=lambda h: (h["hop"] is None, h["hop"] or 0, h["key"]))
    path_hops = [h for h in hops if h["role"] in ("router", "hop")]
    if len(path_hops) > 1:
        path_hops[1]["role"] = "isp"
        path_hops[1]["label"] = "Hop %d - first beyond your router" % path_hops[1]["hop"]
    if inet["sent"]:
        inet["loss"] = round(100.0 * (inet["sent"] - inet["recv"]) / inet["sent"], 3)
        inet["rtt_p50"] = pct(inet_rtts, 50)
        inet["rtt_p95"] = pct(inet_rtts, 95)
    dead = c.execute("SELECT SUM(dead_lan_s) lan, SUM(dead_access_s) access, SUM(dead_upstream_s) upstream, "
                     "SUM(rounds) rounds, MAX(inet_burst) burst FROM samples WHERE ts>=? AND ts<=?%s" % nf, [frm, to] + args).fetchone()

    if dead:
        inet["burst"] = dead["burst"] or 0
    mark_rate_limited(path_hops + [inet])
    # loss over time per hop
    bucket = bucket_for(frm, to, q)
    srows = c.execute(("SELECT CAST(ts/? AS INTEGER) k, key, SUM(sent) sent, SUM(recv) recv FROM hops "
                       "WHERE ts>=? AND ts<=?%s GROUP BY k, key ORDER BY k") % nf, [bucket, frm, to] + args).fetchall()
    ks = sorted({r["k"] for r in srows})
    idx = {k: i for i, k in enumerate(ks)}
    series = {"t": [k * bucket for k in ks]}
    agg_inet = [[0, 0] for _ in ks]
    for r in srows:
        key = r["key"]
        if key.startswith("inet:"):
            a = agg_inet[idx[r["k"]]]
            a[0] += r["sent"]
            a[1] += r["recv"]
            continue
        arr = series.setdefault(key, [None] * len(ks))
        arr[idx[r["k"]]] = round(100.0 * (r["sent"] - r["recv"]) / r["sent"], 2) if r["sent"] else None
    series["inet"] = [round(100.0 * (s - rv) / s, 2) if s else None for s, rv in agg_inet]
    return {"hops": path_hops, "targets": [h for h in hops if h["role"] == "target"], "internet": inet,
            "dead": dict(dead) if dead else {}, "verdict": verdict(path_hops, inet, dict(dead) if dead else {}),
            "bucket": bucket, "series": series, "latest_path": latest_path(c, names)}


def mark_rate_limited(chain):
    """Flag hops whose loss doesn't carry on to everything after them: the router is
    rate-limiting replies to probes aimed at it, not dropping the traffic it forwards."""
    for i, h in enumerate(chain):
        after = [x.get("loss") or 0 for x in chain[i + 1:]]
        own = h.get("loss") or 0
        h["limited"] = bool(after) and own >= 0.5 and min(after) < own * 0.5


def latest_path(c, names):
    r = c.execute("SELECT ts, detail FROM events WHERE kind='path' ORDER BY ts DESC LIMIT 1").fetchone()
    if not r:
        return None
    d = json.loads(r["detail"])
    d["ts"] = r["ts"]
    return d


def verdict(path_hops, inet, dead):
    """Where loss to the internet first appears and then persists all the way out."""
    L = inet.get("loss")
    if not inet.get("sent"):
        return {"level": "none", "text": "No data in this range yet."}
    chain = [(h["label"], h["role"], h.get("loss") or 0) for h in path_hops if h.get("sent")]
    chain.append(("the internet hosts", "internet", L))
    msgs = []
    lan, acc, up = (dead.get("lan") or 0), (dead.get("access") or 0), (dead.get("upstream") or 0)
    if lan + acc + up:
        parts = []
        for n, what in ((lan, "your router also unreachable (Wi-Fi/LAN)"),
                        (acc, "router fine, ISP side unreachable (modem/line/ISP access)"),
                        (up, "ISP's first hop fine, internet unreachable (further upstream)")):
            if n:
                parts.append("%s s with %s" % (n, what))
        msgs.append("Blackout seconds (all internet hosts silent): " + "; ".join(parts) + ".")
    if L < 0.3:
        return {"level": "good", "where": None,
                "text": "Packet loss to the internet is %.2f%%, which is negligible. %s" % (L, " ".join(msgs))}
    start = len(chain) - 1
    for i in range(len(chain)):
        if all(x[2] >= 0.5 * L for x in chain[i:]):
            start = i
            break
    label, role, loss = chain[start]
    where = {"router": "local: Wi-Fi or your router itself",
             "isp": "between your router and the ISP: modem, the line, or the ISP's access network",
             "hop": "inside the ISP's / transit network",
             "internet": "only at the far end: beyond the last hop that answers"}.get(role, role)
    text = ("Internet packet loss is %.2f%%. Loss first appears at %s (%.2f%%) and persists to the end of the path, "
            "so the problem is most likely %s. %s") % (L, label, loss, where, " ".join(msgs))
    return {"level": "bad" if L >= 1 else "warn", "where": role, "text": text}


def q_speedtests(c, q):
    frm, to = time_range(q)
    nf, args = net_filter(q)
    rows = c.execute("SELECT ts,net_key,iface,ssid,lid_closed,on_ac,ok,error,dl_mbps,ul_mbps,rpm,"
                     "base_rtt_ms,duration_s,manual FROM speedtests WHERE ts>=? AND ts<=?%s ORDER BY ts" % nf,
                     [frm, to] + args).fetchall()
    return [dict(r) for r in rows]


def q_events(c, q):
    frm, to = time_range(q)
    rows = c.execute("SELECT * FROM events WHERE (ts>=? AND ts<=?) OR (end_ts>=? AND ts<=?) ORDER BY ts",
                     [frm, to, frm, to]).fetchall()
    return [dict(r) for r in rows]


def labels(c):
    return {r["net_key"]: r["label"] for r in c.execute("SELECT * FROM labels")}


def default_label(d):
    if d.get("ssid"):
        return d["ssid"]
    if d.get("net_key") in (None, "none"):
        return "No network"
    kind = {"wifi": "Wi-Fi", "ethernet": "Ethernet", "hotspot": "iPhone hotspot"}.get(
        d.get("iface_type"), d.get("iface_type") or "")
    mac = (d.get("router_mac") or "")[-8:]
    return "%s via %s%s" % (kind, d.get("gateway") or d["net_key"].split(":", 1)[-1],
                            " (router ..%s)" % mac if mac else "")


def q_networks(c, q):
    lab = labels(c)
    rows = c.execute("""SELECT net_key, COUNT(*) n, MIN(ts) first, MAX(ts) last,
        MAX(ssid) ssid, MAX(gateway) gateway, MAX(router_mac) router_mac, MAX(iface_type) iface_type,
        MAX(public_ip) public_ip FROM samples GROUP BY net_key ORDER BY first""").fetchall()
    res = []
    for r in rows:
        d = dict(r)
        d["label"] = lab.get(r["net_key"]) or default_label(d)
        d["custom_label"] = r["net_key"] in lab
        res.append(d)
    return res


def outages_from(rows, interval):
    """Merge consecutive offline / no-network windows into episodes."""
    res, cur = [], None
    for r in rows:
        kind = r["status"] if r["status"] in ("offline", "no-network") else None
        contiguous = cur and r["ts"] - cur["last"] <= interval * 2.5
        if cur and (kind != cur["kind"] or not contiguous):
            res.append(cur)
            cur = None
        if kind:
            if not cur:
                cur = {"kind": kind, "start": r["ts"] - (r["window_s"] or interval), "last": r["ts"],
                       "net_key": r["net_key"], "samples": 0, "lid_closed": r["lid_closed"],
                       "dead_lan_s": 0, "dead_access_s": 0, "dead_upstream_s": 0}
            cur["last"] = r["ts"]
            cur["samples"] += 1
            for k in ("dead_lan_s", "dead_access_s", "dead_upstream_s"):
                cur[k] += r[k] or 0
    if cur:
        res.append(cur)
    for o in res:
        o["end"] = o.pop("last")
        o["duration_s"] = o["end"] - o["start"]
        d = {"lan": o["dead_lan_s"], "access": o["dead_access_s"], "upstream": o["dead_upstream_s"]}
        o["where"] = max(d, key=d.get) if any(d.values()) else None
    return res


OUTAGE_COLS = "ts,window_s,net_key,status,lid_closed,dead_lan_s,dead_access_s,dead_upstream_s"


def q_outages(c, q):
    frm, to = time_range(q)
    nf, args = net_filter(q)
    rows = c.execute("SELECT %s FROM samples WHERE ts>=? AND ts<=?%s ORDER BY ts" % (OUTAGE_COLS, nf),
                     [frm, to] + args).fetchall()
    return outages_from(rows, CFG["interval_s"])


def q_drops(c, q):
    """Windows with blackout seconds but not a full outage: the short drops."""
    frm, to = time_range(q)
    nf, args = net_filter(q)
    rows = c.execute("SELECT ts,window_s,net_key,status,dead_s,dead_lan_s,dead_access_s,dead_upstream_s,"
                     "inet_loss,gw_loss,isp_loss,inet_burst FROM samples WHERE ts>=? AND ts<=? AND dead_s>0 "
                     "AND status!='offline'%s ORDER BY ts DESC LIMIT 500" % nf, [frm, to] + args).fetchall()
    return [dict(r) for r in rows]


def q_summary(c, q):
    frm, to = time_range(q)
    nf, args = net_filter(q)
    interval = CFG["interval_s"]
    rows = c.execute("SELECT %s,inet_rtt,inet_loss,inet_sent,inet_jitter,gw_rtt,gw_loss,gw_sent,isp_loss,isp_sent,"
                     "dead_s,dns_ms,dns_ok,rssi,http_total_ms,on_ac FROM samples WHERE ts>=? AND ts<=?%s ORDER BY ts"
                     % (OUTAGE_COLS, nf), [frm, to] + args).fetchall()
    sts = q_speedtests(c, q)
    groups = {"*": rows}
    for r in rows:
        groups.setdefault(r["net_key"], []).append(r)
    outs = outages_from(rows, interval)

    def wloss(rs, p):
        sent = sum(r[p + "_sent"] or 0 for r in rs if r[p + "_loss"] is not None)
        lost = sum((r[p + "_sent"] or 0) * r[p + "_loss"] / 100.0 for r in rs if r[p + "_loss"] is not None)
        return round(100.0 * lost / sent, 3) if sent else None

    res = {}
    for key, rs in groups.items():
        n = len(rs)
        net = [r for r in rs if r["status"] != "no-network"]
        st = [s for s in sts if s["ok"] and (key == "*" or s["net_key"] == key)]
        oo = [o for o in outs if o["kind"] == "offline" and (key == "*" or o["net_key"] == key)]
        res[key] = {
            "samples": n, "hours": round(sum(r["window_s"] or interval for r in rs) / 3600, 2),
            "first": rs[0]["ts"] if rs else None, "last": rs[-1]["ts"] if rs else None,
            "uptime_pct": round(100.0 * sum(r["status"] != "offline" for r in net) / len(net), 3) if net else None,
            "ok_pct": round(100.0 * sum(r["status"] == "ok" for r in rs) / n, 2) if n else None,
            "degraded_pct": round(100.0 * sum(r["status"] == "degraded" for r in rs) / n, 2) if n else None,
            "inet_rtt_p50": pct([r["inet_rtt"] for r in rs], 50),
            "inet_rtt_p95": pct([r["inet_rtt"] for r in rs], 95),
            "inet_loss": wloss(rs, "inet"), "gw_loss": wloss(rs, "gw"), "isp_loss": wloss(rs, "isp"),
            "inet_jitter_p50": pct([r["inet_jitter"] for r in rs], 50),
            "gw_rtt_p50": pct([r["gw_rtt"] for r in rs], 50),
            "gw_rtt_p95": pct([r["gw_rtt"] for r in rs], 95),
            "dead_s": sum(r["dead_s"] or 0 for r in rs),
            "dead_lan_s": sum(r["dead_lan_s"] or 0 for r in rs),
            "dead_access_s": sum(r["dead_access_s"] or 0 for r in rs),
            "dead_upstream_s": sum(r["dead_upstream_s"] or 0 for r in rs),
            "drop_windows": sum(1 for r in rs if (r["dead_s"] or 0) > 0 and r["status"] != "offline"),
            "dns_p50": pct([r["dns_ms"] for r in rs], 50),
            "dns_fail_pct": round(100.0 * sum(1 for r in net if not r["dns_ok"]) / len(net), 2) if net else None,
            "http_p50": pct([r["http_total_ms"] for r in rs], 50),
            "rssi_p50": pct([r["rssi"] for r in rs], 50),
            "lid_closed_pct": round(100.0 * sum(1 for r in rs if r["lid_closed"]) / n, 1) if n else None,
            "battery_pct": round(100.0 * sum(1 for r in rs if r["on_ac"] == 0) / n, 1) if n else None,
            "outages": len(oo),
            "downtime_s": round(sum(o["duration_s"] for o in oo)),
            "longest_outage_s": round(max([o["duration_s"] for o in oo] or [0])),
            "speedtests": len(st),
            "dl_p50": pct([s["dl_mbps"] for s in st], 50), "dl_min": pct([s["dl_mbps"] for s in st], 0),
            "ul_p50": pct([s["ul_mbps"] for s in st], 50), "rpm_p50": pct([s["rpm"] for s in st], 50),
        }
    return res


HEAT_METRICS = {
    "inet_loss": weighted("inet_loss", "inet_sent"),
    "dead_s": "SUM(dead_s)",
    "isp_loss": weighted("isp_loss", "isp_sent"),
    "gw_loss": weighted("gw_loss", "gw_sent"),
    "inet_rtt": weighted("inet_rtt", "inet_sent"),
    "bad_pct": "AVG(status!='ok')*100",
    "offline_min": "SUM(CASE WHEN status='offline' THEN COALESCE(window_s,0) ELSE 0 END)/60.0",
    "dns_ms": "AVG(dns_ms)",
    "rssi": "AVG(rssi)",
}


def q_heatmap(c, q):
    frm, to = time_range(q)
    nf, args = net_filter(q)
    metric = q.get("metric") or "inet_loss"
    day = "strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime')"
    hour = "CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER)"
    if metric in ("dl_mbps", "ul_mbps"):
        sql = "SELECT %s d, %s h, AVG(%s) v, COUNT(*) n FROM speedtests WHERE ok=1 AND ts>=? AND ts<=?%s GROUP BY d, h" % (
            day, hour, metric, nf)
    else:
        sql = "SELECT %s d, %s h, %s v, COUNT(*) n FROM samples WHERE ts>=? AND ts<=?%s GROUP BY d, h" % (
            day, hour, HEAT_METRICS.get(metric, HEAT_METRICS["inet_loss"]), nf)
    rows = c.execute(sql, [frm, to] + args).fetchall()
    return {"metric": metric,
            "cells": [[r["d"], r["h"], None if r["v"] is None else round(r["v"], 3), r["n"]] for r in rows]}


def q_latest(c, coll):
    s = c.execute("SELECT * FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
    st = c.execute("SELECT * FROM speedtests ORDER BY ts DESC LIMIT 1").fetchone()
    d = {"sample": dict(s) if s else None, "speedtest": dict(st) if st else None, "now": time.time(),
         "config": {k: CFG[k] for k in ("interval_s", "speedtest_interval_min", "internet_targets", "path_dest",
                                        "degraded_loss_pct", "degraded_rtt_ms", "degraded_dead_s")}}
    if s:
        d["label"] = labels(c).get(s["net_key"]) or default_label(dict(s))
    if coll:
        d["collector"] = {"started": coll.started, "speedtest_running": coll.speedtest_running,
                          "next_speedtest": coll.next_speedtest,
                          "targets": [t["key"] for t in coll.pinger.targets]}
    return d


EXPORT_COLS = ["ts", "window_s", "status", "net_key", "ssid", "iface", "iface_type", "gateway", "router_mac",
               "public_ip", "lid_closed", "on_ac", "battery", "rssi", "noise", "tx_rate", "channel", "band",
               "gw_sent", "gw_loss", "gw_rtt", "gw_max", "gw_jitter", "isp_hop", "isp_ip", "isp_sent", "isp_loss",
               "isp_rtt", "inet_sent", "inet_loss", "inet_rtt", "inet_max", "inet_jitter", "inet_burst",
               "dead_s", "dead_lan_s", "dead_access_s", "dead_upstream_s", "dns_ok", "dns_ms", "http_ok",
               "http_total_ms", "http_err", "speedtest_running"]


def export_csv(c, q):
    frm, to = time_range(q)
    nf, args = net_filter(q)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time_local"] + EXPORT_COLS)
    for r in c.execute("SELECT %s FROM samples WHERE ts>=? AND ts<=?%s ORDER BY ts" % (",".join(EXPORT_COLS), nf),
                       [frm, to] + args):
        w.writerow([datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S")] + list(r))
    return buf.getvalue()


# ---------------------------------------------------------------- web

WEB_DIR = os.path.join(APP_DIR, "web")
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}
API = {"/api/series": q_series, "/api/path": q_path, "/api/speedtests": q_speedtests, "/api/events": q_events,
       "/api/networks": q_networks, "/api/summary": q_summary, "/api/outages": q_outages, "/api/drops": q_drops,
       "/api/heatmap": q_heatmap}


def make_handler(coll):
    class H(BaseHTTPRequestHandler):
        server_version = "netscan/1"

        def log_message(self, fmt, *a):
            pass

        def send(self, code, body, ctype="application/json", extra=None):
            if not isinstance(body, bytes):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def js(self, obj, code=200):
            self.send(code, json.dumps(obj, separators=(",", ":")))

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(u.query).items()}
            p = u.path
            try:
                if p.startswith("/api/"):
                    c = connect()
                    try:
                        if p in API:
                            return self.js(API[p](c, q))
                        if p == "/api/latest":
                            return self.js(q_latest(c, coll))
                        if p == "/api/export.csv":
                            name = "netscan-%s.csv" % time.strftime("%Y%m%d-%H%M")
                            return self.send(200, export_csv(c, q), "text/csv; charset=utf-8",
                                             {"Content-Disposition": 'attachment; filename="%s"' % name})
                    finally:
                        c.close()
                    return self.js({"error": "not found"}, 404)
                if p == "/favicon.ico":
                    return self.send(204, b"", "image/x-icon")
                path = "index.html" if p in ("/", "") else p.lstrip("/")
                full = os.path.realpath(os.path.join(WEB_DIR, path))
                if not full.startswith(os.path.realpath(WEB_DIR) + os.sep) or not os.path.isfile(full):
                    return self.send(404, "not found", "text/plain")
                with open(full, "rb") as f:
                    return self.send(200, f.read(), MIME.get(os.path.splitext(full)[1], "application/octet-stream"))
            except BrokenPipeError:
                pass
            except Exception:
                log("GET %s failed: %s" % (self.path, traceback.format_exc()))
                try:
                    self.js({"error": "server error"}, 500)
                except Exception:
                    pass

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self.js({"error": "bad json"}, 400)
            if u.path == "/api/label":
                key, label = body.get("net_key"), (body.get("label") or "").strip()
                if not key:
                    return self.js({"error": "net_key required"}, 400)
                c = connect()
                if label:
                    c.execute("INSERT OR REPLACE INTO labels VALUES (?,?)", (key, label[:80]))
                else:
                    c.execute("DELETE FROM labels WHERE net_key=?", (key,))
                c.commit()
                c.close()
                return self.js({"ok": True})
            if u.path == "/api/speedtest":
                if not coll:
                    return self.js({"error": "collector not running in this process"}, 409)
                if not coll.speedtest_running:
                    coll.speedtest_now.set()
                return self.js({"ok": True})
            return self.js({"error": "not found"}, 404)
    return H


# ---------------------------------------------------------------- CLI

def cmd_run(a):
    init_db()
    coll = Collector()
    port = a.port or CFG["port"]
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(coll))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("netscan: data %s, dashboard http://127.0.0.1:%d/" % (db_path(), port))
    try:
        coll.run()
    except KeyboardInterrupt:
        coll.stop.set()


def cmd_once(a):
    init_db()
    coll = Collector()
    gw, dev = default_route()
    coll.path["busy"] = True
    coll.path["gw"] = gw
    coll._discover(gw)
    coll.pinger.start()
    coll.window_start = time.time()
    time.sleep(a.seconds)
    s = coll.sample()
    print(json.dumps(s, indent=1))


def cmd_path(a):
    hops, dest_ttl = discover_path(a.dest or CFG["path_dest"])
    for ttl in sorted(hops):
        ip = hops[ttl]
        print("%2d  %-16s %s" % (ttl, ip or "*", (reverse_dns(ip) or "") if ip else ""))
    print("%2s  %s" % (dest_ttl or "?", a.dest or CFG["path_dest"]))


def build_helper(dest_dir):
    swiftc = shutil.which("swiftc") or "/usr/bin/swiftc"
    os.makedirs(os.path.join(dest_dir, "bin"), exist_ok=True)
    r = subprocess.run([swiftc, "-O", os.path.join(APP_DIR, "wifiinfo.swift"), "-o",
                        os.path.join(dest_dir, "bin", "wifiinfo")], capture_output=True, text=True)
    if r.returncode != 0:
        print("warning: could not build Wi-Fi helper (falling back to system_profiler):\n" + r.stderr[-800:])


PLIST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>{app}/netscan.py</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key><string>{app}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>{home}/logs/netscan.log</string>
  <key>StandardErrorPath</key><string>{home}/logs/netscan.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>NETSCAN_HOME</key><string>{home}</string>
  </dict>
</dict>
</plist>
"""


def cmd_install(a):
    # The service runs from ~/netscan/app, not from this checkout: launchd jobs
    # reading ~/Documents trip macOS privacy (TCC) prompts/denials.
    app = os.path.join(HOME, "app")
    os.makedirs(os.path.join(HOME, "logs"), exist_ok=True)
    os.makedirs(app, exist_ok=True)
    if os.path.realpath(APP_DIR) != os.path.realpath(app):
        for f in ("netscan.py", "wifiinfo.swift"):
            shutil.copy2(os.path.join(APP_DIR, f), os.path.join(app, f))
        if os.path.exists(os.path.join(app, "web")):
            shutil.rmtree(os.path.join(app, "web"))
        shutil.copytree(os.path.join(APP_DIR, "web"), os.path.join(app, "web"))
    build_helper(app)
    with open(PLIST, "w") as f:
        f.write(PLIST_XML.format(label=LABEL, app=app, home=HOME))
    dom = "gui/%d" % os.getuid()
    subprocess.run(["launchctl", "bootout", "%s/%s" % (dom, LABEL)], capture_output=True)
    time.sleep(1)
    for attempt in range(3):
        r = subprocess.run(["launchctl", "bootstrap", dom, PLIST], capture_output=True, text=True)
        if r.returncode == 0:
            break
        time.sleep(2)
    else:
        print("launchctl bootstrap failed:", r.stderr)
        sys.exit(1)
    print("installed %s -> %s\ndashboard: http://127.0.0.1:%d/\nlogs: %s/logs/" % (LABEL, app, CFG["port"], HOME))


def cmd_uninstall(a):
    subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LABEL)], capture_output=True)
    if os.path.exists(PLIST):
        os.remove(PLIST)
    print("stopped and removed %s; data kept in %s" % (LABEL, os.path.join(HOME, "data")))


def cmd_status(a):
    out = subprocess.run(["launchctl", "print", "gui/%d/%s" % (os.getuid(), LABEL)],
                         capture_output=True, text=True).stdout
    m = re.search(r"state = (\S+)", out)
    print("launchd:", m.group(1) if m else "not loaded")
    if os.path.exists(db_path()):
        c = connect()
        n, last = c.execute("SELECT COUNT(*), MAX(ts) FROM samples").fetchone()
        print("windows: %d, last %s" % (n, datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S") if last else "-"))
    print("dashboard: http://127.0.0.1:%d/" % CFG["port"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--port", type=int)
    o = sub.add_parser("once")
    o.add_argument("--seconds", type=float, default=10)
    p = sub.add_parser("path")
    p.add_argument("dest", nargs="?")
    sub.add_parser("install")
    sub.add_parser("uninstall")
    sub.add_parser("status")
    a = ap.parse_args()
    load_config()
    fn = {"run": cmd_run, "once": cmd_once, "path": cmd_path, "install": cmd_install,
          "uninstall": cmd_uninstall, "status": cmd_status}.get(a.cmd)
    if not fn:
        ap.print_help()
        sys.exit(1)
    fn(a)


if __name__ == "__main__":
    main()
