#!/usr/bin/env python3
"""Radio signal check (MDP-SIGNAL): signal (RSSI) and SNR of every site radio at a busy hour, compared
with the day before (daily) or the week before (weekly).

  python3 tools/zbx_signal.py daily  [--date YYYY-MM-DD]   snapshot of that day (default: today if the snapshot
                                                           time has passed, else yesterday) vs the day before
  python3 tools/zbx_signal.py weekly [--date YYYY-MM-DD]   the week containing that day (default last full week)
                                                           vs the week before: average of the daily snapshots
  python3 tools/zbx_signal.py scheduled                    build what is due and missing (run every hour by cron)
  python3 tools/zbx_signal.py cron [--apply]               install the hourly schedule
  python3 tools/zbx_signal.py list

The snapshot is the average of the readings Zabbix already took between SIGNAL_TIME and SIGNAL_TIME +
SIGNAL_WINDOW minutes (no extra polling). Older than the raw history kept: the hourly trend average of that hour.
Endpoint devices are left out (SIGNAL_EXCLUDE). Radios on LACP links cannot be reached and are listed apart.

Settings (environment, ~/.zabbix_env or /etc/mdatapulse/mdatapulse.conf):
  SIGNAL_TIME=15:00          busy hour of the snapshot (RCA_TZ)
  SIGNAL_WINDOW=10           minutes averaged from SIGNAL_TIME
  SIGNAL_RSSI_WATCH=-65      dBm: signal below this = WATCH
  SIGNAL_RSSI_BAD=-70        dBm: signal below this = BAD
  SIGNAL_SNR_WATCH=20        dB:  SNR below this = WATCH
  SIGNAL_SNR_BAD=15          dB:  SNR below this = BAD
  SIGNAL_DROP=5              dB lost against the previous snapshot = WATCH
  SIGNAL_SAME=2              dB: changes smaller than this count as unchanged
  SIGNAL_DROPS_WATCH=3       link drops in the period = WATCH
  SIGNAL_EXCLUDE=^EP-      host names left out
  SIGNAL_EXCLUDE_ROLES=ap    role tags left out (sectors / access points)
  SIGNAL_EXCLUDE_NAMES=_S[0-9]+$ visible names left out (sectors SITE01_S1, SITE01_S2 ...)
  SIGNAL_LACP=...            host names of radios on LACP links (cannot be reached)
  SIGNAL_LACP_NOTE=...       the LACP links named in the report
"""
import argparse
import html
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zbx_report as zr  # noqa: E402  (REPORT_DIR, TZ, week start, CSS and page layout of the health reports)


def _f(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return float(default)


SNAP_TIME = os.environ.get("SIGNAL_TIME") or "15:00"
WINDOW = int(_f("SIGNAL_WINDOW", 10))
RSSI_WATCH, RSSI_BAD = _f("SIGNAL_RSSI_WATCH", -65), _f("SIGNAL_RSSI_BAD", -70)
SNR_WATCH, SNR_BAD = _f("SIGNAL_SNR_WATCH", 20), _f("SIGNAL_SNR_BAD", 15)
DROP = _f("SIGNAL_DROP", 5)
SAME = _f("SIGNAL_SAME", 2)
DROPS_WATCH = int(_f("SIGNAL_DROPS_WATCH", 3))
EXCLUDE = re.compile(os.environ.get("SIGNAL_EXCLUDE") or r"^EP-")
SKIP_ROLES = {r.strip() for r in (os.environ.get("SIGNAL_EXCLUDE_ROLES", "ap")).split(",") if r.strip()}
SKIP_NAMES = re.compile(os.environ.get("SIGNAL_EXCLUDE_NAMES", r"_S\d+$") or r"(?!)", re.I)
LACP = re.compile(os.environ.get("SIGNAL_LACP") or r"(?!)")   # host names of radios on LACP links, e.g. ^(SITE0000-WAVE-(1|2))$
LACP_NOTE = os.environ.get("SIGNAL_LACP_NOTE") or ""
SNAP_V = 1
KINDS = ("daily", "weekly")
DIRS = {"daily": "signal-daily", "weekly": "signal-weekly"}
KEYS = ["wifi.rssi", "wifi.snr", "wifi.sta.worst", "wifi.stations", "wifi.link.ssid", "wifi.dlmcs", "wifi.ulmcs",
        "wifi.ap.rssi[", "wifi.ap.snr[", "wifi.remote.rssi[", "wifi.remote.snr[", "wifi.capacity.rx[",
        "wifi.capacity.tx["]
LINK_EVENTS = ("Link to", "Radio not linked", "AP lost all its stations")
DOWN_EVENT = "Device unreachable"


def log(msg):
    zr.log("signal: " + msg)


def hm():
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", SNAP_TIME.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise SystemExit(f"SIGNAL_TIME={SNAP_TIME} is not HH:MM")
    return int(m.group(1)), int(m.group(2))


def snap_at(d):
    """Epoch of the snapshot on day d (local time)."""
    h, m = hm()
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=zr.TZ).timestamp())


