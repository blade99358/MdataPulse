#!/usr/bin/env python3
"""Network health reports for management (daily, weekly, monthly) and a Zabbix admin report.

  python3 tools/zbx_report.py daily   [--date YYYY-MM-DD]   one day (default yesterday)
  python3 tools/zbx_report.py weekly  [--date YYYY-MM-DD]   the week containing that day (default last full week)
  python3 tools/zbx_report.py monthly [--date YYYY-MM]      one calendar month (default last month)
  python3 tools/zbx_report.py admin   [--date YYYY-MM-DD]   Zabbix admin report: blind spots, server health
  python3 tools/zbx_report.py scheduled                     every report that is due and not built yet (cron)
  python3 tools/zbx_report.py rebuild [--since YYYY-MM-DD]  build the daily/weekly/monthly reports again
  python3 tools/zbx_report.py cron [--apply]                install the schedule in /etc/cron.d/mdp-zabbix-reports
  python3 tools/zbx_report.py list                          reports already built

Each report is an HTML page plus a snapshot (JSON) of its numbers, kept in REPORT_DIR/<kind>/.
The next report of the same kind compares itself with that snapshot ("better / worse than last
week"). When the previous snapshot is missing (first run) it is computed from Zabbix trends and saved.

Data: hourly trends (kept 2 years) for availability, radio, UPS and traffic numbers, problem events
for outages and their cause (Outage cause: POWER / CONNECTIVITY triggers), host tags site/role/parent
for the topology. Only reads from Zabbix.

Settings (environment, e.g. in ~/.zabbix_env):
  REPORT_DIR=/var/lib/mdp-reports   where reports and snapshots are written
  REPORT_WEEK_START=sun              first day of the report week (sun, mon, sat ...)
  RCA_TZ=UTC                day boundaries
ZABBIX_URL / ZABBIX_TOKEN as for the other tools; PostgreSQL access: see tools/zbxdb.py.
"""
import argparse
import html
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REPORT_DIR = os.environ.get("REPORT_DIR", "/var/lib/mdp-reports")
TZ = ZoneInfo(os.environ.get("RCA_TZ", "UTC"))
WEEK_START = os.environ.get("REPORT_WEEK_START", "sun").lower()[:3]
LOG = "/var/log/mdp-zabbix-reports.log"
GUARD_HOST = "Zabbix server guard"

# --------------------------------------------------------------- thresholds (see REPORTS.md)
AVAIL_TARGET = 99.9      # % availability target (charts: grey bars below it)
AVAIL_POINTS = 3         # score points lost per 1 % of device-time lost at the site (up to 50)
RSSI_MIN = -75           # dBm, weak signal (Cambium); Ubiquiti Wave uses WAVE_RSSI_MIN
WAVE_RSSI_MIN = -70
SNR_MIN = 15             # dB
RF_DROP = 3              # dB of RSSI or SNR lost since last period = degraded link
MCS_DROP = 1.5           # modulation steps lost since last period = degraded link
CAP_DROP = 0.20          # share of radio capacity lost since last period = degraded link
UPS_RUNTIME_MIN = 10     # minutes of backup below which a UPS is weak
UPS_RUNTIME_DROP = 0.20  # share of backup minutes lost since last period = battery aging
VIN_LOW, VIN_HIGH = 190, 250   # V, mains outside this range = unstable supply (below 50 V = mains off)
UTIL_BUSY, UTIL_FULL = 0.70, 0.90   # peak traffic / port or radio capacity
DOWN_DELAY = 180         # s: "Device unreachable" fires after {$ICMP.DOWN.TIME} = 3m without ping
SITE_DOWN_DELAY = 120    # s: "Site SITExxxx DOWN" fires after {$SITE.DOWN.TIME} = 2m without the main link
GROUP_WINDOW = 10 * 60   # s: device alarms of one site that start this close together are ONE outage
SITE_DOWN_SHARE = 0.8    # this share of a site's devices down together = the whole site is down
DIAG_AFTER = 45 * 60     # an "Outage cause" problem this long after recovery explains the outage
REPEAT_DAYS = 3          # outages on this many different days = repeat offender
LOOKBACK = 7 * 86400     # problems that started this long before the period are read with it
LONG_LOOKBACK = 90 * 86400   # "Device unreachable" problems are read back this far (long outages)

KINDS = ("daily", "weekly", "monthly", "admin")
SNAP_VERSION = 2         # snapshots of older versions counted every device of an outage: not compared with
# hosts left out of the reports: endpoint devices (CPEs, power units); the report is about the site sites
EXCLUDE = re.compile(os.environ.get("REPORT_EXCLUDE", r"^EP-"))
ITEM_PREFIXES = ["wifi.rssi", "wifi.snr", "wifi.ap.rssi[", "wifi.ap.snr[", "wifi.dlmcs", "wifi.ulmcs",
                 "wifi.mcs.tx[", "wifi.mcs.rx[", "wifi.capacity.tx[", "wifi.capacity.rx[", "wifi.mode",
                 "ups.battery.minutes", "ups.battery.charge", "ups.input.voltage", "ups.output.load",
                 "net.if.in[", "net.if.out[", "net.if.speed["]


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line if not sys.stdout.isatty() else msg)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------- periods
def at(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp())


def today():
    return datetime.now(TZ).date()


def week_start(d):
    wd = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(WEEK_START)
    return d - timedelta(days=(d.weekday() - wd) % 7)


class Period:
    """One report period in local time: kind, id (file name), first day, day after the last."""

    def __init__(self, kind, first, after):
        self.kind, self.first, self.after = kind, first, after
        self.a, self.b = at(first), at(after)
        self.days = (after - first).days
        self.id = f"{first:%Y-%m}" if kind == "monthly" else f"{first:%Y-%m-%d}"

    @classmethod
    def of(cls, kind, when=None):
        """The period of that kind containing `when` (a date), default the last finished one."""
        if kind == "monthly":
            if when is None:
                when = today().replace(day=1) - timedelta(days=1)
            first = when.replace(day=1)
            after = (first + timedelta(days=32)).replace(day=1)
            return cls(kind, first, after)
        if kind == "weekly":
            first = week_start(when) if when else week_start(today()) - timedelta(days=7)
            return cls(kind, first, first + timedelta(days=7))
        d = when or today() - timedelta(days=1)
        return cls(kind, d, d + timedelta(days=1))

    def prev(self):
        return Period.of(self.kind, self.first - timedelta(days=1))

    def label(self):
        last = self.after - timedelta(days=1)
        if self.kind == "monthly":
            return f"{self.first:%B %Y}"
        if self.kind == "weekly":
            return f"{self.first:%d %b} to {last:%d %b %Y}"
        return f"{self.first:%A %d %B %Y}"

    def word(self):
        return {"daily": "yesterday", "weekly": "last week", "monthly": "last month"}.get(self.kind, "last report")


def parse_when(kind, s):
    if not s:
        return None
    if kind == "monthly":
        return date.fromisoformat(s[:7] + "-01")
    return date.fromisoformat(s)


# --------------------------------------------------------------- fetch
def chunks(xs, n=2000):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def trend_stats(db, items, a, b):
    """{itemid: [hours, min, avg, max, peak_avg]} from hourly trends in [a, b)."""
    out = {}
    for vt, table in ((0, "trends"), (3, "trends_uint")):
        ids = [i["itemid"] for i in items if int(i["value_type"]) == vt]
        for part in chunks(ids):
            for r in db.rows(f"SELECT itemid, count(*), min(value_min), avg(value_avg), max(value_max), "
                             f"max(value_avg), min(value_min) FILTER (WHERE value_min >= 50) FROM {table} "
                             f"WHERE clock >= {a} AND clock < {b} "
                             f"AND itemid IN ({','.join(part)}) GROUP BY itemid"):
                out[r[0]] = [int(r[1])] + [None if x in (None, "") else float(x) for x in r[2:]]
    return out


def trend_first(db, items, a, b):
    """{itemid: value_avg of the first hour} (for growth, e.g. database size)."""
    out = {}
    for vt, table in ((0, "trends"), (3, "trends_uint")):
        ids = [i["itemid"] for i in items if int(i["value_type"]) == vt]
        for part in chunks(ids):
            for r in db.rows(f"SELECT DISTINCT ON (itemid) itemid, value_avg FROM {table} WHERE clock >= {a} "
                             f"AND clock < {b} AND itemid IN ({','.join(part)}) ORDER BY itemid, clock"):
                out[r[0]] = float(r[1])
    return out


def fetch(api, db, a, b):
    """Everything one management report needs, as plain dicts (so analyze() can be tested offline)."""
    raw_hosts = api.call("host.get", {"output": ["hostid", "host", "name"], "selectTags": "extend",
                                      "monitored_hosts": True})
    hosts, site_hosts = {}, {}
    for h in raw_hosts:
        tags = {t["tag"]: t["value"] for t in h["tags"]}
        if tags.get("sitedown"):
            # host "Site SITExxxx" (zbx_sites.py sitedown): its "Site DOWN" alarm replaces the device alarms
            site_hosts[h["hostid"]] = {"site": tags["sitedown"],
                                       "upstream": sorted(t["value"] for t in h["tags"] if t["tag"] == "upstream")}
            continue
        if not tags.get("site") or EXCLUDE.search(h["host"]):
            continue
        hosts[h["hostid"]] = {"host": h["host"], "name": h["name"], "site": tags["site"],
                              "role": tags.get("role", ""), "vendor": tags.get("vendor", ""),
                              "model": tags.get("model", ""), "parent": tags.get("parent", ""),
                              "mainlink": tags.get("mainlink", "")}
    items = []
    if hosts:
        items = api.call("item.get", {
            "output": ["itemid", "hostid", "key_", "value_type", "lastvalue"], "hostids": list(hosts),
            "search": {"key_": ITEM_PREFIXES}, "searchByAny": True, "startSearch": True, "monitored": True,
            "selectTags": "extend"})
    numeric = [i for i in items if int(i["value_type"]) in (0, 3)]
    stats = trend_stats(db, numeric, a, b)
    events = []
    ids = list(hosts) + list(site_hosts)
    if ids:
        q = {"output": ["eventid", "clock", "name", "severity", "r_eventid"], "hostids": ids,
             "source": 0, "object": 0, "value": 1, "selectTags": "extend", "selectHosts": ["hostid"],
             "sortfield": ["clock"], "sortorder": "ASC"}
        events = api.call("event.get", {**q, "time_from": a - LOOKBACK, "time_till": b + DIAG_AFTER})
        # a device or site down since before that still counts against availability: read its older outage too
        down = {"search": {"name": ["Device unreachable", "Site "]}, "searchByAny": True, "startSearch": True}
        older = api.call("event.get", {**q, **down, "time_from": a - LONG_LOOKBACK, "time_till": a - LOOKBACK - 1})
        open_ids = [p["eventid"] for p in api.call("problem.get", {
            "output": ["eventid"], "hostids": ids, "source": 0, "object": 0, "recent": False,
            "time_till": a - LONG_LOOKBACK - 1, **down})]
        if open_ids:
            older += api.call("event.get", {**q, "eventids": open_ids})
        seen = {e["eventid"] for e in events}
        events = [e for e in older if e["eventid"] not in seen] + events
    rec_ids = [e["r_eventid"] for e in events if e["r_eventid"] != "0"]
    recovery = {}
    for part in chunks(rec_ids, 1000):
        for r in api.call("event.get", {"eventids": part, "output": ["eventid", "clock"]}):
            recovery[r["eventid"]] = int(r["clock"])
    ev = []
    for e in events:
        if not e.get("hosts"):
            continue
        ev.append({"hostid": e["hosts"][0]["hostid"], "clock": int(e["clock"]), "name": e["name"],
                   "end": recovery.get(e["r_eventid"]),
                   "tags": {t["tag"]: t["value"] for t in e.get("tags", [])}})
    trim_site_alarms(api, db, hosts, site_hosts, ev)   # HEALTH-FIX-17
    return {"hosts": hosts, "site_hosts": site_hosts, "items": items, "stats": stats, "events": ev,
            "now": int(time.time())}


def trim_site_alarms(api, db, hosts, site_hosts, ev):
    """HEALTH-FIX-17: a Site DOWN alarm can stay open after the site is back (e.g. its main-link radio lost
    its tag, so the alarm could not clear). Ping history decides: the outage ends when at least half of the
    site's devices answer ping again. Only shortens an alarm, never makes one longer."""
    now = int(time.time())
    todo = [e for e in ev if e["hostid"] in site_hosts
            and (e["tags"].get("alarm") in ("site-down", "site-cutoff") or " DOWN" in e["name"])]
    if not todo:
        return
    by_site = {}
    for hid, h in hosts.items():
        by_site.setdefault(h["site"], []).append(hid)
    ping = {}
    for part in chunks([h for s in {site_hosts[e["hostid"]]["site"] for e in todo} for h in by_site.get(s, [])], 500):
        for i in api.call("item.get", {"output": ["itemid", "hostid"], "hostids": part,
                                       "filter": {"key_": "icmpping"}, "monitored": True}):
            ping[i["hostid"]] = i["itemid"]
    for e in todo:
        ids = [ping[h] for h in by_site.get(site_hosts[e["hostid"]]["site"], []) if h in ping]
        end = e["end"] or now
        start = e["clock"] + 60
        if len(ids) < 2 or end - start < 900:
            continue
        try:
            rows = db.rows(f"SELECT itemid, clock, value FROM history_uint WHERE itemid IN ({','.join(ids)}) "
                           f"AND clock >= {start} AND clock < {end} ORDER BY clock")
        except Exception as x:
            log(f"ping history for {e['name']}: {x}")
            continue
        last, need = {}, (len(ids) + 1) // 2
        for itemid, clock, value in rows:
            last[itemid] = value == "1"
            if sum(last.values()) >= need:
                if int(clock) < end - 600:
                    log(f"{e['name']}: alarm ended {'never' if e['end'] is None else ts(e['end'])}, "
                        f"site answered ping again {ts(int(clock))}: using that")
                    e["end"] = int(clock)
                break


