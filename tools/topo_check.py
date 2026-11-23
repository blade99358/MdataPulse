#!/usr/bin/env python3
"""Check the parent links in Zabbix against what the devices are doing now (read-only).

  python3 tools/topo_check.py

A device that answers ping while its parent does not, cannot really be behind that parent: the parent
is wrong, or it is a backup link that is off. Such a parent also hides alarms: Zabbix does not raise
"Device unreachable" for anything below a parent that is down, so a whole site can go down silently.
Lists, per site, the devices that are down and which device the alarm is shown on.
"""
import collections
import sys

from zbxapi import ZabbixAPI

api = ZabbixAPI()
hosts = api.call("host.get", {"output": ["hostid", "host", "status"], "selectTags": "extend",
                              "tags": [{"tag": "site", "operator": 4}]})
info = {}
for h in hosts:
    t = {x["tag"]: x["value"] for x in h["tags"]}
    info[h["host"]] = {"id": h["hostid"], "on": h["status"] == "0", "site": t.get("site", "?"),
                       "parent": t.get("parent", ""), "ping": None, "alarm": False}
by_id = {v["id"]: k for k, v in info.items()}
for i in api.call("item.get", {"output": ["hostid", "lastvalue", "lastclock"], "hostids": list(by_id),
                               "filter": {"key_": "icmpping"}, "monitored": True}):
    if i["lastclock"] != "0":
        info[by_id[i["hostid"]]]["ping"] = i["lastvalue"] == "1"
for t in api.call("trigger.get", {"output": ["triggerid"], "hostids": list(by_id), "selectHosts": ["hostid"],
                                  "filter": {"description": "Device unreachable", "value": 1}, "monitored": True}):
    info[by_id[t["hosts"][0]["hostid"]]]["alarm"] = True

wrong = []
for name, h in sorted(info.items()):
    p = info.get(h["parent"])
    if not h["on"] or not p:
        continue
    if not p["on"]:
        wrong.append(f"{name} ({h['site']}) hangs off {h['parent']}, which is DISABLED")
    elif h["ping"] and p["ping"] is False:
        wrong.append(f"{name} ({h['site']}) answers ping, its parent {h['parent']} ({p['site']}) does not")
print("Parent links that cannot be right:" if wrong else "Parent links: no contradiction found.")
for w in wrong:
    print("  " + w)


def shown_on(name):
    """The device whose 'Device unreachable' alarm covers this one (itself or the first parent with one)."""
    seen = set()
    while name in info and name not in seen:
        seen.add(name)
        if info[name]["alarm"]:
            return name
        name = info[name]["parent"]
    return None


down = collections.defaultdict(list)
for name, h in info.items():
    if h["on"] and h["ping"] is False:
        down[h["site"]].append(name)
print("\nDevices down now, per site:" if down else "\nNo device is down.")
for site in sorted(down):
    total = sum(1 for h in info.values() if h["site"] == site and h["on"])
    roots = collections.Counter(shown_on(n) or "NO ALARM ANYWHERE" for n in down[site])
    print(f"  {site}: {len(down[site])} of {total} down; alarm shown on " +
          ", ".join(f"{r} ({n})" for r, n in roots.most_common(3)) + (" ..." if len(roots) > 3 else ""))
sys.exit(1 if wrong else 0)