def ready(d):
    return snap_at(d) + WINDOW * 60 + 120 <= time.time()


def default_day():
    t = zr.today()
    return t if ready(t) else t - timedelta(days=1)


# --------------------------------------------------------------- fetch
def fetch_hosts(api):
    """Monitored radios of the sites: {host: {...}}, items [{itemid, hostid, key_, name, value_type, lastvalue}]."""
    raw = api.call("host.get", {"output": ["hostid", "host", "name"], "selectTags": "extend",
                                "selectInterfaces": ["ip", "main", "type"], "monitored_hosts": True})
    hosts = {}
    for h in raw:
        tags = {t["tag"]: t["value"] for t in h["tags"]}
        if not tags.get("site") or EXCLUDE.search(h["host"]) or zr.EXCLUDE.search(h["host"]):
            continue
        if tags.get("role", "") in SKIP_ROLES or SKIP_NAMES.search(h["name"]):
            continue   # sectors (access points): left out of the signal check
        ips = sorted(h.get("interfaces") or [], key=lambda i: (str(i.get("main")) != "1", str(i.get("type")) != "2"))
        hosts[h["hostid"]] = {"host": h["host"], "name": h["name"], "site": tags["site"],
                              "ip": ips[0]["ip"] if ips else "", "parent": tags.get("parent", ""),
                              "role": tags.get("role", "")}
    items = []
    if hosts:
        items = api.call("item.get", {"output": ["itemid", "hostid", "key_", "name", "value_type", "lastvalue"],
                                      "hostids": list(hosts), "search": {"key_": KEYS}, "searchByAny": True,
                                      "startSearch": True, "monitored": True})
    have = {i["hostid"] for i in items if i["key_"].startswith(("wifi.rssi", "wifi.ap.rssi[", "wifi.sta.worst"))}
    hosts = {k: v for k, v in hosts.items() if k in have}
    items = [i for i in items if i["hostid"] in hosts]
    return hosts, items


class Values:
    """Average of each numeric item over a time window: history from PostgreSQL, else the Zabbix API;
    hourly trends when the raw history of that time is gone."""

    def __init__(self, api):
        self.api, self.db = api, None
        if os.environ.get("RCA_DB", "1") != "0":
            try:
                from zbxdb import DB
                self.db = DB()
            except (SystemExit, Exception) as ex:  # noqa: BLE001 - fall back to the API
                log(f"database not used ({ex}); reading through the API")

    def window(self, items, a, b):
        num = [i for i in items if int(i["value_type"]) in (0, 3)]
        out = self._read(num, a, b, trends=False)
        miss = [i for i in num if i["itemid"] not in out]
        if miss:
            h = a - a % 3600
            out.update(self._read(miss, h, h + 3600, trends=True))
        return out

    def _read(self, items, a, b, trends):
        out = {}
        for vt, hist, tr in ((0, "history", "trends"), (3, "history_uint", "trends_uint")):
            ids = [i["itemid"] for i in items if int(i["value_type"]) == vt]
            for part in zr.chunks(ids, 500):
                if self.db:
                    col, table = ("value_avg", tr) if trends else ("value", hist)
                    for r in self.db.rows(f"SELECT itemid, avg({col}) FROM {table} WHERE clock >= {a} AND clock < {b} "
                                          f"AND itemid IN ({','.join(part)}) GROUP BY itemid"):
                        if r[1] not in ("", None):
                            out[r[0]] = float(r[1])
                    continue
                acc = {}
                if trends:
                    for r in self.api.call("trend.get", {"itemids": part, "time_from": a, "time_till": b - 1,
                                                         "output": ["itemid", "value_avg"]}):
                        acc.setdefault(r["itemid"], []).append(float(r["value_avg"]))
                else:
                    for r in self.api.call("history.get", {"itemids": part, "history": vt, "time_from": a,
                                                           "time_till": b - 1, "output": "extend"}):
                        acc.setdefault(r["itemid"], []).append(float(r["value"]))
                out.update({k: sum(v) / len(v) for k, v in acc.items() if v})
        return out