# --------------------------------------------------------------- analysis
def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def r1(x, n=1):
    return None if x is None else round(x, n)


def descendants(hosts_by_name):
    """{host: [all devices that reach the network through it]} from the parent tags."""
    kids = {}
    for n, h in hosts_by_name.items():
        if h["parent"] and h["parent"] in hosts_by_name and h["parent"] != n:
            kids.setdefault(h["parent"], []).append(n)
    memo = {}

    def walk(n, seen):
        if n in memo:
            return memo[n]
        out = []
        for c in kids.get(n, []):
            if c not in seen:
                out += [c] + walk(c, seen | {c})
        memo[n] = out
        return out
    return {n: walk(n, {n}) for n in hosts_by_name}


def site_parents(hosts_by_name):
    """{site: upstream site} from the parent of each site's devices that sit in another site."""
    up = {}
    for h in hosts_by_name.values():
        p = hosts_by_name.get(h["parent"])
        if p and p["site"] != h["site"]:
            up.setdefault(h["site"], p["site"])
    return up


def site_chains(raw, names):
    """{site: [its upstream site, that one's upstream, ...]}: from the Site hosts' upstream tags (main links),
    else from the parent of each site's devices that sit in another site."""
    direct = site_parents(names)
    sh = {x["site"]: set(x["upstream"]) for x in raw.get("site_hosts", {}).values()}
    for site, ups in sh.items():
        for u in ups:
            if sh.get(u) == ups - {u}:      # the upstream site whose own chain is the rest = the direct one
                direct[site] = u
                break
    out = {}
    for site in set(direct) | {h["site"] for h in names.values()}:
        c, s = [], direct.get(site)
        while s and s not in c and s != site:
            c.append(s)
            s = direct.get(s)
        out[site] = c
    return out


def outages(raw, a, b):
    """Outages overlapping [a, b), each counted ONCE, where it started.

    One outage = the alarms of one site that started together (within GROUP_WINDOW of each other, or during
    that site's "Site DOWN" alarm): a site of 4 devices down for 30 minutes is one outage of 30 minutes, not
    4 outages and 2 hours. Its downtime is the time any of them was down (the union), not the sum.
    Alarms in a site cut off by an outage upstream (dependencies usually hide them, a few slip out) belong
    to that upstream outage. scope "site" = Site DOWN alarm or SITE_DOWN_SHARE of the site down together,
    else "devices" (one or a few devices of the site)."""
    hosts = raw["hosts"]
    names = {h["host"]: h for h in hosts.values()}
    desc = descendants(names)
    chain = site_chains(raw, names)
    by_site = {}
    for h in hosts.values():
        by_site.setdefault(h["site"], []).append(h["host"])
    behind_sites = {s: [x for x in by_site if s in chain.get(x, [])] for s in by_site}
    now, end_of = raw["now"], min(b, raw["now"])
    fin = lambda x: now if x is None else x
    spans, diag = {}, {}
    for e in raw["events"]:
        sh = raw.get("site_hosts", {}).get(e["hostid"])
        if sh:
            if (e["tags"].get("alarm") == "site-down" or " DOWN" in e["name"]) and sh["site"] in by_site:
                spans.setdefault(sh["site"], []).append({"start": e["clock"] - SITE_DOWN_DELAY, "end": e["end"], "host": None})
            continue
        h = hosts.get(e["hostid"])
        if not h:
            continue
        if e["name"].startswith("Device unreachable"):
            spans.setdefault(h["site"], []).append({"start": e["clock"] - DOWN_DELAY, "end": e["end"], "host": h["host"]})
        elif e["tags"].get("diagnosis") in ("power", "connectivity"):
            diag.setdefault(h["site"], []).append((e["clock"], e["tags"]["diagnosis"]))

    # 1. group the alarms of each site
    groups = []
    for site, sp in spans.items():
        cur = []
        for x in sorted(sp, key=lambda x: (x["start"], x["host"] is not None)):
            g = next((g for g in cur if x["start"] <= g["last"] + GROUP_WINDOW
                      or (g["sd_end"] is not None and x["start"] <= g["sd_end"] + GROUP_WINDOW)), None)
            if g is None:
                g = {"site": site, "spans": [], "last": x["start"], "sd_end": None}
                cur.append(g)
            g["spans"].append(x)
            g["last"] = max(g["last"], x["start"])
            if x["host"] is None:
                g["sd_end"] = max(g["sd_end"] or 0, fin(x["end"]))
        groups += cur
    for g in groups:
        site = g["site"]
        g["members"] = sorted({x["host"] for x in g["spans"] if x["host"]},
                              key=lambda n: min(x["start"] for x in g["spans"] if x["host"] == n))
        hit = set(g["members"])
        for m in g["members"]:
            hit.update(desc.get(m, []))
        in_site = sum(1 for x in hit if names[x]["site"] == site)
        g["scope"] = "site" if g["sd_end"] is not None or in_site >= SITE_DOWN_SHARE * len(by_site[site]) else "devices"
        if g["scope"] == "site":
            hit.update(by_site[site])
            for s2 in behind_sites[site]:
                hit.update(by_site[s2])
        g["hit"] = hit
        g["union"] = merge([(x["start"], fin(x["end"])) for x in g["spans"]])
        g["start"] = g["union"][0][0]
        g["end"] = None if any(x["end"] is None for x in g["spans"]) else max(x["end"] for x in g["spans"])
        g["sites_behind"] = sorted({names[x]["site"] for x in hit} - {site})
        g["up"] = None

    # 2. a group inside an outage upstream (same time, its devices cut off by it) is part of that outage
    for g in groups:
        cands = [j for j in groups if j["site"] != g["site"] and j["start"] - GROUP_WINDOW <= g["start"] <= fin(j["end"]) + GROUP_WINDOW
                 and (set(g["members"]) <= j["hit"] if g["members"] else g["site"] in j["sites_behind"])]
        if cands:
            g["up"] = max(cands, key=lambda j: len(j["hit"]))
    def root(g):
        seen = set()
        while g["up"] is not None and id(g) not in seen:
            seen.add(id(g))
            g = g["up"]
        return g
    family = {}
    for g in groups:
        family.setdefault(id(root(g)), []).append(g)

    # 3. the outages (root groups) in the period, and the device-time each device lost
    # HEALTH-FIX-3: a site cut off by an outage upstream that is still down GROUP_WINDOW after the upstream came
    # back has an outage of its own from then on (before, all of it was charged to the upstream outage)
    out, lost = [], {}

    def make(scope, site, start, end, union, hit, members, extra):
        minutes = sum(max(0, min(q, end_of) - max(p, a)) for p, q in union) / 60
        cause = "ongoing"
        if end is not None:
            found = [c for t, c in diag.get(site, []) if end - 300 <= t <= end + DIAG_AFTER]
            cause = ("power" if found.count("power") >= found.count("connectivity") else "connectivity") if found else "unknown"
        link = ""
        if scope == "site":
            ml = [h for h in names.values() if h.get("mainlink") == site]
            link = " + ".join(f"{h['parent']} ~ {h['host']}" if h["parent"] else h["host"] for h in ml)
        elif members:
            m0 = names[members[0]]
            link = f"{m0['host']} (behind {m0['parent']})" if m0["parent"] else m0["host"]   # HEALTH-FIX-18
        o = {"link": link, "carried": start < a, "site": site, "scope": scope, "host": members[0] if members else "",
             "hosts": members, "role": names[members[0]]["role"] if members else "",
             "start": start, "end": end, "minutes": round(minutes, 1), "cause": cause,
             "site_devices": len(by_site[site]),
             "down_here": sum(1 for x in hit if names[x]["site"] == site),
             "affected": len(hit), "sites_behind": sorted(s2 for s2 in {names[x]["site"] for x in hit} - {site}
                                                          if scope == "site" or sum(1 for x in hit if names[x]["site"] == s2)
                                                          >= SITE_DOWN_SHARE * len(by_site[s2])),   # HEALTH-FIX-4
             "impact": round(minutes * max(1, len(hit)))}
        o.update(extra)
        return o

    for g in groups:
        if g["up"] is not None or g["start"] >= b or (g["end"] is not None and g["end"] <= a):
            continue
        site = g["site"]
        back = fin(g["end"])
        rend = back + GROUP_WINDOW
        for f in family[id(g)]:
            mine = f is g
            cut = (lambda p, q: (p, q)) if mine else (lambda p, q: (p, min(q, rend)))
            left = []
            for x in f["spans"]:
                p, q = x["start"], fin(x["end"])
                if x["host"]:
                    for d in [x["host"]] + desc.get(x["host"], []):
                        lost.setdefault(d, []).append((*cut(p, q), site))
                if not mine and g["end"] is not None and q > rend:
                    left.append(x)
            if f["scope"] == "site":
                for d in f["hit"]:
                    lost.setdefault(d, []).extend((*cut(p, q), site) for p, q in f["union"])
            if left:
                fs = f["site"]
                un = merge([(back, fin(x["end"])) for x in left])
                mem = sorted({x["host"] for x in left if x["host"]})
                if f["scope"] == "site":
                    hit2 = set(f["hit"])
                    for d in hit2:
                        lost.setdefault(d, []).extend((p, q, fs) for p, q in un)
                else:
                    hit2 = set()
                    for x in left:
                        if x["host"]:
                            for d in [x["host"]] + desc.get(x["host"], []):
                                hit2.add(d)
                                lost.setdefault(d, []).append((back, fin(x["end"]), fs))
                end2 = None if any(x["end"] is None for x in left) else max(x["end"] for x in left)
                if back < b and (end2 is None or end2 > a):
                    out.append(make(f["scope"], fs, back, end2, un, hit2, mem, {"after": site}))
        hit = set().union(*(f["hit"] for f in family[id(g)]))
        out.append(make(g["scope"], site, g["start"], g["end"], g["union"], hit, g["members"], {}))
    # HEALTH-FIX-5: a site outage with no diagnosis: UPS on battery when it went down = power (the UPS ran out),
    # UPS on mains = connectivity. Sites with no monitored UPS stay "unknown".
    bat, ups_sites = {}, set()
    for e in raw["events"]:
        h = hosts.get(e["hostid"])
        if h and e["name"].startswith("UPS: On battery"):
            bat.setdefault(h["site"], []).append((e["clock"], fin(e["end"])))
    for h in hosts.values():
        if h.get("role") == "ups" or "UPS" in h["host"].upper():
            ups_sites.add(h["site"])
    for o in out:
        if o["cause"] == "unknown" and o["scope"] == "site" and o["site"] in ups_sites:
            on_bat = any(p <= o["start"] + 300 and q >= o["start"] - 60 for p, q in bat.get(o["site"], []))
            o["cause"], o["cause_from"] = ("power" if on_bat else "connectivity"), "ups"
    for o in out:   # outages at other sites that started within 2 minutes: likely one common cause
        o["together"] = sorted({x["site"] for x in out if x is not o and x["site"] != o["site"]
                                and abs(x["start"] - o["start"]) <= 120})
    return sorted(out, key=lambda o: o["start"]), lost


def battery_events(raw, a, b):
    out = []
    for e in raw["events"]:
        if not e["name"].startswith("UPS: On battery"):
            continue
        end = e["end"]
        if e["clock"] >= b or (end is not None and end <= a):
            continue
        h = raw["hosts"].get(e["hostid"])
        if h:
            in_b = min(end if end is not None else min(b, raw["now"]), b)
            out.append({"host": h["host"], "site": h["site"], "start": e["clock"], "end": end,
                        "minutes": round(max(0, in_b - max(e["clock"], a)) / 60, 1)})
    return out


def mains_failures(batt):
    """UPS on-battery alarms of one UPS that follow each other within GROUP_WINDOW = ONE mains failure."""
    out = {}
    for x in sorted(batt, key=lambda x: x["start"]):
        L = out.setdefault(x["host"], [])
        if L and (L[-1]["end"] is None or x["start"] <= L[-1]["end"] + GROUP_WINDOW):
            if L[-1]["end"] is not None:
                L[-1]["end"] = None if x["end"] is None else max(L[-1]["end"], x["end"])
        else:
            L.append({"start": x["start"], "end": x["end"], "site": x["site"]})
    return out


