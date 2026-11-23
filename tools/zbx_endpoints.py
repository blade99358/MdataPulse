#!/usr/bin/env python3
"""Endpoints from the endpoint list (2026-11-17): set each endpoint radio's displayed name and the
aggregation site (site) it hangs off. Dry run by default.

  python3 tools/zbx_endpoints.py              show what would change
  python3 tools/zbx_endpoints.py --apply      make the changes

For every row (name | IP | endpoint | site), matched to a Zabbix host by IP:
  - visible name = the Excel name (the technical host name stays, it is what scripts use)
  - site: host group Sites/<site> (the old Sites/ group is removed) and tag site=<site>
  - parent (the sector AP the endpoint waits on for alarms), only when the site changed or the parent is
    missing or sits in another site: the AP whose name matches the endpoint's live "Connected AP" SSID
    (e.g. SITE0010-S1 = SITE10_S1), else the parent most endpoints of the site on that SSID already use, else
    the site's only AP; if none fits the endpoint has no parent and waits on "Site SITExxxx DOWN" only
  - IPs not in Zabbix yet: new host EP-<endpoint>-CPE[-02] with the ePMP templates
Then the trigger dependencies follow the new parents. Endpoints in Zabbix but not in the list are only listed.
"""
import argparse
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zbxapi import ZabbixAPI, ZabbixError   # noqa: E402
from site_conf import data_file              # noqa: E402
import site_conf as sc                       # noqa: E402
import zbx_sites as zs                       # noqa: E402

MODEL = "ePMP Force 4625"
SSID_ITEM = "Wireless: Connected AP (SM)"

# DATA: private site data, kept out of the repository. One row per line, read from
# $MDP_DATA_DIR/endpoints.txt (default: data/endpoints.txt next to tools/). See examples/endpoints.txt.
DATA = data_file("endpoints.txt")


def load(path):
    text = open(path, encoding="utf-8").read() if path else DATA
    rows = []
    for line in text.splitlines():
        if line.strip() and not line.startswith("#"):
            name, ip, endpoint, site = [x.strip() for x in line.split("|")]
            rows.append({"name": name, "ip": ip, "endpoint": endpoint, "site": site.upper()})
    for col in ("name", "ip"):
        dup = [v for v, n in collections.Counter(r[col] for r in rows).items() if n > 1]
        if dup:
            sys.exit(f"duplicate {col} in the list: {', '.join(dup)}")
    return rows


def sector_key(text):
    """SITE0010-S1, SITE10_S1, site35 s1 -> ('35', '1'); None when there is no site and sector in it."""
    m = re.search(sc.P + r"\D?0*(\d+)\D{0,3}S(?:ECTOR)?\D?0*(\d+)", (text or "").upper())
    return (m.group(1), m.group(2)) if m else None


