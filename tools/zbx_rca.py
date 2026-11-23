#!/usr/bin/env python3
"""Root cause analysis for a site outage, from Zabbix history.

  python3 zbx_rca.py --site SITE0000 --from "2026-10-20 14:00" --to "2026-10-20 16:00"
  python3 zbx_rca.py --host SITE0000-BH-01 --from ... --to ... --out report.html

It pulls ping, uptime, wireless, PoE and interface history for every host of
the site (tag site=<code>, or group Sites/<code>), from --pad before to --pad
after the window, then classifies the cause:

  power / reboot   the device's boot time (sample time - uptime) is inside the outage
  connectivity     the device was down but its uptime kept counting
  upstream         the device's parent was down too; the root is the highest failed device
  RF degradation   reachable, but RSSI/SNR fell
  link flap        interface went down/up without a reboot
  monitoring gap   every host is missing data at once (Zabbix server/proxy side)

Only read-only API calls are made.
"""
import argparse
import html
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# --------------------------------------------------------------- settings
POWER_SHARE = 0.6        # share of down hosts that rebooted, to call it a site power outage
BOOT_TOLERANCE = 120     # seconds of slack when comparing boot time with outage start
SAME_EVENT = 600         # outages starting within this many seconds are the same incident
RF_DROP_DB = 6           # RSSI/SNR drop that counts as degradation
PAD_DEFAULT = "30m"


def parse_duration(s):
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return int(float(s[:-1]) * units[s[-1]]) if s[-1] in units else int(s)


def fmt_dur(sec):
    if sec is None:
        return "-"
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s" if m else f"{s}s"


# --------------------------------------------------------------- data model
@dataclass
class Host:
    hostid: str
    host: str
    name: str
    role: str = ""
    model: str = ""
    parent: str = ""
    site: str = ""
    external: bool = False                         # pulled in because it is upstream of the site
    ups: list = field(default_factory=list)        # [(clock, upsOutputSource)]  5 = on battery
    link_uptime: list = field(default_factory=list)  # [(clock, seconds)] wireless link uptime (Wave Pro)
    link_resets: list = field(default_factory=list)  # clocks when the wireless link restarted
    ping: list = field(default_factory=list)       # [(clock, 0|1)]
    loss: list = field(default_factory=list)
    uptime: list = field(default_factory=list)     # [(clock, seconds)] best source
    uptime_src: str = ""
    rssi: list = field(default_factory=list)
    snr: list = field(default_factory=list)
    rf_src: str = ""                               # where the signal readings come from
    stations: list = field(default_factory=list)   # [(clock, n)] ePMP AP connected stations
    sta_rssi: list = field(default_factory=list)   # per-station UL RSSI series seen by an AP
    sta_snr: list = field(default_factory=list)
    window: tuple = ()                             # (w_from, w_till) of the analysis
    poe: dict = field(default_factory=dict)        # port -> [(clock, status)]
    ifstatus: dict = field(default_factory=dict)   # ifname -> [(clock, status)]
    # analysis results
    outages: list = field(default_factory=list)
    reboots: list = field(default_factory=list)    # boot clocks inside the range
    poe_drops: list = field(default_factory=list)  # (clock, port)
    flaps: list = field(default_factory=list)      # (clock, ifname)
    rf: dict = field(default_factory=dict)
    verdict: str = ""
    detail: str = ""


@dataclass
class Outage:
    start: int
    end: int | None              # None = still down at the end of the data
    rebooted: bool | None = None  # None = no uptime after recovery
    boot: int | None = None
    uptime_after: int | None = None
    sample_after: int | None = None

    def overlaps(self, other, slack=SAME_EVENT):
        a_end = self.end if self.end is not None else 1 << 62
        b_end = other.end if other.end is not None else 1 << 62
        return self.start <= b_end + slack and other.start <= a_end + slack


# --------------------------------------------------------------- fetch
ITEM_PREFIXES = ["icmpping", "system.hw.uptime", "system.net.uptime", "system.uptime", "wifi.rssi", "wifi.snr",
                 "wifi.ap.rssi", "wifi.ap.snr", "wifi.sta.ulrssi", "wifi.sta.ulsnr", "wifi.sta.worst", "wifi.stations",
                 "wifi.link.uptime", "poe.port.status", "net.if.status", "ups.output.source"]
RADIO_MODELS = ("epmp", "wave", "force", "cambium")
MAX_UPSTREAM = 15  # how many parent levels to follow outside the site


class RCAError(Exception):
    pass


ARCHIVE_USED = []   # set when the readings came from the archive files instead of the database


HIST_ITEMS = 40          # items per history.get call (API fallback)
HIST_WINDOW = 6 * 3600   # seconds per history.get call (API fallback)


def _hist_db(by_type, t_from, t_till):
    """All series straight from PostgreSQL: no PHP memory/time limit. None if the DB is not reachable."""
    if os.environ.get("RCA_DB", "1") == "0":
        return None
    try:
        from zbxdb import DB
        db = DB()
        out = {}
        for vt, its in by_type.items():
            table = "history" if vt == 0 else "history_uint"
            ids = [str(int(i["itemid"])) for i in its]
            for c in range(0, len(ids), 500):
                for itemid, clock, value in db.rows(
                        f"SELECT itemid, clock, value FROM {table} WHERE itemid IN ({','.join(ids[c:c + 500])}) "
                        f"AND clock BETWEEN {int(t_from)} AND {int(t_till)} ORDER BY itemid, clock"):
                    out.setdefault(itemid, []).append((int(clock), float(value)))
        return out
    except (Exception, SystemExit) as e:
        print(f"RCA: database read failed, using the API in chunks: {e}", file=sys.stderr)
        return None


