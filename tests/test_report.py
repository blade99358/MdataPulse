#!/usr/bin/env python3
"""Offline checks of the health report rules (no Zabbix or database needed): python3 tests/test_report.py"""
import os
os.environ.setdefault("RCA_TZ", "Etc/GMT-3")  # tests assume a UTC+3 day
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import zbx_report as z  # noqa: E402

fails = 0


def check(name, cond, got=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f": {got}" if got != "" and not cond else ""))
    fails += not cond


# periods
w = z.Period.of("weekly", date(2026, 10, 27))
check("week runs Sunday to Saturday", (w.first, w.after) == (date(2026, 10, 25), date(2026, 11, 1)), (w.first, w.after))
check("previous week", w.prev().first == date(2026, 10, 18))
m = z.Period.of("monthly", date(2026, 3, 15))
check("month and previous month", (m.id, m.prev().id, m.days) == ("2026-03", "2026-02", 31), (m.id, m.prev().id))
d = z.Period.of("daily", date(2026, 10, 26))
check("a UTC+3 day starts at 21:00 UTC", d.a % 86400 == 75600 and d.b - d.a == 86400)

# a small network: SITE0001 core -> backhaul -> SITE0002 radio -> SITE0002 switch, plus a UPS
P = z.Period.of("weekly", date(2026, 10, 18))
A, H = P.a, 3600
hosts = {}
items, stats = [], {}


def host(hid, name, site, role, parent="", vendor="cambium"):
    hosts[hid] = {"host": name, "name": name, "site": site, "role": role, "vendor": vendor, "model": "", "parent": parent}


def item(hid, key, vt, st, last="", tags=()):
    iid = str(1000 + len(items))
    items.append({"itemid": iid, "hostid": hid, "key_": key, "value_type": str(vt), "lastvalue": last,
                  "tags": [{"tag": k, "value": v} for k, v in tags]})
    if st:
        stats[iid] = st


host("1", "SITE0001-SW", "SITE0001", "aggregation", "SITE0000-RADIO")
host("2", "SITE0001-BH", "SITE0001", "backhaul", "SITE0001-SW")
host("3", "SITE0002-RADIO", "SITE0002", "backhaul", "SITE0001-BH")
host("4", "SITE0002-SW", "SITE0002", "aggregation", "SITE0002-RADIO")
host("5", "SITE0001-UPS", "SITE0001", "ups", "SITE0001-SW", "huawei")
item("2", "wifi.rssi", 0, [168, -70, -66, -60, -62])
item("2", "wifi.snr", 0, [168, 20, 25, 30, 26])
item("3", "wifi.rssi", 0, [168, -80, -78, -76, -77])
item("5", "ups.battery.minutes", 0, [168, 20, 24, 30, 25])
item("5", "ups.input.voltage", 0, [168, 180, 220, 240, 230])
item("1", "net.if.in[ifHCInOctets.3]", 3, [168, 0, 2e8, 9.5e8, 6e8], tags=[("interface", "sfp3")])
item("1", "net.if.out[ifHCOutOctets.3]", 3, [168, 0, 1e8, 3e8, 2e8], tags=[("interface", "sfp3")])
item("1", "net.if.speed[ifHighSpeed.3]", 3, None, last="1000000000")
ev = [
    {"hostid": "1", "clock": A + 10 * H, "end": A + 10 * H + 2400, "name": "Device unreachable", "tags": {}},
    {"hostid": "1", "clock": A + 10 * H + 2520, "end": A + 11 * H, "name": "Outage cause: POWER (device rebooted)",
     "tags": {"diagnosis": "power"}},
    {"hostid": "5", "clock": A + 10 * H - 60, "end": A + 10 * H + 1200, "name": "UPS: On battery (mains power lost)", "tags": {}},
]
for day in (1, 2, 4):   # the SITE0002 radio drops on three days
    c = A + day * 86400 + 5 * H
    ev += [{"hostid": "3", "clock": c, "end": c + 600, "name": "Device unreachable", "tags": {}},
           {"hostid": "3", "clock": c + 720, "end": c + 3600, "name": "Outage cause: CONNECTIVITY (device did not reboot)",
            "tags": {"diagnosis": "connectivity"}}]