def link_events(api, hostids, a, b):
    """{hostid: [link drops, times unreachable]} for problems that started in [a, b)."""
    out = {h: [0, 0] for h in hostids}
    if not hostids:
        return out
    for e in api.call("event.get", {"output": ["name"], "hostids": list(hostids), "source": 0, "object": 0,
                                    "value": 1, "time_from": a, "time_till": b - 1, "selectHosts": ["hostid"],
                                    "search": {"name": [*LINK_EVENTS, DOWN_EVENT]}, "searchByAny": True,
                                    "startSearch": True}):
        if not e.get("hosts"):
            continue
        h = e["hosts"][0]["hostid"]
        if h not in out:
            continue
        if e["name"].startswith(DOWN_EVENT):
            out[h][1] += 1
        elif "re-established" in e["name"] or not e["name"].startswith("Link to"):
            out[h][0] += 1
    return out


# --------------------------------------------------------------- per-host readings
def peer_of(name):
    m = re.match(r"Link to (.+?): ", name or "")
    return m.group(1) if m else ""


def readings(hosts, items, vals):
    """{host name: {rssi, snr, far_rssi, far_snr, cap, mcs, linked, stations, ap}} from one snapshot."""
    by = {}
    for i in items:
        by.setdefault(i["hostid"], []).append(i)
    out = {}
    for hid, h in hosts.items():
        its = by.get(hid, [])

        def first(prefix, exact=False):
            for i in sorted(its, key=lambda x: x["key_"]):
                if (i["key_"] == prefix if exact else i["key_"].startswith(prefix)) and i["itemid"] in vals:
                    return vals[i["itemid"]], i
            return None, None

        rssi, _ = first("wifi.rssi", True)
        if rssi is None:
            rssi, _ = first("wifi.ap.rssi[")
        snr, _ = first("wifi.snr", True)
        if snr is None:
            snr, _ = first("wifi.ap.snr[")
        ap = False
        if rssi is None:
            rssi, _ = first("wifi.sta.worst", True)
            ap = rssi is not None
        far_rssi, it = first("wifi.remote.rssi[")
        far_snr, _ = first("wifi.remote.snr[")
        cap_rx, _ = first("wifi.capacity.rx[")
        cap_tx, _ = first("wifi.capacity.tx[")
        dl, _ = first("wifi.dlmcs", True)
        ul, _ = first("wifi.ulmcs", True)
        st, _ = first("wifi.stations", True)
        linked = ""
        for i in its:
            if i["key_"].startswith(("wifi.remote.rssi[", "wifi.ap.rssi[")):
                linked = peer_of(i["name"]) or linked
            if i["key_"] == "wifi.link.ssid" and i.get("lastvalue"):
                linked = i["lastvalue"]
        caps = [c for c in (cap_rx, cap_tx) if c]
        out[h["host"]] = {"rssi": rssi, "snr": snr, "far_rssi": far_rssi, "far_snr": far_snr,
                          "cap": min(caps) / 1e6 if caps else None,
                          "mcs": [None if x is None else round(x) for x in (dl, ul)] if (dl or ul) else None,
                          "linked": linked, "stations": None if st is None else round(st), "ap": ap}
    return out


def day_snapshot(api, vals, hosts, items, d):
    """Readings of day d at the busy hour, kept in REPORT_DIR/signal-snap/ once the day's window has passed."""
    path = os.path.join(zr.REPORT_DIR, "signal-snap", f"{d}.json")
    try:
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        if s.get("v") == SNAP_V and s.get("time") == SNAP_TIME and s.get("window") == WINDOW:
            return s["r"]
    except (OSError, ValueError):
        pass
    a = snap_at(d)
    r = readings(hosts, items, vals.window(items, a, a + WINDOW * 60))
    if ready(d) and any(v["rssi"] is not None for v in r.values()):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"v": SNAP_V, "time": SNAP_TIME, "window": WINDOW, "r": r}, f, separators=(",", ":"))
        os.replace(path + ".tmp", path)
    return r