def _hist_api(api, vt, ids, t0, t1, out):
    try:
        rows = api.call("history.get", {"history": vt, "itemids": ids, "time_from": t0, "time_till": t1,
                                        "sortfield": "clock", "sortorder": "ASC",
                                        "output": ["itemid", "clock", "value"]})
    except Exception:
        if len(ids) > 1:          # too much for PHP: halve the items, then the time
            h = len(ids) // 2
            _hist_api(api, vt, ids[:h], t0, t1, out)
            _hist_api(api, vt, ids[h:], t0, t1, out)
            return
        if t1 - t0 > 900:
            m = (t0 + t1) // 2
            _hist_api(api, vt, ids, t0, m, out)
            _hist_api(api, vt, ids, m + 1, t1, out)
            return
        raise
    for row in rows:
        out.setdefault(row["itemid"], []).append((int(row["clock"]), float(row["value"])))


def history_series(api, by_type, t_from, t_till):
    """{itemid: [(clock, value)]} for the window, sorted by clock."""
    series = _hist_db(by_type, t_from, t_till)
    if series is not None:
        return series
    series = {}
    for vt, its in by_type.items():
        ids = [i["itemid"] for i in its]
        for c in range(0, len(ids), HIST_ITEMS):
            t0 = int(t_from)
            while t0 <= t_till:
                t1 = min(int(t_till), t0 + HIST_WINDOW - 1)
                _hist_api(api, vt, ids[c:c + HIST_ITEMS], t0, t1, series)
                t0 = t1 + 1
    for v in series.values():
        v.sort()
    return series


def fetch(api, site=None, group=None, host=None, t_from=0, t_till=0):
    ARCHIVE_USED.clear()
    params = {"output": ["hostid", "host", "name"], "selectTags": "extend", "monitored_hosts": True}
    if host:
        params["filter"] = {"host": [host]}
    elif site:
        params["tags"] = [{"tag": "site", "value": site, "operator": 1}]
    groups = None
    if group or (site and not host):
        groups = api.call("hostgroup.get", {"output": ["groupid"], "filter": {"name": [group or f"Sites/{site}"]}})
    raw = api.call("host.get", params)
    if not raw and groups:
        raw = api.call("host.get", {**{k: v for k, v in params.items() if k != "tags"},
                                    "groupids": [g["groupid"] for g in groups]})
    if not raw:
        raise RCAError("No hosts found for that site/group/host.")
    hosts = {}

    def add(raw_hosts, external):
        for h in raw_hosts:
            tags = {t["tag"]: t["value"] for t in h["tags"]}
            hosts[h["hostid"]] = Host(h["hostid"], h["host"], h["name"], tags.get("role", ""), tags.get("model", ""),
                                      tags.get("parent", ""), tags.get("site", ""), external)
    add(raw, False)
    # follow parents outside the site (remote radio, remote core ...) so the chain to the core is complete
    for _ in range(MAX_UPSTREAM):
        names = {h.host for h in hosts.values()}
        missing = sorted({h.parent for h in hosts.values() if h.parent and h.parent not in names})
        if not missing:
            break
        found = api.call("host.get", {"output": ["hostid", "host", "name"], "selectTags": "extend",
                                      "monitored_hosts": True, "filter": {"host": missing}})
        if not found:
            break
        add(found, True)

    items = api.call("item.get", {
        "output": ["itemid", "hostid", "key_", "value_type", "name"], "hostids": list(hosts),
        "search": {"key_": ITEM_PREFIXES}, "searchByAny": True, "startSearch": True,
        "filter": {"value_type": [0, 3]}, "selectTags": "extend",
    })
    by_type = {}
    for it in items:
        by_type.setdefault(int(it["value_type"]), []).append(it)
    series = history_series(api, by_type, t_from, t_till)
    if not series:
        # older than the history kept in Zabbix: read the daily archive files (tools/zbx_data.py archive)
        root = os.environ.get("RCA_ARCHIVE") or os.environ.get("ARCHIVE_DIR") or "/var/backups/zabbix"
        if os.path.isdir(os.path.join(root, "archive")):
            from zbx_data import read_archive
            wanted = {(hosts[it["hostid"]].host, it["key_"]): (it["itemid"], int(it["value_type"])) for it in items}
            series = read_archive(root, wanted, t_from, t_till)
            if series:
                ARCHIVE_USED.append(root)

    hw, net = {}, {}
    for it in items:
        h = hosts[it["hostid"]]
        s = series.get(it["itemid"], [])
        k = it["key_"]
        iftag = next((t["value"] for t in it.get("tags", []) if t["tag"] == "interface"), None)
        if k == "icmpping":
            h.ping = [(c, int(v)) for c, v in s]
        elif k == "icmppingloss":
            h.loss = s
        elif k.startswith("system.hw.uptime"):
            hw[h.hostid] = [(c, v) for c, v in s if v > 0]   # 0 = device has no hrSystemUptime
        elif k.startswith(("system.net.uptime", "system.uptime")):
            net[h.hostid] = net.get(h.hostid) or s
        elif k == "wifi.rssi" or (k.startswith("wifi.ap.rssi[") and not h.rssi):
            if s or not h.rssi:
                h.rssi, h.rf_src = s, ("own link" if k == "wifi.rssi" else "link to peer")
        elif k == "wifi.snr" or (k.startswith("wifi.ap.snr[") and not h.snr):
            if s or not h.snr:
                h.snr = s
        elif k == "wifi.sta.worst":
            if s:
                h.sta_rssi.insert(0, s)
        elif k.startswith("wifi.sta.ulrssi["):
            if s:
                h.sta_rssi.append(s)
        elif k.startswith("wifi.sta.ulsnr["):
            if s:
                h.sta_snr.append(s)
        elif k == "wifi.stations":
            h.stations = s
        elif k.startswith("wifi.link.uptime[") and not h.link_uptime:
            h.link_uptime = s
        elif k == "ups.output.source":
            h.ups = [(c, int(v)) for c, v in s]
        elif k.startswith("poe.port.status"):
            h.poe[k[len("poe.port.status["):-1]] = [(c, int(v)) for c, v in s]
        elif k.startswith("net.if.status"):
            h.ifstatus[iftag or k] = [(c, int(v)) for c, v in s]
    for hid, h in hosts.items():
        # an AP has no signal of its own: use the weakest connected station per minute
        if not h.rssi and h.sta_rssi:
            h.rssi, h.rf_src = worst(h.sta_rssi), "weakest station"
        if not h.snr and h.sta_snr:
            h.snr = worst(h.sta_snr)
        if hw.get(hid):
            h.uptime, h.uptime_src = hw[hid], "hrSystemUptime"
        elif net.get(hid):
            h.uptime, h.uptime_src = net[hid], "sysUpTime"

    events = api.call("event.get", {
        "output": ["eventid", "clock", "name", "severity", "r_eventid"], "hostids": list(hosts),
        "source": 0, "object": 0, "value": 1, "time_from": t_from, "time_till": t_till,
        "selectHosts": ["host"], "sortfield": ["clock"], "sortorder": "ASC",
    })
    return list(hosts.values()), events