def new_host_name(r, taken):
    endpoint = r["endpoint"]
    base = f"EP-P{int(endpoint):04d}" if endpoint.isdigit() else "EP-" + re.sub(r"[^A-Za-z0-9]+", "-", endpoint).strip("-")
    m = re.search(r"-(\d+)$", r["name"])
    name = f"{base}-CPE" + (f"-{m.group(1)}" if m and m.group(1) != "01" else "")
    while name in taken:
        name += "x"
    return name


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    ap.add_argument("--file", help="read the list from this file (name|ip|endpoint|site per line) instead of the built-in one")
    args = ap.parse_args()
    rows = load(args.file)
    api = ZabbixAPI()
    plan = zs.Planner(api, args.apply, False)
    try:
        print(f"Zabbix {api.version()} at {api.url}: {len(rows)} endpoints in the list")
        tpl = {t["host"]: t["templateid"] for t in api.call("template.get", {
            "output": ["host"], "filter": {"host": [zs.BASE, zs.EPMP]}})}
        if len(tpl) < 2:
            sys.exit("Templates missing: run zbx_sites.py templates --apply first.")
        hosts = api.call("host.get", {"output": ["host", "name", "status"], "selectHostGroups": ["groupid", "name"],
                                      "selectTags": "extend", "selectInterfaces": ["ip", "type"],
                                      "selectParentTemplates": ["host"]})
        by_ip = {}
        for h in hosts:
            for i in h["interfaces"]:
                by_ip.setdefault(i["ip"], h)
        by_host = {h["host"]: h for h in hosts}
        tag = lambda h, k: next((t["value"] for t in h["tags"] if t["tag"] == k), "")
        visible = {h["name"] for h in hosts}
        gids = plan.group_ids({f"Sites/{r['site']}" for r in rows} | {"Devices/Radios"})
        aps = collections.defaultdict(list)          # site -> AP hosts
        for h in hosts:
            if tag(h, "role") == "ap" and h["status"] == "0":
                aps[tag(h, "site")].append(h)
        ssid = {i["hostid"]: i["lastvalue"] for i in api.call("item.get", {
            "output": ["hostid", "lastvalue"], "filter": {"name": SSID_ITEM}, "templated": False})}

        listed = {r["ip"] for r in rows}
        # where each endpoint goes, before choosing parents (peers on the same SSID vote with their new site)
        for r in rows:
            r["h"] = by_ip.get(r["ip"])
            r["ssid"] = ssid.get(r["h"]["hostid"], "") if r["h"] else ""
            r["parent"] = tag(r["h"], "parent") if r["h"] else ""
            r["old_site"] = tag(r["h"], "site") if r["h"] else ""
        votes = collections.defaultdict(collections.Counter)   # (site, ssid) -> parents in use
        for r in rows:
            p = by_host.get(r["parent"])
            if r["ssid"] and p and tag(p, "site") == r["site"] and r["old_site"] == r["site"]:
                votes[(r["site"], r["ssid"])][r["parent"]] += 1

        def pick_parent(r):
            key = sector_key(r["ssid"])
            if key:
                m = [a for a in aps[r["site"]] if key in (sector_key(a["name"]), sector_key(a["host"]))]
                if len(m) == 1:
                    return m[0]["host"], f"matches its live SSID {r['ssid']}"
            if votes[(r["site"], r["ssid"])]:
                return votes[(r["site"], r["ssid"])].most_common(1)[0][0], f"other endpoints on SSID {r['ssid']} use it"
            if len(aps[r["site"]]) == 1:
                return aps[r["site"]][0]["host"], "the only AP of the site"
            return "", (f"live SSID {r['ssid']!r} matches no AP of {r['site']}" if r["ssid"] else "no live SSID yet") \
                + "; waits on Site DOWN only (run again once it is linked)"

        moved, renamed, created, reparented, unparented = 0, 0, 0, 0, []
        dep_rows = []
        taken = set(by_host)
        for r in rows:
            h, site = r["h"], r["site"]
            label = f"{r['name']} ({r['ip']}, endpoint {r['endpoint']}, {site})"
            p = by_host.get(r["parent"])
            if not h or not r["parent"] or not p or tag(p, "site") != site:
                parent, why = pick_parent(r)
            else:
                parent, why = r["parent"], ""
            if not parent:
                unparented.append(f"{label}: {why}")
            tags_want = {"site": site, "role": "sm", "vendor": "cambium", "model": MODEL, "parent": parent}
            tags = [{"tag": k, "value": v} for k, v in tags_want.items() if v]
            if not h:
                host = new_host_name(r, taken)
                taken.add(host)
                created += 1
                plan.act(f"{label}: NEW host {host} in Sites/{site}" + (f", parent {parent} ({why})" if parent else ""),
                         "host.create", {
                             "host": host, "name": r["name"],
                             "groups": [{"groupid": gids[f"Sites/{site}"]}, {"groupid": gids["Devices/Radios"]}],
                             "interfaces": [{"type": 2, "main": 1, "useip": 1, "ip": r["ip"], "dns": "", "port": "161",
                                             "details": zs.snmp_details({"vendor": "cambium"})}],
                             "templates": [{"templateid": tpl[zs.BASE]}, {"templateid": tpl[zs.EPMP]}],
                             "tags": tags})
                dep_rows.append({"host": host, "parent": parent, "site": site})
                continue
            upd, what = {"hostid": h["hostid"]}, []
            if h["name"] != r["name"]:
                if r["name"] in visible:
                    print(f"  SKIP name of {h['host']}: another host is already called {r['name']!r}")
                else:
                    upd["name"] = r["name"]
                    what.append(f"name {h['name']!r} -> {r['name']!r}")
                    renamed += 1
            if not h["host"].startswith("EP-"):   # found by discovery: give it a endpoint host name
                upd["host"] = new_host_name(r, taken)
                taken.add(upd["host"])
                what.append(f"host name {h['host']} -> {upd['host']}")
            groups = {g["groupid"] for g in h["hostgroups"] if not g["name"].startswith("Sites/")}
            groups |= {gids[f"Sites/{site}"], gids["Devices/Radios"]}
            if groups != {g["groupid"] for g in h["hostgroups"]}:
                upd["groups"] = [{"groupid": g} for g in sorted(groups)]
                old = [g["name"] for g in h["hostgroups"] if g["name"].startswith("Sites/")]
                what.append(f"site {', '.join(old) or '-'} -> Sites/{site}")
                moved += 1
            have = {t["tag"]: t["value"] for t in h["tags"]}
            if any(have.get(k, "") != v for k, v in tags_want.items()):
                upd["tags"] = [{"tag": t["tag"], "value": t["value"]} for t in h["tags"] if t["tag"] not in tags_want] + tags
                if have.get("parent", "") != parent:
                    what.append(f"parent {have.get('parent') or '-'} -> {parent or '-'}" + (f" ({why})" if why else ""))
                    reparented += 1
            if what or len(upd) > 1:
                plan.act(f"{label}: " + ("; ".join(what) or "tags"), "host.update", upd)
            missing_t = [t for t in (zs.BASE, zs.EPMP) if t not in {x["host"] for x in h["parentTemplates"]}]
            if missing_t:
                plan.act(f"{label}: link {', '.join(missing_t)}", "host.massadd",
                         {"hosts": [{"hostid": h["hostid"]}], "templates": [{"templateid": tpl[t]} for t in missing_t]})
            dep_rows.append({"host": upd.get("host", h["host"]), "parent": parent, "site": site})

        others = sorted(f"{h['host']} ({h['name']}, {next((i['ip'] for i in h['interfaces']), '')}, {tag(h, 'site')})"
                        for h in hosts if tag(h, "role") == "sm"
                        and not any(i["ip"] in listed for i in h["interfaces"]))
        plan.dependencies(dep_rows)
        print(f"\nSummary: {renamed} renamed, {moved} moved to another site, {reparented} new parent, {created} new hosts.")
        if unparented:
            print(f"{len(unparented)} endpoint(s) without a sector AP parent:\n  " + "\n  ".join(unparented))
        if others:
            print(f"{len(others)} endpoint radio(s) in Zabbix that are NOT in the list (left as they are):\n  " + "\n  ".join(others))
        print(f"{plan.changes} change(s) " + ("made. Now run: python3 tools/zbx_sites.py endpoints --apply" if args.apply
                                            else "planned; re-run with --apply to make them."))
    except ZabbixError as e:
        sys.exit(f"Zabbix API error: {e}")


if __name__ == "__main__":
    main()