def average(snaps):
    """Weekly reading: average of the daily ones that have a value; lowest day kept."""
    out = {}
    names = set().union(*[s.keys() for _, s in snaps]) if snaps else set()
    for n in names:
        rows = [(d, s[n]) for d, s in snaps if n in s]
        r = {}
        for k in ("rssi", "snr", "far_rssi", "far_snr", "cap"):
            xs = [x[k] for _, x in rows if x.get(k) is not None]
            r[k] = sum(xs) / len(xs) if xs else None
        lows = [(x["rssi"], d) for d, x in rows if x.get("rssi") is not None]
        r["low"] = min(lows) if lows else None
        r["days"] = len(lows)
        last = rows[-1][1] if rows else {}
        r.update({k: last.get(k) for k in ("linked", "mcs", "stations", "ap")})
        for _, x in reversed(rows):
            if x.get("linked"):
                r["linked"] = x["linked"]
                break
        out[n] = r
    return out


# --------------------------------------------------------------- compare
def judge(cur, old, drops):
    """(status, reasons): BAD / WATCH / OK / NO DATA."""
    if cur is None or cur.get("rssi") is None:
        return "nodata", ["no reading at the snapshot time (radio down or not answering)"]
    why, bad = [], False
    rssi, snr = cur["rssi"], cur.get("snr")
    lim_bad, lim_watch = RSSI_BAD, RSSI_WATCH
    if rssi < lim_bad:
        bad = True
        why.append(f"signal below {lim_bad:g} dBm")
    elif rssi < lim_watch:
        why.append(f"signal below {lim_watch:g} dBm")
    if snr is not None:
        if snr < SNR_BAD:
            bad = True
            why.append(f"SNR below {SNR_BAD:g} dB")
        elif snr < SNR_WATCH:
            why.append(f"SNR below {SNR_WATCH:g} dB")
    if old:
        for k, label in (("rssi", "signal"), ("snr", "SNR")):
            if cur.get(k) is not None and old.get(k) is not None and old[k] - cur[k] >= DROP:
                why.append(f"{label} dropped {old[k] - cur[k]:.0f} dB")
    if drops and drops >= DROPS_WATCH:
        why.append(f"link dropped {drops} times")
    if cur.get("ap"):
        why.append("signal = weakest station")
    status = "bad" if bad else ("watch" if [w for w in why if w != "signal = weakest station"] else "ok")
    return status, why


def compare(hosts, cur, old, events):
    rows, lacp = [], []
    for hid, h in hosts.items():
        if LACP.search(h["host"]):
            lacp.append(h["name"])
            continue
        c, o = cur.get(h["host"]), old.get(h["host"])
        ev = events.get(hid, [0, 0])
        status, why = judge(c, o, ev[0])
        ch = None
        if c and o and c.get("rssi") is not None and o.get("rssi") is not None:
            ch = c["rssi"] - o["rssi"]
        sch = None
        if c and o and c.get("snr") is not None and o.get("snr") is not None:
            sch = c["snr"] - o["snr"]
        rows.append({"host": h["host"], "name": h["name"], "ip": h["ip"], "site": h["site"], "cur": c or {},
                     "old": o or {}, "ch": ch, "sch": sch, "drops": ev[0], "down": ev[1], "status": status,
                     "why": why})
    rank = {"bad": 0, "watch": 1, "ok": 2, "nodata": 3}
    worst = lambda r: min(x for x in (r["ch"], r["sch"], 0) if x is not None)  # noqa: E731
    rows.sort(key=lambda r: (rank[r["status"]], worst(r), r["site"], r["name"]))
    counts = {"radios": len(rows), "bad": 0, "watch": 0, "ok": 0, "nodata": 0, "worse": 0, "better": 0, "same": 0}
    for r in rows:
        counts[r["status"]] += 1
        d = [x for x in (r["ch"], r["sch"]) if x is not None]
        if not d:
            continue
        if min(d) <= -SAME:
            counts["worse"] += 1
        elif max(d) >= SAME:
            counts["better"] += 1
        else:
            counts["same"] += 1
    return rows, counts, sorted(lacp)