# --------------------------------------------------------------- analysis
def find_outages(ping, interval_guess=60):
    outs, cur = [], None
    for clock, v in ping:
        if v == 0 and cur is None:
            cur = Outage(clock, None)
        elif v == 1 and cur is not None:
            cur.end = clock
            outs.append(cur)
            cur = None
    if cur:
        outs.append(cur)
    return outs


def find_reboots(uptime):
    """Boot clocks where uptime went backwards (or the series starts fresh)."""
    boots = []
    prev = None
    for clock, up in uptime:
        boot = clock - up
        if prev is not None:
            pclock, pup = prev
            if up + BOOT_TOLERANCE < pup + (clock - pclock):  # uptime did not advance with the clock
                boots.append(int(boot))
        prev = (clock, up)
    return boots


def judge_outage(h, o):
    after = [(c, u) for c, u in h.uptime if c >= (o.end if o.end else o.start)]
    if not after:
        return
    c, u = after[0]
    o.sample_after, o.uptime_after = c, int(u)
    o.boot = int(c - u)
    o.rebooted = o.boot >= o.start - BOOT_TOLERANCE


def worst(series_list):
    """Merge several signal series into the lowest value per minute."""
    by_min = {}
    for s in series_list:
        for c, v in s:
            k = c // 60
            if k not in by_min or v < by_min[k][1]:
                by_min[k] = (k * 60, v)
    return sorted(by_min.values())


def is_radio(h):
    m = (h.model or "").lower()
    return any(x in m for x in RADIO_MODELS) or h.role in ("ap", "sm", "backhaul")


def signal_text(h):
    """The readings cell: before -> during for RSSI and SNR, or why there is nothing to show."""
    if not is_radio(h):
        return "not a radio"
    if not h.rssi and not h.snr:
        return "no signal readings (check SNMP on this radio)"
    parts = []
    for label, unit, series, rf in (("RSSI", "dBm", h.rssi, h.rf.get("rssi")), ("SNR", "dB", h.snr, h.rf.get("snr"))):
        if not series:
            continue
        if rf:
            parts.append(f"{label} {rf['before']} → {rf['during']} {unit}")
        else:
            last = series[-1][1]
            parts.append(f"{label} {round(last, 1)} {unit} (latest; none during the outage)" if h.outages
                         else f"{label} {round(last, 1)} {unit}")
    if h.stations:
        st = rf_change(h.stations, *h.window) if h.window else None
        parts.append(f"stations {int(st['before'])} → {int(st['during'])}" if st else f"stations {int(h.stations[-1][1])}")
    src = f" ({h.rf_src})" if h.rf_src and h.rf_src != "own link" else ""
    return "; ".join(parts) + src


def mean(vals):
    return sum(vals) / len(vals) if vals else None


def rf_change(series, w_from, w_till):
    before = [v for c, v in series if c < w_from]
    during = [v for c, v in series if w_from <= c <= w_till]
    b, d = mean(before), mean(during)
    if b is None or d is None:
        return None
    return {"before": round(b, 1), "during": round(d, 1), "min": round(min(during), 1), "drop": round(b - d, 1)}


def transitions(series, bad):
    out, prev = [], None
    for c, v in series:
        if prev is not None and prev not in bad and v in bad:
            out.append(c)
        prev = v
    return out


def common_gap(hosts, w_from, w_till, min_gap=300):
    """A window where no host has any ping sample = monitoring gap."""
    clocks = sorted(c for h in hosts for c, _ in h.ping if w_from <= c <= w_till)
    if len(clocks) < 2 or not all(h.ping for h in hosts):
        return None
    best = None
    for a, b in zip(clocks, clocks[1:]):
        if b - a >= min_gap and (best is None or b - a > best[1] - best[0]):
            best = (a, b)
    return best