ev.append({"hostid": "4", "clock": A - 86400, "end": A - 86000, "name": "Device unreachable", "tags": {}})  # before the week
raw = {"hosts": hosts, "items": items, "stats": stats, "events": ev, "daily": {}, "now": P.b + 86400}
prev = {"radios": {"SITE0001-BH": {"rssi": -60.0, "snr": 30.0, "mcs": None, "cap": None}},
        "ups": {"SITE0001-UPS": {"minutes": 40.0}},
        "network": {"score": 95, "avail": 99.95, "outages": 1, "power": 0, "connectivity": 1, "minutes": 10,
                    "impact": 20, "battery_events": 0, "degraded": 0, "weak_ups": 0},
        "sites": {"SITE0001": {"score": 98, "avail": 99.99}, "SITE0002": {"score": 99, "avail": 99.99}},
        "hosts": {"SITE0002-RADIO": {"outages": 1}}, "repeat": [], "links": {}}
s = z.analyze(raw, P, prev)
outs = s["outages"]
check("outages before the period are left out", len(outs) == 4, len(outs))
core = next(o for o in outs if o["host"] == "SITE0001-SW")
check("power cause from the diagnosis problem after recovery", core["cause"] == "power", core["cause"])
check("the core switch takes its whole site down", core["scope"] == "site" and core["down_here"] == 3, core)
check("devices affected counted through the parent chain", core["affected"] == 5, core["affected"])
check("downstream site found", core["sites_behind"] == ["SITE0002"], core["sites_behind"])
check("outage starts 3 minutes before the problem (detection delay)", core["minutes"] == 43, core["minutes"])
av = s["hosts"]["SITE0002-SW"]["avail"]
check("availability counts the outages in front of the device", av == round(100 * (1 - (43 + 3 * 13) / 10080), 3), av)
check("own availability leaves out the upstream outage", s["hosts"]["SITE0002-SW"]["avail_own"] == round(100 * (1 - 39 / 10080), 3),
      s["hosts"]["SITE0002-SW"]["avail_own"])
check("a device in front is not hit by outages behind it", s["hosts"]["SITE0001-SW"]["avail"] == round(100 * (1 - 43 / 10080), 3))
check("availability per day", len(s["daily"]) == 7 and s["daily"]["2026-10-19"] < 100, s["daily"])
check("connectivity cause", all(o["cause"] == "connectivity" for o in outs if o["host"] == "SITE0002-RADIO"))
check("UPS on battery counted", s["ups"]["SITE0001-UPS"]["events"] == 1)
check("battery aging found (40 -> 24 min)", any("aging" in i for i in s["ups"]["SITE0001-UPS"]["issues"]),
      s["ups"]["SITE0001-UPS"]["issues"])
check("low mains voltage found", any("low mains" in i for i in s["ups"]["SITE0001-UPS"]["issues"]))
check("radio degraded 6 dB vs last week", s["radios"]["SITE0001-BH"]["degraded"] == ["signal -60.0 → -66.0 dBm", "SNR 30.0 → 25.0 dB"],
      s["radios"]["SITE0001-BH"]["degraded"])
check("weak radio (-78 dBm)", s["radios"]["SITE0002-RADIO"]["weak"])
link = s["links"].get("SITE0001-SW sfp3")
check("port peak against its speed", link and link["util"] == 0.95 and link["limit"] == 1000.0, link)
check("repeat offender: the SITE0002 site went down on 3 days", s["repeat_sites"] == ["SITE0002"], s["repeat_sites"])
st = s["sites"]
check("SITE0002 cut off by upstream outage", st["SITE0002"]["upstream"] == 1 and st["SITE0002"]["upstream_site"] == "SITE0001")
check("SITE0002 score not charged for the SITE0001 outage",
      st["SITE0002"]["penalties"]["availability"] == round((100 - st["SITE0002"]["avail_own"]) * z.AVAIL_POINTS, 1), st["SITE0002"])
