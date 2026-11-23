#!/usr/bin/env python3
"""Endpoint signal check (MDP-ENDPOINT_SIGNAL): signal and SNR of every endpoint CPE (ePMP Force 4625, EP-* hosts) at the
busy hour, grouped by the site it belongs to, with the sector AP it is connected to. Compared with the day
before (daily) or the week before (weekly). Same snapshot time and method as the radio signal check.

  python3 tools/zbx_endpoint_signal.py daily  [--date YYYY-MM-DD] [--site SITE0001]
  python3 tools/zbx_endpoint_signal.py weekly [--date YYYY-MM-DD] [--site SITE0001]
  python3 tools/zbx_endpoint_signal.py scheduled                 build what is due and missing (hourly cron)
  python3 tools/zbx_endpoint_signal.py cron [--apply]            install the hourly schedule
  python3 tools/zbx_endpoint_signal.py list

Per CPE: its own signal and SNR (downlink, what the CPE receives), what the sector AP receives from it
(uplink, read from the AP's station table), MCS, link drops and the sector it is on (live "Connected AP"
SSID, matched to the AP device; else the parent AP set in Zabbix).

Settings (environment, ~/.zabbix_env or /etc/mdatapulse/mdatapulse.conf); time and window from SIGNAL_TIME /
SIGNAL_WINDOW, change limits from SIGNAL_DROP / SIGNAL_SAME / SIGNAL_DROPS_WATCH:
  ENDPOINT_SIGNAL_RSSI_WATCH=-70     dBm: signal below this = WATCH
  ENDPOINT_SIGNAL_RSSI_BAD=-75       dBm: signal below this = BAD (same as the Zabbix "Signal weak" alarm)
  ENDPOINT_SIGNAL_SNR_WATCH=20       dB
  ENDPOINT_SIGNAL_SNR_BAD=15         dB
  ENDPOINT_SIGNAL_HOSTS=^EP-       host names of the endpoint CPEs (role tag sm)
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zbx_report as zr   # noqa: E402
import site_conf as sc    # noqa: E402
import zbx_signal as sg   # noqa: E402  (snapshot time, history reader, link events)


def _f(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return float(default)


RSSI_WATCH, RSSI_BAD = _f("ENDPOINT_SIGNAL_RSSI_WATCH", -70), _f("ENDPOINT_SIGNAL_RSSI_BAD", -75)
SNR_WATCH, SNR_BAD = _f("ENDPOINT_SIGNAL_SNR_WATCH", 20), _f("ENDPOINT_SIGNAL_SNR_BAD", 15)
HOSTS = re.compile(os.environ.get("ENDPOINT_SIGNAL_HOSTS") or r"^EP-")
SNAP_V = 1
KINDS = ("daily", "weekly")
DIRS = {"daily": "endpoint_signal-daily", "weekly": "endpoint_signal-weekly"}
CPE_KEYS = ["wifi.rssi", "wifi.snr", "wifi.dlmcs", "wifi.ulmcs", "wifi.link.ssid"]
AP_KEYS = ["wifi.sta.ulrssi[", "wifi.sta.ulsnr["]
STA = re.compile(r"Station (\S+) \(")
URL = "endpoint-signal"


def log(msg):
    zr.log("endpoint signal: " + msg)


def sector_key(text):
    """SITE0010-S1, SITE10_S1, site35 s1 -> ('35', '1'); None when there is no site and sector in it."""
    m = re.search(sc.P + r"\D?0*(\d+)\D{0,3}S(?:ECTOR)?\D?0*(\d+)", (text or "").upper())
    return (m.group(1), m.group(2)) if m else None


# --------------------------------------------------------------- fetch
def fetch(api):
    """(cpes {hostid: {...}}, items of the CPEs and of the AP station tables)."""
    raw = api.call("host.get", {"output": ["hostid", "host", "name"], "selectTags": "extend",
                                "selectInterfaces": ["ip", "main", "type"], "monitored_hosts": True})
    cpes, aps = {}, {}
    for h in raw:
        tags = {t["tag"]: t["value"] for t in h["tags"]}
        ips = sorted(h.get("interfaces") or [], key=lambda i: (str(i.get("main")) != "1", str(i.get("type")) != "2"))
        d = {"host": h["host"], "name": h["name"], "site": tags.get("site", ""), "ip": ips[0]["ip"] if ips else "",
             "parent": tags.get("parent", "")}
        if tags.get("role") == "ap":
            aps[h["hostid"]] = d
        elif tags.get("role") == "sm" and HOSTS.search(h["host"]):
            d["site"] = d["site"] or "No site"
            cpes[h["hostid"]] = d
    items = []
    if cpes:
        items = api.call("item.get", {"output": ["itemid", "hostid", "key_", "name", "value_type", "lastvalue"],
                                      "hostids": list(cpes), "search": {"key_": CPE_KEYS}, "searchByAny": True,
                                      "startSearch": True, "monitored": True})
    if aps:
        items += api.call("item.get", {"output": ["itemid", "hostid", "key_", "name", "value_type", "lastvalue"],
                                       "hostids": list(aps), "search": {"key_": AP_KEYS}, "searchByAny": True,
                                       "startSearch": True, "monitored": True})
    return cpes, aps, items


def readings(cpes, aps, items, vals):
    """{CPE host: {rssi, snr, up_rssi, up_snr, mcs, ssid, ap, ap_host}} from one snapshot."""
    own, up = {}, {}
    for i in items:
        if i["hostid"] in cpes:
            own.setdefault(i["hostid"], []).append(i)
        elif i["hostid"] in aps:
            m = STA.match(i["name"] or "")
            if m:
                up.setdefault(m.group(1), []).append(i)
    ap_by_host = {a["host"]: a for a in aps.values()}
    out = {}
    for hid, h in cpes.items():
        its = {i["key_"]: i for i in own.get(hid, [])}

        def v(key):
            i = its.get(key)
            return vals.get(i["itemid"]) if i else None

        ssid = (its.get("wifi.link.ssid") or {}).get("lastvalue") or ""
        # the sector: AP named like the live SSID (same site first), else the parent set in Zabbix
        ap = None
        key = sector_key(ssid)
        if key:
            m = [a for a in aps.values() if key in (sector_key(a["name"]), sector_key(a["host"]))]
            m.sort(key=lambda a: a["site"] != h["site"])
            ap = m[0] if m else None
        ap = ap or ap_by_host.get(h["parent"])
        # what the AP hears from this CPE: its station rows; the connected AP's first, then any with a value
        u_rssi = u_snr = None
        rows = sorted(up.get(h["ip"], []), key=lambda i: not (ap and aps.get(i["hostid"]) is ap))
        for i in rows:
            if i["key_"].startswith("wifi.sta.ulrssi[") and u_rssi is None and i["itemid"] in vals:
                u_rssi = vals[i["itemid"]]
            if i["key_"].startswith("wifi.sta.ulsnr[") and u_snr is None and i["itemid"] in vals:
                u_snr = vals[i["itemid"]]
        dl, ul = v("wifi.dlmcs"), v("wifi.ulmcs")
        out[h["host"]] = {"rssi": v("wifi.rssi"), "snr": v("wifi.snr"), "up_rssi": u_rssi, "up_snr": u_snr,
                          "mcs": [None if x is None else round(x) for x in (dl, ul)] if (dl or ul) else None,
                          "ssid": ssid, "ap": ap["name"] if ap else "", "ap_host": ap["host"] if ap else ""}
    return out


def day_snapshot(vals, cpes, aps, items, d):
    path = os.path.join(zr.REPORT_DIR, "endpoint_signal-snap", f"{d}.json")
    try:
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
        if s.get("v") == SNAP_V and s.get("time") == sg.SNAP_TIME and s.get("window") == sg.WINDOW:
            return s["r"]
    except (OSError, ValueError):
        pass
    a = sg.snap_at(d)
    r = readings(cpes, aps, items, vals.window(items, a, a + sg.WINDOW * 60))
    if sg.ready(d) and any(v["rssi"] is not None for v in r.values()):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"v": SNAP_V, "time": sg.SNAP_TIME, "window": sg.WINDOW, "r": r}, f, separators=(",", ":"))
        os.replace(path + ".tmp", path)
    return r


def average(snaps):
    """Weekly reading: average of the daily ones; lowest day kept; sector of the last day."""
    out = {}
    names = set().union(*[s.keys() for _, s in snaps]) if snaps else set()
    for n in names:
        rows = [(d, s[n]) for d, s in snaps if n in s]
        r = {}
        for k in ("rssi", "snr", "up_rssi", "up_snr"):
            xs = [x[k] for _, x in rows if x.get(k) is not None]
            r[k] = sum(xs) / len(xs) if xs else None
        lows = [(x["rssi"], d) for d, x in rows if x.get("rssi") is not None]
        r["low"] = min(lows) if lows else None
        r["days"] = len(lows)
        r["mcs"] = rows[-1][1].get("mcs") if rows else None
        aps_seen = []
        for _, x in rows:
            if x.get("ap") and x["ap"] not in aps_seen:
                aps_seen.append(x["ap"])
        last = next((x for _, x in reversed(rows) if x.get("ap") or x.get("ssid")), {})
        r.update({"ap": last.get("ap", ""), "ssid": last.get("ssid", ""), "ap_host": last.get("ap_host", ""),
                  "moved": aps_seen[:-1]})
        out[n] = r
    return out


# --------------------------------------------------------------- compare
def judge(cur, old, drops):
    """(status, reasons): bad / watch / ok / nodata."""
    if cur is None or cur.get("rssi") is None:
        return "nodata", ["no reading at the snapshot time (CPE down or not linked)"]
    why, bad = [], False
    for k, who in (("rssi", "signal"), ("up_rssi", "AP hears it")):
        x = cur.get(k)
        if x is None:
            continue
        if x < RSSI_BAD:
            bad = True
            why.append(f"{who} below {RSSI_BAD:g} dBm")
        elif x < RSSI_WATCH:
            why.append(f"{who} below {RSSI_WATCH:g} dBm")
    for k, who in (("snr", "SNR"), ("up_snr", "SNR at the AP")):
        x = cur.get(k)
        if x is None:
            continue
        if x < SNR_BAD:
            bad = True
            why.append(f"{who} below {SNR_BAD:g} dB")
        elif x < SNR_WATCH:
            why.append(f"{who} below {SNR_WATCH:g} dB")
    if old:
        for k, label in (("rssi", "signal"), ("snr", "SNR")):
            if cur.get(k) is not None and old.get(k) is not None and old[k] - cur[k] >= sg.DROP:
                why.append(f"{label} dropped {old[k] - cur[k]:.0f} dB")
        if old.get("ap") and cur.get("ap") and old["ap"] != cur["ap"]:
            why.append(f"moved from sector {old['ap']}")
    if drops and drops >= sg.DROPS_WATCH:
        why.append(f"link dropped {drops} times")
    return ("bad" if bad else "watch" if why else "ok"), why


RANK = {"bad": 0, "watch": 1, "ok": 2, "nodata": 3}


def compare(cpes, cur, old, events):
    rows = []
    for hid, h in cpes.items():
        c, o = cur.get(h["host"]), old.get(h["host"])
        ev = events.get(hid, [0, 0])
        status, why = judge(c, o, ev[0])
        ch = sch = None
        if c and o and c.get("rssi") is not None and o.get("rssi") is not None:
            ch = c["rssi"] - o["rssi"]
        if c and o and c.get("snr") is not None and o.get("snr") is not None:
            sch = c["snr"] - o["snr"]
        rows.append({"host": h["host"], "name": h["name"], "ip": h["ip"], "site": h["site"], "cur": c or {},
                     "old": o or {}, "ch": ch, "sch": sch, "drops": ev[0], "down": ev[1], "status": status,
                     "why": why})
    worst = lambda r: min(x for x in (r["ch"], r["sch"], 0) if x is not None)  # noqa: E731
    rows.sort(key=lambda r: (r["site"], RANK[r["status"]], worst(r), r["cur"].get("rssi") or 0, natural(r["name"])))
    sites = {}
    total = new_counts()
    for r in rows:
        s = sites.setdefault(r["site"], new_counts())
        for cnt in (s, total):
            count(cnt, r)
    return rows, total, sites


def natural(s):
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s or "")]


def new_counts():
    return {"endpoints": 0, "bad": 0, "watch": 0, "ok": 0, "nodata": 0, "worse": 0, "better": 0, "same": 0}


def count(c, r):
    c["endpoints"] += 1
    c[r["status"]] += 1
    d = [x for x in (r["ch"], r["sch"]) if x is not None]
    if not d:
        return
    if min(d) <= -sg.SAME:
        c["worse"] += 1
    elif max(d) >= sg.SAME:
        c["better"] += 1
    else:
        c["same"] += 1


# --------------------------------------------------------------- HTML
CSS = sg.CSS + """
table.sitecnt{width:auto;min-width:520px}.sitecnt td,.sitecnt th{padding:4px 12px}.sig td.ap .why,.sig td.cpe .why{white-space:nowrap}.sitecnt a{text-decoration:none;font-weight:700}.sig td.ap{white-space:nowrap}
.sitecnt tr.tothead th{border-top:2px solid var(--fg);padding-top:8px}.sitecnt tr.tot td{font-weight:700;border-top:1px solid var(--line)}
h2.site{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-top:26px}
h2.site .muted{font-size:13px;font-weight:400}.c-bad{color:var(--bad);font-weight:700}.c-warn{color:var(--warn);font-weight:700}
@media print{h2.site{break-before:auto}}
"""


def num(v, kind="rssi"):
    if v is None:
        return "<span class='muted'>-</span>"
    if kind == "rssi":
        cls = "bad" if v < RSSI_BAD else "warn" if v < RSSI_WATCH else "ok"
    else:
        cls = "bad" if v < SNR_BAD else "warn" if v < SNR_WATCH else "ok"
    return f"<span class='lv {cls}'>{v:.0f}</span>"


def apcell(c, o):
    e = zr.e
    ap = c.get("ap") or o.get("ap")
    ssid = c.get("ssid") or o.get("ssid")
    if not ap and not ssid:
        return "<span class='muted'>-</span>"
    out = f"{e(ap or ssid)}"
    if ap and ssid and ssid != ap:
        out += f"<span class='why'>SSID {e(ssid)}</span>"
    if c.get("moved"):
        out += f"<span class='why'>also on {e(', '.join(c['moved']))}</span>"
    return out


def site_counts(c):
    parts = [f"{c['endpoints']} endpoint{'s' if c['endpoints'] != 1 else ''}"]
    if c["bad"]:
        parts.append(f"<span class='c-bad'>{c['bad']} bad</span>")
    if c["watch"]:
        parts.append(f"<span class='c-warn'>{c['watch']} watch</span>")
    if c["nodata"]:
        parts.append(f"{c['nodata']} no data")
    return ", ".join(parts)


def anchor(site):
    return "s-" + re.sub(r"[^A-Za-z0-9]+", "-", site)


def render(kind, label, sub, rows, total, sites, was, now, span):
    e = zr.e
    tiles = [("Endpoints checked", total["endpoints"], ""), ("Bad", total["bad"], "bad"), ("Watch", total["watch"], "warn"),
             (f"Worse than {was.lower()}", total["worse"], ""), (f"Better than {was.lower()}", total["better"], ""),
             ("Unchanged", total["same"], ""), ("No data", total["nodata"], "")]
    body = ["<section class='kpis' style='margin-top:18px;grid-template-columns:repeat(auto-fit,minmax(120px,1fr))'>" + "".join(
        f"<div><b{' style=color:var(--' + c + ')' if c and v else ''}>{v}</b><span>{e(t)}</span></div>"
        for t, v, c in tiles) + "</section>"]
    weekly = kind == "weekly"
    if sites:
        trs = "".join(
            f"<tr><td><a href='#{anchor(s)}'>{e(s)}</a></td><td class=n>{c['endpoints']}</td>"
            f"<td class=n>{'<span class=c-bad>' + str(c['bad']) + '</span>' if c['bad'] else 0}</td>"
            f"<td class=n>{'<span class=c-warn>' + str(c['watch']) + '</span>' if c['watch'] else 0}</td>"
            f"<td class=n>{c['ok']}</td><td class=n>{c['nodata']}</td><td class=n>{c['worse']}</td></tr>"
            for s, c in sorted(sites.items()))
        hdr = ("<tr><th>site</th><th class=n>Endpoints</th><th class=n>Bad</th><th class=n>Watch</th><th class=n>OK</th>"
               f"<th class=n>No data</th><th class=n>Worse than {e(was.lower())}</th></tr>")
        t = total  # ENDPOINT_SIGNAL-TOTAL: totals row under the table, headings repeated above it
        tot = (f"<tr class='tot'><td>Total ({len(sites)} sites)</td><td class=n>{t['endpoints']}</td>"
               f"<td class=n>{'<span class=c-bad>' + str(t['bad']) + '</span>' if t['bad'] else 0}</td>"
               f"<td class=n>{'<span class=c-warn>' + str(t['watch']) + '</span>' if t['watch'] else 0}</td>"
               f"<td class=n>{t['ok']}</td><td class=n>{t['nodata']}</td><td class=n>{t['worse']}</td></tr>")
        body.append("<h2>By site</h2><div class='wrap'><table class='sitecnt'>" + hdr + trs +
                    hdr.replace("<tr>", "<tr class='tothead'>", 1) + tot + "</table></div>")
    head = ("<tr><th rowspan=2>CPE</th><th rowspan=2>IP</th><th rowspan=2>Sector AP</th>"
            "<th class=g colspan=3>Signal (dBm)</th><th class=g colspan=3>SNR (dB)</th>"
            "<th rowspan=2 class=n>AP hears it<br>signal / SNR</th><th rowspan=2 class=n>MCS<br>DL / UL</th>"
            f"<th rowspan=2 class=n>Link drops<br>{e(span)}</th><th rowspan=2>Status</th></tr>"
            f"<tr><th class=n>{e(was)}</th><th class=n>{e(now)}</th><th class=n>Change</th>"
            f"<th class=n>{e(was)}</th><th class=n>{e(now)}</th><th class=n>Change</th></tr>")
    by_site = {}
    for r in rows:
        by_site.setdefault(r["site"], []).append(r)
    for site in sorted(by_site):
        trs = []
        for r in by_site[site]:
            c, o = r["cur"], r["old"]
            low = ""
            if weekly and c.get("low") and c.get("days"):
                v, d = c["low"]
                low = f"<span class='why'>lowest {v:.0f} ({date.fromisoformat(d):%a}), {c['days']} days</span>"
            drops = str(r["drops"]) + (f"<span class='why'>down {r['down']}x</span>" if r["down"] else "")
            up = ("<span class='muted'>-</span>" if c.get("up_rssi") is None and c.get("up_snr") is None
                  else f"{num(c.get('up_rssi'))} / {num(c.get('up_snr'), 'snr')}")
            mcs = "/".join("-" if x is None else str(x) for x in c["mcs"]) if c.get("mcs") else "<span class='muted'>-</span>"
            endpoint = re.sub(r"^EP-", "", r["host"])
            trs.append(f"<tr class='r-{r['status']}'><td class=cpe><b>{e(r['name'])}</b><span class='why'>{e(endpoint)}</span></td>"
                       f"<td class=nw>{e(r['ip'])}</td><td class=ap>{apcell(c, o)}</td>"
                       f"<td class=n>{num(o.get('rssi'))}</td><td class=n>{num(c.get('rssi'))}{low}</td><td class=n>{sg.chg(r['ch'])}</td>"
                       f"<td class=n>{num(o.get('snr'), 'snr')}</td><td class=n>{num(c.get('snr'), 'snr')}</td><td class=n>{sg.chg(r['sch'])}</td>"
                       f"<td class=n>{up}</td><td class=n>{mcs}</td><td class=n>{drops}</td>"
                       f"<td>{sg.PILL[r['status']]}{''.join(f'<span class=why>{e(w)}</span>' for w in r['why'] if r['status'] != 'ok')}</td></tr>")
        body.append(f"<h2 class='site' id='{anchor(site)}'>{e(site)} <span class='muted'>{site_counts(sites[site])}</span></h2>"
                    f"<div class='wrap'><table class='sig'>{head}{''.join(trs)}</table></div>")
    if not rows:
        body.append("<div class='empty'>No endpoint CPEs with signal readings found.</div>")
    snap = "the average of the readings over" if not weekly else "the average of the daily snapshots, each over"
    body.append(f"<div class='note small'><b>How to read this.</b> Each value is {snap} {sg.WINDOW} minutes from "
                f"{e(sg.SNAP_TIME)} ({e(str(zr.TZ))}), a busy hour. Signal and SNR: what the CPE receives from its "
                f"sector AP. AP hears it: what the sector AP receives from the CPE. Signal: higher is better "
                f"(-55 is stronger than -70). OK: signal {RSSI_WATCH:g} dBm or better and SNR {SNR_WATCH:g} dB or "
                f"better, both ways. WATCH: signal below {RSSI_WATCH:g} dBm, SNR below {SNR_WATCH:g} dB, a drop of "
                f"{sg.DROP:g} dB or more since {e(was.lower())}, a change of sector, or {sg.DROPS_WATCH} or more link "
                f"drops. BAD: signal below {RSSI_BAD:g} dBm or SNR below {SNR_BAD:g} dB. Changes under {sg.SAME:g} dB "
                f"count as unchanged. Sector AP: the AP the CPE is connected to at the snapshot.</div>")
    return zr.page(f"Endpoint signal check {label}", f"{kind} endpoint signal check", "Endpoint signal", sub,
                   "".join(body)).replace("</style>", CSS + "</style>", 1)


# --------------------------------------------------------------- build
def path(kind, pid, ext="html"):
    return os.path.join(zr.REPORT_DIR, DIRS[kind], f"{pid}.{ext}")


def build(kind, when=None, store=True, api=None, site=None):
    from zbxapi import ZabbixAPI
    api = api or ZabbixAPI()
    vals = sg.Values(api)
    every, aps, items = fetch(api)   # snapshots always hold every CPE; --site only narrows the report
    cpes = every
    if site:
        store = False
        cpes = {k: v for k, v in every.items() if v["site"].upper() == site.upper()}
    if kind == "daily":
        d = when or sg.default_day()
        if sg.snap_at(d) > time.time():
            raise SystemExit(f"The snapshot of {d} is at {sg.SNAP_TIME}: not taken yet.")
        p = d - timedelta(days=1)
        cur = day_snapshot(vals, every, aps, items, d)
        old = day_snapshot(vals, every, aps, items, p)
        a = sg.snap_at(d)
        events = sg.link_events(api, cpes, a - 86400, a + sg.WINDOW * 60)
        rows, total, sites = compare(cpes, cur, old, events)
        sub = f"Snapshot {d:%A %d %B %Y} {sg.SNAP_TIME} compared with {p:%A %d %B} {sg.SNAP_TIME}"
        page = render(kind, str(d), sub, rows, total, sites, "Yesterday", "Today", "24 h")
        pid, done = str(d), sg.ready(d)
    else:
        first = zr.week_start(when) if when else zr.week_start(zr.today()) - timedelta(days=7)
        days = [first + timedelta(days=i) for i in range(7)]
        prev = [x - timedelta(days=7) for x in days]
        cur = average([(str(x), day_snapshot(vals, every, aps, items, x)) for x in days if sg.snap_at(x) <= time.time()])
        old = average([(str(x), day_snapshot(vals, every, aps, items, x)) for x in prev])
        events = sg.link_events(api, cpes, zr.at(first), min(zr.at(first + timedelta(days=7)), int(time.time())))
        rows, total, sites = compare(cpes, cur, old, events)
        sub = (f"Week {first:%d %b} to {days[-1]:%d %b %Y} compared with {prev[0]:%d %b} to {prev[-1]:%d %b}, "
               f"daily snapshots at {sg.SNAP_TIME}")
        page = render(kind, str(first), sub, rows, total, sites, "Last week", "This week", "week")
        pid, done = str(first), sg.ready(days[-1])
    if site:
        page = page.replace("<h1>Endpoint signal</h1>", f"<h1>Endpoint signal {zr.e(site.upper())}</h1>", 1)
    if store and done:
        os.makedirs(os.path.dirname(path(kind, pid)), exist_ok=True)
        with open(path(kind, pid), "w", encoding="utf-8") as f:
            f.write(page)
        with open(path(kind, pid, "json"), "w", encoding="utf-8") as f:
            json.dump({"kind": kind, "id": pid, "counts": total, "sites": sites, "built": int(time.time())}, f)
    return pid, page, total, done


def due():
    out = []
    t = zr.today()
    for i in range(7, -1, -1):
        d = t - timedelta(days=i)
        if sg.ready(d) and not os.path.exists(path("daily", str(d))):
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
            log(f"{kind} {pid} built: endpoints={c['endpoints']} bad={c['bad']} watch={c['watch']} nodata={c['nodata']}")
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


CRON_FILE = "/etc/cron.d/mdp-endpoint_signal-report"


def cmd_cron(do_apply):
    here = os.path.dirname(os.path.abspath(__file__))
    env = os.path.expanduser("~/.zabbix_env")
    text = (f"# Endpoint signal check (tools/zbx_endpoint_signal.py): daily and weekly, once {sg.SNAP_TIME} has passed.\n"
            f"SHELL=/bin/bash\n"
            f"25 * * * * root bash -c 'source {env} && python3 {here}/zbx_endpoint_signal.py scheduled' >> {zr.LOG} 2>&1\n")
    print(text)
    if do_apply:
        with open(CRON_FILE, "w") as f:
            f.write(text)
        os.chmod(CRON_FILE, 0o644)
        print("Installed " + CRON_FILE)
    else:
        print("Dry run; re-run with --apply to install.")


# --------------------------------------------------------------- web pages (rca_server.py /endpoint-signal)
def host_site(api, host):
    for h in api.call("host.get", {"output": ["host"], "selectTags": "extend", "filter": {"host": [host]}}):
        for t in h["tags"]:
            if t["tag"] == "site":
                return t["value"]
    return None


def web(api, parts, q, prefix="/"):
    """(code, title, html, is_report) for /endpoint-signal..., parts = path after 'endpoint-signal'."""
    e = zr.e
    base = f"<base href='{e(prefix)}'>"
    site = q.get("site") or (host_site(api, q["host"]) if q.get("host") else None)
    try:
        if len(parts) == 2 and parts[0] in KINDS and re.fullmatch(r"\d{4}-\d{2}-\d{2}\.html", parts[1]):
            f = path(parts[0], parts[1][:-5])
            if site or not os.path.isfile(f):
                _, page, _, _ = build(parts[0], date.fromisoformat(parts[1][:10]), api=api, site=site)
                return 200, "", page, True
            return 200, "", open(f, encoding="utf-8").read(), True
        if len(parts) == 2 and parts[0] == "now" and parts[1] in KINDS:
            when = sg.default_day() if parts[1] == "daily" else zr.week_start(zr.today())
            _, page, _, _ = build(parts[1], when, api=api, site=site)
            return 200, "", page, True
        if parts == ["view"] and q.get("kind") in KINDS and q.get("date"):
            d = date.fromisoformat(q["date"])
            if q["kind"] == "weekly":
                d = zr.week_start(d)
            _, page, _, _ = build(q["kind"], d, api=api, site=site)
            return 200, "", page, True
        if parts == ["build"]:
            done = []
            for kind, d in due():
                try:
                    build(kind, d, api=api)
                    done.append(f"<li><b>{kind} {d}: built</b></li>")
                except Exception as ex:  # noqa: BLE001
                    done.append(f"<li>{kind} {d}: failed ({e(ex)})</li>")
            body = "<h1>Endpoint signal reports built</h1><ul>" + ("".join(done) or "<li>Nothing was missing.</li>") + \
                   f"</ul><p><a class='btn alt' href='{URL}'>Back</a></p>"
            return 200, "Endpoint signal check", base + body, False
    except SystemExit as ex:
        return 400, "Endpoint signal check", base + f"<h1>No report</h1><div class='verdict err'>{e(ex)}</div>" \
            f"<p><a class='btn alt' href='{URL}'>Back</a></p>", False
    cols = []
    for kind, ids in list_reports().items():
        links = "".join(f"<li><a href='{URL}/{kind}/{i}.html'>{e(i)}{' week' if kind == 'weekly' else ''}</a></li>"
                        for i in ids[:60])
        cols.append(f"<div class='finding'><b>{kind.title()}</b><ul>{links or '<li class=muted>none yet</li>'}</ul></div>")
    today = zr.today()
    body = (f"{base}<h1>Endpoint signal check</h1><div class='muted'>Signal and SNR of every endpoint CPE at {e(sg.SNAP_TIME)} "
            f"(average of {sg.WINDOW} minutes), grouped by site, with the sector AP each CPE is connected to and what "
            f"that AP hears from it. Compared with the day before; the weekly report compares the week's snapshots "
            f"with the week before. Built every hour once the snapshot time has passed.</div>"
            f"<p><a class='btn' href='{URL}/now/daily'>Latest daily</a> "
            f"<a class='btn' href='{URL}/now/weekly'>This week so far</a> "
            f"<a class='btn alt' href='{URL}/build'>Build now</a></p>"
            f"<form class='rca' action='{URL}/view' method='get'>"
            "<label>Report<select name='kind'><option value='daily'>Daily</option><option value='weekly'>Weekly</option>"
            f"</select></label><label>Day<input type='date' name='date' value='{sg.default_day()}' max='{today}' required></label>"
            "<label>site (optional)<input name='site' placeholder='all, or e.g. SITE0001'></label>"
            "<div class='actions'><button type='submit'>View report</button></div></form>"
            "<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-top:16px'>"
            + "".join(cols) + "</div>")
    return 200, "Endpoint signal check", body, False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for kind in KINDS:
        s = sub.add_parser(kind)
        s.add_argument("--date", help="YYYY-MM-DD")
        s.add_argument("--site", help="one site only (not saved)")
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
    pid, page, c, done = build(a.cmd, when, store=not a.no_save, site=a.site)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(page)
    print(f"{a.cmd} {pid}: " + ", ".join(f"{k}={v}" for k, v in c.items()))
    if done and not a.no_save and not a.site:
        print("Report: " + path(a.cmd, pid))
    elif not done:
        print("Period not finished: not saved" + (f" (copy in {a.out})" if a.out else " (use --out)"))


if __name__ == "__main__":
    main()
