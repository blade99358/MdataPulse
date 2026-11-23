#!/usr/bin/env python3
"""Offline checks of the radio signal check (no Zabbix or database needed): python3 tests/test_signal.py"""
import os
import sys
import tempfile
from datetime import date, timedelta

os.environ["RCA_DB"] = "0"
os.environ["SIGNAL_EXCLUDE"] = "^EP-"
os.environ["SIGNAL_LACP"] = "^SITE0000-WAVE-35$"
os.environ["SIGNAL_LACP_NOTE"] = "SITE0000 to SITE0006"
os.environ.setdefault("RCA_TZ", "Etc/GMT-3")
os.environ["REPORT_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import zbx_signal as s  # noqa: E402

fails = 0


def check(name, cond, got=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f": {got}" if got != "" and not cond else ""))
    fails += not cond


D = date(2026, 11, 15)
T1, T0 = s.snap_at(D), s.snap_at(D - timedelta(days=1))
check("snapshot at 15:00 UTC+3 = 12:00 UTC", T1 % 86400 == 12 * 3600, T1 % 86400)

HOSTS = [
    ("1", "SITE0001-WAVE-1", "SITE01_SITE00_Wave_P2P", "SITE0001", "10.20.0.18"),
    ("2", "SITE0001-P2P-2", "SITE01_SITE02_P2P", "SITE0001", "10.20.0.19"),
    ("3", "SITE0001-P2MP-3", "SITE01_S1", "SITE0001", "10.20.0.16"),
    ("4", "EP-P0001-SM", "P0001", "SITE0001", "10.20.1.11"),
    ("5", "SITE0000-WAVE-35", "SITE00_SITE06_LACP", "SITE0000", "10.20.0.20"),
    ("6", "SITE0001-WAVE-6", "SITE01_SITE03_Wave", "SITE0001", "10.20.0.21"),
    ("7", "SITE0001-SW-1", "SITE01_Unifi_SW", "SITE0001", "10.20.0.22"),
]
ITEMS = []


def item(hid, key, vt=0, name="", last=""):
    ITEMS.append({"itemid": str(100 + len(ITEMS)), "hostid": hid, "key_": key, "name": name or key,
                  "value_type": str(vt), "lastvalue": last})
    return ITEMS[-1]["itemid"]


V = {}   # itemid -> (value yesterday, value today)
V[item("1", "wifi.rssi")] = (-55, -63)              # dropped 8 dB -> WATCH
V[item("1", "wifi.snr")] = (30, 24)
V[item("1", "wifi.remote.rssi[x]", name="Link to SITE0000-WAVE-9: Remote signal")] = (-56, -60)
V[item("1", "wifi.remote.snr[x]", name="Link to SITE0000-WAVE-9: Remote SNR")] = (29, 26)
V[item("1", "wifi.capacity.rx[x]", 3)] = (1751750000, 1500000000)
V[item("2", "wifi.rssi")] = (-60, -60)              # steady, fine
V[item("2", "wifi.snr")] = (35, 36)
item("2", "wifi.link.ssid", 1, last="SITE0002-AP")
V[item("2", "wifi.dlmcs", 3)] = (9, 9)
V[item("3", "wifi.sta.worst")] = (-72, -73)         # sector AP: must not appear
V[item("3", "wifi.stations", 3)] = (12, 12)
item("3", "wifi.rssi")                               # AP: no own signal
V[item("4", "wifi.rssi")] = (-80, -80)               # endpoint: must not appear
V[item("5", "wifi.rssi")] = (-50, -50)               # LACP: listed apart
V[item("6", "wifi.rssi")] = (-58, None)              # down at the snapshot -> NO DATA
V[item("6", "wifi.snr")] = (30, None)


class FakeAPI:
    def call(self, method, p):
        if method == "host.get":
            return [{"hostid": h, "host": n, "name": vn, "tags": [{"tag": "site", "value": site}],
                     "interfaces": [{"ip": ip, "main": "1", "type": "2"}]} for h, n, vn, site, ip in HOSTS]
        if method == "item.get":
            return [i for i in ITEMS if i["hostid"] in p["hostids"]]
        if method == "history.get":
            out = []
            for iid in p["itemids"]:
                if iid not in V:
                    continue
                for t, v in ((T0, V[iid][0]), (T1, V[iid][1])):
                    if v is not None and p["time_from"] <= t + 60 <= p["time_till"]:
                        out.append({"itemid": iid, "value": str(v)})
            return out
        if method == "trend.get":
            return []
        if method == "event.get":
            return [{"name": "Link to SITE0000-WAVE-9: Link re-established (uptime below 10m)", "hosts": [{"hostid": "1"}]}] * 3 + \
                   [{"name": "Device unreachable", "hosts": [{"hostid": "6"}]}]
        raise AssertionError(method)


pid, page, c, done = s.build("daily", D, store=True, api=FakeAPI())
check("daily saved (window passed)", done and os.path.exists(s.path("daily", pid)))
check("endpoint left out", "P0001" not in page)
check("counts", (c["radios"], c["bad"], c["watch"], c["ok"], c["nodata"]) == (3, 0, 1, 1, 1), c)
check("worse / unchanged", (c["worse"], c["same"]) == (1, 1), c)
check("LACP radio listed apart, not in the table", "SITE00_SITE06_LACP" in page.split("Not measured")[1]
      and "SITE00_SITE06_LACP</b>" not in page)
check("drop reason shown", "signal dropped 8 dB" in page)
check("link drops counted", "link dropped 3 times" in page)
check("linked-to from the Wave peer", "SITE0000-WAVE-9" in page)
check("linked-to from the Cambium SSID", "SITE0002-AP" in page)
check("capacity shown", "1500 Mbps" in page or "1.5 Gbps" in page, "")
check("sector left out", "SITE01_S1" not in page)
check("no data row", "NO DATA" in page and "down 1x" in page)
check("WATCH sorted before OK", page.index("SITE01_SITE00_Wave_P2P") < page.index("SITE01_SITE02_P2P"))
check("never 'for management'", "for management" not in page.lower())
check("snapshot cached", os.path.exists(os.path.join(os.environ["REPORT_DIR"], "signal-snap", f"{D}.json")))
st, why = s.judge({"rssi": -60, "snr": 30}, {"rssi": -60, "snr": 30}, 0)
check("judge ok", st == "ok", (st, why))
st, _ = s.judge({"rssi": -71, "snr": 30}, None, 0)
check("judge bad signal", st == "bad")
st, _ = s.judge({"rssi": -60, "snr": 18}, None, 0)
check("judge watch snr", st == "watch")
w = s.average([("2026-11-15", {"A": {"rssi": -60, "snr": 30}}), ("2026-11-16", {"A": {"rssi": -64, "snr": 26}})])
check("weekly average + lowest day", w["A"]["rssi"] == -62 and w["A"]["low"] == (-64, "2026-11-16") and w["A"]["days"] == 2, w)
code, title, body, rep = s.web(FakeAPI(), [], {}, "/")
check("listing page links the saved report", code == 200 and f"signal/daily/{D}.html" in body)
if os.environ.get("SIGNAL_PREVIEW"):
    open(os.environ["SIGNAL_PREVIEW"], "w").write(page)
print(f"{'ALL PASSED' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