def merge(spans):
    out = []
    for x, y in sorted(spans):
        if out and x <= out[-1][1]:
            out[-1][1] = max(out[-1][1], y)
        elif y > x:
            out.append([x, y])
    return out


def per_host_items(raw):
    by = {}
    for it in raw["items"]:
        by.setdefault(it["hostid"], []).append(it)
    return by


def stat(raw, it, col):
    s = raw["stats"].get(it["itemid"])
    i = {"hours": 0, "min": 1, "avg": 2, "max": 3, "peak": 4, "min50": 5}[col]
    return None if not s or len(s) <= i else s[i]


def key_base(k):
    return k.split("[", 1)[0]


def analyze(raw, period, prev=None):
    """The numbers of one period (the snapshot). prev = snapshot of the previous period, for comparison."""
    a, b = period.a, period.b
    hosts = raw["hosts"]
    by_items = per_host_items(raw)
    snap = {"v": SNAP_VERSION, "kind": period.kind, "id": period.id, "from": a, "to": b, "label": period.label(),
            "generated": int(time.time()), "hosts": {}, "radios": {}, "ups": {}, "links": {}, "sites": {},
            "names": {h["host"]: h["name"] for h in hosts.values() if h.get("name") and h["host"] not in h["name"]}}

    outs, lost = outages(raw, a, b)
    chains = site_chains(raw, {h["host"]: h for h in hosts.values()})
    # availability per device = time it had service: down during an outage of its own, of a device in
    # front of it or of its whole site, here or upstream. "own" counts only outages that started at its site.
    end_of = min(b, raw["now"])
    day_cut = [at(period.first + timedelta(days=i)) for i in range(period.days + 1)]
    daily = {}
    for hid, h in hosts.items():
        spans = lost.get(h["host"], [])
        merged = merge([(max(x, a), min(y, end_of)) for x, y, _ in spans])
        mine = merge([(max(x, a), min(y, end_of)) for x, y, s_ in spans if s_ == h["site"]])
        down = sum(y - x for x, y in merged)
        span = end_of - a
        snap["hosts"][h["host"]] = {"site": h["site"], "role": h["role"], "outages": 0, "minutes": 0.0, "days": [],
                                    "avail": round(100 * (1 - down / span), 3) if span > 0 else None,
                                    "avail_own": round(100 * (1 - sum(y - x for x, y in mine) / span), 3) if span > 0 else None}
        if period.days > 1:
            for d0, d1 in zip(day_cut, day_cut[1:]):
                if d0 >= end_of:
                    break
                dd = sum(max(0, min(y, d1) - max(x, d0)) for x, y in merged)
                k = str(datetime.fromtimestamp(d0, TZ).date())
                daily.setdefault(k, []).append(1 - dd / (min(d1, end_of) - d0))
    snap["daily"] = {k: round(100 * sum(v) / len(v), 3) for k, v in daily.items()}

    batt = battery_events(raw, a, b)
    mains = mains_failures(batt)
    # HEALTH-FIX-15: one device restarted while the site's UPS saw no mains failure = it restarted on its own
    # (PoE port, adapter, reboot), not a power failure; at a site with no UPS we cannot tell, it stays power
    ups_sites = {h["site"] for h in hosts.values() if h.get("role") == "ups" or "UPS" in h["host"].upper()}
    msite = {}
    for v in mains.values():
        for x in v:
            msite.setdefault(x["site"], []).append((x["start"], x["end"]))
    for o in outs:
        if o["scope"] == "devices" and o["cause"] == "power" and o["site"] in ups_sites and not any(
                s0 <= o["start"] + 300 and (s1 is None or s1 >= o["start"] - 600) for s0, s1 in msite.get(o["site"], [])):
            o["cause"] = "restart"
    for o in outs:     # top 10 / repeat offenders: the devices where a device outage started
        for n in (o["hosts"] if o["scope"] == "devices" else []):
            hs = snap["hosts"][n]
            mine = merge([(max(x, a), min(y, end_of)) for x, y, s_ in lost.get(n, []) if s_ == o["site"]
                          and o["start"] <= x <= (o["end"] or end_of)])
            hs["outages"] += 1
            hs["minutes"] = round(hs["minutes"] + sum(max(0, y - x) for x, y in mine) / 60, 1)
            hs["days"] = sorted(set(hs["days"]) | {str(datetime.fromtimestamp(max(o["start"], a), TZ).date())})
    snap["outages"] = outs
    snap["battery"] = batt

    # radios
    for hid, h in hosts.items():
        its = {i["key_"]: i for i in by_items.get(hid, [])}
        def avg_of(prefixes, col="avg"):
            return mean([stat(raw, i, col) for k, i in its.items() if any(k == p or k.startswith(p + "[") for p in prefixes)])
        rssi = avg_of(["wifi.rssi"]) if stat(raw, its.get("wifi.rssi", {"itemid": ""}), "avg") is not None else avg_of(["wifi.ap.rssi"])
        snr = avg_of(["wifi.snr"]) if stat(raw, its.get("wifi.snr", {"itemid": ""}), "avg") is not None else avg_of(["wifi.ap.snr"])
        if rssi is None and snr is None:
            continue
        rmin = mean([stat(raw, i, "min") for k, i in its.items() if key_base(k) in ("wifi.rssi", "wifi.ap.rssi")])
        mcs = avg_of(["wifi.dlmcs", "wifi.ulmcs"]) or avg_of(["wifi.mcs.tx", "wifi.mcs.rx"])
        cap = avg_of(["wifi.capacity.tx", "wifi.capacity.rx"])
        limit = WAVE_RSSI_MIN if h["vendor"] == "ubiquiti" else RSSI_MIN
        r = {"site": h["site"], "role": h["role"], "rssi": r1(rssi), "rssi_min": r1(rmin), "snr": r1(snr),
             "mcs": r1(mcs), "cap": r1(cap / 1e6, 1) if cap else None, "weak": False, "degraded": [], "parent": h["parent"]}
        r["weak"] = (rssi is not None and rssi < limit) or (snr is not None and snr < SNR_MIN)
        p = (prev or {}).get("radios", {}).get(h["host"])
        if p:
            for k, lim, txt in (("rssi", RF_DROP, "signal"), ("snr", RF_DROP, "SNR")):
                if r[k] is not None and p.get(k) is not None and p[k] - r[k] >= lim:
                    r["degraded"].append(f"{txt} {p[k]} → {r[k]} dB" + ("m" if k == "rssi" else ""))
            if r["mcs"] is not None and p.get("mcs") is not None and p["mcs"] - r["mcs"] >= MCS_DROP:
                r["degraded"].append(f"modulation {p['mcs']} → {r['mcs']}")
            if r["cap"] and p.get("cap") and (p["cap"] - r["cap"]) / p["cap"] >= CAP_DROP:
                r["degraded"].append(f"capacity {p['cap']} → {r['cap']} Mbps")
        snap["radios"][h["host"]] = r

    # UPS
    for hid, h in hosts.items():
        its = {i["key_"]: i for i in by_items.get(hid, [])}
        if "ups.battery.minutes" not in its and "ups.input.voltage" not in its:
            continue
        g = lambda k, c: stat(raw, its[k], c) if k in its else None
        # HEALTH-FIX-8: the UPS's own runtime estimate is read at full charge (highest hourly average), not
        # averaged over hours of discharge and recharge; mains voltage only while mains was present
        mins = g("ups.battery.minutes", "peak")
        vmin = g("ups.input.voltage", "min50")
        if vmin is None:
            vmin = g("ups.input.voltage", "min")
            vmin = vmin if vmin is not None and vmin >= 50 else None
        u = {"site": h["site"], "minutes": r1(mins), "charge_min": r1(g("ups.battery.charge", "min"), 0),
             "vin_min": r1(vmin, 0) if vmin is not None else None, "vin_max": r1(g("ups.input.voltage", "max"), 0),
             "load_max": r1(g("ups.output.load", "max"), 0), "events": 0, "battery_minutes": 0.0, "issues": []}
        u["periods"] = [[m["start"], m["end"]] for m in mains.get(h["host"], [])]
        u["events"] = len(u["periods"])
        p = (prev or {}).get("ups", {}).get(h["host"])
        tf = "%d %b %Y %H:%M"
        u["ran_out"] = []
        for s0, s1 in u["periods"]:   # on battery, then the site went down on power: the UPS ran out
            o = next((o for o in outs if o["site"] == h["site"] and o["scope"] == "site" and o["cause"] == "power"
                      and s0 <= o["start"] <= (s1 if s1 is not None else end_of) + 600), None)
            if o:
                u["ran_out"].append([s0, o["start"]])
                txt = f"ran out after {dur((o['start'] - s0) / 60)} on battery ({ts(s0, tf)}–{ts(o['start'], '%H:%M' if ts(s0, '%d%m%Y') == ts(o['start'], '%d%m%Y') else tf)})"
                if mins and (o["start"] - s0) / 60 < 0.7 * mins:
                    txt += f", the UPS estimated {dur(mins)}: battery weaker than the UPS reports"
                u["issues"].append(txt)
        if not u["ran_out"] and mins is not None and 0 < mins < UPS_RUNTIME_MIN:
            u["issues"].append(f"the UPS reports only {r1(mins)} min of backup at full charge")
        elif period.kind != "daily" and mins and p and p.get("minutes") and (p["minutes"] - mins) / p["minutes"] >= UPS_RUNTIME_DROP:
            u["issues"].append(f"UPS estimate fell {dur(p['minutes'])} → {dur(mins)} (battery aging)")
        if u["vin_min"] is not None and 50 <= u["vin_min"] < VIN_LOW:
            u["issues"].append(f"low mains voltage ({u['vin_min']:.0f} V)")
        if u["vin_max"] is not None and u["vin_max"] > VIN_HIGH:
            u["issues"].append(f"high mains voltage ({u['vin_max']:.0f} V)")
        if u["load_max"] is not None and u["load_max"] >= 80:
            u["issues"].append(f"load up to {u['load_max']:.0f} %")
        snap["ups"][h["host"]] = u

    # HEALTH-FIX-11 capacity: peak traffic per port against the PORT SPEED only (the capacity a radio reports
    # moves with its signal, so it is not a limit)
    for hid, h in hosts.items():
        if (h.get("vendor") or "").lower() == "linksys":
            continue   # HEALTH-FIX-12: Linksys switches only give PoE power, the data goes through the UniFi
        its = by_items.get(hid, [])
        speed = {}
        for i in its:
            if i["key_"].startswith("net.if.speed["):
                sp = stat(raw, i, "max")   # the speed during the period; else the current one
                speed[re.sub(r"\D", "", i["key_"])] = float(sp if sp not in (None, "") else (i["lastvalue"] or 0))
        ports = {}
        for i in its:
            m = re.match(r"net\.if\.(in|out)\[ifHC(?:In|Out)Octets\.(\d+)\]", i["key_"])
            if not m:
                continue
            pk = stat(raw, i, "max")
            if pk in (None, ""):
                continue
            name = next((t["value"] for t in i.get("tags", []) if t["tag"] == "interface"), m.group(2))
            p = ports.setdefault(name, {"peak": 0.0, "avg": 0.0, "speed": speed.get(m.group(2), 0.0)})
            hr = float(stat(raw, i, "peak") or 0)   # the busiest hour (highest hourly average)
            pk = float(pk)
            if p["speed"] and pk > p["speed"]:
                pk = hr   # more than the port can carry = a bad counter reading: use the busiest hour
            p["peak"] = max(p["peak"], pk)
            p["avg"] = max(p["avg"], hr)
        for name, p in ports.items():
            limit = p["speed"]
            if not limit or p["peak"] <= 0 or p["peak"] > limit:
                continue   # no speed, no traffic, or readings the port cannot carry: no trustworthy number
            snap["links"][f"{h['host']} {name}"] = {
                "host": h["host"], "site": h["site"], "role": h["role"], "port": name, "peak": round(p["peak"] / 1e6, 1),
                "avg": round(p["avg"] / 1e6, 1), "limit": round(limit / 1e6, 1), "what": "port",
                "util": round(p["peak"] / limit, 3)}

    # one row per physical link: ports of a device and of its parent with about the same peak carry the same
    # traffic (both radios of a link and the switch port behind them), so they are one link, shown at its busiest end
    by_name = {h["host"]: h for h in hosts.values()}
    host_ports = {}
    for k, l in snap["links"].items():
        host_ports.setdefault(l["host"], []).append(k)
    grp = {k: k for k in snap["links"]}

    def root_(k):
        while grp[k] != k:
            grp[k] = grp[grp[k]]
            k = grp[k]
        return k
    for hn, ks in host_ports.items():
        par = by_name.get(hn, {}).get("parent")
        for k in ks:
            for q in host_ports.get(par, []):
                x, y = snap["links"][k]["peak"], snap["links"][q]["peak"]
                if max(x, y) > 0 and abs(x - y) <= 0.1 * max(x, y):
                    grp[root_(k)] = root_(q)

    def depth(hn, n=0):
        par = by_name.get(hn, {}).get("parent")
        return n if not par or n > 30 else depth(par, n + 1)
    groups = {}
    for k in snap["links"]:
        groups.setdefault(root_(k), []).append(k)
    snap["caplinks"] = []
    for ks in groups.values():
        top = max(ks, key=lambda k: (snap["links"][k]["util"], k))
        hs = sorted({snap["links"][k]["host"] for k in ks}, key=lambda hn: (depth(hn), hn))
        snap["caplinks"].append(dict(snap["links"][top], key=top, hosts=hs))
    snap["caplinks"].sort(key=lambda l: (-l["util"], l["key"]))

    # sites and the health score
    scale = max(1.0, period.days / 7)
    for code in sorted({h["site"] for h in hosts.values()}):
        hs = [x for x in snap["hosts"].values() if x["site"] == code]
        av = mean([x["avail"] for x in hs])
        av_own = mean([x["avail_own"] for x in hs])
        own = [o for o in outs if o["site"] == code and not o.get("carried")]
        site_outs = [o for o in own if o["scope"] == "site"]
        dev_outs = [o for o in own if o["scope"] == "devices"]
        power = [o for o in own if o["cause"] == "power"]
        conn = [o for o in own if o["cause"] == "connectivity"]
        restart = [o for o in own if o["cause"] == "restart"]
        upstream = [o for o in outs if code in o["sites_behind"]]
        radios = [r for r in snap["radios"].values() if r["site"] == code]
        degraded = [r for r in radios if r["degraded"]]
        weak = [r for r in radios if r["weak"] and not r["degraded"]]
        ups = [u for u in snap["ups"].values() if u["site"] == code]
        bev = sum(u["events"] for u in ups)
        weak_ups = [u for u in ups if u["issues"]]
        links = [l for l in snap["caplinks"] if l["site"] == code]
        full = [l for l in links if l["util"] >= UTIL_FULL]
        busy = [l for l in links if UTIL_BUSY <= l["util"] < UTIL_FULL]
        # each problem costs points once: an outage costs its downtime and its count, nothing else
        pen = {
            "availability": min(50.0, (100 - av_own) * AVAIL_POINTS) if av_own is not None else 0.0,
            "outages": min(20.0, (4 * len(site_outs) + 1 * len(dev_outs)) / scale),
            "power": min(10.0, 5 * len(weak_ups)),
            "radio": 0.0,
            "capacity": 0.0,   # HEALTH-FIX-11: a busy link is not a fault, it only goes to Recommended actions
        }
        score = max(0, round(100 - sum(pen.values())))
        # for the network score the availability is what the users got, cut-offs from upstream included
        service = max(0, round(100 - sum(pen.values()) + pen["availability"]
                               - (min(50.0, (100 - av) * AVAIL_POINTS) if av is not None else 0.0)))
        snap["sites"][code] = {
            "devices": len(hs), "avail": r1(av, 3), "avail_own": r1(av_own, 3), "score": score if av is not None else None,
            "service_score": service if av is not None else None,
            "penalties": {k: round(v, 1) for k, v in pen.items()}, "outages": len(own), "site_outages": len(site_outs),
            "device_outages": len(dev_outs), "power": len(power), "connectivity": len(conn),
            "unknown": len(own) - len(power) - len(conn) - len(restart), "restart": len(restart), "upstream": len(upstream),
            "upstream_minutes": round(sum(o["minutes"] for o in upstream), 1),
            "minutes": round(sum(o["minutes"] for o in site_outs), 1),
            "device_minutes": round(sum(o["minutes"] for o in dev_outs), 1),
            "days_down": len({str(datetime.fromtimestamp(max(o["start"], a), TZ).date()) for o in site_outs}),
            "impact": sum(o["impact"] for o in own),
            "radios": len(radios), "degraded": len(degraded), "weak": len(weak), "ups": len(ups),
            "battery_events": bev, "weak_ups": len(weak_ups), "busy_links": len(busy), "full_links": len(full),
            "upstream_site": (chains.get(code) or [""])[0]}

    sites = [s for s in snap["sites"].values() if s["score"] is not None]
    tot = sum(s["devices"] for s in sites)
    so = [o for o in outs if o["scope"] == "site" and not o.get("carried")]
    do = [o for o in outs if o["scope"] == "devices" and not o.get("carried")]
    snap["network"] = {
        "score": round(sum(s["service_score"] * s["devices"] for s in sites) / tot) if tot else None,
        "avail": r1(mean([x["avail"] for x in snap["hosts"].values()]), 3),
        "devices": len(snap["hosts"]), "sites": len(snap["sites"]),
        "outages": len(outs), "site_outages": len(so), "device_outages": len(do),
        "power": sum(1 for o in so if o["cause"] == "power"),
        "connectivity": sum(1 for o in so if o["cause"] == "connectivity"),
        "unknown": sum(1 for o in so if o["cause"] == "unknown"),
        "ongoing": sum(1 for o in so if o["cause"] == "ongoing"),
        "sites_down": len({o["site"] for o in so}),
        "minutes": round(sum(o["minutes"] for o in so), 1),
        "device_minutes": round(sum(o["minutes"] for o in do), 1),
        "sites_cut": sum(len(o["sites_behind"]) for o in so),
        "impact": sum(o["impact"] for o in outs),
        "battery_events": len(batt), "mains": sum(len(v) for v in mains.values()),
        "mains_sites": len({x["site"] for v in mains.values() for x in v}),
        "carried": [[o["site"], o["start"]] for o in outs if o.get("carried") and o["scope"] == "site"],
        "degraded": sum(1 for r in snap["radios"].values() if r["degraded"]),
        "weak": sum(1 for r in snap["radios"].values() if r["weak"]),
        "weak_ups": sum(1 for u in snap["ups"].values() if u["issues"]),
        "busy_links": sum(1 for l in snap["caplinks"] if l["util"] >= UTIL_BUSY), "links_checked": len(snap["caplinks"])}
    snap["repeat"] = sorted([n for n, x in snap["hosts"].items()
                             if len(x["days"]) >= (REPEAT_DAYS if period.days > 1 else 99) or
                             (period.days == 1 and x["outages"] >= 2)],
                            key=lambda n: -snap["hosts"][n]["outages"])
    snap["repeat_sites"] = sorted([c for c, x in snap["sites"].items()
                                   if (x["days_down"] >= REPEAT_DAYS if period.days > 1 else x["site_outages"] >= 2)],
                                  key=lambda c: -snap["sites"][c]["site_outages"])
    return snap


