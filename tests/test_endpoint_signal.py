#!/usr/bin/env python3
"""Offline checks of the endpoint signal check (no Zabbix or database): python3 tests/test_endpoint_signal.py"""
import os
import sys
import tempfile
from datetime import date, timedelta

os.environ["RCA_DB"] = "0"
os.environ["REPORT_EXCLUDE"] = "^EP-"          # as on the live server: must NOT hide the endpoints here
os.environ.setdefault("RCA_TZ", "Etc/GMT-3")
os.environ["REPORT_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import zbx_endpoint_signal as p  # noqa: E402
import zbx_signal as sg  # noqa: E402

fails = 0


def check(name, cond, got=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f": {got}" if got != "" and not cond else ""))
    fails += not cond


D = date(2026, 11, 15)
T1, T0 = sg.snap_at(D), sg.snap_at(D - timedelta(days=1))
# hostid, host, visible, site, ip, role, parent
HOSTS = [
    ("1", "EP-P0001-CPE", "CPE 1", "SITE0001", "10.20.0.13", "sm", "SITE0001-P2MP-1"),
    ("2", "EP-P0002-CPE", "CPE 2", "SITE0001", "10.20.0.14", "sm", "SITE0001-P2MP-1"),
    ("3", "EP-P0003-CPE-02", "CPE 3-02", "SITE0006", "10.20.2.11", "sm", "SITE0006-P2MP-1"),
    ("4", "EP-P0004-CPE", "CPE 4", "SITE0001", "10.20.0.15", "sm", "SITE0001-P2MP-1"),
    ("10", "SITE0001-P2MP-1", "SITE01_S1", "SITE0001", "10.20.0.16", "ap", ""),
    ("11", "SITE0001-P2MP-2", "SITE01_S2", "SITE0001", "10.20.0.17", "ap", ""),
    ("12", "SITE0006-P2MP-1", "SITE06_S1", "SITE0006", "10.20.3.11", "ap", ""),
    ("20", "SITE0001-WAVE-1", "SITE01_SITE00_Wave_P2P", "SITE0001", "10.20.0.18", "backhaul", ""),
]
ITEMS, V = [], {}


def item(hid, key, vt=0, name="", last="", vals=None):
    ITEMS.append({"itemid": str(100 + len(ITEMS)), "hostid": hid, "key_": key, "name": name or key,
                  "value_type": str(vt), "lastvalue": last})
    if vals:
        V[ITEMS[-1]["itemid"]] = vals


item("1", "wifi.rssi", vals=(-58, -66))            # dropped 8 dB -> WATCH
item("1", "wifi.snr", vals=(32, 27))
item("1", "wifi.dlmcs", 3, vals=(9, 7)); item("1", "wifi.ulmcs", 3, vals=(9, 8))
item("1", "wifi.link.ssid", 1, last="SITE01_S2")     # live on S2 although its parent is S1
item("2", "wifi.rssi", vals=(-60, -60)); item("2", "wifi.snr", vals=(30, 31))
item("2", "wifi.link.ssid", 1, last="SITE0001-S1")
item("3", "wifi.rssi", vals=(-77, -78)); item("3", "wifi.snr", vals=(14, 13))   # BAD
item("3", "wifi.link.ssid", 1, last="SITE06_S1")
item("4", "wifi.rssi", vals=(-61, None))           # down -> NO DATA
item("11", "wifi.sta.ulrssi[5]", name="Station 10.20.0.13 (00:04:56:aa:bb:cc): UL RSSI", vals=(-57, -73))
item("11", "wifi.sta.ulsnr[5]", name="Station 10.20.0.13 (00:04:56:aa:bb:cc): UL SNR", vals=(31, 25))
item("10", "wifi.sta.ulrssi[2]", name="Station 10.20.0.14 (00:04:56:aa:bb:01): UL RSSI", vals=(-59, -59))
item("20", "wifi.rssi", vals=(-50, -50))           # site radio: not in this check


class FakeAPI:
    def call(self, method, q):
        if method == "host.get":
            return [{"hostid": h, "host": n, "name": vn, "tags": [{"tag": "site", "value": s}, {"tag": "role", "value": r}]
                     + ([{"tag": "parent", "value": par}] if par else []),
                     "interfaces": [{"ip": ip, "main": "1", "type": "2"}]} for h, n, vn, s, ip, r, par in HOSTS
                    if "filter" not in q or n in q["filter"]["host"]]
        if method == "item.get":
            pre = tuple(q["search"]["key_"])
            return [i for i in ITEMS if i["hostid"] in q["hostids"] and i["key_"].startswith(pre)]
        if method == "history.get":
            out = []
            for iid in q["itemids"]:
                for t, v in zip((T0, T1), V.get(iid, ())):
                    if v is not None and q["time_from"] <= t + 60 <= q["time_till"]:
                        out.append({"itemid": iid, "value": str(v)})
            return out
        if method == "trend.get":
            return []
        if method == "event.get":
            return [{"name": "Radio not linked to its AP (scanning)", "hosts": [{"hostid": "2"}]}] * 3 + \
                   [{"name": "Device unreachable", "hosts": [{"hostid": "4"}]}]
        raise AssertionError(method)


pid, page, c, done = p.build("daily", D, store=True, api=FakeAPI())
check("daily saved", done and os.path.exists(p.path("daily", pid)))
check("counts", (c["endpoints"], c["bad"], c["watch"], c["ok"], c["nodata"]) == (4, 1, 2, 0, 1), c)
check("site radio not included", "SITE01_SITE00_Wave_P2P" not in page)
check("grouped by site", page.index("id='s-SITE0001'") < page.index("id='s-SITE0006'"))
check("sector from live SSID (S2, not the S1 parent)", "<td class=ap>SITE01_S2</td>" in page, "")
check("SSID shown when it differs from the AP name", "SSID SITE0001-S1" in page)
check("AP hears it read from the AP station table", "-73</span> / " in page)
check("AP-side weak signal flagged", "AP hears it below -70 dBm" in page)
check("drop reason", "signal dropped 8 dB" in page)
check("link drops", "link dropped 3 times" in page)
check("no data row", "NO DATA" in page and "down 1x" in page)
check("bad CPE", "SNR below 15 dB" in page and "signal below -75 dBm" in page)
check("visible names + endpoint", "CPE 3-02" in page and "P0003-CPE-02" in page)
check("never 'for management'", "for management" not in page.lower())
check("snapshot cached", os.path.exists(os.path.join(os.environ["REPORT_DIR"], "endpoint_signal-snap", f"{D}.json")))
_, sp, sc, _ = p.build("daily", D, store=True, api=FakeAPI(), site="SITE0006")
check("one site view", sc["endpoints"] == 1 and "CPE 1" not in sp and "Endpoint signal SITE0006" in sp, sc)
_, sp, sc, _ = p.build("weekly", D, store=False, api=FakeAPI())
check("weekly builds", sc["endpoints"] == 4 and "lowest" in sp, sc)
code, title, body, rep = p.web(FakeAPI(), [], {}, "/")
check("listing links the saved report", code == 200 and f"endpoint-signal/daily/{D}.html" in body)
code, _, b2, rep = p.web(FakeAPI(), ["now", "daily"], {"host": "EP-P0003-CPE-02"}, "/")
check("host menu opens its site", rep and "Endpoint signal SITE0006" in b2)
if os.environ.get("ENDPOINT_SIGNAL_PREVIEW"):
    open(os.environ["ENDPOINT_SIGNAL_PREVIEW"], "w").write(page)
print(f"{'ALL PASSED' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