def analyze(hosts, w_from, w_till):
    by_name = {h.host: h for h in hosts}
    for h in hosts:
        h.outages = [o for o in find_outages(h.ping)
                     if o.start <= w_till and (o.end is None or o.end >= w_from)]
        for o in h.outages:
            judge_outage(h, o)
        h.reboots = [b for b in find_reboots(h.uptime) if w_from - BOOT_TOLERANCE <= b <= w_till]
        h.link_resets = [b for b in find_reboots(h.link_uptime)
                         if w_from - BOOT_TOLERANCE <= b <= w_till
                         and not any(abs(b - r) <= BOOT_TOLERANCE for r in h.reboots)]
        for port, s in h.poe.items():
            h.poe_drops += [(c, port) for c in transitions(s, {1, 2, 4, 5, 6}) if w_from <= c <= w_till]
        for ifn, s in h.ifstatus.items():
            h.flaps += [(c, ifn) for c in transitions(s, {2, 7}) if w_from <= c <= w_till]
        h.window = (w_from, w_till)
        if h.rssi:
            h.rf["rssi"] = rf_change(h.rssi, w_from, w_till)
        if h.snr:
            h.rf["snr"] = rf_change(h.snr, w_from, w_till)

    findings = []
    local = [h for h in hosts if not h.external]
    gap = common_gap(local, w_from, w_till)
    if gap and not any(h.outages for h in hosts):
        findings.append({"cause": "Monitoring gap", "root": "Zabbix server / proxy",
                         "start": gap[0], "end": gap[1], "confidence": "medium",
                         "evidence": f"No host at the site returned any ping sample for {fmt_dur(gap[1] - gap[0])}; "
                                     "devices did not report down, so the gap is on the monitoring side.",
                         "affected": [h.host for h in local]})

    on_battery = []
    for h in hosts:
        for start, end in battery_periods(h.ups):
            if start <= w_till and (end is None or end >= w_from):
                on_battery.append((h, start, end))

    # Each incident (outages that overlap or start within SAME_EVENT of each other) is judged on its own,
    # so a window with a link cut and, later, a power cut gives two findings.
    all_outages = {h.host: h.outages for h in hosts}
    batt_all, used_batt = on_battery, []
    history = {h.host: [] for h in hosts}
    for c_from, c_till in incidents(hosts, w_till):
        for h in hosts:
            h.outages = [o for o in all_outages[h.host] if o.start <= c_till and (o.end or w_till) >= c_from]
            h.verdict, h.detail = "", ""
        on_battery = [b for b in batt_all if b not in used_batt
                      and b[1] <= c_till and (b[2] is None or b[2] >= c_from - SAME_EVENT)]
        cl_batt = list(on_battery)
        down = [h for h in local if h.outages]
        judged = [h for h in down if h.outages[0].rebooted is not None]
        rebooted = [h for h in judged if h.outages[0].rebooted]
        if len(judged) >= 2 and len(rebooted) / len(judged) >= POWER_SHARE:
            boots = sorted(h.outages[0].boot for h in rebooted)
            if boots[-1] - boots[0] <= SAME_EVENT:
                start = min(h.outages[0].start for h in down)
                ends = [h.outages[0].end for h in down if h.outages[0].end]
                ups_note = ""
                if on_battery:
                    u, ub, ue = on_battery[0]
                    ups_note = (f" UPS {u.host} switched to battery at {ts(ub)}"
                                + (f" and back to mains at {ts(ue)}" if ue else "")
                                + ": mains failed and the UPS did not carry the load until it returned.")
                findings.append({
                    "cause": "Site power outage", "root": "the site power supply",
                    "start": start, "end": max(ends) if ends else None, "confidence": "high",
                    "evidence": f"{len(rebooted)} of {len(judged)} down devices rebooted, all boot times within "
                                f"{fmt_dur(boots[-1] - boots[0])} of each other." + ups_note,
                    "affected": [h.host for h in down]})
                on_battery = []
                for h in down:
                    h.verdict, h.detail = "Power loss (site)", "Rebooted with the rest of the site"
                down = []  # explained

        # upstream devices from other sites take part in finding the root, not in the site power vote
        down = down + [h for h in hosts if h.external and h.outages]

        def parent_down_overlapping(h, o):
            p = by_name.get(h.parent)
            while p:
                if any(po.overlaps(o) for po in p.outages):
                    return p
                p = by_name.get(p.parent)
            return None

        roots = []
        for h in down:
            o = h.outages[0]
            p = parent_down_overlapping(h, o)
            if p:
                if o.rebooted:
                    h.verdict, h.detail = "Power loss (local)", f"Rebooted during the outage under {p.host}; separate power issue at this device"
                    findings.append(power_finding(h, o, by_name, "medium"))
                else:
                    h.verdict, h.detail = "Upstream", f"Uptime kept counting; lost because the path through {p.host} failed"
            else:
                roots.append(h)

        for h in roots:
            o = h.outages[0]
            downstream = [x.host for x in down if x is not h and is_below(x, h, by_name)]
            if o.rebooted is None:
                h.verdict, h.detail = "Unknown", "No uptime sample after recovery (SNMP not answering?)"
                findings.append({"cause": "Outage, cause not determined", "root": h.host, "start": o.start, "end": o.end,
                                 "confidence": "low", "evidence": h.detail, "affected": [h.host] + downstream})
            elif o.rebooted:
                findings.append(power_finding(h, o, by_name, "high", downstream))
            else:
                p = by_name.get(h.parent)
                if p:
                    link = f"link between {p.host} and {h.host}"
                    h.verdict = "Connectivity"
                    h.detail = f"Uptime kept counting ({fmt_dur(o.uptime_after)} at recovery) while {p.host} stayed reachable"
                    cause, root = "Connectivity fault", link
                else:
                    h.verdict = "Upstream (outside site)"
                    h.detail = f"Site core did not reboot ({fmt_dur(o.uptime_after)} uptime at recovery); fault is beyond it"
                    cause, root = "Upstream failure beyond the site", f"uplink of {h.host}"
                findings.append({"cause": cause, "root": root, "start": o.start, "end": o.end,
                                 "confidence": confidence(o),
                                 "evidence": f"{h.host} was unreachable for {fmt_dur((o.end or w_till) - o.start)} "
                                             f"but its uptime did not reset (boot time {ts(o.boot)} is before the outage). "
                                             f"The device stayed powered; the path to it failed."
                                             + (f" {len(downstream)} device(s) behind it went down with it." if downstream else ""),
                                 "affected": [h.host] + downstream})

        if cl_batt and not on_battery:
            used_batt += cl_batt
        for h in hosts:
            if h.outages and h.verdict:
                history[h.host].append((h.outages[0].start, h.verdict, h.detail))
    for h in hosts:
        h.outages = all_outages[h.host]
        hist = history[h.host]
        if len(hist) == 1:
            h.verdict, h.detail = hist[0][1], hist[0][2]
        elif hist:
            h.verdict = " / ".join(dict.fromkeys(v for _, v, _ in hist))
            h.detail = "; ".join(f"{ts(c)[11:16]} {v}: {d}" for c, v, d in hist)
        else:
            h.verdict, h.detail = "", ""
    on_battery = [b for b in batt_all if b not in used_batt]

    for u, ub, ue in on_battery:
        dur = fmt_dur((ue or w_till) - ub)
        findings.append({"cause": "Mains power failure (UPS on battery)", "root": f"mains power at {u.site or u.host}",
                         "start": ub, "end": ue, "confidence": "high",
                         "evidence": f"UPS {u.host} ran on battery for {dur} from {ts(ub)}. "
                                     + ("Devices behind it did not reboot, so the UPS held the load."
                                        if not any(o.rebooted for h in local for o in h.outages) else
                                        "Some devices still rebooted: check which ones are not on this UPS."),
                         "affected": [u.host]})

    for h in local:
        if h.outages:
            continue
        if h.reboots:
            h.verdict, h.detail = "Reboot", f"Uptime reset at {ts(h.reboots[0])} without a visible ping outage"
            findings.append({"cause": "Short reboot", "root": h.host, "start": h.reboots[0], "end": None,
                             "confidence": "medium", "evidence": h.detail, "affected": [h.host]})
            continue
        if h.link_resets:
            h.verdict = "Wireless link drop"
            h.detail = f"Link restarted at {ts(h.link_resets[0])} while the radio kept running"
            findings.append({"cause": "Wireless link drop", "root": h.host, "start": h.link_resets[0], "end": None,
                             "confidence": "high",
                             "evidence": h.detail + ": the radio link dropped and re-formed; power was not lost.",
                             "affected": [h.host]})
            continue
        degraded = {k: v for k, v in h.rf.items() if v and v["drop"] >= RF_DROP_DB}
        if degraded:
            h.verdict = "RF degradation"
            h.detail = "; ".join(f"{k.upper()} {v['before']} → {v['during']} (min {v['min']})" for k, v in degraded.items())
            findings.append({"cause": "RF degradation", "root": h.host, "start": w_from, "end": w_till,
                             "confidence": "medium", "evidence": h.detail + ". Device stayed reachable.",
                             "affected": [h.host]})
            continue
        if h.flaps and h.role in ("backhaul", "ptp", "aggregation"):
            h.verdict = "Link flap"
            h.detail = ", ".join(f"{i} at {ts(c)}" for c, i in h.flaps[:5])
            findings.append({"cause": "Link flap", "root": h.host, "start": h.flaps[0][0], "end": None,
                             "confidence": "medium", "evidence": "Interface went down without a reboot: " + h.detail,
                             "affected": [h.host]})
            continue
        h.verdict = h.verdict or "OK"

    findings.sort(key=lambda f: (-len(f["affected"]), f["start"] or 0))
    return findings