# --------------------------------------------------------------- HTML
CSS = """
.sig th.g{text-align:center;border-bottom:1px solid var(--line)}.sig td.n{font-variant-numeric:tabular-nums}
.sig td,.sig th{padding:5px 6px}.sig .why{display:block;color:var(--muted);font-size:11px;white-space:normal}
.sig tr.r-bad td:first-child{box-shadow:inset 3px 0 0 var(--bad)}.sig tr.r-watch td:first-child{box-shadow:inset 3px 0 0 var(--warn)}
.sig .lv{display:inline-block;min-width:34px;text-align:right}.sig .lv.bad{color:var(--bad);font-weight:700;background:none}
.sig .lv.warn{color:var(--warn);font-weight:700;background:none}.sig .lv.ok{color:var(--ok);background:none}
@media print{@page{size:A4 landscape;margin:10mm}.sig{font-size:11px}}
"""
PILL = {"bad": "<span class='pill bad'>BAD</span>", "watch": "<span class='pill warn'>WATCH</span>",
        "ok": "<span class='pill ok'>OK</span>", "nodata": "<span class='pill nodata'>NO DATA</span>"}


def num(v, kind="rssi"):
    if v is None:
        return "<span class='muted'>-</span>"
    if kind == "rssi":
        cls = "bad" if v < RSSI_BAD else "warn" if v < RSSI_WATCH else "ok"
    elif kind == "snr":
        cls = "bad" if v < SNR_BAD else "warn" if v < SNR_WATCH else "ok"
    else:
        cls = ""
    return f"<span class='lv {cls}'>{v:.0f}</span>"


def chg(d):
    if d is None:
        return "<span class='muted'>-</span>"
    if abs(d) < SAME:
        return f"<span class='d same'>= {d:+.0f}</span>" if round(d) else "<span class='d same'>= same</span>"
    return f"<span class='d {'up' if d > 0 else 'down'}'>{'▲' if d > 0 else '▼'} {abs(d):.0f}</span>"


def far(c):
    if c.get("far_rssi") is None and c.get("far_snr") is None:
        return "<span class='muted'>-</span>"
    return f"{num(c.get('far_rssi'))} / {num(c.get('far_snr'), 'snr')}"


def rate(c):
    if c.get("cap"):
        return zr.mbps(round(c["cap"]))
    if c.get("mcs"):
        return "MCS " + "/".join("-" if x is None else str(x) for x in c["mcs"])
    if c.get("stations") is not None:
        return f"{c['stations']} stations"
    return "<span class='muted'>-</span>"