def status_of(score):
    if score is None:
        return "nodata", "NO DATA"
    return ("ok", "GOOD") if score >= 90 else ("warn", "FAIR") if score >= 75 else ("bad", "POOR")


def changes(snap, prev):
    """HEALTH-FIX-2: what happened, worst first, as facts: which site, what, when (each period, no totals)."""
    kind = snap["kind"]
    w = {"daily": "the day before", "weekly": "last week", "monthly": "last month"}[kind]
    this = {"daily": "this day", "weekly": "this week", "monthly": "this month"}[kind]
    psites = (prev or {}).get("sites", {})
    def per(o):
        a_, b_ = t_from(o["start"], snap), t_to(o["end"], snap)
        if o["end"] and ts(o["start"], "%d %b") == ts(o["end"], "%d %b"):
            b_ = ts(o["end"], "%H:%M")
        return f"{a_} to {b_}"
    times = lambda k: "once" if k == 1 else "twice" if k == 2 else f"{k} times"

    def when(os_):
        if len(os_) <= 3:
            return ", ".join(per(o) for o in os_)
        return f"{times(len(os_))} between {t_from(os_[0]['start'], snap)} and {t_to(os_[-1]['end'], snap)}"

    # UPS on-battery periods per site (to tell power cuts the UPS rode through from the ones that took the site down)
    ups_p = {}
    for u in snap["ups"].values():
        for s0, s1 in u.get("periods", []):
            ups_p.setdefault(u["site"], []).append((s0, s1))
    used = set()
    bad, warn, good = [], [], []
    outs = sorted([o for o in snap["outages"] if not o.get("carried")], key=lambda o: o["start"])
    by = {}
    for o in outs:
        by.setdefault((o["site"], o["scope"], "connectivity" if o["cause"] == "connectivity" else o["cause"]), []).append(o)
    for (site, scope, cause), os_ in by.items():
        behind = sorted({s for o in os_ for s in o["sites_behind"]})
        tail = (f" {', '.join(behind)} went down with it." if behind else "")
        rep = " It also went down " + w + " (repeat)." if psites.get(site, {}).get("site_outages") and scope == "site" else ""
        if scope == "site":
            if cause == "power":
                ups = []
                for o in os_:
                    bs = [x for x in ups_p.get(site, []) if x[0] <= o["start"] + 300 and (x[1] is None or x[1] >= o["start"] - 600)]
                    used.update(bs)
                    if bs:
                        b0 = min(x[0] for x in bs)
                        ups.append(t_from(b0, snap) + (f", it lasted {dur((o['start'] - b0) / 60)}" if o["start"] - b0 >= 60 else ""))
                txt = f"{site} lost power and went down {when(os_)}." + (f" UPS on battery from {', '.join(ups)}." if ups else
                                                                        (" No UPS is monitored there." if site not in ups_p else ""))
            elif cause == "ongoing":
                txt = f"{site} went down at {t_from(os_[0]['start'], snap)} and is still down."
            else:
                link = os_[0].get("link")
                why = "lost its main link" if cause == "connectivity" else "went down (cause not known)"
                txt = f"{site} {why} {when(os_)}" + (f" ({link})" if link and cause == "connectivity" else "") + "."
            aft = sorted({o["after"] for o in os_ if o.get("after")})
            if aft:
                txt += f" It stayed down after {', '.join(aft)} came back."
            tog = sorted({s for o in os_ for s in o.get("together", [])})
            if tog:
                txt += f" {', '.join(tog)} went down at the same time{' (once)' if len(os_) > 1 and sum(1 for o in os_ if o.get('together')) == 1 else ''}: look for a common cause."
            bad.append((len(os_) * 1000 + sum(o["affected"] for o in os_), "bad", txt + tail + rep))
        else:
            devs = sorted({h for o in os_ for h in o["hosts"]})
            who = devs[0] if len(devs) == 1 else f"{len(devs)} devices ({', '.join(devs[:3])}{', ...' if len(devs) > 3 else ''})"
            aft = sorted({o["after"] for o in os_ if o.get("after")})
            warn.append((len(os_), "warn", f"{site}: {who} " + ("restarted on its own (mains was on)" if cause == "restart" else "failed alone")
                         + f" {when(os_)}, the rest of the site stayed up." + tail
                         + (f" It stayed down after {', '.join(aft)} came back." if aft else "")))
    for c, t in snap["network"].get("carried") or []:
        bad.append((10 ** 6, "bad", f"{c} is still down since {ts(t, '%d %b %H:%M')}."))
    for s, ps in sorted(ups_p.items()):
        rode = sorted(x for x in ps if x not in used)
        if rode:
            warn.append((len(rode) + 0.5, "warn", f"{s}: mains power failed {times(len(rode))}"
                         + (f" ({', '.join(t_from(a_, snap) + ' to ' + t_to(b_, snap, 'still on battery') for a_, b_ in rode)})" if len(rode) <= 3 else "")
                         + ", the UPS kept the site up."))
    for h, u in snap["ups"].items():
        for i in u["issues"]:
            warn.append((5, "warn", f"UPS {h} ({u['site']}): {i}."))
    for h, r in snap["radios"].items():
        pass
    if prev:
        for code, ps in psites.items():
            s = snap["sites"].get(code)
            if s and ps.get("site_outages") and not s["site_outages"] and not s["device_outages"]:
                good.append((ps["site_outages"], "good", f"{code}: no outage {this} (it went down {times(ps['site_outages'])} {w})."))
    lines = [(c, t) for _, c, t in sorted(bad, key=lambda x: -x[0])] + [(c, t) for _, c, t in sorted(warn, key=lambda x: -x[0])]
    more = len(lines) - 8
    lines = lines[:8] + [(c, t) for _, c, t in sorted(good, key=lambda x: -x[0])[:2]]
    if more > 0:
        lines.append(("warn", f"And {more} more: see Outages, Connectivity and Power below."))
    n, p = snap["network"], (prev or {}).get("network", {})
    if n["avail"] is not None:
        if p.get("avail") is not None and abs(n["avail"] - p["avail"]) >= 0.01:
            up = n["avail"] > p["avail"]
            lines.append(("good" if up else "bad", f"Availability {n['avail']:.2f}%, {'up' if up else 'down'} from {p['avail']:.2f}% {w}."))
        else:
            lines.append(("good", f"Availability {n['avail']:.2f}%" + (f", the same as {w}." if p.get("avail") is not None else ".")))
    if not bad and not warn:
        lines.insert(0, ("good", f"No outage or power failure {this}."))
    return lines


WHY = {"availability": "downtime at the site", "outages": "outages at the site", "power": "UPS problems",
       "radio": "weaker radio links", "capacity": "links close to full"}