def incidents(hosts, w_till):
    """Merge every host's outages into incident windows [(start, end)]."""
    spans = sorted((o.start, o.end or w_till) for h in hosts for o in h.outages)
    out = []
    for st, en in spans:
        if out and st <= out[-1][1] + SAME_EVENT:
            out[-1][1] = max(out[-1][1], en)
        else:
            out.append([st, en])
    return [tuple(x) for x in out]


def battery_periods(series):
    out, start = [], None
    for c, v in series:
        if v == 5 and start is None:
            start = c
        elif v != 5 and start is not None:
            out.append((start, c))
            start = None
    if start is not None:
        out.append((start, None))
    return out


def is_below(x, root, by_name):
    p = by_name.get(x.parent)
    while p:
        if p is root:
            return True
        p = by_name.get(p.parent)
    return False


def confidence(o):
    if o.sample_after is None:
        return "low"
    margin = o.start - o.boot if not o.rebooted else o.boot - o.start
    late = o.sample_after - (o.end or o.start)
    return "high" if late <= 300 and abs(margin) > 2 * BOOT_TOLERANCE else "medium"


def power_finding(h, o, by_name, conf, downstream=()):
    p = by_name.get(h.parent)
    poe = [(c, port) for c, port in (p.poe_drops if p else []) if abs(c - o.start) <= SAME_EVENT]
    if poe:
        cause = "PoE / cable fault"
        ev = f"{h.host} rebooted (boot {ts(o.boot)}) and {p.host} PoE port {poe[0][1]} stopped delivering power at {ts(poe[0][0])}."
        h.verdict = "PoE / cable"
    else:
        cause = "Device power loss or reboot"
        ev = (f"{h.host} rebooted during the outage: boot time {ts(o.boot)} is after the outage began at {ts(o.start)}. "
              f"Only this device{' and those behind it' if downstream else ''} lost power, so look at its power supply, PoE injector or a crash.")
        h.verdict = h.verdict or "Power loss / reboot"
    h.detail = h.detail or ev
    return {"cause": cause, "root": h.host, "start": o.start, "end": o.end, "confidence": conf, "evidence": ev,
            "affected": [h.host] + list(downstream)}


# --------------------------------------------------------------- report
TZ = None


def ts(clock):
    if clock is None:
        return "-"
    return datetime.fromtimestamp(clock, TZ).strftime("%Y-%m-%d %H:%M:%S")


def sparkline(series, t0, t1, w=260, hgt=34, kind="line", w_from=None, w_till=None):
    if not series:
        return '<span class="muted">no data</span>'
    span = max(t1 - t0, 1)
    x = lambda c: (c - t0) / span * w
    parts = [f'<svg width="{w}" height="{hgt}" viewBox="0 0 {w} {hgt}" class="spark">']
    if w_from is not None:
        parts.append(f'<rect x="{x(w_from):.1f}" y="0" width="{max(x(w_till) - x(w_from), 1):.1f}" height="{hgt}" class="win"/>')
    if kind == "ping":
        for c, v in series:
            parts.append(f'<rect x="{x(c):.1f}" y="{4 if v == 0 else hgt - 8}" width="2" height="{hgt - 8 if v == 0 else 4}" '
                         f'class="{"down" if v == 0 else "up"}"/>')
    else:
        vals = [v for _, v in series]
        lo, hi = min(vals), max(vals)
        rng = (hi - lo) or 1
        pts = " ".join(f"{x(c):.1f},{hgt - 3 - (v - lo) / rng * (hgt - 6):.1f}" for c, v in series)
        parts.append(f'<polyline points="{pts}" class="ln"/>')
    parts.append("</svg>")
    return "".join(parts)


