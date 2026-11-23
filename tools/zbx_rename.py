#!/usr/bin/env python3
"""Set the Zabbix visible name of every device in data/device_names.txt.

Only the visible name changes. The technical host name stays, so dependencies, parent tags,
site-down alarms, maps, RCA and reports keep working.
Matches each row by IP, then by technical host name (Excel "Device" column).

  python3 tools/zbx_rename.py                 dry run: shows what would change
  python3 tools/zbx_rename.py --apply         renames; saves old names to examples/rename_backup_<time>.csv
  python3 tools/zbx_rename.py --undo FILE     puts the old names back (add --apply)
"""
import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zbxapi import ZabbixAPI  # noqa: E402
from site_conf import data_file  # noqa: E402

# ROWS: private site data, kept out of the repository. One row per line, read from
# $MDP_DATA_DIR/device_names.txt (default: data/device_names.txt next to tools/). See examples/device_names.txt.
ROWS = data_file("device_names.txt")


def rows():
    out = []
    for line in ROWS.strip().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        ip, dev, name = (x.strip() for x in line.split("|"))
        out.append({"ip": ip, "dev": dev, "name": name})
    return out


def load_hosts(api):
    hosts = api.call("host.get", {"output": ["hostid", "host", "name", "status"],
                                  "selectInterfaces": ["ip"], "selectTags": "extend"})
    by_ip, by_host = {}, {}
    for h in hosts:
        by_host[h["host"]] = h
        for i in h["interfaces"]:
            by_ip.setdefault(i["ip"], []).append(h)
    return hosts, by_ip, by_host


def plan(hosts, by_ip, by_host):
    todo, same, missing = [], [], []
    used = set()
    for r in rows():
        cand = by_ip.get(r["ip"], [])
        if len(cand) > 1:
            cand = [h for h in cand if h["host"] == r["dev"]] or cand
        h = cand[0] if len(cand) == 1 else by_host.get(r["dev"])
        if not h:
            h = next((x for x in hosts if x["name"] == r["name"]), None)
        if not h:
            missing.append(r)
            continue
        if h["hostid"] in used:
            print(f"  ! {r['ip']} {r['name']}: host {h['host']} already matched by another row, skipped")
            continue
        used.add(h["hostid"])
        (same if h["name"] == r["name"] else todo).append((h, r))
    return todo, same, missing, used


def rename(api, todo, hosts, apply):
    """Rename in passes so a name that another host still holds frees up first."""
    owner = {h["name"]: h["hostid"] for h in hosts}
    pending, done, blocked = list(todo), [], []
    while pending:
        nxt = []
        for h, new in pending:
            o = owner.get(new)
            if o and o != h["hostid"]:
                nxt.append((h, new))
                continue
            old = h["name"]
            if apply:
                api.call("host.update", {"hostid": h["hostid"], "name": new})
            owner.pop(old, None)
            owner[new] = h["hostid"]
            done.append(({**h, "name": old}, new))
        if len(nxt) == len(pending):
            blocked = nxt
            break
        pending = nxt
    return done, blocked


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo", metavar="CSV")
    a = ap.parse_args()
    api = ZabbixAPI()
    hosts, by_ip, by_host = load_hosts(api)
    byid = {h["hostid"]: h for h in hosts}

    if a.undo:
        with open(a.undo) as f:
            back = [(byid[r["hostid"]], r["old_name"]) for r in csv.DictReader(f)
                    if r["hostid"] in byid and byid[r["hostid"]]["name"] != r["old_name"]]
        done, blocked = rename(api, back, hosts, a.apply)
        for h, n in done:
            print(f"  {h['host']}: {h['name']} -> {n}")
        print(f"{'Restored' if a.apply else 'Would restore'} {len(done)} names; blocked {len(blocked)}.")
        return

    todo, same, missing, used = plan(hosts, by_ip, by_host)
    work = [(h, r["name"]) for h, r in todo]
    done, blocked = rename(api, work, hosts, False)
    if a.apply and done:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "examples")
        os.makedirs(base, exist_ok=True)
        path = os.path.abspath(os.path.join(base, time.strftime("rename_backup_%Y%m%d_%H%M%S.csv")))
        with open(path, "w", newline="") as f:          # saved before anything changes
            w = csv.writer(f)
            w.writerow(["hostid", "host", "old_name", "new_name"])
            for h, n in done:
                w.writerow([h["hostid"], h["host"], h["name"], n])
        print(f"Old names saved to {path}")
        done, blocked = rename(api, work, hosts, True)

    print("\nRENAME (technical name: old visible name -> new visible name)")
    for h, n in sorted(done, key=lambda x: x[1]):
        print(f"  {h['host']:<24} {h['name']:<44} -> {n}")
    if blocked:
        print("\nBLOCKED: another host already has this visible name")
        for h, n in blocked:
            print(f"  {h['host']} -> {n}")
    print(f"\nNOT FOUND in Zabbix ({len(missing)} rows of the Excel)")
    for r in missing:
        print(f"  {r['ip']:<15} {r['dev']:<24} {r['name']}")
    rest = sorted(h["host"] for h in hosts if h["hostid"] not in used and not h["host"].startswith("EP-")
                  and any(t["tag"] == "site" for t in h["tags"]))
    print(f"\nIn Zabbix but not in the Excel, names unchanged ({len(rest)}; endpoint hosts not listed):")
    for i in range(0, len(rest), 4):
        print("  " + "  ".join(f"{x:<24}" for x in rest[i:i + 4]))
    print(f"\n{'Renamed' if a.apply else 'Would rename'} {len(done)}, already right {len(same)}, "
          f"blocked {len(blocked)}, not found {len(missing)}."
          + ("" if a.apply else "  Dry run: add --apply to rename."))


if __name__ == "__main__":
    main()