check("SITE0001 score loses points for downtime, UPS, radio, capacity",
      st["SITE0001"]["score"] <= 90 and all(st["SITE0001"]["penalties"][k] > 0 for k in ("availability", "power")) and st["SITE0001"]["penalties"]["capacity"] == 0,
      st["SITE0001"])
check("network score between the site scores",
      min(x["score"] for x in st.values()) - 5 <= s["network"]["score"] <= max(x["score"] for x in st.values()))
ch = z.changes(s, prev)
check("changes mention the score drop", any("SITE0001 lost power" in t for _, t in ch), ch)
page = z.render(s, prev, [])
check("report renders with every section", all(x in page for x in ("Network health", "What changed", "Sites",
      "Outages and their impact", "Power and UPS", "Capacity", "Top 10", "Recommended actions")))
check("no previous report: still renders", "No earlier report" in z.render(z.analyze(raw, P), None, []))
# a device that went down 20 days before the week and came back on day 2 is down for those 2 days
long = dict(raw, events=[{"hostid": "4", "clock": A - 20 * 86400, "end": A + 2 * 86400, "name": "Device unreachable",
                          "tags": {}}])
av = z.analyze(long, P)["hosts"]["SITE0002-SW"]["avail"]
check("an outage that started long before the period still counts", av == round(100 * (1 - 2 / 7), 3), av)
check("long outages are read back far enough", z.LONG_LOOKBACK >= 30 * 86400)

# --- the user's case: a site of 4 devices down for 30 minutes = ONE outage of 30 minutes, not 4 and 2 hours
D = z.Period.of("daily", date(2026, 10, 30))
B = D.a + 10 * H
hosts.clear()
host("11", "SITE0003-RADIO", "SITE0003", "backhaul", "SITE0000-BH")
host("12", "SITE0003-SW", "SITE0003", "aggregation", "SITE0003-RADIO")
host("13", "SITE0003-UPS", "SITE0003", "ups", "SITE0003-SW", "huawei")
host("14", "SITE0003-AP", "SITE0003", "ap", "SITE0003-SW")
host("15", "SITE0000-BH", "SITE0000", "backhaul", "")
for k in range(9):
    host(str(20 + k), f"SITE0000-DEV{k}", "SITE0000", "access-switch", "SITE0000-BH")
# the alarms all came out within 40 s of each other (the dependencies did not catch them)
ev = [{"hostid": h, "clock": B + 180 + 10 * k, "end": B + 1800, "name": "Device unreachable", "tags": {}}
      for k, h in enumerate(("11", "12", "13", "14"))]
ev.append({"hostid": "11", "clock": B + 1900, "end": B + 3000, "name": "Outage cause: POWER (device rebooted)",
           "tags": {"diagnosis": "power"}})
raw = {"hosts": hosts, "items": [], "stats": {}, "events": ev, "now": D.b + 86400}
s = z.analyze(raw, D)
o = s["outages"]
check("4 device alarms together = 1 outage", len(o) == 1 and o[0]["scope"] == "site", o)
check("downtime is 30 minutes, not 4 x 30", s["network"]["minutes"] == 30 and s["sites"]["SITE0003"]["minutes"] == 30,
      (s["network"]["minutes"], s["sites"]["SITE0003"]["minutes"]))
check("counted as 1 site outage, 0 device outages", (s["network"]["site_outages"], s["network"]["device_outages"]) == (1, 0))
check("power cause found on the site", o[0]["cause"] == "power", o[0]["cause"])
sc = s["sites"]["SITE0003"]["score"]
check("a 30-minute site outage on a day is not POOR", 75 <= sc <= 90, (sc, s["sites"]["SITE0003"]["penalties"]))
check("SITE0000 not affected", s["sites"]["SITE0000"]["score"] == 100, s["sites"]["SITE0000"])
w7 = z.Period.of("weekly", date(2026, 10, 30))
sw = z.analyze(dict(raw, now=w7.b + 86400), w7)["sites"]["SITE0003"]["score"]
check("the same outage in a week costs fewer points (GOOD)", sw >= 90, sw)