def render(kind, label, sub, rows, counts, lacp, was, now, span):
    e = zr.e
    tiles = [("Radios checked", counts["radios"], ""), ("Bad", counts["bad"], "bad"), ("Watch", counts["watch"], "warn"),
             (f"Worse than {was.lower()}", counts["worse"], ""), (f"Better than {was.lower()}", counts["better"], ""),
             ("Unchanged", counts["same"], ""), ("No data", counts["nodata"], "")]
    body = ["<section class='kpis' style='margin-top:18px;grid-template-columns:repeat(auto-fit,minmax(120px,1fr))'>" + "".join(
        f"<div><b{' style=color:var(--' + c + ')' if c and v else ''}>{v}</b><span>{e(t)}</span></div>"
        for t, v, c in tiles) + "</section>"]
    weekly = kind == "weekly"
    head = ("<tr><th rowspan=2>Device</th><th rowspan=2>IP</th><th rowspan=2>Linked to</th>"
            f"<th class=g colspan=3>Signal (dBm)</th><th class=g colspan=3>SNR (dB)</th>"
            f"<th rowspan=2 class=n>Far end<br>signal / SNR</th><th rowspan=2 class=n>Capacity</th>"
            f"<th rowspan=2 class=n>Link drops<br>{e(span)}</th><th rowspan=2>Status</th></tr>"
            f"<tr><th class=n>{e(was)}</th><th class=n>{e(now)}</th><th class=n>Change</th>"
            f"<th class=n>{e(was)}</th><th class=n>{e(now)}</th><th class=n>Change</th></tr>")
    trs = []
    for r in rows:
        c, o = r["cur"], r["old"]
        low = ""
        if weekly and c.get("low") and c.get("days"):
            v, d = c["low"]
            low = f"<span class='why'>lowest {v:.0f} ({date.fromisoformat(d):%a}), {c['days']} days</span>"
        drops = str(r["drops"]) if r["drops"] else "0"
        if r["down"]:
            drops += f"<span class='why'>down {r['down']}x</span>"
        why = r["why"]
        trs.append(f"<tr class='r-{r['status']}'><td><b>{e(r['name'])}</b><span class='why'>{e(r['site'])}</span></td>"
                   f"<td class=nw>{e(r['ip'])}</td><td>{e(c.get('linked') or o.get('linked') or '-')}</td>"
                   f"<td class=n>{num(o.get('rssi'))}</td><td class=n>{num(c.get('rssi'))}{low}</td><td class=n>{chg(r['ch'])}</td>"
                   f"<td class=n>{num(o.get('snr'), 'snr')}</td><td class=n>{num(c.get('snr'), 'snr')}</td><td class=n>{chg(r['sch'])}</td>"
                   f"<td class=n>{far(c)}</td><td class=n>{rate(c)}</td><td class=n>{drops}</td>"
                   f"<td>{PILL[r['status']]}{''.join(f'<span class=why>{e(w)}</span>' for w in why if r['status'] != 'ok')}</td></tr>")
    body.append("<h2>Radios</h2><div class='muted small'>Worst first: BAD, then WATCH, then the biggest drop.</div>")
    body.append(f"<div class='wrap'><table class='sig'>{head}{''.join(trs)}</table></div>" if trs else
                "<div class='empty'>No site radios with signal readings found.</div>")
    if LACP_NOTE or lacp:
        txt = f"Not measured: the LACP links {e(LACP_NOTE)}." if LACP_NOTE else "Not measured:"
        txt += " Their radios cannot be reached by Zabbix" if LACP_NOTE else " radios on LACP links"
        txt += f" ({e(', '.join(lacp))})." if lacp else "."
        body.append(f"<div class='note'>{txt}</div>")
    snap = "the average of the readings over" if not weekly else "the average of the daily snapshots, each over"
    body.append(f"<div class='note small'><b>How to read this.</b> Each value is {snap} {WINDOW} minutes from "
                f"{e(SNAP_TIME)} ({e(str(zr.TZ))}), a busy hour. Signal: higher is better (-50 is stronger than -65). "
                f"OK: signal {RSSI_WATCH:g} dBm or better and SNR {SNR_WATCH:g} dB or better. WATCH: signal below "
                f"{RSSI_WATCH:g} dBm, SNR below {SNR_WATCH:g} dB, a drop of {DROP:g} dB or more since {e(was.lower())}, "
                f"or {DROPS_WATCH} or more link drops. BAD: signal below {RSSI_BAD:g} dBm or SNR below {SNR_BAD:g} dB. "
                f"Changes under {SAME:g} dB count as unchanged. Far end = what the radio at the other end receives. "
                f"Sectors and endpoint devices are not included.</div>")
    title = f"Radio signal check {label}"
    return zr.page(title, f"{kind} radio signal check", "Radio signal", sub, "".join(body)).replace(
        "</style>", CSS + "</style>", 1)


# --------------------------------------------------------------- build
def path(kind, pid, ext="html"):
    return os.path.join(zr.REPORT_DIR, DIRS[kind], f"{pid}.{ext}")