CSS = """
:root{--pow:#b54708;--pow-bg:#fef0c7;--lnk:#c01048;--lnk-bg:#fde7ee;--ok-bg:#dcfae6;--bg:#fff;--fg:#1d2433;--muted:#667085;--line:#e4e7ec;--card:#f8f9fb;--accent:#2563eb;--bad:#d92d20;--ok:#12b76a;--win:rgba(37,99,235,.08)}
@media (prefers-color-scheme:dark){:root{--pow:#f79009;--pow-bg:#3a2a10;--lnk:#f63d68;--lnk-bg:#3a1520;--ok-bg:#0f2e1e;--bg:#111418;--fg:#e6e8eb;--muted:#98a2b3;--line:#2a2f37;--card:#181c22;--accent:#6ea8ff;--bad:#f97066;--ok:#32d583;--win:rgba(110,168,255,.12)}}
body{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;padding:24px 16px}
main{max-width:1200px;margin:auto}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}
.muted{color:var(--muted)}.verdict{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);padding:14px 16px;border-radius:8px;margin:16px 0}
.verdict b{font-size:17px}.finding{border:1px solid var(--line);border-radius:8px;padding:10px 14px;margin:8px 0}
.conf{font-size:12px;padding:1px 8px;border-radius:10px;border:1px solid var(--line);margin-left:6px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600}.wrap{overflow-x:auto}.spark .down{fill:var(--bad)}.spark .up{fill:var(--ok)}
.spark .ln{fill:none;stroke:var(--accent);stroke-width:1.5}.spark .win{fill:var(--win)}
.rhead{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;border-bottom:2px solid var(--fg);padding-bottom:10px;flex-wrap:wrap}
.kicker{text-transform:uppercase;letter-spacing:.08em;font-size:12px;color:var(--muted)}.rhead h1{font-size:28px;margin:2px 0}
.exec{margin:20px 0 8px}.kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}
.kpis div{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpis b{display:block;font-size:26px;line-height:1.2}.kpis span{color:var(--muted);font-size:13px}
.incident{border:1px solid var(--line);border-radius:10px;margin:12px 0;overflow:hidden;break-inside:avoid}
.status{display:flex;align-items:center;gap:12px;padding:12px 16px;flex-wrap:wrap}.status b{font-size:17px}
.status em{margin-left:auto;font-style:normal;color:var(--muted);font-size:13px}
.status span{font-size:12px;font-weight:700;letter-spacing:.06em;padding:3px 10px;border-radius:999px;color:#fff;background:var(--muted)}
.status.power{background:var(--pow-bg)}.status.power span{background:var(--pow)}
.status.link{background:var(--lnk-bg)}.status.link span{background:var(--lnk)}
.status.ok{background:var(--ok-bg)}.status.ok span{background:var(--ok)}.status.gap span{background:var(--muted)}
.incident dl{display:grid;grid-template-columns:150px 1fr;gap:8px 16px;margin:0;padding:14px 16px}
.incident dt{color:var(--muted);font-weight:600}.incident dd{margin:0}
h2.tech{margin-top:40px;padding-top:14px;border-top:1px solid var(--line)}
@media (max-width:600px){.incident dl{grid-template-columns:1fr}.kpis b{font-size:20px}}
@media print{h2.tech{break-before:page}.wrap{overflow:visible}}
"""


# cause -> (category, plain-language explanation, recommended action). {root} is filled in.
PLAIN = {
    "Site power outage": ("power", "The whole site lost electrical power. Every device switched off and restarted "
                          "when power came back.",
                          "Check the site's mains supply and the UPS. The UPS did not keep the equipment running: "
                          "check its battery health and runtime."),
    "Mains power failure (UPS on battery)": ("ok", "Mains power failed, but the UPS kept the equipment running. "
                                             "There was no service interruption.",
                                             "Find out why mains power failed, and check that the UPS battery recharged."),
    "Device power loss or reboot": ("power", "One device ({root}) lost power or restarted. The rest of the site kept running.",
                                    "Check that device's power supply, PoE injector and power cable. "
                                    "If it happens again, replace the power supply or the device."),
    "PoE / cable fault": ("power", "The switch port that powers {root} stopped supplying power (PoE), so the device "
                          "switched off.", "Check the network cable and the PoE port on the switch feeding {root}."),
    "Short reboot": ("power", "{root} restarted briefly without a noticeable outage.",
                     "No action unless it repeats. If it does, check its power supply."),
    "Connectivity fault": ("link", "The equipment kept its power, but the connection failed on the {root}. "
                           "Everything behind that link was cut off.",
                           "Check the radio link: alignment, obstructions (trees, buildings), interference, "
                           "and the cables and connectors at both ends."),
    "Upstream failure beyond the site": ("link", "The site's equipment kept its power, but the network feeding the "
                                         "site failed ({root}).",
                                         "The fault is outside this site: check the upstream site or the core network."),
    "Wireless link drop": ("link", "The wireless link on {root} dropped and reconnected on its own. Power was not lost.",
                           "Check signal level and interference on that link. Repeated drops point to alignment "
                           "or interference problems."),
    "RF degradation": ("link", "The radio signal on {root} became weaker, but the link stayed up.",
                       "Check alignment, obstructions and interference before it turns into an outage."),
    "Link flap": ("link", "The cable connection on {root} went down and up again without a restart.",
                  "Check the cable, connector or SFP module on that port."),
    "Monitoring gap": ("gap", "No network fault was found: the monitoring server stopped collecting data for a while.",
                       "Check the Zabbix server. The network itself was not affected."),
    "Outage, cause not determined": ("gap", "{root} was unreachable, but it did not report enough data afterwards "
                                     "to tell why.", "Check that SNMP monitoring works on that device."),
}
CATEGORY = {"power": "POWER", "link": "CONNECTIVITY", "ok": "NO OUTAGE", "gap": "CHECK"}


def impact(names, by_name):
    """'3 radios, 1 switch, 12 endpoint devices' for the affected hosts."""
    kinds = {"backhaul": "radio link", "ptp": "radio link", "ap": "access point", "sm": "endpoint radio",
             "aggregation": "switch", "access-switch": "switch", "ups": "UPS"}
    c = {}
    for n in names:
        h = by_name.get(n)
        k = "endpoint power unit" if h and h.model == "power units" else kinds.get(h.role if h else "", "device")
        c[k] = c.get(k, 0) + 1
    return ", ".join(f"{v} {k}{'s' if v > 1 else ''}" for k, v in sorted(c.items(), key=lambda x: -x[1]))