# --------------------------------------------------------------- admin report
def fetch_admin(api, db, a, b):
    hosts = api.call("host.get", {"output": ["hostid", "host", "status"], "selectTags": "extend",
                                  "selectInterfaces": ["type", "available", "error", "errors_from"],
                                  "selectMacros": ["macro", "value"]})
    now = int(time.time())
    out = {"version": api.version(), "now": now}
    mon = [h for h in hosts if h["status"] == "0"]
    out["hosts"] = len(mon)
    out["disabled"] = sorted(h["host"] for h in hosts if h["status"] == "1")
    out["no_site"] = sorted(h["host"] for h in mon if h["host"] not in (GUARD_HOST, "Zabbix server")
                            and not any(t["tag"] == "site" for t in h["tags"]))
    snmp_down = []
    for h in mon:
        for i in h.get("interfaces", []):
            if i["type"] == "2" and i["available"] == "2":
                snmp_down.append({"host": h["host"], "error": i["error"], "since": int(i["errors_from"] or 0)})
    out["snmp_down"] = sorted(snmp_down, key=lambda x: x["since"] or now)
    out["ping_only"] = sorted(h["host"] for h in mon if any(m["macro"] == "{$SNMP.ALERT}" and m["value"] == "0"
                                                              for m in h.get("macros", [])))
    hid = {h["hostid"]: h["host"] for h in mon}
    ping = api.call("item.get", {"output": ["hostid", "lastclock"], "filter": {"key_": "icmpping"}, "monitored": True})
    out["silent"] = sorted(hid.get(i["hostid"], "?") for i in ping if int(i["lastclock"] or 0) < now - 900)
    uns = api.call("item.get", {"output": ["hostid", "key_", "error"], "filter": {"state": 1}, "monitored": True})
    by_key, by_host = {}, {}
    for i in uns:
        k = re.sub(r"\[.*", "[...]", i["key_"])
        e = by_key.setdefault(k, {"count": 0, "error": i["error"]})
        e["count"] += 1
        by_host[hid.get(i["hostid"], "?")] = by_host.get(hid.get(i["hostid"], "?"), 0) + 1
    out["unsupported"] = len(uns)
    out["unsupported_keys"] = sorted(by_key.items(), key=lambda kv: -kv[1]["count"])[:15]
    out["unsupported_hosts"] = sorted(by_host.items(), key=lambda kv: -kv[1])[:10]
    stale = api.call("problem.get", {"output": ["eventid", "clock", "name", "objectid", "acknowledged"],
                                     "time_till": now - 7 * 86400, "recent": False, "acknowledged": False,
                                     "sortfield": ["eventid"], "sortorder": "ASC"})
    trig = {}
    for part in chunks([p["objectid"] for p in stale], 500):
        for t in api.call("trigger.get", {"triggerids": part, "output": ["triggerid"], "selectHosts": ["host"]}):
            trig[t["triggerid"]] = t["hosts"][0]["host"] if t["hosts"] else "?"
    out["stale"] = [{"host": trig.get(p["objectid"], "?"), "name": p["name"], "since": int(p["clock"])} for p in stale]
    guard = api.call("host.get", {"output": ["hostid"], "filter": {"host": [GUARD_HOST]}})
    out["guard"] = None
    if guard:
        gid = guard[0]["hostid"]
        items = api.call("item.get", {"output": ["itemid", "key_", "value_type", "lastvalue", "lastclock", "name"],
                                      "hostids": [gid]})
        st = trend_stats(db, [i for i in items if int(i["value_type"]) in (0, 3)], a, b)
        first = trend_first(db, [i for i in items if i["key_"] == "mdp.db.size"], a, b)
        g = {}
        for i in items:
            s = st.get(i["itemid"])
            g[i["key_"]] = {"name": i["name"], "last": float(i["lastvalue"] or 0) if int(i["lastclock"] or 0) else None,
                            "lastclock": int(i["lastclock"] or 0), "max": s[3] if s else None,
                            "min": s[1] if s else None, "avg": s[2] if s else None, "first": first.get(i["itemid"])}
        out["guard"] = g
        evs = api.call("event.get", {"output": ["clock", "name", "r_eventid"], "hostids": [gid], "source": 0,
                                     "object": 0, "value": 1, "time_from": a, "time_till": b})
        cnt = {}
        for e in evs:
            cnt[e["name"]] = cnt.get(e["name"], 0) + 1
        out["guard_problems"] = sorted(cnt.items(), key=lambda kv: -kv[1])
    return out


# --------------------------------------------------------------- HTML
CSS = """
:root{--bg:#fff;--fg:#1d2433;--muted:#667085;--line:#e4e7ec;--card:#f8f9fb;--accent:#2563eb;
--ok:#067647;--ok-bg:#dcfae6;--warn:#b54708;--warn-bg:#fef0c7;--bad:#b42318;--bad-bg:#fee4e2;--bar:#2563eb;--bar2:#98a2b3}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--fg:#e6e8eb;--muted:#98a2b3;--line:#2a2f37;--card:#181c22;
--accent:#6ea8ff;--ok:#47cd89;--ok-bg:#0f2e1e;--warn:#fdb022;--warn-bg:#3a2a10;--bad:#f97066;--bad-bg:#3a1517;--bar:#6ea8ff;--bar2:#667085}}
*{box-sizing:border-box}body{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;padding:24px 16px}
main{max-width:1100px;margin:auto}h2{font-size:17px;margin:32px 0 6px}h3{font-size:14px;margin:18px 0 6px}
.muted{color:var(--muted)}.small{font-size:12px}
.rhead{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;border-bottom:2px solid var(--fg);padding-bottom:10px;flex-wrap:wrap}
.kicker{text-transform:uppercase;letter-spacing:.08em;font-size:12px;color:var(--muted)}.rhead h1{font-size:26px;margin:2px 0}
.hero{display:grid;grid-template-columns:220px 1fr;gap:16px;margin:20px 0}
.score{border-radius:12px;padding:16px;text-align:center;border:1px solid var(--line)}
.score b{display:block;font-size:52px;line-height:1.1}.score .tag{font-weight:700;letter-spacing:.06em;font-size:13px}
.ok{background:var(--ok-bg)}.ok .tag,.ok b{color:var(--ok)}.warn{background:var(--warn-bg)}.warn .tag,.warn b{color:var(--warn)}
.bad{background:var(--bad-bg)}.bad .tag,.bad b{color:var(--bad)}.nodata{background:var(--card)}
.kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}section.kpis{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.kpis div{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.kpis b{display:block;font-size:22px;line-height:1.25}.kpis span{color:var(--muted);font-size:12px}
.d{font-size:12px;font-weight:600;white-space:nowrap}.d.up{color:var(--ok)}.d.down{color:var(--bad)}.d.same{color:var(--muted)}
ul.changes{list-style:none;padding:0;margin:8px 0}ul.changes li{padding:6px 10px 6px 30px;position:relative;border-bottom:1px solid var(--line)}
ul.changes li:before{position:absolute;left:8px;font-weight:700}ul.changes li.good:before{content:"▲";color:var(--ok)}
ul.changes li.bad:before{content:"▼";color:var(--bad)}ul.changes li.warn:before{content:"●";color:var(--warn)}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px}td.n,th.n{text-align:right;white-space:nowrap}.wrap{overflow-x:auto}
.pill{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.05em;padding:1px 8px;border-radius:999px}.chip{display:inline-block;white-space:nowrap;font-size:12px;line-height:1.35;padding:1px 7px;margin:2px 3px 2px 0;border-radius:6px;background:var(--card);border:1px solid var(--line)}.chip.bad{border-color:var(--bad)}.chip.unk{color:var(--muted);border-style:dashed}.nw{white-space:nowrap}
.pill.ok{color:var(--ok)}.pill.warn{color:var(--warn)}.pill.bad{color:var(--bad)}.pill.nodata{color:var(--muted)}
.chart{display:block;width:100%;height:auto}.chart .b{fill:var(--bar)}.chart .b2{fill:var(--bar2)}.chart .sok{fill:var(--ok)}.chart .swarn{fill:var(--warn)}.chart .sbad{fill:var(--bad)}.chart .b:hover,.chart .b2:hover{opacity:.75}
.chart text{fill:var(--muted);font-size:11px}.chart line{stroke:var(--line)}
.note{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;margin:10px 0}
.empty{color:var(--muted);padding:6px 0}
@media (max-width:640px){.hero{grid-template-columns:1fr}.kpis{grid-template-columns:1fr 1fr}.score b{font-size:40px}}
@media print{body{padding:0}.wrap{overflow:visible}h2{break-after:avoid}table{break-inside:auto}tr{break-inside:avoid}}
"""


def e(x):
    return html.escape(str(x))


def ts(c, fmt="%Y-%m-%d %H:%M"):
    return "-" if c is None else datetime.fromtimestamp(c, TZ).strftime(fmt)


def dur(minutes):
    m = int(round(minutes))
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if h < 48 else f"{h // 24} d {h % 24} h"


def t_from(c, snap):
    """Start time: hour only on a daily report when it is that day, else with the date."""
    return ts(c, "%H:%M" if snap["kind"] == "daily" and c >= snap["from"] else "%d %b %H:%M")


def site_cell(os_, snap, by=False):
    """Sites table cell: how many times, and on a daily report the periods when 3 or fewer (no summed time)."""
    if not os_:
        return "-"
    os_ = sorted(os_, key=lambda o: o["start"])
    head = f"{len(os_)}" + (f" <span class='muted small'>by {e(', '.join(sorted({o['site'] for o in os_})))}</span>" if by else "")
    if snap["kind"] == "daily" and len(os_) <= 3:
        head += "<br><span class='muted small'>" + "<br>".join(
            f"{t_from(o['start'], snap)} to {t_to(o['end'], snap)}" for o in os_) + "</span>"
    return head


def t_to(c, snap, still="still down"):
    if c is None:
        return still
    return ts(c, "%H:%M" if snap["kind"] == "daily" and c < snap["to"] else "%d %b %H:%M")


def delta(cur, old, unit="", higher_good=True, digits=0, pct=False):
    """'▲ 3' style change against the previous period, coloured by whether it is good."""
    if cur is None or old is None:
        return ""
    d = cur - old
    if abs(d) < (0.5 * 10 ** -digits):
        return "<span class='d same'>= same</span>"
    good = (d > 0) == higher_good
    txt = f"{abs(d):.{digits}f}{unit}"
    return f"<span class='d {'up' if good else 'down'}'>{'▲' if d > 0 else '▼'} {txt}</span>"


def page(title, kicker, heading, sub, body):
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{e(title)}</title><style>{CSS}</style></head><body><main>"
            f"<header class='rhead'><div><div class='kicker'>{e(kicker)}</div><h1>{e(heading)}</h1>"
            f"<div class='muted'>{e(sub)}</div></div><div class='muted small'>Generated {ts(int(time.time()))} ({TZ})</div></header>"
            f"{body}</main></body></html>")