def build(kind, when=None, store=True, api=None):
    from zbxapi import ZabbixAPI
    api = api or ZabbixAPI()
    vals = Values(api)
    hosts, items = fetch_hosts(api)
    if kind == "daily":
        d = when or default_day()
        if snap_at(d) > time.time():
            raise SystemExit(f"The snapshot of {d} is at {SNAP_TIME}: not taken yet.")
        p = d - timedelta(days=1)
        cur = day_snapshot(api, vals, hosts, items, d)
        old = day_snapshot(api, vals, hosts, items, p)
        a = snap_at(d)
        events = link_events(api, hosts, a - 86400, a + WINDOW * 60)
        rows, counts, lacp = compare(hosts, cur, old, events)
        sub = (f"Snapshot {d:%A %d %B %Y} {SNAP_TIME} compared with {p:%A %d %B} {SNAP_TIME}")
        page = render(kind, str(d), sub, rows, counts, lacp, "Yesterday", "Today", "24 h")
        pid, done = str(d), ready(d)
    else:
        first = zr.week_start(when) if when else zr.week_start(zr.today()) - timedelta(days=7)
        days = [first + timedelta(days=i) for i in range(7)]
        prev = [x - timedelta(days=7) for x in days]
        cur = average([(str(x), day_snapshot(api, vals, hosts, items, x)) for x in days if snap_at(x) <= time.time()])
        old = average([(str(x), day_snapshot(api, vals, hosts, items, x)) for x in prev])
        events = link_events(api, hosts, zr.at(first), min(zr.at(first + timedelta(days=7)), int(time.time())))
        rows, counts, lacp = compare(hosts, cur, old, events)
        sub = (f"Week {first:%d %b} to {days[-1]:%d %b %Y} compared with {prev[0]:%d %b} to {prev[-1]:%d %b}, "
               f"daily snapshots at {SNAP_TIME}")
        page = render(kind, str(first), sub, rows, counts, lacp, "Last week", "This week", "week")
        pid, done = str(first), ready(days[-1])
    if store and done:
        os.makedirs(os.path.dirname(path(kind, pid)), exist_ok=True)
        with open(path(kind, pid), "w", encoding="utf-8") as f:
            f.write(page)
        with open(path(kind, pid, "json"), "w", encoding="utf-8") as f:
            json.dump({"kind": kind, "id": pid, "counts": counts, "built": int(time.time())}, f)
    return pid, page, counts, done


def due():
    """[(kind, date)] of finished reports not built yet: the last 7 days and the last full week."""
    out = []
    t = zr.today()
    for i in range(7, -1, -1):
        d = t - timedelta(days=i)
        if ready(d) and not os.path.exists(path("daily", str(d))):
            out.append(("daily", d))
    w = zr.week_start(t) - timedelta(days=7)
    if not os.path.exists(path("weekly", str(w))):
        out.append(("weekly", w))
    return out


def cmd_scheduled():
    todo = due()
    if not todo:
        return
    from zbxapi import ZabbixAPI
    api, failed = ZabbixAPI(), 0
    for kind, d in todo:
        try:
            pid, _, c, _ = build(kind, d, api=api)
            log(f"{kind} {pid} built: radios={c['radios']} bad={c['bad']} watch={c['watch']} nodata={c['nodata']}")
        except Exception as ex:  # noqa: BLE001 - keep building the others
            failed += 1
            log(f"ERROR {kind} {d}: {ex}")
    if failed:
        sys.exit(1)


def list_reports():
    out = {}
    for kind in KINDS:
        try:
            files = os.listdir(os.path.join(zr.REPORT_DIR, DIRS[kind]))
        except OSError:
            files = []
        out[kind] = sorted({f[:-5] for f in files if f.endswith(".html")}, reverse=True)
    return out


CRON_FILE = "/etc/cron.d/mdp-signal-report"


def cmd_cron(do_apply):
    here = os.path.dirname(os.path.abspath(__file__))
    env = os.path.expanduser("~/.zabbix_env")
    text = (f"# Radio signal check (tools/zbx_signal.py): builds the daily snapshot report once {SNAP_TIME} has passed,\n"
            f"# and the weekly one. Runs hourly; does nothing when everything is built.\n"
            f"SHELL=/bin/bash\n"
            f"20 * * * * root bash -c 'source {env} && python3 {here}/zbx_signal.py scheduled' >> {zr.LOG} 2>&1\n")
    print(text)
    if do_apply:
        with open(CRON_FILE, "w") as f:
            f.write(text)
        os.chmod(CRON_FILE, 0o644)
        print("Installed " + CRON_FILE)
    else:
        print("Dry run; re-run with --apply to install.")