def summary(site_label, hosts, findings, w_till):
    """The management part of the report: one card per incident, in plain language."""
    e = html.escape
    by_name = {h.host: h for h in hosts}
    major = [f for f in findings if f["cause"] not in ("RF degradation", "Short reboot", "Link flap")] or findings
    if not findings:
        return ("<section class='exec'><div class='status ok'><span>ALL GOOD</span>"
                "<b>No outage found in this period</b></div><p>Every device at the site stayed reachable and "
                "none restarted.</p></section>")
    incidents = sorted(major, key=lambda f: f["start"] or 0)
    downtime = sum(((f["end"] or w_till) - f["start"]) for f in incidents if f["cause"] not in PLAIN or
                   PLAIN[f["cause"]][0] in ("power", "link"))
    out = ["<section class='exec'>",
           f"<div class='kpis'><div><b>{len(incidents)}</b><span>incident{'s' if len(incidents) != 1 else ''}</span></div>"
           f"<div><b>{fmt_dur(downtime) if downtime else '0'}</b><span>total downtime</span></div>"
           f"<div><b>{len({a for f in incidents for a in f['affected']})}</b><span>devices affected</span></div></div>"]
    for i, f in enumerate(incidents, 1):
        cat, text, action = PLAIN.get(f["cause"], ("gap", f["evidence"], "Review the technical details below."))
        tag = CATEGORY[cat]
        root = f["root"].replace("the site power supply", f"the {site_label} power supply")
        if f["cause"] == "Site power outage" and not any(h.role == "ups" and not h.external for h in hosts):
            action = "Check the site's mains supply. This site has no monitored UPS: consider adding one."
        dur = fmt_dur((f["end"] or w_till) - f["start"]) if f["start"] else "-"
        end = ts(f["end"])[11:16] if f["end"] else "still ongoing at the end of the report"
        effect = ("No service interruption" if cat == "ok" or f["cause"] == "RF degradation" else
                  f"{impact(f['affected'], by_name)} out of service")
        out.append(
            f"<div class='incident {cat}'><div class='status {cat}'><span>{tag}</span><b>{e(f['cause'])}</b>"
            f"<em>{e(f['confidence'])} confidence</em></div>"
            f"<dl><dt>When</dt><dd>{ts(f['start'])[:16]} to {e(end)} ({dur})</dd>"
            f"<dt>What happened</dt><dd>{e(text.format(root=root))}</dd>"
            f"<dt>Impact</dt><dd>{e(effect)}</dd>"
            f"<dt>Where it started</dt><dd>{e(root)}</dd>"
            f"<dt>What to do</dt><dd>{e(action.format(root=root))}</dd></dl></div>")
    out.append("</section>")
    return "".join(out)