# --- with the Site down alarm: device alarms closed by event correlation, a site behind lets one alarm slip
hosts.clear()
host("31", "SITE0004-WAVE", "SITE0004", "backhaul", "SITE0000-BH")
host("32", "SITE0004-SW", "SITE0004", "aggregation", "SITE0004-WAVE")
host("33", "SITE0004-AP", "SITE0004", "ap", "SITE0004-SW")
host("34", "SITE0005-RADIO", "SITE0005", "backhaul", "SITE0004-SW")
host("35", "SITE0005-SW", "SITE0005", "aggregation", "SITE0005-RADIO")
host("15", "SITE0000-BH", "SITE0000", "backhaul", "")
site_hosts = {"90": {"site": "SITE0004", "upstream": ["SITE0000"]}, "91": {"site": "SITE0005", "upstream": ["SITE0000", "SITE0004"]},
              "92": {"site": "SITE0000", "upstream": []}}
ev = [{"hostid": "90", "clock": B + 120, "end": B + 2520, "name": "Site SITE0004 DOWN: main link lost", "tags": {"alarm": "site-down"}},
      {"hostid": "32", "clock": B + 180, "end": B + 125, "name": "Device unreachable", "tags": {"alarm": "unreachable"}},
      {"hostid": "35", "clock": B + 185, "end": B + 2700, "name": "Device unreachable", "tags": {"alarm": "unreachable"}},
      # an unrelated fault later in the day at SITE0005, and a device dead since 20 days at SITE0004
      {"hostid": "35", "clock": B + 6 * H, "end": B + 6 * H + 900, "name": "Device unreachable", "tags": {}},
      {"hostid": "33", "clock": D.a - 20 * 86400, "end": None, "name": "Device unreachable", "tags": {}}]
raw = {"hosts": hosts, "site_hosts": site_hosts, "items": [], "stats": {}, "events": ev, "now": D.b + 3600}
s = z.analyze(raw, D)
o = s["outages"]
main = [x for x in o if x["scope"] == "site"]
check("Site down alarm = 1 site outage at SITE0004", len(main) == 1 and main[0]["site"] == "SITE0004", o)
check("site outage lasts the Site down alarm (+2 min detection)", main and main[0]["minutes"] == 42, main and main[0]["minutes"])
check("the alarm that slipped out at SITE0005 is part of it", main and main[0]["sites_behind"] == ["SITE0005"] and
      s["sites"]["SITE0005"]["upstream"] == 1 and s["sites"]["SITE0005"]["site_outages"] == 0, s["sites"]["SITE0005"])
check("a later fault and a long-dead device are separate outages", len(o) == 3, [(x["site"], x["hosts"], x["minutes"]) for x in o])
check("site chain from the Site hosts", s["sites"]["SITE0005"]["upstream_site"] == "SITE0004", s["sites"]["SITE0005"]["upstream_site"])
check("devices with no alarm of their own lose availability in the site outage",
      s["hosts"]["SITE0004-WAVE"]["avail"] < 100 and s["hosts"]["SITE0005-SW"]["avail"] < s["hosts"]["SITE0005-SW"]["avail_own"])
page = z.render(s, None, [])
check("site outage shown once with SITE DOWN", page.count("SITE DOWN") == 1)

check("endpoint devices left out, site devices kept", z.EXCLUDE.search("EP-P0001-PC") and z.EXCLUDE.search("EP-P0005-CPE")
      and not z.EXCLUDE.search("SITE0016-POE-SW-1") and not z.EXCLUDE.search("SITE0002-PMP-3"))
n = s["network"]
check("site outage causes add up to the total", n["power"] + n["connectivity"] + n["unknown"] + n["ongoing"] == n["site_outages"], n)
check("no site downtime total box", "site downtime, each" not in page and "sites went down" in page)
sys.exit(1 if fails else 0)
