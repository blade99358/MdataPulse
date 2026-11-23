#!/usr/bin/env python3
"""Offline checks of the retention and sizing helpers (no Zabbix or database needed)."""
import gzip
import json
import os
os.environ.setdefault("RCA_TZ", "Etc/GMT-3")  # tests assume a UTC+3 day
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import zbx_data  # noqa: E402
import zbx_health  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name)
    fails += not cond


a, b = zbx_data.day_bounds(date(2026, 10, 25))
check("a UTC+3 day is 24 h and starts at 21:00 UTC", b - a == 86400 and a % 86400 == 75600)
check("delay with macro resolves", zbx_health.seconds("{$IF.INTERVAL}", lambda m: "5m") == 300)
check("flexible delay uses the base interval", zbx_health.seconds("1m;wd1-5,9:00-18:00") == 60)
check("cache size parsing", zbx_health.size("512M") == 512 * 1024 ** 2 and zbx_health.size("1G") == 1024 ** 3)

with tempfile.TemporaryDirectory() as root:
    d = date(2026, 10, 25)
    folder = zbx_data.day_dir(root, d)
    os.makedirs(folder)
    with gzip.open(os.path.join(folder, "items.csv.gz"), "wt") as f:
        f.write("101,SITE0001-SW-01,icmpping,3\n102,SITE0001-SW-01,system.hw.uptime,3\n103,OTHER,icmpping,3\n")
    with gzip.open(os.path.join(folder, "history_uint.csv.gz"), "wt") as f:
        f.write(f"101,{a + 120},1,0\n101,{a + 60},0,0\n102,{a + 60},5000,0\n103,{a + 60},1,0\n101,{b + 10},1,0\n")
    with gzip.open(os.path.join(folder, "history.csv.gz"), "wt") as f:
        f.write("")
    json.dump({"tables": {}}, open(os.path.join(folder, "MANIFEST.json"), "w"))
    s = zbx_data.read_archive(root, {("SITE0001-SW-01", "icmpping"): ("9001", 3),
                                     ("SITE0001-SW-01", "system.hw.uptime"): ("9002", 3)}, a, b)
    check("archive rows are matched by host and key to the current item ids", set(s) == {"9001", "9002"})
    check("archive series is sorted and limited to the window", s["9001"] == [(a + 60, 0.0), (a + 120, 1.0)])
    check("archived days are listed", zbx_data.archived_days(root) == [d])

sys.exit(1 if fails else 0)