def render(site_label, hosts, events, findings, w_from, w_till, t0, t1):
    e = html.escape
    top = findings[0] if findings else None
    out = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>RCA {e(site_label)}</title><style>{CSS}</style></head><body><main>",
           f"<header class='rhead'><div><div class='kicker'>Root cause analysis</div><h1>Site {e(site_label)}</h1>"
           f"<div class='muted'>{ts(w_from)[:16]} to {ts(w_till)[:16]} ({TZ})</div></div>"
           f"<div class='muted gen'>Generated {ts(int(datetime.now().timestamp()))[:16]}</div></header>",
           summary(site_label, hosts, findings, w_till),
           "<h2 class='tech'>Technical details</h2>",
           f"<div class='muted'>Data read from {ts(t0)} to {ts(t1)}. The first finding is the main cause.</div>"]
    if top:
        out.append(f"<div class='verdict'><b>{e(top['cause'])}</b> at <b>{e(top['root'])}</b>"
                   f"<span class='conf'>{e(top['confidence'])} confidence</span><br>"
                   f"{e(top['evidence'])}<br><span class='muted'>Started {ts(top['start'])}, "
                   f"ended {ts(top['end'])}. {len(top['affected'])} device(s) affected.</span></div>")
    else:
        out.append("<div class='verdict'><b>No outage, reboot or degradation found in this window.</b></div>")
    if len(findings) > 1:
        out.append("<h2>Other findings</h2>")
        for f in findings[1:]:
            out.append(f"<div class='finding'><b>{e(f['cause'])}</b>: {e(f['root'])}<span class='conf'>{e(f['confidence'])}</span>"
                       f"<br>{e(f['evidence'])}<br><span class='muted'>{ts(f['start'])}; affected: {e(short_list(f['affected']))}</span></div>")

    out.append("<h2>Timeline</h2><div class='wrap'><table><tr><th>Time</th><th>Device</th><th>Event</th></tr>")
    tl = []
    for h in hosts:
        for o in h.outages:
            tl.append((o.start, h.host, "Unreachable"))
            if o.end:
                tl.append((o.end, h.host, f"Reachable again after {fmt_dur(o.end - o.start)}"))
            if o.boot and o.rebooted:
                tl.append((o.boot, h.host, "Booted (from uptime)"))
        for b in h.reboots:
            if not any(o.boot == b for o in h.outages):
                tl.append((b, h.host, "Booted (uptime reset)"))
        for c, port in h.poe_drops:
            tl.append((c, h.host, f"PoE port {port} stopped delivering power"))
        for c, i in h.flaps:
            tl.append((c, h.host, f"Interface {i} down"))
    for ev in events:
        tl.append((int(ev["clock"]), ev["hosts"][0]["host"] if ev.get("hosts") else "", "Problem: " + ev["name"]))
    order = {n: i for i, n in enumerate(topo_order(hosts))}
    grouped = {}   # the same event at the same minute on many devices is one row
    for c, hn, what in sorted(tl, key=lambda x: (x[0], order.get(x[1], 999), x[2])):
        grouped.setdefault((c // 60, what), []).append((c, hn))
    for (_, what), devs in sorted(grouped.items(), key=lambda kv: (kv[1][0][0], order.get(kv[1][0][1], 999))):
        names = [hn for _, hn in devs]
        who = e(names[0]) if len(names) == 1 else (
            f"<details><summary>{e(names[0])} and {len(names) - 1} more</summary>"
            f"<span class='muted'>{e(', '.join(names[1:]))}</span></details>")
        out.append(f"<tr><td>{ts(devs[0][0])}</td><td>{who}</td><td>{e(what)}</td></tr>")
    out.append("</table></div>")

    out.append("<h2>Device readings</h2><div class='wrap'><table><tr><th>Device</th><th>Role / parent</th>"
               "<th>Verdict</th><th>Down from → to</th><th>Uptime at recovery</th><th>Boot time</th>"
               "<th>Signal before → during</th><th>Ping</th><th>Uptime</th></tr>")
    for h in sorted(hosts, key=lambda h: order.get(h.host, 0)):
        o = h.outages[0] if h.outages else None
        rf = signal_text(h)
        out.append(
            f"<tr><td><b>{e(h.host)}</b><br><span class='muted'>{e(h.model)}</span></td>"
            f"<td>{e(h.role)}<br><span class='muted'>{e(h.parent or 'site core')}</span></td>"
            f"<td><b>{e(h.verdict)}</b><br><span class='muted'>{e(h.detail)}</span></td>"
            f"<td>{(ts(o.start) + ' → ' + ts(o.end)) if o else '-'}</td>"
            f"<td>{fmt_dur(o.uptime_after) if o else '-'}</td>"
            f"<td>{ts(o.boot) if o else '-'}<br><span class='muted'>{e(h.uptime_src)}</span></td>"
            f"<td>{e(rf)}</td>"
            f"<td>{sparkline(h.ping, t0, t1, kind='ping', w_from=w_from, w_till=w_till)}</td>"
            f"<td>{sparkline(h.uptime, t0, t1, w_from=w_from, w_till=w_till)}</td></tr>")
    out.append("</table></div><p class='muted'>Shaded area on the charts = incident window. "
               "Boot time = sample time minus uptime; a boot time inside the outage means the device lost power "
               "or rebooted, an earlier boot time means it stayed powered and the path to it failed."
               + (" Readings for this window came from the history archive (older than the history kept in "
                  "Zabbix)." if ARCHIVE_USED else "") + "</p></main></body></html>")
    return "\n".join(out)


def short_list(names, n=8):
    return ", ".join(names[:n]) + (f" and {len(names) - n} more" if len(names) > n else "")


def topo_order(hosts):
    kids = {}
    names = {h.host for h in hosts}
    for h in hosts:
        kids.setdefault(h.parent if h.parent in names else "", []).append(h.host)
    out = []

    def walk(n):
        for c in sorted(kids.get(n, [])):
            out.append(c)
            walk(c)
    walk("")
    return out


# --------------------------------------------------------------- main
def generate(api, site=None, group=None, host=None, w_from=0, w_till=0, pad=PAD_DEFAULT, tz="UTC"):
    """Build one report. Returns (label, html, findings). Used by main() and rca_server.py."""
    global TZ
    TZ = ZoneInfo(tz) if tz else datetime.now().astimezone().tzinfo
    if w_till <= w_from:
        raise RCAError("The end time must be after the start time.")
    pad = parse_duration(pad)
    t0, t1 = w_from - pad, w_till + pad
    hosts, events = fetch(api, site, group, host, t0, t1)
    findings = analyze(hosts, w_from, w_till)
    label = site or group or host
    return label, visible_names(render(label, hosts, events, findings, w_from, w_till, t0, t1), hosts), findings


def visible_names(doc, hosts):
    """Swap technical host names for the renamed visible names (only where the name is a real rename)."""
    import re
    names = {h.host: h.name for h in hosts if h.name and h.host not in h.name}
    if not names:
        return doc
    rx = re.compile(r"(?<![\w-])(" + "|".join(re.escape(k) for k in sorted(names, key=len, reverse=True))
                    + r")(?![\w-])")
    return rx.sub(lambda m: html.escape(names[m.group(1)]), doc)


def parse_local(s, tz):
    """'YYYY-MM-DD HH:MM' (or the HTML 'YYYY-MM-DDTHH:MM') in tz -> epoch seconds."""
    s = s.strip().replace("T", " ")[:16]
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(tz)).timestamp())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--site", help="site code, e.g. SITE0000 (tag site= or group Sites/<code>)")
    g.add_argument("--group", help="host group name")
    g.add_argument("--host", help="single host (its parents are not included)")
    ap.add_argument("--from", dest="t_from", required=True, help='"YYYY-MM-DD HH:MM" local time')
    ap.add_argument("--to", dest="t_to", required=True)
    ap.add_argument("--tz", default=os.environ.get("RCA_TZ", "UTC"), help="time zone (default UTC)")
    ap.add_argument("--pad", default=PAD_DEFAULT, help="extra data before/after the window (default 30m)")
    ap.add_argument("--out", help="HTML report path (default rca_<site>_<date>.html)")
    ap.add_argument("--json", help="also write findings as JSON")
    args = ap.parse_args()

    w_from, w_till = parse_local(args.t_from, args.tz), parse_local(args.t_to, args.tz)
    from zbxapi import ZabbixAPI, ZabbixError
    api = ZabbixAPI()
    try:
        label, page, findings = generate(api, args.site, args.group, args.host, w_from, w_till, args.pad, args.tz)
    except ZabbixError as e:
        sys.exit(f"Zabbix API error: {e}")
    except RCAError as e:
        sys.exit(str(e))

    path = args.out or f"rca_{label.replace('/', '_')}_{datetime.fromtimestamp(w_from, TZ):%Y%m%d_%H%M}.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"site": label, "window": [ts(w_from), ts(w_till)], "findings": [
                {**x, "start": ts(x["start"]), "end": ts(x["end"])} for x in findings]}, f, indent=2)
    if findings:
        top = findings[0]
        print(f"{top['cause']} at {top['root']} ({top['confidence']} confidence)\n  {top['evidence']}")
    else:
        print("No outage, reboot or degradation found in this window.")
    print(f"Report: {path}")


if __name__ == "__main__":
    main()