def bars(points, unit="", lo=None, hi=None, h=150, fmt="{:.2f}", target=None, status=False):
    """Simple single-series bar chart, inline SVG; hover shows the value."""
    if not points:
        return "<div class='empty'>Not enough reports yet for a trend.</div>"
    vals = [v for _, v in points if v is not None]
    lo = min(vals) if lo is None else lo
    hi = max(vals) if hi is None else hi
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    w, left, bottom = 1000, 44, 22
    bw = (w - left) / len(points)
    out = [f"<div class='wrap'><svg class='chart' viewBox='0 0 {w} {h}' role='img' style='min-width:560px'>"]
    for frac in (0, 0.5, 1):
        y = (h - bottom) - frac * (h - bottom - 8)
        out.append(f"<line x1='{left}' x2='{w}' y1='{y:.1f}' y2='{y:.1f}'/>"
                   f"<text x='{left - 6}' y='{y + 4:.1f}' text-anchor='end'>{fmt.format(lo + frac * (hi - lo))}</text>")
    step = max(1, len(points) // 10)
    for i, (lab, v) in enumerate(points):
        x = left + i * bw + max(2, bw * 0.15)
        if v is not None:
            bh = max(2, (v - lo) / (hi - lo) * (h - bottom - 8))
            cls = "b" if target is None or v >= target else "b2"
            if status:
                cls = "s" + status_of(v)[0]
            out.append(f"<rect class='{cls}' x='{x:.1f}' y='{h - bottom - bh:.1f}' width='{max(bw * 0.7, 2):.1f}' "
                       f"height='{bh:.1f}' rx='3'><title>{e(lab)}: {fmt.format(v)}{unit}</title></rect>")
            if status:
                ty, st = h - bottom - bh - 4, ""
                if ty < 11:
                    ty, st = h - bottom - bh + 13, " style='fill:#fff'"
                out.append(f"<text x='{x + bw * 0.35:.1f}' y='{ty:.1f}' text-anchor='middle'{st}>{fmt.format(v)}</text>")
        if i % step == 0:
            out.append(f"<text x='{x + bw * 0.35:.1f}' y='{h - 6}' text-anchor='middle'>{e(lab)}</text>")
    out.append("</svg></div>")
    return "".join(out)


def mbps(v):
    return f"{v / 1000:g} Gbps" if v >= 1000 else f"{v:g} Mbps"


def table(head, rows, empty="Nothing to report."):
    if not rows:
        return f"<div class='empty'>{e(empty)}</div>"
    th = "".join(f"<th{' class=n' if h.startswith('#') else ''}>{e(h.lstrip('#'))}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td{' class=n' if head[i].startswith('#') else ''}>{c}</td>"
                                    for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f"<div class='wrap'><table><tr>{th}</tr>{body}</table></div>"


CAUSE = {"power": "<span class='pill warn'>POWER</span>", "connectivity": "<span class='pill bad'>CONNECTIVITY</span>",
         "unknown": "<span class='pill nodata'>NOT KNOWN</span>", "restart": "<span class='pill warn'>RESTARTED</span>", "ongoing": "<span class='pill bad'>STILL DOWN</span>"}


def render(snap, prev, history):
    """The management report. history = earlier snapshots of the same kind, oldest first."""
    n, p = snap["network"], (prev or {}).get("network", {})
    kind = snap["kind"]
    vs = {"daily": "the day before", "weekly": "last week", "monthly": "last month"}[kind]
    cls, tag = status_of(n["score"])
    k = []
    k.append(f"<div><b>{n['avail']:.2f}%</b><span>availability</span> {delta(n['avail'], p.get('avail'), '%', True, 2)}</div>"
             if n["avail"] is not None else "<div><b>-</b><span>availability</span></div>")
    split = [f"{n.get(c, 0)} {w}" for c, w in (("power", "power"), ("connectivity", "connectivity"),
                                                  ("unknown", "cause not known"), ("ongoing", "still down")) if n.get(c)]
    so_txt = f", {n['site_outages']} time{'s' if n['site_outages'] != 1 else ''}" if n["site_outages"] else ""
    k.append(f"<div><b>{n['sites_down']}</b><span>of {n['sites']} sites went down{so_txt}"
             + (f" ({', '.join(split)})" if split else "")
             + f"</span> {delta(n['sites_down'], p.get('sites_down'), '', False)}</div>")
    car = n.get("carried") or []
    k.append(f"<div><b>{len(car)}</b><span>" + (("still down from before: " + ", ".join(
        f"{e(c)} since {ts(t, '%d %b %H:%M')}" for c, t in car)) if car else "sites still down from before") + "</span></div>")
    k.append(f"<div><b>{n['device_outages']}</b><span>devices that failed alone (their site stayed up)</span> "
             f"{delta(n['device_outages'], p.get('device_outages'), '', False)}</div>")
    k.append(f"<div><b>{n['weak_ups']}</b><span>UPS to check (short backup, aging battery, bad voltage or high load)</span></div>")
    mn = n.get("mains", n["battery_events"])
    k.append(f"<div><b>{mn}</b><span>mains power failures (UPS on battery)"
             + (f" at {n.get('mains_sites', 0)} site{'s' if n.get('mains_sites', 0) != 1 else ''}" if mn else "")
             + f"</span> {delta(mn, p.get('mains'), '', False)}</div>")
    body = [f"<section class='hero'><div class='score {cls}'><div class='tag'>{tag}</div><b>{n['score'] if n['score'] is not None else '-'}</b>"
            f"<div class='muted small'>network health score / 100</div>{delta(n['score'], p.get('score'))}"
            f"<div class='muted small'>vs {vs}</div></div><div class='kpis'>{''.join(k)}</div></section>"]
    ch = changes(snap, prev)
    body.append("<h2>What changed</h2><ul class='changes'>" + "".join(f"<li class='{c}'>{e(t)}</li>" for c, t in ch) + "</ul>")
    if not prev:
        body.append("<div class='note small'>No earlier report to compare with.</div>")

    # trend
    hist = (history or []) + [snap]
    if kind != "daily" or len(hist) > 1:
        lab = (lambda s: s["id"][5:]) if kind != "monthly" else (lambda s: s["id"])
        body.append(f"<h2>Health score, last {len(hist)} {kind[:-2] if kind != 'daily' else 'day'} reports</h2>")
        body.append("<div class='muted small'>Green = GOOD (90+), amber = FAIR (75 to 89), red = POOR (below 75).</div>")
        body.append(bars([(lab(s), s["network"]["score"]) for s in hist], "", 0, 100, fmt="{:.0f}", target=90, status=True))
    if snap.get("daily"):
        body.append("<h2>Availability per day</h2><div class='muted small'>Grey bars are days below the "
                    f"{AVAIL_TARGET}% target.</div>")
        vals = list(snap["daily"].values())
        body.append(bars([(d[5:], v) for d, v in snap["daily"].items()], "%", min(min(vals), 99) - 0.2, 100,
                         target=AVAIL_TARGET))

    # sites
    rows = []
    for code, s in sorted(snap["sites"].items(), key=lambda kv: (kv[1]["score"] is None, kv[1]["score"] or 0)):
        ps = (prev or {}).get("sites", {}).get(code, {})
        c, t = status_of(s["score"])
        rows.append([f"<b>{e(code)}</b><br><span class='muted small'>{s['devices']} device{'s' if s['devices'] != 1 else ''}"
                     + (f", via {e(s['upstream_site'])}" if s["upstream_site"] else "") + "</span>",
                     f"<span class='pill {c}'>{t}</span> <b>{s['score'] if s['score'] is not None else '-'}</b> {delta(s['score'], ps.get('score'))}",
                     (f"{s['avail']:.2f}% {delta(s['avail'], ps.get('avail'), '%', True, 2)}"
                      + ("<br><span class='muted small'>(cut off)</span>" if s["upstream"] and (s.get("avail_own") or 0) > s["avail"] else ""))
                     if s["avail"] is not None else "-",
                     site_cell([o for o in snap["outages"] if o["site"] == code and o["scope"] == "site" and not o.get("carried")], snap),
                     site_cell([o for o in snap["outages"] if o["site"] == code and o["scope"] == "devices" and not o.get("carried")], snap),
                     site_cell([o for o in snap["outages"] if code in o["sites_behind"]], snap, by=True),
                     (", ".join(x for x in (f"{s['battery_events']} mains failure{'s' if s['battery_events'] != 1 else ''}" if s["battery_events"] else "",
                                            f"{s['weak_ups']} UPS to check" if s["weak_ups"] else "") if x) or "-") if s["ups"] else "<span class='muted'>no UPS monitored</span>",
                     f"{s['full_links'] + s['busy_links']}" if s["full_links"] + s["busy_links"] else "-"])
    body.append("<h2>Sites</h2><div class='muted small'>Worst first. Site down = the whole site lost service (counted once, "
                "however many devices it has). Device outages = one or a few devices of the site. Cut off = an outage at "
                "another site took this one down: it lowers the availability shown here but not this site's score.</div>")
    body.append(table(["Site", "Score", "Availability", "Site down", "Device outages", "Cut off by upstream",
                       "Power / UPS", "#Busy links"], rows))

    # outages and topology impact
    outs = sorted(snap["outages"], key=lambda o: (o["scope"] != "site", -o["impact"]))
    body.append("<h2>Outages and their impact</h2><div class='muted small'>Each outage once, at the site where it "
                "started. From = first device down, To = last one back. Devices affected = the devices "
                "that lost service, here and in the sites behind.</div>")

    def where(o):
        if o["scope"] == "site":
            w = f"<b>{e(o['site'])}</b> <span class='pill bad'>SITE DOWN</span><br><span class='muted small'>{o['down_here']} of {o['site_devices']} devices"
        else:
            w = (f"<b>{e(o['site'])}</b> {e(', '.join(o['hosts'][:3]))}" + (f" +{len(o['hosts']) - 3}" if len(o["hosts"]) > 3 else "")
                 + f"<br><span class='muted small'>{o['down_here']} of {o['site_devices']} devices")
        if o.get("after"):
            w += f"<br>stayed down after {e(o['after'])} came back"
        if o.get("together"):
            w += f"<br>together with {e(', '.join(o['together']))}"
        return w + "</span>"
    if kind == "daily":
        shown = outs[:100]
    else:
        so_ = [o for o in outs if o["scope"] == "site"]
        shown = so_ + [o for o in outs if o["scope"] != "site"][:max(0, 25 - len(so_))]
    hidden = len(outs) - len(shown)
    shown = sorted(shown, key=lambda o: o["start"])
    rows = [[t_from(o["start"], snap), t_to(o["end"], snap), dur(o["minutes"]) if o["end"] else "-", where(o),
             CAUSE[o["cause"]] + (("<br><span class='muted small'>UPS stayed on mains</span>" if o["cause"] == "connectivity" else  # HEALTH-FIX-16
                                   "<br><span class='muted small'>UPS was on battery</span>") if o.get("cause_from") == "ups" else ""),
             e(o.get("link") or "-") if o["cause"] != "power" else "-",
             f"{o['affected']}" + (f"<br><span class='muted small'>sites {e(', '.join(o['sites_behind']))}</span>" if o["sites_behind"] else "")]
            for o in shown]
    body.append(table(["From", "To", "Lasted", "Where it started", "Cause", "Where it dropped", "#Devices affected"], rows,
                      "No outage in this period."))
    if hidden > 0:
        body.append(f"<div class='muted small'>And {hidden} more single-device outages (every site outage is listed).</div>")

    # HEALTH-FIX-9: Connectivity and Power as tables, one time chip per period (nothing added up)
    chip = lambda txt, cls="": f"<span class='chip {cls}'>{txt}</span>"
    span = lambda a_, b_, still="still down": f"{t_from(a_, snap)}–{t_to(b_, snap, still)}"

    def pspan(a_, b_, still="still down"):   # power: always the full date (evidence for the power company)
        end = still if b_ is None else ts(b_, "%H:%M" if ts(a_, "%d%m%Y") == ts(b_, "%d%m%Y") else "%d %b %Y %H:%M")
        return f"{ts(a_, '%d %b %Y %H:%M')}–{end}"
    # HEALTH-FIX-10: full date on every connectivity period, plain headings
    body.append("<h2>Connectivity</h2><div class='muted small'>Dashed = cause not known.</div>")
    cl = {}
    for o in sorted(snap["outages"], key=lambda o: o["start"]):
        if o["cause"] not in ("power", "restart"):
            cl.setdefault((o["site"], o.get("link") or ""), []).append(o)
    rows = []
    for (site, link), os_ in sorted(cl.items()):
        chips = " ".join(chip(pspan(o["start"], o["end"]) + ("" if o["scope"] == "site" else f" · {e(', '.join(o['hosts'][:2]))}"),
                              "unk" if o["cause"] == "unknown" else "") for o in os_)
        note = ("<br><span class='muted small'>kept power and did not reboot: check the cable and port</span>"
                if any(o["scope"] == "devices" and o["cause"] == "connectivity" for o in os_) and "(behind " in link else "")
        rows.append([f"<b>{e(site)}</b>", f"<span class='small'>{e(link) if link else '-'}</span>{note}", f"{len(os_)}", chips])
    body.append(table(["Site", "Where it dropped", "#Times", "When"], rows, "No connectivity loss."))

    # power
    body.append("<h2>Power and UPS</h2>")
    pw = [o for o in sorted(snap["outages"], key=lambda o: o["start"]) if o["cause"] == "power"]
    ups_p = {}
    for u in snap["ups"].values():
        for s0, s1 in u.get("periods", []):
            ups_p.setdefault(u["site"], []).append((s0, s1))
    used, down = set(), {}
    for o in pw:
        bs = [(s0, s1) for s0, s1 in ups_p.get(o["site"], []) if s0 <= o["start"] + 300 and (s1 is None or s1 >= o["start"] - 600)]
        used.update(bs)
        txt = pspan(o["start"], o["end"]) + ("" if o["scope"] == "site" else f" · {e(', '.join(o['hosts'][:3]))}")
        if o["scope"] != "site" and not snap["sites"].get(o["site"], {}).get("ups"):
            txt += "<br><span class='muted'>no UPS to confirm</span>"
        if bs:
            b0 = min(s for s, _ in bs)
            txt += f"<br><span class='muted'>UPS on battery from {ts(b0, '%H:%M')}" + (
                f", lasted {dur((o['start'] - b0) / 60)}" if o["start"] - b0 >= 60 else "") + "</span>"
        down.setdefault(o["site"], []).append(chip(txt, "bad"))
    rode = {}
    for s, ps in ups_p.items():
        for x in sorted(ps):
            if x not in used:
                rode.setdefault(s, []).append(chip(pspan(x[0], x[1], "on battery")))
    rst = {}
    for o in sorted(snap["outages"], key=lambda o: o["start"]):
        if o["cause"] == "restart":
            rst.setdefault(o["site"], []).append(chip(pspan(o["start"], o["end"]) + f" · {e(', '.join(o['hosts'][:3]))}"))
    rows = [[f"<b>{e(s)}</b>", " ".join(down.get(s, [])) or "-", f"{len(rode.get(s, []))}" if s in rode else "-",
             " ".join(rode.get(s, [])) or "-", " ".join(rst.get(s, [])) or "-"] for s in sorted(set(down) | set(rode) | set(rst))]
    body.append(table(["Site", "Went down on power", "#Mains lost", "Mains lost, the UPS kept the site up",
                       "Restarted alone (mains was on)"], rows, "No mains power failure."))
    rows, quiet = [], []
    for h, u in sorted(snap["ups"].items(), key=lambda kv: (-len(kv[1]["issues"]), -kv[1]["events"])):
        if not u["issues"] and not u["events"]:
            quiet.append(h)
            continue
        pu = (prev or {}).get("ups", {}).get(h, {})
        st = []
        for i_ in u["issues"]:
            head, _, rest = i_.partition(", the UPS estimated ")
            st.append(f"<span class='pill warn'>CHECK</span> {e(head)}"
                      + (f"<br><span class='muted small'>the UPS estimated {e(rest)}</span>" if rest else ""))
        rows.append([f"<b>{e(h)}</b>", e(u["site"]), f"{u['events']}",
                     ("over 10 h" if u["minutes"] is not None and u["minutes"] >= 600 else
                      f"{u['minutes']:.0f} min" + (f" {delta(u['minutes'], pu.get('minutes'), ' min', True)}" if kind != "daily" else "")
                      if u["minutes"] else "-"),
                     f"<span class='nw'>{u['vin_min']:.0f}–{u['vin_max']:.0f} V</span>" if u["vin_min"] is not None and u["vin_max"] is not None else "-",
                     f"{u['load_max']:.0f}%" if u["load_max"] is not None else "-",
                     "<br>".join(st) or "<span class='pill ok'>OK</span>"])
    body.append("<h3>UPS</h3>")
    body.append(table(["UPS", "Site", "#Mains failures", "#UPS estimate", "Mains voltage", "#Peak load", "Status"], rows,
                      "No UPS had a mains failure or anything to check."))
    if quiet:
        body.append(f"<div class='muted small'>No mains failure and nothing to check: {e(', '.join(quiet))}.</div>")
    body.append("<div class='muted small'>UPS estimate = the runtime the UPS itself reports at full charge; at very low load "
                "it is only a guess, so Status uses what really happened. Mains voltage = lowest to highest while mains "
                "was present.</div>")
    no_ups = [c for c, s in snap["sites"].items() if not s["ups"] and s["power"]]
    if no_ups:
        body.append(f"<div class='note small'>Sites with power outages and no monitored UPS: {e(', '.join(no_ups))}.</div>")

    # capacity (HEALTH-FIX-11): one row per link, only the busy ones
    cap = snap.get("caplinks", [])
    pcap = {l["key"]: l for l in (prev or {}).get("caplinks", [])}
    hot = [l for l in cap if l["util"] >= UTIL_BUSY]
    body.append(f"<h2>Capacity</h2><div class='muted small'>Highest traffic on each link against its port speed. "
                f"{len(cap)} links checked, {len(hot)} at {int(UTIL_BUSY * 100)}% or more "
                f"(full from {int(UTIL_FULL * 100)}%).</div>")
    rows = []
    for l in hot:
        pl = pcap.get(l["key"])
        c = "bad" if l["util"] >= UTIL_FULL else "warn"
        rows.append([f"<b>{e(' ~ '.join(l['hosts']))}</b><br><span class='muted small'>busiest port: {e(l['host'])} {e(l['port'])}</span>",
                     e(l["site"]),
                     f"<span class='pill {c}'>{l['util'] * 100:.0f}%</span> {delta(l['util'] * 100, pl['util'] * 100 if pl else None, ' pts', False)}",
                     mbps(l["peak"]), mbps(l["avg"]), mbps(l["limit"])])
    body.append(table(["Link", "Site", "Peak use", "#Peak", "#Busiest hour", "#Port speed"], rows,
                      f"No link reached {int(UTIL_BUSY * 100)}%."))

    # top 10 (HEALTH-FIX-13): ranked by how many times, with the longest single outage (nothing added up)
    body.append("<h2>Top 10</h2>")
    own_o = [o for o in snap["outages"] if not o.get("carried")]

    def longest(os_):
        o = max(os_, key=lambda o: (o["minutes"], -o["start"]))
        why = {"power": "power", "connectivity": "link", "restart": "restarted", "unknown": "cause not known",
               "ongoing": "still down"}.get(o["cause"], "")
        return f"{dur(o['minutes'])} <span class='muted small'>{pspan(o['start'], o['end'])}{f' ({why})' if why else ''}</span>"

    def last(n):   # HEALTH-FIX-14: a plain count, the column says which period
        return str(n) if n else "<span class='muted'>0</span>"
    before = {"daily": "the day before", "weekly": "the week before", "monthly": "the month before"}.get(kind, "last period")
    psites = (prev or {}).get("sites", {})
    so = {}
    for o in own_o:
        if o["scope"] == "site":
            so.setdefault(o["site"], []).append(o)
    worst_sites = sorted(so.items(), key=lambda kv: (-len(kv[1]), -max(o["minutes"] for o in kv[1]), kv[0]))[:10]
    body.append("<h3>Sites that went down most often</h3>")
    body.append(table(["Site", "#Times down", "Longest outage", f"#Times down {before}"],
                      [[f"<b>{e(c)}</b>", str(len(os_)), longest(os_), last(psites.get(c, {}).get("site_outages") or 0)]
                       for c, os_ in worst_sites], "No site went down."))
    do_ = {}
    for o in own_o:
        if o["scope"] == "devices":
            for n in o["hosts"]:
                do_.setdefault(n, []).append(o)
    phosts = (prev or {}).get("hosts", {})
    worst = sorted(do_.items(), key=lambda kv: (-len(kv[1]), -max(o["minutes"] for o in kv[1]), kv[0]))[:10]
    body.append("<h3>Devices that failed alone most often (their site stayed up)</h3>")
    body.append(table(["Device", "Site", "#Times down", "Longest outage", f"#Times down {before}"],
                      [[f"<b>{e(h)}</b>", e(os_[0]["site"]), str(len(os_)), longest(os_), last(phosts.get(h, {}).get("outages") or 0)]
                       for h, os_ in worst], "No single device went down."))
    body.append(actions(snap))
    body.append("<h2>How the score works</h2><div class='muted small'>Each site starts at 100. Every problem costs "
                "points once:</div>")
    body.append(table(["Reason", "Points", "#Most"], [
        ["Downtime: share of the site's device-time lost in outages that started at the site",
         f"{AVAIL_POINTS} per 1% lost (the whole site down 30 min = {AVAIL_POINTS * 30 / 14.4:.0f} on a day, "
         f"{AVAIL_POINTS * 30 / 100.8:.1f} on a week)", "50"],
        ["Outages that started at the site", "4 per site outage, 1 per single-device outage (per week)", "20"],
        ["UPS", "5 per UPS to check (battery, voltage, load); mains failures the UPS rode through cost nothing", "10"]]))
    body.append("<div class='muted small'>Mains failures the UPS rode through, busy links and radio signal cost no "
                "points. An outage is charged to the site where it started: a site cut off by an "
                "upstream site keeps its score, and the outage counts at the upstream site. The network score is the "
                "average of the sites weighted by devices, with the availability the users really got (cut-offs "
                "included), so an outage at a core site weighs by the devices it took down. 90+ is good, 75 to 89 "
                "fair, below 75 poor. Outages shorter than 3 minutes are not counted. The cause comes from the "
                "device's uptime after it came back (rebooted = power).</div>")
    return visible_names(page(f"{kind.title()} network report {snap['id']}", f"{kind} network health report", "Network health",
                              snap["label"], "".join(body)), snap.get("names") or {})


def visible_names(doc, names):
    """HEALTH-FIX-6: technical host names -> the renamed visible names (Excel "Device name in zabbix")."""
    if not names:
        return doc
    rx = re.compile(r"(?<![\w-])(" + "|".join(re.escape(k) for k in sorted(names, key=len, reverse=True)) + r")(?![\w-])")
    return rx.sub(lambda m: html.escape(names[m.group(1)]), doc)


def actions(snap):
    """HEALTH-FIX-13: one line per site, worst site first: what happened, then what to do."""
    per = {}

    def add(site, fact, todo_):
        f, t = per.setdefault(site, ([], []))
        if fact and fact not in f:
            f.append(fact)
        if todo_ and todo_ not in t:
            t.append(todo_)
    for h, u in snap["ups"].items():
        low = u.get("load_max") is None or u["load_max"] < 50
        for i in u["issues"]:
            if "voltage" in i:
                todo_ = "check the mains supply with the power company / generator"
            elif i.startswith("load up to"):
                todo_ = "reduce the UPS load or use a bigger UPS"
            else:   # ran out early, short backup, aging: at low load that is the battery
                todo_ = f"test or replace the battery of {h}" if low else f"reduce the load on {h} or use a bigger UPS"
            add(u["site"], f"UPS {h}: {i}", todo_)
    for code, s in snap["sites"].items():
        n = s["outages"]
        if s["site_outages"] >= 2 or s["connectivity"] >= 2 or s["power"]:
            why = ", ".join(x for x in (f"{s['connectivity']} link" if s["connectivity"] else "",
                                        f"{s['power']} power" if s["power"] else "",
                                        f"{s.get('restart', 0)} device restart" if s.get("restart") else "",
                                        f"{s['unknown']} cause not known" if s["unknown"] else "") if x)
            add(code, f"down {n} time{'s' if n != 1 else ''}" + (f" ({why})" if why else "")
                + (f", the whole site {s['site_outages']} time{'s' if s['site_outages'] != 1 else ''}" if s["site_outages"] else ""), None)
        ups_said = code in per and any(t.startswith(("test or replace the battery", "reduce the load on")) for t in per[code][1])
        if s["power"] and not ups_said:
            add(code, None, "test the UPS battery and check that every device is on it" if s["ups"] else "check the site power")
        if s.get("restart"):
            add(code, None, "check the PoE port or power adapter of the device that restarted")
        if s["connectivity"] >= 2 or (s["connectivity"] and s["site_outages"] >= 2):
            add(code, None, "check the main link alignment and cabling")
    for l in snap.get("caplinks", []):
        if l["util"] >= UTIL_FULL:
            add(l["site"], f"link {' ~ '.join(l['hosts'])} peaked at {l['util'] * 100:.0f}% of its {mbps(l['limit'])} port",
                "plan more capacity on that link")
    for h in snap["repeat"][:10]:
        x = snap["hosts"][h]
        add(x["site"], f"{h} failed alone {x['outages']} times", f"visit {h}")
    if not per:
        return "<h2>Recommended actions</h2><div class='empty'>Nothing to do.</div>"
    sc = lambda c: (snap["sites"].get(c, {}).get("score") if snap["sites"].get(c, {}).get("score") is not None else 101, c)
    items = []
    for c in sorted(per, key=sc):
        f, t = per[c]
        line = f"<b>{e(c)}</b>: " + e("; ".join(f)) + ("." if f else "")
        if t:
            line += f" <b>To do:</b> {e('; '.join(t))}."
        items.append(f"<li>{line}</li>")
    return "<h2>Recommended actions</h2><div class='muted small'>Worst site first.</div><ol>" + "".join(items[:25]) + "</ol>"


def render_admin(adm, prev, period):
    now = adm["now"]
    p = prev or {}
    def cnt(cur, key, higher_good=False):
        return delta(cur, len(p[key]) if isinstance(p.get(key), list) else p.get(key), "", higher_good)
    k = [f"<div><b>{adm['hosts']}</b><span>monitored devices</span> {delta(adm['hosts'], p.get('hosts'), '', True)}</div>",
         f"<div><b>{len(adm['snmp_down'])}</b><span>devices with SNMP not answering</span> {cnt(len(adm['snmp_down']), 'snmp_down')}</div>",
         f"<div><b>{len(adm['silent'])}</b><span>devices with no ping data for 15 min</span> {cnt(len(adm['silent']), 'silent')}</div>",
         f"<div><b>{adm['unsupported']}</b><span>items not supported</span> {cnt(adm['unsupported'], 'unsupported')}</div>",
         f"<div><b>{len(adm['stale'])}</b><span>problems open 7+ days, not acknowledged</span> {cnt(len(adm['stale']), 'stale')}</div>",
         f"<div><b>{len(adm['disabled'])}</b><span>devices disabled on purpose</span></div>"]
    body = [f"<section class='kpis' style='margin:20px 0'>{''.join(k)}</section>"]
    g = adm.get("guard")
    body.append("<h2>Zabbix server</h2>")
    if not g:
        body.append("<div class='note'>Host \"Zabbix server guard\" not found: run <code>zbx_sites.py guard --apply</code> "
                    "(OPTIMIZE.md) to get cache, process and backup numbers here.</div>")
    else:
        rows = []
        for key, item in sorted(g.items()):
            if not key.startswith("zabbix[") or item["max"] is None:
                continue
            warn = ("pused" in key or "busy" in key) and item["max"] >= 75
            rows.append([e(item["name"]), f"{item['avg']:.1f}", f"<b>{item['max']:.1f}</b>",
                         "<span class='pill warn'>HIGH</span>" if warn else "<span class='pill ok'>OK</span>"])
        body.append(table(["Cache / process / queue", "#Average", "#Highest", "Status"], rows,
                          "No trend data on the guard host yet."))
        facts = []
        db = g.get("mdp.db.size", {})
        if db.get("last"):
            grow = (db["last"] - db["first"]) / max(1, (min(period.b, now) - period.a) / 86400) if db.get("first") else None
            facts.append(f"Database size <b>{db['last'] / 1e9:.1f} GB</b>" + (f", growing {grow / 1e9:.2f} GB per day" if grow else ""))
        for key, label in (("mdp.disk.pfree[db]", "database disk"), ("mdp.disk.pfree[archive]", "archive disk")):
            d = g.get(key, {})
            if d.get("last") is not None:
                facts.append(f"{label}: <b>{d['last']:.0f}% free</b>" + (f" (lowest {d['min']:.0f}%)" if d.get("min") is not None else ""))
        for key, label in (("mdp.archive.last", "daily archive"), ("mdp.backup.last", "nightly backup")):
            d = g.get(key, {})
            if d.get("last"):
                age = (now - d["last"]) / 3600
                facts.append(f"last {label}: <b>{ts(int(d['last']))}</b>" + (" <span class='pill bad'>LATE</span>" if age > 30 else ""))
            else:
                facts.append(f"{label}: <span class='pill bad'>NO DATA</span> (cron for zbx_data.py nightly installed?)")
        cp = g.get("mdp.copy.last", {})
        if cp.get("lastclock"):
            late = not cp.get("last") or (now - cp["last"]) / 3600 > 30
            facts.append("last off-server copy: " + (f"<b>{ts(int(cp['last']))}</b>" if cp.get("last") else "<b>never</b>")
                         + (" <span class='pill bad'>LATE</span>" if late else ""))
        else:
            facts.append("off-server copy: <span class='pill warn'>NOT SET UP</span> (COPY_TO in ~/.zabbix_env, "
                         "RETENTION.md): archive and backups are on the same disk as the database")
        hd = g.get("mdp.history.days", {})
        if hd.get("last"):
            facts.append(f"raw history in the database: <b>{hd['last']:.0f} days</b>")
        body.append("<ul>" + "".join(f"<li>{f}</li>" for f in facts) + "</ul>")
        if adm.get("guard_problems"):
            body.append("<h3>Server problems in this period</h3>")
            body.append(table(["Problem", "#Times"], [[e(n), str(c)] for n, c in adm["guard_problems"]]))
    body.append("<h2>Blind spots</h2><div class='muted small'>Devices Zabbix cannot read: the reports and the RCA "
                "cannot see power or radio problems on them.</div>")
    body.append("<h3>SNMP not answering (ping works or not)</h3>")
    body.append(table(["Device", "Since", "Error"], [[e(x["host"]), ts(x["since"]) if x["since"] else "-", e(x["error"])]
                                                    for x in adm["snmp_down"][:50]], "Every device answers SNMP."))
    body.append("<h3>No ping data for 15 minutes or more</h3><div class='muted small'>Zabbix is not polling them "
                "(poller busy, host just added, or item disabled).</div>")
    body.append(f"<div class='note'>{e(', '.join(adm['silent'][:80])) or 'None.'}</div>")
    if adm["ping_only"]:
        body.append(f"<h3>Ping only (SNMP alarms off on purpose, {len(adm['ping_only'])})</h3>"
                    f"<div class='note small'>{e(', '.join(adm['ping_only'][:80]))}</div>")
    if adm["no_site"]:
        body.append(f"<h3>Devices without a site ({len(adm['no_site'])})</h3><div class='muted small'>Found by discovery "
                    "but not in hosts.csv: they are missing from the management reports. Add them to hosts.csv and run "
                    f"zbx_sites.py apply.</div><div class='note small'>{e(', '.join(adm['no_site'][:80]))}</div>")
    body.append("<h2>Items not supported</h2>")
    body.append(table(["Item key", "#Devices", "Example error"], [[e(k), str(v["count"]), e(v["error"][:140])]
                                                                for k, v in adm["unsupported_keys"]], "None."))
    if adm["unsupported_hosts"]:
        body.append("<h3>Devices with the most</h3>" + table(["Device", "#Items"], [[e(h), str(c)] for h, c in adm["unsupported_hosts"]]))
    body.append("<h2>Old problems nobody acknowledged</h2>")
    body.append(table(["Device", "Problem", "Since"], [[e(x["host"]), e(x["name"]), ts(x["since"])] for x in adm["stale"][:40]],
                      "None."))
    if adm["disabled"]:
        body.append(f"<h3>Disabled devices ({len(adm['disabled'])})</h3><div class='note small'>{e(', '.join(adm['disabled'][:100]))}</div>")
    return page(f"Zabbix admin report {period.id}", "Zabbix admin report", "Monitoring health",
                f"{period.label()} · Zabbix {adm['version']}", "".join(body))


# --------------------------------------------------------------- storage
def snap_path(kind, pid, ext="json"):
    return os.path.join(REPORT_DIR, kind, f"{pid}.{ext}")


def load_snap(kind, pid):
    try:
        with open(snap_path(kind, pid), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save(kind, pid, snap=None, page_html=None):
    os.makedirs(os.path.join(REPORT_DIR, kind), exist_ok=True)
    if snap is not None:
        tmp = snap_path(kind, pid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"))
        os.replace(tmp, snap_path(kind, pid))
    if page_html is not None:
        with open(snap_path(kind, pid, "html"), "w", encoding="utf-8") as f:
            f.write(page_html)


def history(kind, before, n):
    d = os.path.join(REPORT_DIR, kind)
    try:
        ids = sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json") and f[:-5] < before)
    except OSError:
        return []
    out = [load_snap(kind, i) for i in ids[-n:]]
    return [s for s in out if s and s.get("network") and s.get("v") == SNAP_VERSION]


# --------------------------------------------------------------- commands
def build(kind, when=None, sites=None, store=True):
    from zbxapi import ZabbixAPI
    from zbxdb import DB
    api, db = ZabbixAPI(), DB()
    period = Period.of(kind, when)
    if period.a >= time.time():
        raise SystemExit(f"{kind} {period.id} has not started yet.")
    if period.b > time.time() and store:
        # a period still running: show it, but keep no snapshot, or the scheduled run would treat the
        # finished period as built and comparisons would use a part of it
        print(f"{kind} {period.id} is not finished yet: built so far, not saved (use --out to keep a copy).")
        store = False
    if kind == "admin":
        prev = load_snap(kind, period.prev().id)
        adm = fetch_admin(api, db, period.a, min(period.b, int(time.time())))
        page_html = render_admin(adm, prev, period)
        summary = {k: (len(v) if isinstance(v, list) else v) for k, v in adm.items() if k in
                   ("hosts", "snmp_down", "silent", "unsupported", "stale")}
        if store:
            save(kind, period.id, {**summary, "id": period.id, "kind": kind}, page_html)
        return period, page_html, summary

    def one(p, prev):
        raw = fetch(api, db, p.a, p.b)
        s = analyze(raw, p, prev)
        if sites:
            s["sites"] = {k: v for k, v in s["sites"].items() if k in sites}
        return s

    pp = period.prev()
    prev = load_snap(kind, pp.id)
    if prev is not None and prev.get("v") != SNAP_VERSION:
        prev = None      # made by an older version (other counting rules): computed again below
    if prev is None and not sites:
        before = load_snap(kind, pp.prev().id)
        prev = one(pp, before if before and before.get("v") == SNAP_VERSION else None)  # first run: from trends
        prev["backfilled"] = True
        if store:
            save(kind, pp.id, prev)
    snap = one(period, prev)
    hist = history(kind, period.id, {"daily": 13, "weekly": 11, "monthly": 11}[kind])
    page_html = render(snap, prev, hist)
    if store and not sites:
        save(kind, period.id, snap, page_html)
    return period, page_html, snap["network"]


def due(kind):
    """The latest finished period of that kind."""
    return Period.of(kind)


def cmd_scheduled():
    failed = []
    for kind in KINDS:
        p = due(kind)
        if os.path.exists(snap_path(kind, p.id, "html")):
            continue
        try:
            _, _, s = build(kind)
            log(f"{kind} {p.id} built: " + ", ".join(f"{k}={v}" for k, v in s.items() if k in
                                                      ("score", "avail", "outages", "unsupported", "snmp_down")))
        except Exception as ex:  # noqa: BLE001 - one failed report must not stop the others
            failed.append(kind)
            log(f"ERROR {kind} {p.id}: {ex}")
    if failed:
        sys.exit(1)


def cmd_rebuild(since):
    """Build every daily, weekly and monthly report again, oldest first, from `since` to the last finished
    period (after a change in how the reports count). The admin reports are kept as they are."""
    if since is None:
        ids = []
        for kind in ("daily", "weekly"):
            try:
                ids += [f[:10] for f in os.listdir(os.path.join(REPORT_DIR, kind)) if f.endswith(".json")]
            except OSError:
                pass
        since = date.fromisoformat(min(ids)) if ids else today() - timedelta(days=1)
    print(f"Rebuilding the reports from {since} (each takes a few seconds to a minute)...")
    failed = 0
    for kind in ("daily", "weekly", "monthly"):
        p, last = Period.of(kind, since), due(kind)
        while p.a <= last.a:
            try:
                _, _, s = build(kind, p.first)
                log(f"rebuilt {kind} {p.id}: score={s['score']} avail={s['avail']} site_outages={s['site_outages']} "
                    f"site_downtime={s['minutes']}min device_outages={s['device_outages']}")
            except Exception as ex:  # noqa: BLE001 - keep going with the other periods
                failed += 1
                log(f"ERROR rebuilding {kind} {p.id}: {ex}")
            p = Period.of(kind, p.after)
    if failed:
        sys.exit(1)


def cmd_cron(do_apply):
    here = os.path.dirname(os.path.abspath(__file__))
    env = os.path.expanduser("~/.zabbix_env")
    text = (f"# MDP network health reports (tools/zbx_report.py, REPORTS.md). Times are server local time.\n"
            f"# 04:15 is after midnight in UTC+3 whether the server clock is UTC or UTC.\n"
            f"SHELL=/bin/bash\n"
            f"15 4 * * * root bash -c 'source {env} && python3 {here}/zbx_report.py scheduled' >> {LOG} 2>&1\n")
    print(text)
    if not os.path.exists(env):
        print(f"WARNING: {env} not found; create it first (INSTALL.md step 3).")
    if do_apply:
        with open("/etc/cron.d/mdp-zabbix-reports", "w") as f:
            f.write(text)
        os.chmod("/etc/cron.d/mdp-zabbix-reports", 0o644)
        print("Installed /etc/cron.d/mdp-zabbix-reports")
    else:
        print("Dry run; re-run with --apply to install.")


def list_reports():
    """{kind: [(id, has_html)]} newest first."""
    out = {}
    for kind in KINDS:
        d = os.path.join(REPORT_DIR, kind)
        try:
            files = os.listdir(d)
        except OSError:
            files = []
        out[kind] = sorted({f.rsplit(".", 1)[0] for f in files if f.endswith(".html")}, reverse=True)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for kind in KINDS:
        s = sub.add_parser(kind)
        s.add_argument("--date", help="a day in the period (YYYY-MM-DD; monthly: YYYY-MM)")
        s.add_argument("--out", help="also write the HTML here")
        s.add_argument("--no-save", action="store_true", help="do not write to REPORT_DIR")
        if kind != "admin":
            s.add_argument("--sites", help="only these sites in the site table (not saved as a snapshot)")
    sub.add_parser("scheduled")
    rb = sub.add_parser("rebuild", help="build the daily/weekly/monthly reports again (after an update)")
    rb.add_argument("--since", help="first day (YYYY-MM-DD), default the oldest report kept")
    c = sub.add_parser("cron")
    c.add_argument("--apply", action="store_true")
    sub.add_parser("list")
    args = ap.parse_args()
    if args.cmd == "scheduled":
        return cmd_scheduled()
    if args.cmd == "rebuild":
        return cmd_rebuild(date.fromisoformat(args.since) if args.since else None)
    if args.cmd == "cron":
        return cmd_cron(args.apply)
    if args.cmd == "list":
        for kind, ids in list_reports().items():
            print(f"{kind:8} {len(ids):4}  {', '.join(ids[:6])}{' ...' if len(ids) > 6 else ''}")
        print(f"in {REPORT_DIR}")
        return
    sites = args.sites.split(",") if getattr(args, "sites", None) else None
    period, page_html, s = build(args.cmd, parse_when(args.cmd, args.date), sites, not args.no_save)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(page_html)
    print(f"{args.cmd} {period.id} ({period.label()}): " + ", ".join(f"{k}={v}" for k, v in s.items()
                                                                     if not isinstance(v, (list, dict))))
    if not args.no_save and not sites and period.b <= time.time():
        print("Report: " + snap_path(args.cmd, period.id, "html"))
    elif args.out:
        print("Report: " + args.out)


if __name__ == "__main__":
    main()