# --------------------------------------------------------------- web pages (rca_server.py /signal)
def web(api, parts, q, prefix="/"):
    """(code, title, html, is_report) for /signal..., parts = path after 'signal'."""
    e = zr.e
    base = f"<base href='{e(prefix)}'>"
    try:
        if len(parts) == 2 and parts[0] in KINDS and re.fullmatch(r"\d{4}-\d{2}-\d{2}\.html", parts[1]):
            f = path(parts[0], parts[1][:-5])
            if not os.path.isfile(f):
                pid, page, _, _ = build(parts[0], date.fromisoformat(parts[1][:10]), api=api)
                return 200, "", page, True
            return 200, "", open(f, encoding="utf-8").read(), True
        if len(parts) == 2 and parts[0] == "now" and parts[1] in KINDS:
            when = default_day() if parts[1] == "daily" else zr.week_start(zr.today())
            _, page, _, _ = build(parts[1], when, api=api)
            return 200, "", page, True
        if parts == ["view"] and q.get("kind") in KINDS and q.get("date"):
            d = date.fromisoformat(q["date"])
            if q["kind"] == "weekly":
                d = zr.week_start(d)
            _, page, _, _ = build(q["kind"], d, api=api)
            return 200, "", page, True
        if parts == ["build"]:
            done = []
            for kind, d in due():
                try:
                    build(kind, d, api=api)
                    done.append(f"<li><b>{kind} {d}: built</b></li>")
                except Exception as ex:  # noqa: BLE001
                    done.append(f"<li>{kind} {d}: failed ({e(ex)})</li>")
            body = "<h1>Radio signal reports built</h1><ul>" + ("".join(done) or "<li>Nothing was missing.</li>") + \
                   "</ul><p><a class='btn alt' href='signal'>Back</a></p>"
            return 200, "Radio signal check", base + body, False
    except SystemExit as ex:
        return 400, "Radio signal check", base + f"<h1>No report</h1><div class='verdict err'>{e(ex)}</div>" \
            "<p><a class='btn alt' href='signal'>Back</a></p>", False
    cols = []
    for kind, ids in list_reports().items():
        links = "".join(f"<li><a href='signal/{kind}/{i}.html'>{e(i)}{' week' if kind == 'weekly' else ''}</a></li>"
                        for i in ids[:60])
        cols.append(f"<div class='finding'><b>{kind.title()}</b><ul>{links or '<li class=muted>none yet</li>'}</ul></div>")
    today = zr.today()
    body = (f"{base}<h1>Radio signal check</h1><div class='muted'>Signal and SNR of every site radio at {e(SNAP_TIME)} "
            f"(average of {WINDOW} minutes), compared with the day before; the weekly report compares the week's "
            f"snapshots with the week before. Built every hour once the snapshot time has passed. Sectors and endpoints are not included.</div>"
            "<p><a class='btn' href='signal/now/daily'>Latest daily</a> "
            "<a class='btn' href='signal/now/weekly'>This week so far</a> "
            "<a class='btn alt' href='signal/build'>Build now</a></p>"
            "<form class='rca' action='signal/view' method='get'>"
            "<label>Report<select name='kind'><option value='daily'>Daily</option><option value='weekly'>Weekly</option>"
            f"</select></label><label>Day<input type='date' name='date' value='{default_day()}' max='{today}' required></label>"
            "<div class='actions'><button type='submit'>View report</button></div></form>"
            "<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-top:16px'>"
            + "".join(cols) + "</div>")
    return 200, "Radio signal check", body, False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for kind in KINDS:
        s = sub.add_parser(kind)
        s.add_argument("--date", help="YYYY-MM-DD")
        s.add_argument("--out", help="also write the HTML here")
        s.add_argument("--no-save", action="store_true")
    sub.add_parser("scheduled")
    c = sub.add_parser("cron")
    c.add_argument("--apply", action="store_true")
    sub.add_parser("list")
    a = ap.parse_args()
    if a.cmd == "scheduled":
        return cmd_scheduled()
    if a.cmd == "cron":
        return cmd_cron(a.apply)
    if a.cmd == "list":
        for kind, ids in list_reports().items():
            print(f"{kind:7} {len(ids):4}  {', '.join(ids[:6])}")
        return
    when = date.fromisoformat(a.date) if a.date else None
    pid, page, c, done = build(a.cmd, when, store=not a.no_save)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(page)
    print(f"{a.cmd} {pid}: " + ", ".join(f"{k}={v}" for k, v in c.items()))
    if done and not a.no_save:
        print("Report: " + path(a.cmd, pid))
    elif not done:
        print("Period not finished: not saved" + (f" (copy in {a.out})" if a.out else " (use --out)"))


if __name__ == "__main__":
    main()
