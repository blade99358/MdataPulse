#!/usr/bin/env python3
"""Organise Zabbix hosts by site from a CSV: groups, tags, templates, parents.

  export  hosts.csv            write current Zabbix hosts to a CSV to fill in
  apply   hosts.csv            show what would change (dry run, nothing written)
  apply   hosts.csv --apply    make the changes
  dashboards [--apply]         one dashboard per Sites/<code> group (dry run by default)
  overview   [--apply]         the overview dashboard and the network map
  discovery  [--apply]         network discovery rule + actions that add new devices automatically
  templates  [--apply]         import templates/mdp_templates.yaml (removes items the new version dropped)
  disable HOST... [--apply]    stop monitoring devices that are off on purpose (enable HOST... to undo)
  delete HOST... | --file F [--apply]   delete hosts by name or IP (their history goes too)
  prune --sites A,B [--apply]  delete every site host that is not in those sites (and their uplink)
  guard [--apply]              the "Zabbix server guard" host: cache, process, disk and backup alarms
  endpoints [--apply]              the "Endpoints" dashboard: one window per site with its endpoints (signal, SNR,
                               sector, uptime, latency)
  sitedown [--apply]           one "Site SITExxxx DOWN" alarm per site from data/main_links.txt; parents
                               follow the main link (apply does that too while main_links.txt exists)

CSV columns (header row required):
  host, name, ip, site, role, vendor, model, parent, poe, uplink_ports, snmp_community, snmp_version, monitor

  role:          backhaul | ptp | ap | sm | aggregation | access-switch
  vendor:        cambium | ubiquiti | unifi | ekit | linksys
  parent:        host name of the upstream device (empty for the site core)
  poe:           yes on PoE switches
  uplink_ports:  interface names separated by ; that should alert on link down
  snmp_community: optional, stored as a secret host macro {$SNMP_COMMUNITY}. UniFi rows
                 without one use the global macro {$SNMP_COMMUNITY_UNIFI}
  snmp_version:  2 (default) or 3. v3 uses authPriv SHA-256/AES128 with the global macros
                 {$SNMPV3_USER}, {$SNMPV3_AUTHPASS}, {$SNMPV3_PRIVPASS} (Administration > Macros)
  monitor:       optional. no = host disabled (not polled, no alarms, left out of dashboards and RCA),
                 yes = enabled. Empty = leave as it is in Zabbix.

Existing hosts are matched by host name, then by IP. Existing groups, tags,
macros and templates are kept; only the managed ones are added or set.
"""
import argparse
import csv
import os
import re
import sys

from zbxapi import ZabbixAPI, ZabbixError
import site_conf as sc

BASE = "MDP Network Base SNMP"
EPMP = "MDP Cambium ePMP SNMP"
POE = "MDP PoE Switch SNMP"
UPS = "MDP UPS SNMP"
WAVE = "MDP Ubiquiti Wave SNMP"
UNIFI = "MDP UniFi Switch SNMP"
LACP = "MDP LACP Trunk SNMP"
GUARD = "MDP Zabbix Server Guard"
GUARD_HOST = "Zabbix server guard"
ALL_TEMPLATES = [BASE, EPMP, POE, UPS, WAVE, UNIFI, LACP]
UNREACHABLE = "Device unreachable"
DIAG_CONN = "Outage cause: CONNECTIVITY (device did not reboot)"
DIAG_POWER = "Outage cause: POWER (device rebooted)"
LOSS = "High ICMP packet loss"
REBOOT = "Device rebooted (uptime below 10m)"
NOT_LINKED = "Radio not linked to its AP (scanning)"
# child trigger -> parent triggers it depends on (follows the parent tag)
# child trigger -> the parent's triggers it waits on, so only the device where a fault starts is reported
DEPENDS = {UNREACHABLE: [UNREACHABLE], DIAG_CONN: [DIAG_CONN, DIAG_POWER], DIAG_POWER: [DIAG_CONN, DIAG_POWER],
           LOSS: [LOSS, UNREACHABLE], REBOOT: [REBOOT, DIAG_POWER], NOT_LINKED: [UNREACHABLE]}
# ... and the device's own triggers: a reboot already explained by "Outage cause: POWER" is not repeated
SELF_DEPENDS = {REBOOT: [DIAG_POWER]}
UNIFI_COMMUNITY = "{$SNMP_COMMUNITY_UNIFI}"
SITE_TPL = "MDP Site Status"
# sites whose "Site DOWN" alarm is a Disaster (core sites), e.g. DISASTER_SITES=SITE0000,SITE0001  # SEVERITY-1
DISASTER_SITES = {s.strip().upper() for s in os.environ.get("DISASTER_SITES", sc.CORE_SITE).split(",") if s.strip()}
SITE_PREFIX = "Site "  # host "Site SITE0007" carries the "Site SITE0007 DOWN" alarm
MAIN_LINKS = os.environ.get("MAIN_LINKS") or os.path.join(sc.DATA_DIR, "main_links.txt")  # private, see examples/
# event correlation: a Site down closes the alarms that slipped out just before it (same site, sites behind)
CORRELATIONS = [
    ("MDP: Site down closes the device alarms of that site",
     [{"type": 5, "tag": "alarm", "value": "site-down", "operator": 0}, {"type": 3, "newtag": "sitedown", "oldtag": "site"},
      {"type": 4, "tag": "alarm", "value": "unreachable", "operator": 0}]),
    ("MDP: Site down closes the Site down alarms of the sites behind it",
     [{"type": 5, "tag": "alarm", "value": "site-down", "operator": 0},
      {"type": 3, "newtag": "sitedown", "oldtag": "upstream"},
      {"type": 4, "tag": "alarm", "value": "site-down", "operator": 0}]),
]

ROLE_TYPE = {
    "backhaul": "Radios", "ptp": "Radios", "ap": "Radios", "sm": "Radios",
    "aggregation": "Switches", "access-switch": "Switches", "ups": "UPS",
}
MANAGED_TAGS = ("site", "role", "vendor", "model", "parent")
COLUMNS = ["host", "name", "ip", "site", "role", "vendor", "model", "parent",
           "poe", "uplink_ports", "snmp_community", "snmp_version", "monitor"]
NO = ("no", "n", "0", "false", "off", "disabled")
YES = ("yes", "y", "1", "true", "on", "enabled")


def snmp_details(row):
    if row.get("snmp_version") == "3":
        return {"version": 3, "bulk": 1, "securityname": "{$SNMPV3_USER}", "securitylevel": 2,
                "authprotocol": 3, "authpassphrase": "{$SNMPV3_AUTHPASS}",
                "privprotocol": 1, "privpassphrase": "{$SNMPV3_PRIVPASS}", "contextname": ""}
    if row.get("vendor") == "unifi" and not row.get("snmp_community"):
        return {"version": 2, "bulk": 1, "community": UNIFI_COMMUNITY}
    return {"version": 2, "bulk": 1, "community": "{$SNMP_COMMUNITY}"}


# vendor-specific values for template macros (set as host macros by apply)
VENDOR_MACROS = {
    # LGS328PC firmware: PoE state 1 = searching, 2 = delivering, 4 = disabled; power in 0.1 W
    "linksys": {"{$POE.STATUS.MAP}": "1:2,2:3,3:4,4:1", "{$POE.POWER.MULT}": "0.1"},
}
# set on every host of the vendor
VENDOR_ALWAYS = {
    # power units are watched by ping; no "SNMP not responding" alarm until SNMP is confirmed on them
    "powercube": {"{$SNMP.ALERT}": "0"},
}


def row_has_poe(row):
    return row.get("poe", "").lower() in ("yes", "y", "1", "true")


def templates_for(row):
    t = [BASE]
    if row["role"] == "ups":
        t.append(UPS)
    if row["vendor"] == "cambium":
        t.append(EPMP)
    if row["vendor"] == "ubiquiti":
        t.append(WAVE)
    if row["vendor"] == "unifi":
        t.append(UNIFI)
    if row["vendor"] == "ekit":  # finds LACP trunks only; a switch without one gets no items from it
        t.append(LACP)
    if row_has_poe(row):
        t.append(POE)
    return t


def only_sites(rows, sites):
    """Rows of the given sites, plus the devices on their uplink path in other sites."""
    if not sites:
        return rows
    by = {r["host"]: r for r in rows}
    keep = {r["host"] for r in rows if r["site"] in sites}
    for h in list(keep):
        p = by[h].get("parent")
        while p and p in by and p not in keep:
            keep.add(p)
            p = by[p].get("parent")
    print(f"--sites {','.join(sorted(sites))}: {len(keep)} of {len(rows)} hosts")
    return [r for r in rows if r["host"] in keep]


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [{k.strip().lower(): (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(f)]
    errors = []
    names = {r["host"] for r in rows}
    for col in ("host", "name", "ip"):
        seen = {}
        for i, r in enumerate(rows, start=2):
            v = r.get(col) or (r.get("host") if col == "name" else "")
            if v and v in seen:
                errors.append(f"line {i}: duplicate {col} {v!r} (also line {seen[v]})")
            seen.setdefault(v, i)
    for i, r in enumerate(rows, start=2):
        for col in ("host", "ip", "site", "role"):
            if not r.get(col):
                errors.append(f"line {i}: missing {col}")
        if r.get("role") and r["role"] not in ROLE_TYPE:
            errors.append(f"line {i}: unknown role {r['role']!r}")
        r["vendor"] = r.get("vendor", "").lower()
        if r.get("parent") and r["parent"] not in names:
            errors.append(f"line {i}: parent {r['parent']!r} is not a host in this CSV")
        if r.get("monitor") and r["monitor"].lower() not in NO + YES:
            errors.append(f"line {i}: monitor must be yes or no, not {r['monitor']!r}")
    if errors:
        sys.exit("CSV problems:\n  " + "\n  ".join(errors))
    return rows


# ------------------------------------------------------------------ export
def cmd_export(api, path):
    hosts = api.call("host.get", {
        "output": ["host", "name"], "selectHostGroups": ["name"], "selectTags": "extend",
        "selectInterfaces": ["ip", "main"], "selectParentTemplates": ["host"],
    })
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS + ["current_groups", "current_templates"])
        w.writeheader()
        for h in sorted(hosts, key=lambda h: h["host"]):
            tags = {t["tag"]: t["value"] for t in h["tags"]}
            ip = next((i["ip"] for i in h["interfaces"] if i["main"] == "1"), "")
            site = tags.get("site") or next(
                (g["name"].split("/", 1)[1] for g in h["hostgroups"] if g["name"].startswith("Sites/")), "")
            w.writerow({
                "host": h["host"], "name": h["name"], "ip": ip, "site": site,
                "role": tags.get("role", ""), "vendor": tags.get("vendor", ""), "model": tags.get("model", ""),
                "parent": tags.get("parent", ""), "poe": "", "uplink_ports": "", "snmp_community": "",
                "snmp_version": "",
                "current_groups": "; ".join(g["name"] for g in h["hostgroups"]),
                "current_templates": "; ".join(t["host"] for t in h["parentTemplates"]),
            })
    print(f"Wrote {len(hosts)} hosts to {path}. Fill in site, role, vendor, model, parent, then run apply.")


# ------------------------------------------------------------------ apply
class Planner:
    def __init__(self, api, do_apply, replace_templates, rename=False):
        self.api = api
        self.do_apply = do_apply
        self.replace_templates = replace_templates
        self.rename = rename
        self.changes = 0

    def act(self, desc, method, params):
        self.changes += 1
        print(("  APPLY " if self.do_apply else "  would ") + desc)
        if self.do_apply:
            return self.api.call(method, params)
        return None

    def group_ids(self, names):
        existing = {g["name"]: g["groupid"] for g in
                    self.api.call("hostgroup.get", {"output": ["groupid", "name"], "filter": {"name": sorted(names)}})}
        for n in sorted(set(names) - set(existing)):
            res = self.act(f"create host group {n}", "hostgroup.create", {"name": n})
            existing[n] = res["groupids"][0] if res else f"<new:{n}>"
        return existing

    def run(self, rows):
        tpl = {t["host"]: t["templateid"] for t in
               self.api.call("template.get", {"output": ["host"], "filter": {"host": ALL_TEMPLATES}})}
        missing = set(ALL_TEMPLATES) - set(tpl)
        if missing:
            sys.exit(f"Import templates/mdp_templates.yaml first; missing: {', '.join(sorted(missing))}")

        wanted_groups = set()
        for r in rows:
            wanted_groups |= {f"Sites/{r['site']}", f"Devices/{ROLE_TYPE[r['role']]}"}
        gids = self.group_ids(wanted_groups)

        existing = self.api.call("host.get", {
            "output": ["host", "name", "status"], "selectHostGroups": ["groupid"], "selectTags": "extend",
            "selectMacros": "extend", "selectInterfaces": ["interfaceid", "ip", "type", "main", "details"],
            "selectParentTemplates": ["host"],
        })
        by_name = {h["host"]: h for h in existing}
        by_ip = {i["ip"]: h for h in existing for i in h["interfaces"]}

        for r in rows:
            h = by_name.get(r["host"]) or by_ip.get(r["ip"])
            print(f"{r['host']} ({r['ip']}): " + ("existing" if h else "new"))
            want_tpl = templates_for(r)
            groups = [gids[f"Sites/{r['site']}"], gids[f"Devices/{ROLE_TYPE[r['role']]}"]]
            tags = [{"tag": k, "value": r.get(k, "")} for k in MANAGED_TAGS if r.get(k)]
            macros = []
            if r.get("snmp_community"):
                macros.append({"macro": "{$SNMP_COMMUNITY}", "value": r["snmp_community"], "type": "1"})
            if r["vendor"] in VENDOR_MACROS and row_has_poe(r):
                macros += [{"macro": k, "value": v} for k, v in VENDOR_MACROS[r["vendor"]].items()]
            macros += [{"macro": k, "value": v} for k, v in VENDOR_ALWAYS.get(r["vendor"], {}).items()]
            if r["host"].startswith("EP-"):   # CPE-UPTIME-1: "CPE uptime below 7d" alarm on endpoint CPEs
                macros.append({"macro": "{$CPE.UPTIME.ALERT}", "value": "1"})
            mon = r.get("monitor", "").lower()
            status = 1 if mon in NO else 0 if mon in YES else None
            for port in filter(None, (p.strip() for p in r.get("uplink_ports", "").split(";"))):
                macros.append({"macro": f'{{$IF.LINKDOWN.ALERT:"{port}"}}', "value": "1"})

            if not h:
                self.act(f"create host in {', '.join(['Sites/' + r['site'], 'Devices/' + ROLE_TYPE[r['role']]])} "
                         f"with {', '.join(want_tpl)}", "host.create", {
                             "host": r["host"], "name": r.get("name") or r["host"],
                             "groups": [{"groupid": g} for g in groups],
                             "interfaces": [{"type": 2, "main": 1, "useip": 1, "ip": r["ip"], "dns": "",
                                             "port": "161",
                                             "details": snmp_details(r)}],
                             "templates": [{"templateid": tpl[t]} for t in want_tpl],
                             "tags": tags, "macros": macros, "status": status or 0,
                         })
                continue

            hid = h["hostid"]
            update = {"hostid": hid}
            if status is not None and int(h["status"]) != status:
                update["status"] = status
            if self.rename and h["host"] != r["host"]:
                update["host"] = r["host"]
                update["name"] = r.get("name") or r["host"]
            have_g = {g["groupid"] for g in h["hostgroups"]}
            if not set(groups) <= have_g:
                update["groups"] = [{"groupid": g} for g in sorted(have_g | set(groups))]
            have_tags = {t["tag"]: t["value"] for t in h["tags"]}
            if any(have_tags.get(t["tag"]) != t["value"] for t in tags):
                merged = [t for t in h["tags"] if t["tag"] not in MANAGED_TAGS] + tags
                update["tags"] = [{"tag": t["tag"], "value": t["value"]} for t in merged]
            have_m = {m["macro"]: m for m in h["macros"]}
            if any(m["macro"] not in have_m or (m.get("type") != "1" and have_m[m["macro"]]["value"] != m["value"])
                   for m in macros):
                keep = [{"hostmacroid": m["hostmacroid"]} for k, m in have_m.items()
                        if k not in {x["macro"] for x in macros}]
                update["macros"] = keep + macros
            if self.replace_templates:
                stale = [t for t in h["parentTemplates"] if t["host"] not in want_tpl]
                if stale:
                    update["templates_clear"] = [{"templateid": t["templateid"]} for t in stale]
            if len(update) > 1:
                what = ", ".join(("DISABLE" if update["status"] else "ENABLE") if k == "status" else k
                                 for k in update if k != "hostid")
                if "templates_clear" in update:
                    what += " (unlink and CLEAR " + ", ".join(t["host"] for t in h["parentTemplates"]
                                                              if t["host"] not in want_tpl) + ": their items and history are deleted)"
                self.act(f"update {what}", "host.update", update)
            want = snmp_details(r)
            for i in h["interfaces"]:
                if i["type"] != "2" or i["main"] != "1":
                    continue
                d = i["details"]
                if str(d.get("version")) != str(want["version"]) or \
                        (want["version"] == 2 and d.get("community") != want["community"]):
                    self.act(f"set SNMP interface to v{want['version']}"
                             + (f" with {want['community']}" if want["version"] == 2 else ""),
                             "hostinterface.update", {"interfaceid": i["interfaceid"], "details": want})
            have_t = {t["host"] for t in h["parentTemplates"]}
            add_t = [t for t in want_tpl if t not in have_t]
            if add_t:
                self.act(f"link {', '.join(add_t)}", "host.massadd",
                         {"hosts": [{"hostid": hid}], "templates": [{"templateid": tpl[t]} for t in add_t]})

        self.dependencies(rows)

    def dependencies(self, rows):
        """Follow the parent tag: a child's 'Device unreachable' depends on its parent's, and its
        'Outage cause' triggers depend on the parent's, so only the root device is reported."""
        if not self.do_apply:
            n = sum(1 for r in rows if r.get("parent") or r["site"])
            print(f"would set trigger dependencies on {n} hosts (unreachable and outage cause follow the parent)")
            return
        names = [r["host"] for r in rows]
        hostids = [h["hostid"] for h in self.api.call("host.get", {"output": ["hostid"], "filter": {"host": names}})]
        trig = self.api.call("trigger.get", {
            "output": ["triggerid", "description"], "filter": {"description": sorted(set(DEPENDS) | {DIAG_POWER})},
            "selectHosts": ["host"], "selectDependencies": ["triggerid"], "hostids": hostids,
        })
        by_host = {}
        for t in trig:
            by_host.setdefault(t["hosts"][0]["host"], {})[t["description"]] = t
        # "Device unreachable" also waits on its site's "Site down" (zbx_sites.py sitedown)
        site_down = {}   # SEVERITY-1: two Site DOWN triggers per site (High / Disaster), wait on both
        for t in self.api.call("trigger.get", {
                "output": ["triggerid"], "selectHosts": ["host"], "templated": False,
                "tags": [{"tag": "alarm", "value": "site-down", "operator": 1}]}):
            if t["hosts"][0]["host"].startswith(SITE_PREFIX):
                site_down.setdefault(t["hosts"][0]["host"][len(SITE_PREFIX):], []).append(t["triggerid"])
        todo = []
        for r in rows:
            p = r.get("parent")
            if r["host"] not in by_host:
                continue
            for name, parent_names in DEPENDS.items():
                child = by_host[r["host"]].get(name)
                if not child:
                    continue
                want = sorted(by_host[p][n]["triggerid"] for n in parent_names if n in by_host[p]) \
                    if p in by_host else []
                want = sorted(want + [by_host[r["host"]][n]["triggerid"] for n in SELF_DEPENDS.get(name, [])
                                      if n in by_host[r["host"]]]
                              + (site_down[r["site"]] if name == UNREACHABLE and r["site"] in site_down else []))
                if sorted(d["triggerid"] for d in child["dependencies"]) != want:
                    todo.append((r["host"], " and ".join(filter(None, [
                        p, f"Site {r['site']} DOWN" if name == UNREACHABLE and r["site"] in site_down else ""])), name, child, want))
        # clear stale dependencies first: when the topology changed, setting the new ones
        # directly could make a loop with an old one that has not been removed yet
        for host, p, name, child, want in todo:
            if child["dependencies"]:
                self.act(f"{host}: clear old dependencies of '{name}'", "trigger.update",
                         {"triggerid": child["triggerid"], "dependencies": []})
        for host, p, name, child, want in todo:
            if want:
                self.act(f"{host}: '{name}' depends on {p or 'its site / own outage cause'}", "trigger.update",
                         {"triggerid": child["triggerid"], "dependencies": [{"triggerid": i} for i in want]})


# ------------------------------------------------------------------ main links and Site down
def load_main_links(path):
    """data/main_links.txt, one site per line: 'SITE0016 <- SITE0015: SITE0015-WAVE-1 ~ SITE0016-WAVE-2'
    (near-end radio at the upstream site ~ far-end radio at the site; A + B = two radios in LACP).
    An LACP trunk whose radios cannot be reached is given by its switches instead, each with its trunk:
    'SITE0008 <- SITE0015: SITE0015-SW-1/Eth-Trunk1 ~ SITE0008-SW-1/Eth-Trunk1'.
    Returns {site: {"up": upstream site, "near": [hosts], "far": [hosts], "trunks": {host: trunk}}}."""
    links = {}
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            m = re.fullmatch("(" + sc.P + r"\d{4})\s*<-\s*(" + sc.P + r"\d{4})\s*:\s*(.+?)\s*~\s*(.+)", line, re.I)
            if not m:
                sys.exit(f"{path} line {n}: expected 'SITE <- UPSTREAM: NEAR ~ FAR', got {line!r}")
            ends = [r.strip().split("/", 1) for r in (m[3] + "+" + m[4]).split("+") if r.strip()]
            trunks = {e[0]: e[1] for e in ends if len(e) == 2}
            radios = lambda x: [r.strip().split("/", 1)[0] for r in x.split("+") if r.strip()]
            links[m[1].upper()] = {"up": m[2].upper(), "near": radios(m[3]), "far": radios(m[4]), "trunks": trunks}
    return links


def main_link_parents(rows, links):
    """Each far-end main radio goes under its near-end radio (never under a backup radio, whose dead parent
    would hide every alarm behind it), and what hung off the old uplink radio goes under the main one.
    At an LACP site (two radios) those devices get no parent: the Site down alarm covers both radios.
    Changes rows in place; returns [(host, old parent, new parent)]."""
    by = {r["host"]: r for r in rows}
    changes = []

    def setp(r, p):
        if r.get("parent", "") != p:
            changes.append((r["host"], r.get("parent", ""), p))
            r["parent"] = p

    for site, link in links.items():
        far = [h for h in link["far"] if h in by]
        if not far:
            continue
        old = {r["host"] for r in rows if r["site"] == site and r["host"] not in far
               and by.get(r.get("parent"), {}).get("site", site) != site}
        for i, h in enumerate(far):
            near = link["near"][min(i, len(link["near"]) - 1)]
            if near in by:
                setp(by[h], near)
        for r in rows:
            if r["site"] == site and r["host"] not in far and r.get("parent") in old | set(far):
                setp(r, "" if len(far) > 1 else far[0])
    for r in rows:  # a wrong main_links.txt must not make a parent loop
        seen, p = {r["host"]}, r.get("parent")
        while p in by:
            if p in seen:
                sys.exit(f"main links make a parent loop at {r['host']} (check {MAIN_LINKS})")
            seen.add(p)
            p = by[p].get("parent")
    return changes


def cmd_sitedown(api, path, do_apply):
    """One host "Site <code>" per site with the MDP Site Status template and its "Site <code> DOWN" alarm;
    parent and mainlink tags from the main links; dependencies and event correlation so that a cut link
    gives one problem."""
    links = load_main_links(path)
    pl = Planner(api, do_apply, False)
    tpl = api.call("template.get", {"output": ["templateid"], "filter": {"host": [SITE_TPL]}})
    if not tpl:
        sys.exit(f"Template {SITE_TPL} not found: run 'templates --apply' first.")
    hosts = api.call("host.get", {"output": ["hostid", "host", "status"], "selectTags": "extend",
                                  "selectMacros": "extend", "selectParentTemplates": ["templateid"],
                                  "selectHostGroups": ["groupid"]})
    by = {h["host"]: h for h in hosts}
    rows = []
    for h in hosts:
        t = {x["tag"]: x["value"] for x in h["tags"]}
        if t.get("site"):
            rows.append({"host": h["host"], "site": t["site"], "role": t.get("role", ""), "parent": t.get("parent", "")})
    in_zabbix = {r["site"] for r in rows}
    ping = {i["hosts"][0]["host"]: i["lastvalue"] for i in api.call("item.get", {
        "output": ["lastvalue"], "selectHosts": ["host"], "filter": {"key_": "icmpping"}, "monitored": True})}
    # safety: a main-link radio that is silent while its site answers is not the live link (a backup that
    # is off, or a wrong line): as a parent it would hide the site's alarms, so that site is left out
    for site, link in sorted(links.items()):
        up = [r["host"] for r in rows if r["site"] == site and ping.get(r["host"]) == "1"]
        dead = [f for f in link["far"] if ping.get(f) == "0"]
        if up and len(dead) == len(link["far"]):
            print(f"  SKIP    {site}: main-link radio {', '.join(dead)} does not answer ping but {len(up)} devices "
                  f"at the site do, so it cannot be the live link. Fix {os.path.basename(path)} and run again.")
            del links[site]

    print("Parents and main-link radios:")
    new_parent = {h: p for h, _, p in main_link_parents(rows, links)}
    far_of = {f: s for s, link in links.items() for f in link["far"]}
    for h in hosts:
        name = h["host"]
        have = {(x["tag"], x["value"]) for x in h["tags"] if x["tag"] in ("parent", "mainlink")}
        want = {(k, v) for k, v in have if k == "parent"}
        if name in new_parent:
            want = {("parent", new_parent[name])} if new_parent[name] else set()
        if name in far_of:
            want.add(("mainlink", far_of[name]))
        if want != have:
            tags = [{"tag": x["tag"], "value": x["value"]} for x in h["tags"] if x["tag"] not in ("parent", "mainlink")]
            what = []
            if name in new_parent:
                old = dict(have).get("parent", "")
                what.append(f"parent {old or '-'} -> {new_parent[name] or '(none: LACP site, Site down covers it)'}")
            if ("mainlink", far_of.get(name)) in want - have:
                what.append(f"main link of {far_of[name]}")
            elif any(k == "mainlink" for k, _ in have - want):
                what.append("no longer a main-link radio")
            pl.act(f"{name}: " + ", ".join(what), "host.update",
                   {"hostid": h["hostid"], "tags": tags + [{"tag": k, "value": v} for k, v in sorted(want)]})

    def chain(site):
        out, s = [], links.get(site, {}).get("up")
        while s and s not in out and s != site:
            out.append(s)
            s = links.get(s, {}).get("up")
        return out

    print("Site hosts:")
    gids = pl.group_ids({f"Sites/{s}" for s in links if s in in_zabbix})
    for site, link in sorted(links.items()):
        if site not in in_zabbix:
            print(f"  skip    {site}: not in Zabbix yet")
            continue
        missing = [f for f in link["far"] if f not in by]
        if missing:
            print(f"  skip    {site}: main-link radio {', '.join(missing)} is not a Zabbix host")
            continue
        behind = sorted(s for s in links if site in chain(s) and s in in_zabbix)
        end = lambda h: f"{h} {link['trunks'][h]}" if h in link["trunks"] else h
        macros = {"{$SITE.CODE}": site,
                  "{$SITE.MAINLINK}": f"{' + '.join(map(end, link['near']))} ~ {' + '.join(map(end, link['far']))}"
                                      + (" (LACP)" if link["trunks"] else ""),
                  "{$SITE.CUTOFF}": f". Also cut off: {', '.join(behind)}" if behind else "",
                  "{$SITE.UPSTREAM}": link["up"],
                  "{$SITE.DISASTER}": "1" if site in DISASTER_SITES else "0"}   # SEVERITY-1   # "Site X DOWN via upstream" alarm (MDP Site Status)
        tags = [{"tag": "class", "value": "site"}, {"tag": "sitedown", "value": site}] + \
               [{"tag": "upstream", "value": u} for u in chain(site)]
        name = SITE_PREFIX + site
        h = by.get(name)
        if not h:
            pl.act(f"create host {name} (main link {macros['{$SITE.MAINLINK}']})", "host.create", {
                "host": name, "groups": [{"groupid": gids[f"Sites/{site}"]}],
                "templates": [{"templateid": tpl[0]["templateid"]}], "tags": tags,
                "macros": [{"macro": k, "value": v} for k, v in macros.items()],
                "description": f"Site status of {site}: one 'Site {site} DOWN' alarm when its main link is lost "
                               "(zbx_sites.py sitedown, data/main_links.txt)."})
            continue
        have_m = {m["macro"]: m["value"] for m in h["macros"]}
        have_t = sorted((x["tag"], x["value"]) for x in h["tags"])
        update = {}
        if any(have_m.get(k) != v for k, v in macros.items()):
            update["macros"] = [{"macro": k, "value": v} for k, v in {**have_m, **macros}.items()]
        if have_t != sorted((x["tag"], x["value"]) for x in tags):
            update["tags"] = tags
        if tpl[0]["templateid"] not in {t["templateid"] for t in h["parentTemplates"]}:
            update["templates"] = [{"templateid": t["templateid"]} for t in h["parentTemplates"]] + \
                                  [{"templateid": tpl[0]["templateid"]}]
        if update:
            pl.act(f"update {name}: {', '.join(update)}", "host.update", {"hostid": h["hostid"], **update})
        else:
            print(f"  exists  {name}")

    print("LACP trunk labels:")
    for site, link in sorted(links.items()):
        for host, trunk in sorted(link["trunks"].items()):
            h = by.get(host)
            if not h:
                print(f"  skip    {host}: not a Zabbix host yet")
                continue
            key, val = f'{{$LACP.LABEL:"{trunk}"}}', f" ({site} main link)"
            mine = [m for m in h["macros"] if m["macro"] == key]
            if mine and mine[0]["value"] == val:
                print(f"  exists  {host} {trunk}{val}")
                continue
            # the host's other macros are kept by id (a secret macro's value is never read back)
            keep = [{"hostmacroid": m["hostmacroid"]} for m in h["macros"] if m["macro"] != key]
            pl.act(f"{host}: {trunk} labelled '{site} main link'", "host.update", {"hostid": h["hostid"], "macros":
                   keep + [{"macro": key, "value": val, "description": "set by zbx_sites.py sitedown (main_links.txt)"}]})

    print("Dependencies:")
    if do_apply:
        sd = {}   # SEVERITY-1: two Site DOWN triggers per site (High / Disaster)
        for t in api.call("trigger.get", {
                "output": ["triggerid"], "selectHosts": ["host"], "selectDependencies": ["triggerid"],
                "templated": False, "tags": [{"tag": "alarm", "value": "site-down", "operator": 1}]}):
            if t["hosts"][0]["host"].startswith(SITE_PREFIX):
                sd.setdefault(t["hosts"][0]["host"][len(SITE_PREFIX):], []).append(t)
        for site, ts in sorted(sd.items()):
            up = links.get(site, {}).get("up")
            want = sorted(x["triggerid"] for x in sd.get(up, []))
            for t in ts:
                if sorted(d["triggerid"] for d in t["dependencies"]) != want:
                    pl.act(f"Site {site} DOWN depends on " + (f"Site {up} DOWN" if want else "nothing"),
                           "trigger.update", {"triggerid": t["triggerid"],
                                              "dependencies": [{"triggerid": i} for i in want]})
    else:
        print("  would make each Site down depend on its upstream site's, and each Device unreachable on its site's")
    pl.dependencies(rows)

    print("Event correlation:")
    have = {c["name"]: c for c in api.call("correlation.get", {"output": ["correlationid", "name"],
                                                               "selectFilter": "extend"})}
    for name, conds in CORRELATIONS:
        body = {"filter": {"evaltype": 0, "conditions": conds}, "operations": [{"type": 0}], "status": 0}
        if name not in have:
            pl.act(f"create correlation '{name}'", "correlation.create", {"name": name, **body})
        else:
            print(f"  exists  {name}")
    print(f"\n{pl.changes} change(s) {'made' if do_apply else 'planned; re-run with --apply to make them'}.")


# ------------------------------------------------------------------ enable / disable / prune / templates
def cmd_status(api, names, disable, do_apply):
    """Disable (or enable) hosts by host name, visible name or IP. A disabled host is not polled,
    raises no alarm and is left out of the dashboards and the RCA report; its history is kept."""
    hosts = api.call("host.get", {"output": ["hostid", "host", "name", "status"],
                                  "selectInterfaces": ["ip"]})
    want = 1 if disable else 0
    todo, unknown = [], []
    for n in names:
        h = next((h for h in hosts if n in (h["host"], h["name"]) or any(i["ip"] == n for i in h["interfaces"])), None)
        if not h:
            unknown.append(n)
        elif int(h["status"]) == want:
            print(f"  already {'disabled' if disable else 'enabled'}  {h['host']}")
        else:
            todo.append(h)
            print(("  APPLY " if do_apply else "  would ") + f"{'disable' if disable else 'enable'} {h['host']}")
    if unknown:
        print("  not found: " + ", ".join(unknown))
    if do_apply and todo:
        api.call("host.massupdate", {"hosts": [{"hostid": h["hostid"]} for h in todo], "status": want})
    print(f"{len(todo)} host(s) {'changed' if do_apply else 'to change; re-run with --apply'}.")
    if disable and todo:
        print("Also set monitor=no for them in your CSV, or the next 'apply' with monitor=yes would turn them back on.")


def cmd_delete(api, names, do_apply):
    """Delete hosts by host name, visible name or IP (history and problems go with them)."""
    hosts = api.call("host.get", {"output": ["hostid", "host", "name"], "selectInterfaces": ["ip"]})
    todo, unknown = [], []
    for n in names:
        h = next((h for h in hosts if n in (h["host"], h["name"]) or any(i["ip"] == n for i in h["interfaces"])), None)
        if h:
            todo.append(h)
            print(("  APPLY " if do_apply else "  would ") + f"delete {h['host']}")
        else:
            unknown.append(n)
    if unknown:
        print(f"  not in Zabbix ({len(unknown)}): " + ", ".join(unknown[:10]) + (" ..." if len(unknown) > 10 else ""))
    for i in range(0, len(todo), 100) if do_apply else []:
        api.call("host.delete", [h["hostid"] for h in todo[i:i + 100]])
    print(f"{len(todo)} host(s) {'deleted' if do_apply else 'to delete; re-run with --apply'}.")


def cmd_prune(api, rows, sites, do_apply):
    """Delete the hosts this package manages that are outside the given sites (and their uplink).

    Only hosts in a Sites/*, Devices/* or Discovered/* group or with a site tag are touched; anything
    else (the Zabbix server itself, hosts you made by hand elsewhere) is listed and left alone."""
    keep = {r["host"] for r in only_sites(rows, sites)}
    keep_ips = {r["ip"] for r in rows if r["host"] in keep}
    hosts = api.call("host.get", {"output": ["hostid", "host", "name"], "selectHostGroups": ["name"],
                                  "selectTags": "extend", "selectInterfaces": ["ip"]})
    delete, other = [], []
    for h in hosts:
        groups = [g["name"] for g in h["hostgroups"]]
        site = next((t["value"] for t in h["tags"] if t["tag"] == "site"), "")
        managed = site or any(g.startswith(("Sites/", "Devices/", "Discovered/")) for g in groups)
        if h["host"] in keep or any(i["ip"] in keep_ips for i in h["interfaces"]):
            continue
        if managed and h["host"] != "Zabbix server":
            delete.append((site or next((g for g in groups if g.startswith(("Sites/", "Discovered/"))), "?"), h))
        else:
            other.append(h["host"])
    by_site = {}
    for site, h in delete:
        by_site.setdefault(site.replace("Sites/", ""), []).append(h["host"])
    for site in sorted(by_site):
        names = sorted(by_site[site])
        print(f"  {'APPLY ' if do_apply else 'would '}delete {len(names):3d} host(s) of {site}: "
              + ", ".join(names[:6]) + (f" and {len(names) - 6} more" if len(names) > 6 else ""))
    if other:
        print(f"  left alone ({len(other)}, not managed by this package): " + ", ".join(sorted(other)[:10])
              + (" ..." if len(other) > 10 else ""))
    print(f"  kept: {len(keep)} host(s) of {', '.join(sorted(sites))} and their uplink devices")
    # dashboards of the sites that go away
    dash = [d for d in api.call("dashboard.get", {"output": ["dashboardid", "name"]})
            if d["name"].startswith("Site ") and d["name"][5:] in by_site and d["name"][5:] not in sites]
    if dash:
        print(f"  {'APPLY ' if do_apply else 'would '}delete {len(dash)} site dashboard(s): "
              + ", ".join(d["name"] for d in dash))
    if not do_apply:
        print(f"\n{len(delete)} host(s) would be DELETED with all their history. This cannot be undone:\n"
              "back up the database first (see the guide), then re-run with --apply.")
        return
    ids = [h["hostid"] for _, h in delete]
    for i in range(0, len(ids), 100):
        api.call("host.delete", ids[i:i + 100])
    if dash:
        api.call("dashboard.delete", [d["dashboardid"] for d in dash])
    # empty Sites/* groups of removed sites
    empty = [g for g in api.call("hostgroup.get", {"output": ["groupid", "name"], "search": {"name": "Sites/"},
                                                  "startSearch": True, "selectHosts": "count"})
             if int(g["hosts"]) == 0 and g["name"][6:] not in sites]
    if empty:
        try:
            api.call("hostgroup.delete", [g["groupid"] for g in empty])
        except ZabbixError as e:   # a group still used by an action or a map stays
            print(f"  note: some empty groups were kept ({e})")
    print(f"Deleted {len(ids)} host(s), {len(dash)} dashboard(s), {len(empty)} empty site group(s).")


TEMPLATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates", "mdp_templates.yaml")


def cmd_templates(api, path, do_apply):
    """Import the template file. Unlike the web import default it also deletes items, triggers and
    discovery rules the new version no longer has (e.g. the old ePMP scan-list rule)."""
    src = open(path, encoding="utf-8").read()
    both = {"createMissing": True, "updateExisting": True}
    rules = {"template_groups": both, "templates": both, "valueMaps": {**both, "deleteMissing": True},
             "templateLinkage": {"createMissing": True},
             "items": {**both, "deleteMissing": True}, "triggers": {**both, "deleteMissing": True},
             "discoveryRules": {**both, "deleteMissing": True}, "graphs": {**both, "deleteMissing": True},
             "templateDashboards": {**both, "deleteMissing": True}}
    print(("  APPLY " if do_apply else "  would ") + f"import {os.path.relpath(path)} (create, update, delete missing)")
    if do_apply:
        api.call("configuration.import", {"format": "yaml", "source": src, "rules": rules})
        print("Templates imported.")


# ------------------------------------------------------------------ discovery
# sysObjectID enterprise number -> (label, extra templates, type group)
VENDORS = [
    ("17713", "cambium", [EPMP], "Devices/Radios"),
    ("41112", "ubiquiti", [WAVE], "Devices/Radios"),
    ("2011", "huawei-ekit", [POE], "Devices/Switches"),
    ("3955", "linksys", [POE], "Devices/Switches"),
    ("4413", "unifi", [UNIFI], "Devices/Switches"),
]
DISCOVERED = "Discovered/Network"
DRULE = "MDP network discovery"


def cmd_discovery(api, ranges, delay, do_apply, ups_range=""):
    """Create a network discovery rule and one action per vendor.

    The rule pings and queries SNMP (v2c with {$SNMP_COMMUNITY}, v3 with the {$SNMPV3_*} macros)
    over the given ranges. A device that answers SNMP is added to Discovered/Network with the
    base template, named after its sysName; vendor actions add the vendor template and type group.
    Site, role and parent still come from hosts.csv (apply matches discovered hosts by IP).
    """
    def act(desc, method, params):
        print(("  APPLY " if do_apply else "  would ") + desc)
        return api.call(method, params) if do_apply else None

    tpl = {t["host"]: t["templateid"] for t in
           api.call("template.get", {"output": ["host"], "filter": {"host": ALL_TEMPLATES}})}
    if set([BASE, EPMP, POE]) - set(tpl):
        sys.exit("Import templates/mdp_templates.yaml first.")
    names = [DISCOVERED, "Devices/UPS"] + sorted({v[3] for v in VENDORS})
    groups = {g["name"]: g["groupid"] for g in
              api.call("hostgroup.get", {"output": ["groupid", "name"], "filter": {"name": names}})}
    for n in names:
        if n not in groups:
            res = act(f"create host group {n}", "hostgroup.create", {"name": n})
            groups[n] = res["groupids"][0] if res else "0"

    rule = api.call("drule.get", {"output": "extend", "selectDChecks": "extend", "filter": {"name": DRULE}})
    checks = [
        {"type": 12, "uniq": 0},                                              # ICMP
        {"type": 11, "key_": "1.3.6.1.2.1.1.2.0", "snmp_community": "{$SNMP_COMMUNITY}",
         "ports": "161", "uniq": 0},                                          # SNMPv2 sysObjectID
        {"type": 11, "key_": "1.3.6.1.2.1.1.5.0", "snmp_community": "{$SNMP_COMMUNITY}",
         "ports": "161", "uniq": 0, "host_source": 3, "name_source": 3},      # SNMPv2 sysName -> host name
        {"type": 13, "key_": "1.3.6.1.2.1.1.2.0", "ports": "161", "uniq": 0,
         "snmpv3_securityname": "{$SNMPV3_USER}", "snmpv3_securitylevel": 2,
         "snmpv3_authprotocol": 3, "snmpv3_authpassphrase": "{$SNMPV3_AUTHPASS}",
         "snmpv3_privprotocol": 1, "snmpv3_privpassphrase": "{$SNMPV3_PRIVPASS}"},  # SNMPv3 sysObjectID
        {"type": 11, "key_": "1.3.6.1.2.1.1.2.0", "snmp_community": UNIFI_COMMUNITY,
         "ports": "161", "uniq": 0},                                          # SNMPv2 UniFi community
    ]
    if rule:
        druleid = rule[0]["druleid"]
        have_c = rule[0]["dchecks"]
        upd = {"druleid": druleid, "iprange": ranges, "delay": delay}
        missing = [c for c in checks if not any(h["type"] == str(c["type"]) and h.get("key_", "") == c.get("key_", "")
                                                and h.get("snmp_community", "") == c.get("snmp_community", "")
                                                for h in have_c)]
        if missing:  # existing checks are passed back by id, so the actions that use them keep working
            upd["dchecks"] = [{k: v for k, v in h.items() if k != "druleid"} for h in have_c] + missing
        act(f"update discovery rule '{DRULE}': {ranges}, every {delay}"
            + (f", add {len(missing)} check(s)" if missing else ""), "drule.update", upd)
        dchecks = api.call("dcheck.get", {"output": "extend", "druleids": [druleid]}) if do_apply else have_c
    else:
        res = act(f"create discovery rule '{DRULE}': {ranges}, every {delay}", "drule.create",
                  {"name": DRULE, "iprange": ranges, "delay": delay, "dchecks": checks, "status": 0})
        druleid = res["druleids"][0] if res else "0"
        dchecks = api.call("dcheck.get", {"output": "extend", "druleids": [druleid]}) if res else []

    def find_check(type_, key, community=None):
        return next((c["dcheckid"] for c in dchecks if c["type"] == str(type_) and c["key_"] == key
                     and (community is None or c.get("snmp_community") == community)), "0")

    v2_oid = find_check(11, "1.3.6.1.2.1.1.2.0", "{$SNMP_COMMUNITY}")
    v3_oid = find_check(13, "1.3.6.1.2.1.1.2.0")
    unifi_oid = find_check(11, "1.3.6.1.2.1.1.2.0", UNIFI_COMMUNITY)
    have = {a["name"]: a for a in api.call("action.get", {"output": ["actionid", "name"], "selectFilter": "extend",
                                                          "filter": {"eventsource": 1}})}

    def action(name, conditions, ops, formula):
        f = {"evaltype": 3, "formula": formula, "conditions": conditions}
        if name in have:
            old = have[name]["filter"]
            # Zabbix renames the formula ids, so compare the conditions themselves
            if sorted((c["conditiontype"], c["operator"], c["value"]) for c in old["conditions"]) \
                    == sorted((str(c["conditiontype"]), str(c["operator"]), c["value"]) for c in conditions):
                print(f"  exists  action '{name}'")
            else:
                act(f"update conditions of action '{name}'", "action.update",
                    {"actionid": have[name]["actionid"], "filter": f})
            return
        act(f"create action '{name}'", "action.create",
            {"name": name, "eventsource": 1, "status": 0, "filter": f, "operations": ops})

    def cond(fid, ctype, value, op=0):
        return {"conditiontype": ctype, "operator": op, "value": value, "formulaid": fid}

    rule_c = cond("A", 18, druleid)                     # discovery rule
    up_c = cond("B", 10, "0")                           # discovery status = Up
    any_snmp = [cond("C", 19, v2_oid), cond("D", 19, v3_oid), cond("F", 19, unifi_oid)]
    base_ops = [
        {"operationtype": 2},                                                  # add host
        {"operationtype": 4, "opgroup": [{"groupid": groups[DISCOVERED]}]},    # add to group
        {"operationtype": 6, "optemplate": [{"templateid": tpl[BASE]}]},       # link base template
    ]
    action("MDP discovery: add SNMP devices", [rule_c, up_c] + any_snmp, base_ops,
           formula="A and B and (C or D or F)")
    for num, label, extra, group in VENDORS:
        ops = [{"operationtype": 2}, {"operationtype": 4, "opgroup": [{"groupid": groups[group]}]},
               {"operationtype": 13, "optag": [{"tag": "vendor", "value": label}]}]
        if extra:
            ops.append({"operationtype": 6, "optemplate": [{"templateid": tpl[t]} for t in extra]})
        prefix = f"1.3.6.1.4.1.{num}."
        # the UPS range holds only UPS cards (Huawei UPS cards share enterprise 2011 with the eKit)
        action(f"MDP discovery: {label}",
               [rule_c, up_c] + any_snmp + [cond("E", 12, prefix, op=2), cond("G", 7, ups_range, op=1)],
               ops, formula="A and B and (C or D or F) and E and G")
    if UPS in tpl:
        action("MDP discovery: UPS", [rule_c, up_c] + any_snmp + [cond("G", 7, ups_range)],
               [{"operationtype": 2}, {"operationtype": 4, "opgroup": [{"groupid": groups["Devices/UPS"]}]},
                {"operationtype": 13, "optag": [{"tag": "role", "value": "ups"}]},
                {"operationtype": 6, "optemplate": [{"templateid": tpl[UPS]}]}],
               formula="A and B and (C or D or F) and G")
    print("Discovery hosts appear under " + DISCOVERED + ". Run 'apply hosts.csv --apply' afterwards to "
          "set site, role and parent (hosts are matched by IP).")


def site_groups(api, sites=None):
    groups = api.call("hostgroup.get", {"output": ["groupid", "name"], "search": {"name": "Sites/"},
                                        "startSearch": True})
    if sites:
        groups = [g for g in groups if g["name"].split("/", 1)[1] in sites]
        missing = set(sites) - {g["name"].split("/", 1)[1] for g in groups}
        if missing:
            print("  note: no host group yet for " + ", ".join(sorted(missing)) + " (run apply first)")
    return sorted(groups, key=lambda g: g["name"])


def site_widgets(gid, y=0):
    """The widgets of one site's dashboard: totals, a device status board, problems, radios."""
    g = [gid]
    return [
        {"type": "problemsbysv", "name": "Open problems (click a number for the list)", "x": 0, "y": y,
         "width": 44, "height": 3,
         "fields": f_groups(g) + [f_int("show_type", 0), f_int("hide_empty_groups", 0), f_int("layout", 0)]},
        {"type": "hostavail", "name": "SNMP monitoring", "x": 44, "y": y, "width": 28, "height": 3,
         "fields": f_groups(g) + [f_int("interface_type.0", 2)]},
        status_board("Devices (down first)", 0, y + 3, 44, 14, g),
        problems("Current problems: root cause devices", 44, y + 3, 28, 14, g, tags_shown=0),
        radio_board("Radio links (weakest first)", 0, y + 17, 44, 10, g),
        problems("Last outages: POWER or CONNECTIVITY", 44, y + 17, 28, 10, g, "diagnosis", show=1),
    ]


def topitems(name, x, y, w, h, gids, cols, layout=0, count=50, top_items=None, role=None):
    """Top items table for discovered items, whose names differ per host and port (wildcards allowed,
    e.g. 'Link to *: TX capacity'). cols = [(item pattern, thresholds, decimals)].
    layout 0 = one row per host, a column per item; 1 = one row per item, a column per host, and
    top_items keeps only the N highest values."""
    f = f_groups(gids) + [f_int("layout", layout), f_int("show_column_header", 1), f_int("problems", 2),
                          f_int("host_ordering_limit", count)]
    if top_items:
        f += [f_int("item_ordering_order_by", 3), f_int("item_ordering_order", 2), f_int("item_ordering_limit", top_items)]
    else:
        f += [f_int("item_ordering_order_by", 2), f_int("item_ordering_limit", 100)]
    if role:   # only hosts with this role tag
        f += [f_str("host_tags.0.tag", "role"), f_int("host_tags.0.operator", 1), f_str("host_tags.0.value", role)]
    for i, (pattern, thresholds, decimals) in enumerate(cols):
        f.append(f_str(f"columns.{i}.items.0", pattern))
        if decimals is not None:
            f.append(f_int(f"columns.{i}.decimal_places", decimals))
        for j, (v, colour) in enumerate(thresholds):
            f += [f_str(f"columns.{i}.thresholds.{j}.color", colour), f_str(f"columns.{i}.thresholds.{j}.threshold", str(v))]
    return {"type": "topitems", "name": name, "x": x, "y": y, "width": w, "height": h, "fields": f}


def core_pages(gid, all_gids, site, mapid=None):
    """The detailed dashboard of the core site (SITE0000): every other site hangs off it, so it shows
    the sites behind it, the core devices, every backhaul link leaving it, the switches' health and power."""
    g = [gid]
    any_of = lambda *values: [f_int("evaltype", 2)] + [x for i, v in enumerate(values[1:], start=1) for x in (
        f_str(f"tags.{i}.tag", "component"), f_int(f"tags.{i}.operator", 1), f_str(f"tags.{i}.value", v))]
    sites = {"type": "problemsbysv", "name": f"Sites behind {site}: open problems", "x": 44, "y": 0,
             "width": 28, "height": 8,
             "fields": f_groups(all_gids) + [f_int("show_type", 0), f_int("hide_empty_groups", 0), f_int("layout", 0)]}
    top = [{"type": "map", "name": "Sites behind the core: red site = a device there has a problem, red line = link down",
            "x": 0, "y": 0, "width": 44, "height": 8, "fields": [{"type": 8, "name": "sysmapid.0", "value": mapid}]},
           sites] if mapid else [{**sites, "x": 0, "width": 72}]
    return [
        {"name": "Core", "widgets": top + [
            status_board(f"{site} devices (down first)", 0, 8, 44, 9, g),
            {"type": "hostavail", "name": f"{site}: SNMP monitoring", "x": 44, "y": 8, "width": 28, "height": 3,
             "fields": f_groups(g) + [f_int("interface_type.0", 2)]},
            problems(f"{site}: current problems, root cause devices", 44, 11, 28, 6, g, tags_shown=0),
        ]},
        {"name": "Backhaul", "widgets": [
            radio_board("Backhaul radios at the core: weakest signal first", 0, 0, 44, 7, g, 40, role="backhaul"),
            table("Point-to-point masters: far end connected", 44, 0, 28, 7, g, [
                col("Far end", "Wireless: Connected stations (AP)", thresholds=[(0, RED), (1, GREEN)], **STATE),
                col("Signal", "Wireless: Weakest station signal (dBm)", 2, SIGNAL, -90, -40, 0),
            ], 1, 40, role="backhaul"),
            problems("Backhaul problems (radio and link)", 0, 7, 72, 5, g, "component", "wireless"),
            topitems("Busiest ports of the core switches now (traffic in)", 0, 12, 36, 6, g,
                     [("Interface *: Bits received", [], 1)], layout=1, top_items=15, role="aggregation"),
            topitems("Busiest ports of the core switches now (traffic out)", 36, 12, 36, 6, g,
                     [("Interface *: Bits sent", [], 1)], layout=1, top_items=15, role="aggregation"),
            ap_board(f"{site} sector AP: connected endpoints", 0, 18, 30, 4, g),
            radio_board(f"Endpoints on the {site} sector", 30, 18, 42, 4, g, 20, role="sm"),
        ]},
        {"name": "Switches & power", "widgets": [
            ups_board(f"{site} UPS (red = on battery)", 0, 0, 40, 3, g),
            problems("POWER and CONNECTIVITY outages at the core", 40, 0, 32, 5, g, "diagnosis", show=1),
            table("PoE switches: power drawn", 0, 3, 40, 2, g, [
                col("PoE power", "PoE: Total power consumption", decimals=1)], 1, 20, order=2),
            topitems("UniFi core switches: fans, power supplies, temperature", 0, 5, 40, 5, g, [
                ("Fan *: State", [], 0), ("Power supply *: State", [], 0),
                ("Temperature sensor *", [(0, GREEN), (60, AMBER), (75, RED)], 0)], layout=0, role="aggregation"),
            problems("Switch problems: ports, fans, power supplies, PoE", 40, 5, 32, 5, g, "component", "hardware",
                     extra=any_of("hardware", "network", "poe")),
            table("Most recent reboots at the core", 0, 10, 72, 5, g,
                  [col("Up for", "Hardware uptime"), col("SNMP uptime", "SNMP agent uptime")], 1, 15),
        ]},
    ]


def overview_map(api):
    """The map made by 'overview' (Network map, or '<name> map' when it was built for a few sites)."""
    maps = {m["name"]: m["sysmapid"] for m in api.call("map.get", {"output": ["sysmapid", "name"]})}
    name = MAP_NAME if MAP_NAME in maps else next((n for n in sorted(maps) if n.lower().endswith("overview map")), None)
    return maps.get(name)


def cmd_dashboards(api, do_apply, recreate=False, sites=None, core=sc.CORE_SITE):
    """Create a 'Site <code>' dashboard per Sites/* group (or only the given sites).
    The core site gets the detailed version (core_pages)."""
    groups = site_groups(api, sites)
    site_names = {"Site " + g["name"].split("/", 1)[1] for g in groups}
    existing = {d["name"]: d["dashboardid"] for d in api.call("dashboard.get", {"output": ["dashboardid", "name"]})}
    if recreate:
        ids = [i for n, i in existing.items() if n in site_names]
        print(("  APPLY " if do_apply else "  would ") + f"delete {len(ids)} site dashboard(s) to rebuild them")
        if do_apply and ids:
            api.call("dashboard.delete", ids)
        existing = {n: i for n, i in existing.items() if n not in site_names}
    n = 0
    for g in groups:
        name = "Site " + g["name"].split("/", 1)[1]
        if name in existing:
            continue
        n += 1
        code = g["name"].split("/", 1)[1]
        detailed = code == core
        pages = core_pages(g["groupid"], [x["groupid"] for x in site_groups(api)], code, overview_map(api)) if detailed \
            else [{"widgets": site_widgets(g["groupid"])}]
        print(("  APPLY " if do_apply else "  would ") + f"create dashboard {name}"
              + (f" (detailed core dashboard, {len(pages)} pages)" if detailed else ""))
        if do_apply:
            api.call("dashboard.create", {"name": name, "display_period": 60, "auto_start": 0 if detailed else 1,
                                          "pages": pages})
    print(f"{n} dashboard(s) {'created' if do_apply else 'planned'}.")


OVERVIEW = "Network overview"
RED, AMBER, GREEN, GREY = "E45959", "FFA059", "59DB8F", "97AAB3"


def f_int(name, v):
    return {"type": 0, "name": name, "value": v}


def f_str(name, v):
    return {"type": 1, "name": name, "value": v}


def f_groups(gids):
    return [{"type": 2, "name": f"groupids.{i}", "value": g} for i, g in enumerate(gids)]


def problems(name, x, y, w, h, gids, tag=None, value="", show=3, extra=(), tags_shown=None):
    if tags_shown is None:   # filtered lists already say what they hold: give the space to the host name
        tags_shown = 0 if tag or extra else 2
    f = f_groups(gids) + [f_int("show", show), f_int("show_tags", tags_shown), f_int("tag_name_format", 0), f_int("show_timeline", 0),
                          f_str("tag_priority", "diagnosis,site,role"), f_int("show_lines", 25)]
    if tag:
        f += [f_str("tags.0.tag", tag), f_int("tags.0.operator", 1 if value else 4), f_str("tags.0.value", value)]
    return {"type": "problems", "name": name, "x": x, "y": y, "width": w, "height": h, "fields": f + list(extra)}


def col(title, item, display=1, thresholds=(), mn="", mx="", decimals=None, text=False):
    """One Top hosts column: display 1 = value, 2 = bar, 3 = indicators. thresholds = [(value, colour)].
    text = a text item (shown as it is, no thresholds)."""
    return {"name": title, "item": item, "display": display, "thresholds": list(thresholds),
            "min": mn, "max": mx, "decimals": decimals, "text": text}


def table(name, x, y, w, h, gids, cols, sort_col, count=100, order=3, role=None, ip=False):
    """Top hosts table with coloured cells; sort_col counts from 1 (0 is the host name; the IP column is
    not counted). order 3 = lowest values first (down devices, weakest signal), 2 = highest first.
    role = a role tag value or a tuple of them (any of). ip = an IP column after the name: it has a value
    on every host, so such a table must filter by role, or every device would be listed."""
    f = f_groups(gids) + [f_str("columns.0.name", "Device"), f_int("columns.0.data", 2)]
    first = 1
    if ip:   # {HOST.CONN}: the address Zabbix polls, on screen without opening the host
        f += [f_str("columns.1.name", "IP"), f_int("columns.1.data", 3), f_str("columns.1.text", "{HOST.CONN}")]
        first = 2
    for i, c in enumerate(cols, start=first):
        f += [f_str(f"columns.{i}.name", c["name"]), f_str(f"columns.{i}.item", c["item"]),
              f_int(f"columns.{i}.data", 1)]
        if c["text"]:
            f.append(f_int(f"columns.{i}.display_value_as", 1))
        if c["display"] != 1:
            f.append(f_int(f"columns.{i}.display", c["display"]))
        if c["min"] != "":
            f.append(f_str(f"columns.{i}.min", str(c["min"])))
        if c["max"] != "":
            f.append(f_str(f"columns.{i}.max", str(c["max"])))
        if c["decimals"] is not None:
            f.append(f_int(f"columns.{i}.decimal_places", c["decimals"]))
        for j, (v, colour) in enumerate(c["thresholds"]):
            f += [f_str(f"columnsthresholds.{i}.color.{j}", colour), f_str(f"columnsthresholds.{i}.threshold.{j}", str(v))]
    f += [f_int("column", sort_col + first - 1 if sort_col else 0), f_int("show_lines", count), f_int("order", order)]
    # leave out the "Site SITExxxx" status host (zbx_sites.py sitedown): it has no IP or device items
    f += [f_str("tags.0.tag", "sitedown"), f_int("tags.0.operator", 5), f_str("tags.0.value", "")]
    # several values of one tag are OR-ed (evaltype 0 = And/Or)
    for i, r in enumerate((role,) if isinstance(role, str) else role or (), start=1):
        f += [f_str(f"tags.{i}.tag", "role"), f_int(f"tags.{i}.operator", 1), f_str(f"tags.{i}.value", r)]
    return {"type": "tophosts", "name": name, "x": x, "y": y, "width": w, "height": h, "fields": f}


UP_DOWN = [(0, RED), (1, GREEN)]
STATE = {"decimals": 0}   # mapped states (Up / Down, available ...) without "(1.00)"
RADIOS = ("backhaul", "ptp", "ap", "sm")


def status_board(name, x, y, w, h, gids, count=100):
    return table(name, x, y, w, h, gids, [
        col("Ping", "ICMP ping", thresholds=UP_DOWN, **STATE),
        col("Loss", "ICMP loss", 2, [(0, GREEN), (10, AMBER), (30, RED)], 0, 100, 0),
        col("Latency", "ICMP response time", decimals=1),
        col("Up for", "SNMP agent uptime"),
        col("SNMP", "SNMP agent availability", thresholds=[(0, RED), (1, GREEN), (2, GREY)], **STATE),
    ], 1, count, ip=True)


SIGNAL = [(-200, RED), (-75, AMBER), (-65, GREEN)]


def radio_board(name, x, y, w, h, gids, count=60, role=RADIOS):
    return table(name, x, y, w, h, gids, [
        col("Signal", "Wireless: Signal (dBm)", 2, SIGNAL, -90, -40, 0),
        col("SNR", "Wireless: SNR (dB)", thresholds=[(-100, RED), (15, AMBER), (25, GREEN)], decimals=0),
        col("Link", "Wireless: Link state (SM)", thresholds=[(1, RED), (3, GREEN)], **STATE),
        col("Linked to", "Wireless: Connected AP (SM)", text=True),
    ], 1, count, role=role, ip=True)


def ap_board(name, x, y, w, h, gids, count=30):
    return table(name, x, y, w, h, gids, [
        col("Stations", "Wireless: Connected stations (AP)", thresholds=[(0, RED), (1, GREEN)], **STATE),
        col("Weakest station", "Wireless: Weakest station signal (dBm)", 2, SIGNAL, -90, -40, 0),
    ], 2, count, role="ap", ip=True)


def ups_board(name, x, y, w, h, gids):
    return table(name, x, y, w, h, gids, [
        col("Output", "UPS: Output source", thresholds=[(0, GREY), (3, GREEN), (4, AMBER), (5, RED), (6, AMBER)],
            **STATE),
        col("Charge", "UPS: Battery charge", 2, [(0, RED), (30, AMBER), (60, GREEN)], 0, 100, 0),
        col("Runtime", "UPS: Estimated runtime"),
        col("Input V", "UPS: Input voltage", decimals=0),
        col("Load", "UPS: Output load", 2, [(0, GREEN), (70, AMBER), (90, RED)], 0, 100, 0),
    ], 1, 50, order=2, role="ups", ip=True)


def host_navigator(name, x, y, w, h, gids):
    """Device list grouped by site, each device coloured by its worst problem."""
    return {"type": "hostnavigator", "name": name, "x": x, "y": y, "width": w, "height": h,
            "fields": f_groups(gids) + [f_int("group_by.0.attribute", 0), f_int("status", 0),
                                        f_int("show_lines", 500)]}


# ------------------------------------------------------------------ network map
MAP_NAME = "Network map"


def site_link_info(api, groups):
    """{(site A, site B): {"triggers": set, "radios": set}} for every radio link between two sites, from the
    parent tags. radios = the hosts at site A that carry the link (two for an LACP pair)."""
    names = {g["name"].split("/", 1)[1] for g in groups}
    hosts = api.call("host.get", {"output": ["hostid", "host"], "selectTags": "extend", "monitored_hosts": True,
                                  "groupids": [g["groupid"] for g in groups]})
    info = {h["host"]: {t["tag"]: t["value"] for t in h["tags"]} | {"hostid": h["hostid"]} for h in hosts}
    unreach = {t["hosts"][0]["host"]: t["triggerid"] for t in api.call("trigger.get", {
        "output": ["triggerid"], "selectHosts": ["host"], "hostids": [h["hostid"] for h in hosts],
        "filter": {"description": ["Device unreachable"]}})} if hosts else {}
    links = {}
    for hn, t in info.items():
        p = t.get("parent")
        if p in info and t.get("site") != info[p].get("site") and t.get("site") in names and info[p].get("site") in names:
            link = links.setdefault((info[p]["site"], t["site"]), {"triggers": set(), "radios": set()})
            link["triggers"].update(x for x in (unreach.get(hn), unreach.get(p)) if x)
            link["radios"].add(p)
    return links


def site_links(api, groups):
    """(site A, site B, [triggerids]) for every radio link between two sites, from the parent tags."""
    return [(a, b, sorted(v["triggers"])) for (a, b), v in sorted(site_link_info(api, groups).items())]


def radio_label(site, radios):
    """WAVE-1 for SITE0015-WAVE-1; an LACP pair SITE0015-WAVE-3 + SITE0015-WAVE-4 gives WAVE-3+4."""
    short = sorted(r[len(site) + 1:] if r.startswith(site + "-") else r for r in radios)
    if len(short) > 1 and len({s.rsplit("-", 1)[0] for s in short}) == 1:
        return short[0] + "+" + "+".join(s.rsplit("-", 1)[1] for s in short[1:])
    return " + ".join(short)


# status icons of the map: drawn here and uploaded once (Administration > General > Images)
MAP_ICONS = {"off": ("MDP site OK", (46, 125, 50)), "on": ("MDP site problem", (198, 40, 40)),
             "maintenance": ("MDP site maintenance", (31, 95, 153)), "disabled": ("MDP site disabled", (138, 150, 163))}


def icon_png(rgb, size=30):
    """A filled circle with a white ring, as PNG bytes (no imaging library needed)."""
    import struct
    import zlib
    rows, c, r = [], (size - 1) / 2, size / 2 - 1
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            d = ((x - c) ** 2 + (y - c) ** 2) ** .5
            a = max(0.0, min(1.0, r + .5 - d))
            col = rgb if d < r - 3 else (255, 255, 255) if d < r - 1.2 else (90, 90, 90)
            row += bytes(col) + bytes([int(255 * a)])
        rows.append(bytes(row))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def map_icons(api, do_apply):
    """{state: imageid} of the map's status icons, uploading the ones Zabbix does not have yet."""
    import base64
    have = {i["name"]: i["imageid"] for i in api.call("image.get", {"output": ["imageid", "name"],
                                                                    "filter": {"imagetype": 1}})}
    out = {}
    for state, (name, rgb) in MAP_ICONS.items():
        if name not in have:
            print(("  APPLY " if do_apply else "  would ") + f"upload map icon '{name}'")
            if do_apply:
                have[name] = api.call("image.create", {"name": name, "imagetype": 1,
                                                       "image": base64.b64encode(icon_png(rgb)).decode()})["imageids"][0]
        out[state] = have.get(name) or have.get("Network_(24)") or next(iter(have.values()))
    return out


def icon_fields(icons):
    return {"iconid_off": icons["off"], "iconid_on": icons["on"], "iconid_maintenance": icons["maintenance"],
            "iconid_disabled": icons["disabled"], "use_iconmap": 0}


def save_map(api, body, do_apply, lookup=None, what=""):
    existing = api.call("map.get", {"output": ["sysmapid"], "filter": {"name": lookup or body["name"]}})
    print(("  APPLY " if do_apply else "  would ") + ("update" if existing else "create")
          + f" map '{body['name']}'{what}")
    if not do_apply:
        return existing[0]["sysmapid"] if existing else None
    if existing:
        api.call("map.update", {"sysmapid": existing[0]["sysmapid"], **body})
        return existing[0]["sysmapid"]
    return api.call("map.create", body)["sysmapids"][0]


# no halos or arrows: the icon colour is the status; problems show as a count, not their long names
MAP_OPTIONS = {"label_type": 0, "label_location": 0, "highlight": 0, "expandproblem": 0, "markelements": 0,
               "show_unack": 0, "severity_min": 2}


def branch_map(api, name, head, codes, kids, links, gid, icons, do_apply, lookup=None):
    """The map of one branch: a tree from the branch head down, every link labelled with its radio.
    Opens when the branch is clicked on the overview."""
    level, slot, leaves = {}, {}, [0]

    def place(c, d):
        level[c] = d
        ks = [k for k in kids.get(c, []) if k in codes and k not in level]
        for k in ks:
            place(k, d + 1)
        slot[c] = (slot[ks[0]] + slot[ks[-1]]) / 2 if ks else leaves[0]
        leaves[0] += 0 if ks else 1

    place(head, 0)
    col_w, row_h = 130, 130
    width = max(400, col_w * leaves[0] + 60)
    off = (width - col_w * leaves[0]) / 2 + col_w / 2
    sel, elements = {}, []
    for c in sorted(level, key=lambda c: (level[c], slot[c])):
        sel[c] = str(len(elements) + 1)
        elements.append({"selementid": sel[c], "elementtype": 3, "elements": [{"groupid": gid[c]}], **icon_fields(icons),
                         "label": c, "label_location": 0, "x": int(off + slot[c] * col_w) - 15, "y": 40 + level[c] * row_h})
    maplinks = [{"selementid1": sel[a], "selementid2": sel[b], "color": "2E7D32", "drawtype": 2,
                 "label": radio_label(a, v["radios"]), "indicator_type": 1 if v["triggers"] else 0,
                 "linktriggers": [{"triggerid": t, "color": "DD0000", "drawtype": 2} for t in sorted(v["triggers"])]}
                for (a, b), v in sorted(links.items()) if a in sel and b in sel]
    body = {"name": name, "width": width, "height": 100 + row_h * (max(level.values()) + 1), **MAP_OPTIONS,
            "selements": elements, "links": maplinks}
    return save_map(api, body, do_apply, lookup, f": {len(elements)} sites")


def build_map(api, groups, name, do_apply, lookup=None):
    """The branch board: the core site on top, one panel per branch (the sites that hang off one backhaul
    link of the core). The panel's first icon is the whole branch: red when any device in it has a problem,
    a click opens the branch map. Below it every site of the branch; sites further down say which site
    they hang off. Backhaul links from the core turn red when a radio on them is unreachable."""
    links = site_link_info(api, groups)
    codes = sorted(g["name"].split("/", 1)[1] for g in groups)
    gid = {g["name"].split("/", 1)[1]: g["groupid"] for g in groups}
    try:   # a site with two links in (main + backup) goes under its main link
        main = {k: v["up"] for k, v in load_main_links(MAIN_LINKS).items()}
    except (OSError, SystemExit):
        main = {}
    kids, parent = {}, {}
    for a, b in sorted(links, key=lambda ab: (main.get(ab[1], ab[0]) != ab[0], ab)):
        if b not in parent:
            kids.setdefault(a, []).append(b)
            parent[b] = a
    roots = [c for c in codes if c not in parent]
    core = sc.CORE_SITE if sc.CORE_SITE in roots else (roots[0] if roots else None)
    if core is None:
        return None
    heads = kids.get(core, []) + [r for r in roots if r != core]

    def under(c):   # the sites behind c, nearest first
        out = list(kids.get(c, []))
        for k in out:
            out += [x for x in kids.get(k, []) if x not in out]
        return out

    icons = map_icons(api, do_apply)
    # one map per branch first: the overview needs their ids
    sub = {}
    for h in heads:
        members = [h] + under(h)
        bname = f"{name}: {h} branch"
        blook = f"{lookup}: {h} branch" if lookup else None
        sub[h] = (members, branch_map(api, bname, h, set(members), kids, links, gid, icons, do_apply, blook))
    gap, cell_w, cell_h, top = 20, 95, 86, 170
    ncols = {h: 3 if len(sub[h][0]) > 6 else 2 for h in heads}   # big branches get a third column
    width = max(800, gap + sum(24 + ncols[h] * cell_w + gap for h in heads))
    elements, shapes, maplinks, sel = [], [], [], {}
    cx = width // 2
    sel[core] = "1"
    elements.append({"selementid": "1", "elementtype": 3, "elements": [{"groupid": gid[core]}], **icon_fields(icons),
                     "label": f"{core}\ncore", "label_location": 0, "x": cx - 15, "y": 30})
    height, x = top + 200, gap
    for h in heads:
        members, mapid = sub[h]
        nc = ncols[h]
        pw = 24 + nc * cell_w
        rows = -(-len(members) // nc)
        ph = 120 + rows * cell_h
        height = max(height, top + ph + 30)
        shapes.append({"type": 0, "x": x, "y": top, "width": pw, "height": ph, "text": "",
                       "border_type": 1, "border_width": 1, "border_color": "8A96A3", "background_color": "",
                       "zindex": 0})
        n = len(members)
        head_sel = str(len(elements) + 1)
        if mapid:   # dry run of a new install: the branch map does not exist yet
            elements.append({"selementid": head_sel, "elementtype": 1, "elements": [{"sysmapid": mapid}],
                             **icon_fields(icons), "label": f"{h} branch\n{n} site{'s' if n > 1 else ''}",
                             "label_location": 0, "x": x + pw // 2 - 15, "y": top + 14})
        for j, c in enumerate(members):
            sel[c] = str(len(elements) + 1)
            via = "" if parent.get(c) in (core, h) else f"\nvia {parent[c]}"
            elements.append({"selementid": sel[c], "elementtype": 3, "elements": [{"groupid": gid[c]}],
                             **icon_fields(icons), "label": c + via, "label_location": 0,
                             "x": x + 12 + (j % nc) * cell_w + cell_w // 2 - 15, "y": top + 120 + (j // nc) * cell_h - 10})
        v = links.get((core, h))
        if v and mapid:
            maplinks.append({"selementid1": "1", "selementid2": head_sel, "color": "2E7D32", "drawtype": 2,
                             "label": radio_label(core, v["radios"]), "indicator_type": 1 if v["triggers"] else 0,
                             "linktriggers": [{"triggerid": t, "color": "DD0000", "drawtype": 2}
                                              for t in sorted(v["triggers"])]})
        x += pw + gap
    body = {"name": name, "width": width, "height": height, **MAP_OPTIONS,
            "selements": elements, "shapes": shapes, "links": maplinks}
    return save_map(api, body, do_apply, lookup, f": {len(heads)} branches, {len(sel)} sites")


def map_rows(api, mapid):
    """Dashboard rows (about 70 px each) the map needs to show without scrolling."""
    if not mapid:
        return 9
    m = api.call("map.get", {"output": ["height"], "sysmapids": [mapid]})
    return max(9, -(-(int(m[0]["height"]) + 40) // 70)) if m else 9


def netmap_widget(api):
    """MDP-NETMAP: URL widget with the MDataPulse geo map, or None (then the branch board map is used)."""
    if os.environ.get("NETMAP_OFF") == "1":
        return None
    try:
        import zbx_netmap
        return zbx_netmap.widget(api)
    except Exception as ex:  # noqa: BLE001
        print(f"  note    geo map not used ({ex})")
        return None


def cmd_overview(api, do_apply, sites=None, name=OVERVIEW, rename_from=None):
    """The overview dashboard: site status and map, then device, radio and power pages.

    With sites (e.g. the MVP sites) everything is limited to those sites (and the sites on their uplink)."""
    groups = site_groups(api, sites)
    if sites:   # the uplink devices sit in other site groups: show those sites too
        groups = site_groups(api, sites | uplink_sites(api, groups))
    if not groups:
        sys.exit("No Sites/* host groups yet: run apply first.")
    gids = [g["groupid"] for g in groups]
    lookup = {"dashboard": name, "map": f"{name} map"}
    if rename_from:   # keep the dashboard (and its map) under the new name: same id, links and sharing stay
        for kind, old, new in (("dashboard", rename_from, name), ("map", f"{rename_from} map", f"{name} map")):
            idf = "dashboardid" if kind == "dashboard" else "sysmapid"
            have = api.call(f"{kind}.get", {"output": [idf], "filter": {"name": [old]}})
            if have and not api.call(f"{kind}.get", {"output": [idf], "filter": {"name": [new]}}):
                print(("  APPLY " if do_apply else "  would ") + f"rename {kind} '{old}' to '{new}'")
                if do_apply:
                    api.call(f"{kind}.update", {idf: have[0][idf], "name": new})
                else:   # dry run: nothing renamed yet, so look up the old one for the plan below
                    lookup[kind] = old
    mapid = build_map(api, groups, MAP_NAME if not sites else f"{name} map", do_apply,
                      lookup=None if not sites else lookup["map"])
    narrow = {"tags_shown": 0}
    # the map first, then the per-site counts: both grow with the number of sites
    mh = map_rows(api, mapid)
    geo = netmap_widget(api)   # MDP-NETMAP: the live geo map replaces the branch board on the dashboard
    mh = 14 if geo else mh
    ph = max(5, 2 + -(-len(groups) // 2))   # about two site rows per dashboard row
    net = [
        geo or ({"type": "map", "name": "Network by branch: red = problem, red line = backhaul down",
          "x": 0, "y": 0, "width": 72, "height": mh, "fields": [{"type": 8, "name": "sysmapid.0", "value": mapid}]}
         if mapid else problems("Current problems", 0, 0, 72, mh, gids)),
        {"type": "problemsbysv", "name": "Open problems per site (click a number for the list)", "x": 0, "y": mh,
         "width": 44, "height": ph,
         "fields": f_groups(gids) + [f_int("show_type", 0), f_int("hide_empty_groups", 0), f_int("layout", 0)]},
        problems("Current problems: root cause devices (devices behind them are hidden)", 0, mh + ph, 44, 8, gids),
        problems("Last outages: POWER or CONNECTIVITY", 0, mh + ph + 8, 44, 6, gids, "diagnosis", show=1),
        host_navigator("Devices by site (colour = worst problem)", 44, mh, 28, ph + 6, gids),
        table("Packet loss now (worst first)", 44, mh + ph + 6, 28, 8, gids,
              [col("Loss", "ICMP loss", 2, [(0, GREEN), (10, AMBER), (30, RED)], 0, 100, 0)], 1, 10, order=2),
    ]
    pages = [
        {"name": "Network", "widgets": net},
        {"name": "Devices", "widgets": [
            status_board("Every device: down first, then the worst packet loss", 0, 0, 44, 26, gids, 500),
            problems("Current problems", 44, 0, 28, 13, gids, **narrow),
            table("Most recent reboots", 44, 13, 28, 13, gids,
                  [col("Up for", "Hardware uptime"), col("SNMP uptime", "SNMP agent uptime")], 2, 15),
        ]},
        {"name": "Radios", "widgets": [
            radio_board("Radio links: weakest signal first (green above -65 dBm, amber to -75, red below)",
                        0, 0, 44, 18, gids, 200),
            ap_board("Sector APs: connected stations", 44, 0, 28, 18, gids),
            problems("Radio problems", 0, 18, 72, 7, gids, "component", "wireless"),
        ]},
        {"name": "Power", "widgets": [
            problems("POWER: device rebooted after an outage (mains, UPS, PoE)", 0, 0, 36, 9, gids,
                     "diagnosis", "power", show=1),
            problems("CONNECTIVITY: device kept power, the path failed (link, radio, cable)", 36, 0, 36, 9, gids,
                     "diagnosis", "connectivity", show=1),
            ups_board("UPS (red = on battery)", 0, 9, 36, 8, gids),
            problems("Reboots without an outage", 36, 9, 36, 8, gids, show=1,
                     extra=[f_str("problem", "uptime below")], **narrow),
        ]},
    ]

    existing = api.call("dashboard.get", {"output": ["dashboardid"], "filter": {"name": lookup["dashboard"]}})
    print(("  APPLY " if do_apply else "  would ") + ("rebuild" if existing else "create")
          + f" dashboard '{name}' ({len(pages)} pages, {len(groups)} sites)")
    if not do_apply:
        return
    body = {"name": name, "display_period": 120, "auto_start": 0, "pages": pages}
    if existing:
        api.call("dashboard.update", {"dashboardid": existing[0]["dashboardid"], **body})
    else:
        api.call("dashboard.create", body)


# ENDPOINTS-1: the Endpoints dashboard
ENDPOINTS = "Endpoints"
LATENCY = [(0, GREEN), (0.05, AMBER), (0.1, RED)]   # seconds: amber from 50 ms, red from 100 ms
PAGE_ROWS = 64   # Zabbix 7.x: a widget must end by row 64 of its page


def endpoint_window(code, n, x, y, h, gid):
    return table(f"{code}: {n} endpoint{'s' if n != 1 else ''} (weakest signal first)", x, y, 36, h, [gid], [
        col("Ping", "ICMP ping", thresholds=UP_DOWN, **STATE),
        col("RSSI", "Wireless: Signal (dBm)", thresholds=SIGNAL, decimals=0),
        col("SNR", "Wireless: SNR (dB)", thresholds=[(-100, RED), (15, AMBER), (25, GREEN)], decimals=0),
        col("Sector", "Wireless: Connected AP (SM)", text=True),
        col("Up for", "SNMP agent uptime"),
        col("Latency", "ICMP response time", thresholds=LATENCY, decimals=1),
    ], 2, n + 5, role="sm", ip=True)


def endpoint_height(n):
    """Rows of 35 px: title and header about 70 px, then about 29 px per endpoint."""
    return max(4, min(PAGE_ROWS, -(-(70 + 29 * n) // 35)))


def cmd_endpoints(api, do_apply, sites=None, name=ENDPOINTS):
    """One dashboard, one window per aggregation site with its endpoints (role sm, monitored)."""
    groups = site_groups(api, sites)
    gid = {g["name"].split("/", 1)[1]: g["groupid"] for g in groups}
    hosts = api.call("host.get", {"output": ["host"], "selectTags": "extend", "groupids": list(gid.values()) or None,
                                  "filter": {"status": 0},
                                  "tags": [{"tag": "role", "value": "sm", "operator": 1}]})
    count = {}
    for h in hosts:
        site = next((t["value"] for t in h["tags"] if t["tag"] == "site"), "")
        if site in gid:
            count[site] = count.get(site, 0) + 1
    codes = sorted(count)
    if not codes:
        sys.exit("No monitored endpoints (role sm) found in the Sites/* groups.")
    # two columns of windows, each window goes under the shorter column; a new page when neither has room
    pages, widgets, first, last, ys = [], [], codes[0], codes[0], [0, 0]
    for c in codes:
        h = endpoint_height(count[c])
        j = 0 if ys[0] <= ys[1] else 1
        if ys[j] + h > PAGE_ROWS and widgets:
            pages.append({"name": f"{first} to {last}" if first != last else first, "widgets": widgets})
            widgets, first, ys, j = [], c, [0, 0], 0
        widgets.append(endpoint_window(c, count[c], 36 * j, ys[j], h, gid[c]))
        last, ys[j] = c, ys[j] + h
    pages.append({"name": f"{first} to {last}" if first != last else first, "widgets": widgets})
    for c in codes:
        print(f"  {c}: {count[c]} endpoint(s)")
    existing = api.call("dashboard.get", {"output": ["dashboardid"], "filter": {"name": name}})
    print(("  APPLY " if do_apply else "  would ") + ("rebuild" if existing else "create")
          + f" dashboard '{name}' ({len(pages)} pages, {len(codes)} sites, {sum(count.values())} endpoints)")
    if not do_apply:
        print("Dry run: re-run with --apply to make it.")
        return
    body = {"name": name, "display_period": 60, "auto_start": 0, "pages": pages}
    if existing:
        api.call("dashboard.update", {"dashboardid": existing[0]["dashboardid"], **body})
    else:
        api.call("dashboard.create", body)
    print("done")


def uplink_sites(api, groups):
    """Sites holding the parents of these groups' hosts (e.g. SITE0000 for SITE0001)."""
    hosts = api.call("host.get", {"output": ["host"], "selectTags": "extend",
                                  "groupids": [g["groupid"] for g in groups]})
    parents = {t["value"] for h in hosts for t in h["tags"] if t["tag"] == "parent"}
    names = {h["host"] for h in hosts}
    out = set()
    if parents - names:
        for h in api.call("host.get", {"output": ["host"], "selectTags": "extend",
                                       "filter": {"host": sorted(parents - names)}}):
            out |= {t["value"] for t in h["tags"] if t["tag"] == "site"}
    return out


RCA_SCRIPTS = [
    # name, scope (2 = host action, 4 = event action), URL path
    ("RCA report for this problem", 4, "/rca?eventid={EVENT.ID}"),
    ("RCA report for this site", 2, "/rca?host={HOST.HOST}"),
    ("Health reports", 2, "/reports"),
    ("Health reports (network)", 4, "/reports"),
    ("Device logs", 2, "/logs?host={HOST.HOST}"),
    ("Device logs for this host", 4, "/logs?host={HOST.HOST}"),
    ("Radio signal check", 2, "/signal"),  # MDP-SIGNAL
    ("Radio signal check (network)", 4, "/signal"),
    ("Endpoint signal check", 2, "/endpoint-signal/now/daily?host={HOST.HOST}"),  # MDP-ENDPOINT_SIGNAL
    ("Endpoint signal check (network)", 4, "/endpoint-signal"),
]
# the built-in server-side scripts, made to work on a plain server: fping is there for Zabbix's own pings,
# `true` at the end so a host that does not answer still shows its output instead of "execution failed"
SERVER_SCRIPTS = {
    "Ping": "if command -v fping >/dev/null; then fping -c 3 -p 500 {HOST.CONN} 2>&1; "
            "else ping -c 3 {HOST.CONN} 2>&1; fi; true",
    "Traceroute": "traceroute -n -w 2 -q 1 -m 15 {HOST.CONN} 2>&1; true",
}


def cmd_buttons(api, url, do_apply):
    """Add the 'RCA report' menu entries (Zabbix URL scripts) that open tools/rca_server.py."""
    url = url.rstrip("/")
    existing = {s["name"]: s for s in api.call("script.get", {"output": ["scriptid", "name", "url"]})}
    for name, scope, path in RCA_SCRIPTS:
        body = {"name": name, "type": 6, "scope": scope, "url": url + path, "new_window": 1,
                "description": "Opens the MDP " + ("health reports" if "reports" in path else
                                                   "device log" if "logs" in path else "RCA report")
                               + " (tools/rca_server.py)."}
        if name in existing:
            if existing[name]["url"] == body["url"]:
                print(f"  exists  menu entry '{name}'")
                continue
            print(("  APPLY " if do_apply else "  would ") + f"update menu entry '{name}' -> {body['url']}")
            if do_apply:
                api.call("script.update", {"scriptid": existing[name]["scriptid"],
                                           **{k: v for k, v in body.items() if k not in ("type", "scope")}})
        else:
            print(("  APPLY " if do_apply else "  would ") + f"create menu entry '{name}' -> {body['url']}")
            if do_apply:
                api.call("script.create", body)
    full = {s["name"]: s for s in api.call("script.get", {"output": ["scriptid", "name", "command", "type"],
                                                          "filter": {"name": list(SERVER_SCRIPTS)}})}
    for name, command in SERVER_SCRIPTS.items():
        sc = full.get(name)
        if not sc:
            print(f"  skip    '{name}': no such script in Zabbix")
        elif sc["type"] == "0" and sc["command"] != command:
            print(("  APPLY " if do_apply else "  would ") + f"set the '{name}' script to: {command}")
            if do_apply:
                api.call("script.update", {"scriptid": sc["scriptid"], "command": command})
        else:
            print(f"  exists  script '{name}'")
    print("Ping and Traceroute run on the Zabbix server: they need EnableGlobalScripts=1 in zabbix_server.conf.")


def cmd_guard(api, do_apply):
    """Create the guard host (internal checks run by the server itself; no interface) and link the guard
    template to it. A separate host because the built-in "Zabbix server" host already has some of the keys."""
    tpl = api.call("template.get", {"output": ["templateid"], "filter": {"host": [GUARD]}})
    if not tpl:
        sys.exit(f"Template {GUARD} not found: run 'templates --apply' first.")
    group = api.call("hostgroup.get", {"output": ["groupid"], "filter": {"name": ["Zabbix servers"]}})
    if not group:
        print(("  APPLY " if do_apply else "  would ") + "create host group Zabbix servers")
        group = [{"groupid": api.call("hostgroup.create", {"name": "Zabbix servers"})["groupids"][0]}] \
            if do_apply else [{"groupid": "0"}]
    host = api.call("host.get", {"output": ["hostid"], "selectParentTemplates": ["templateid"],
                                 "filter": {"host": [GUARD_HOST]}})
    if host:
        linked = {t["templateid"] for t in host[0]["parentTemplates"]}
        if tpl[0]["templateid"] in linked:
            print(f"  exists  host {GUARD_HOST} with {GUARD}")
            return
        print(("  APPLY " if do_apply else "  would ") + f"link {GUARD} to {GUARD_HOST}")
        if do_apply:
            api.call("host.massadd", {"hosts": [{"hostid": host[0]["hostid"]}],
                                      "templates": [{"templateid": tpl[0]["templateid"]}]})
        return
    print(("  APPLY " if do_apply else "  would ") + f"create host {GUARD_HOST} with {GUARD}")
    if do_apply:
        api.call("host.create", {"host": GUARD_HOST, "groups": [{"groupid": group[0]["groupid"]}],
                                 "templates": [{"templateid": tpl[0]["templateid"]}],
                                 "description": "Zabbix server self-monitoring: caches, processes, disks, "
                                                "archive and backup jobs (tools/zbx_data.py pushes the last ones).",
                                 "tags": [{"tag": "class", "value": "software"}]})


def parse_sites(value):
    """--sites SITE0001,SITE0002 or --sites @rollout.txt (the first word of each line of that file)."""
    if value and value.startswith("@"):
        with open(value[1:], encoding="utf-8") as f:
            words = [line.split("#", 1)[0].split() for line in f]
        value = ",".join(w[0] for w in words if w)
    return {x.strip().upper() for x in (value or "").split(",") if x.strip()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("csv")
    a = sub.add_parser("apply")
    a.add_argument("csv")
    a.add_argument("--apply", action="store_true", help="write changes (default is a dry run)")
    a.add_argument("--replace-templates", action="store_true",
                   help="unlink and clear templates not in the plan (deletes their items and history)")
    a.add_argument("--rename", action="store_true", help="rename matched hosts to the CSV host name")
    a.add_argument("--sites", help="only these sites, comma-separated or @file (their uplink devices are included)")
    a.add_argument("--main-links", default=MAIN_LINKS,
                   help="parents of the main-link radios come from this file when it exists (default data/main_links.txt; none = off)")
    disc = sub.add_parser("discovery")
    disc.add_argument("--ranges", default=os.environ.get("DISCOVERY_RANGES", ""),
                      help="comma-separated IP ranges, e.g. 10.20.0.0/22,10.20.10.0/24 (default: $DISCOVERY_RANGES)")
    disc.add_argument("--every", default="1h", help="scan interval (default 1h)")
    disc.add_argument("--ups-range", default=os.environ.get("DISCOVERY_UPS_RANGE", ""), help="range holding only UPS cards (default: $DISCOVERY_UPS_RANGE)")
    disc.add_argument("--apply", action="store_true")
    d = sub.add_parser("dashboards")
    d.add_argument("--apply", action="store_true")
    d.add_argument("--recreate", action="store_true", help="delete and rebuild the Site dashboards")
    d.add_argument("--sites", help="only these sites, comma-separated")
    d.add_argument("--core", default=sc.CORE_SITE, help=f"site that gets the detailed core dashboard (default {sc.CORE_SITE})")
    o = sub.add_parser("overview", help="the 'Network overview' dashboard for all sites")
    o.add_argument("--apply", action="store_true")
    o.add_argument("--sites", help="only these sites, comma-separated (one status panel per site)")
    o.add_argument("--name", default=OVERVIEW, help=f"dashboard name (default '{OVERVIEW}')")
    o.add_argument("--rename-from", help="rename this existing dashboard (and its map) to --name instead of making a new one")
    for cmd in ("disable", "enable", "delete"):
        x = sub.add_parser(cmd, help=f"{cmd} hosts by host name, visible name or IP")
        x.add_argument("hosts", nargs="*")
        x.add_argument("--file", help="text file with one host name or IP per line")
        x.add_argument("--apply", action="store_true")
    pr = sub.add_parser("prune", help="delete managed hosts outside --sites (dry run by default)")
    pr.add_argument("csv")
    pr.add_argument("--sites", required=True)
    pr.add_argument("--apply", action="store_true")
    tp = sub.add_parser("templates", help="import templates/mdp_templates.yaml")
    tp.add_argument("--file", default=TEMPLATE_FILE)
    tp.add_argument("--apply", action="store_true")
    b = sub.add_parser("buttons", help="'RCA report' entries in the problem and host menus")
    b.add_argument("--url", required=True, help="where rca_server.py is reached from a browser, e.g. http://10.20.100.11:8081")
    b.add_argument("--apply", action="store_true")
    gd = sub.add_parser("guard", help="the 'Zabbix server guard' host (cache and backup alarms)")
    gd.add_argument("--apply", action="store_true")
    pl = sub.add_parser("endpoints", help="the Endpoints dashboard: one window per site with its endpoints")  # ENDPOINTS-1
    pl.add_argument("--apply", action="store_true")
    pl.add_argument("--sites", help="only these sites, comma-separated or @file")
    pl.add_argument("--name", default="Endpoints", help="dashboard name (default Endpoints)")
    sdn = sub.add_parser("sitedown", help="one 'Site SITExxxx DOWN' alarm per site, parents from the main links")
    sdn.add_argument("--main-links", default=MAIN_LINKS, help="default data/main_links.txt")
    sdn.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    sites = parse_sites(getattr(args, "sites", None))

    api = ZabbixAPI()
    try:
        print(f"Zabbix {api.version()} at {api.url}")
        if args.cmd == "export":
            cmd_export(api, args.csv)
        elif args.cmd == "discovery":
            if not args.ranges or not args.ups_range:
                sys.exit("discovery needs --ranges and --ups-range (or DISCOVERY_RANGES and DISCOVERY_UPS_RANGE)")
            cmd_discovery(api, args.ranges, args.every, args.apply, args.ups_range)
        elif args.cmd == "dashboards":
            cmd_dashboards(api, args.apply, args.recreate, sites, args.core.upper())
        elif args.cmd == "overview":
            cmd_overview(api, args.apply, sites, args.name, args.rename_from)
        elif args.cmd == "buttons":
            cmd_buttons(api, args.url, args.apply)
        elif args.cmd == "delete":
            names = list(args.hosts)
            if args.file:
                names += [x.strip() for x in open(args.file, encoding="utf-8") if x.strip() and not x.startswith("#")]
            if not names:
                sys.exit("Name the hosts (host name, visible name or IP) or pass --file.")
            cmd_delete(api, names, args.apply)
        elif args.cmd in ("disable", "enable"):
            names = list(args.hosts)
            if args.file:
                names += [x.strip() for x in open(args.file, encoding="utf-8") if x.strip() and not x.startswith("#")]
            if not names:
                sys.exit("Name the hosts (host name, visible name or IP) or pass --file.")
            cmd_status(api, names, args.cmd == "disable", args.apply)
        elif args.cmd == "prune":
            cmd_prune(api, read_csv(args.csv), sites, args.apply)
        elif args.cmd == "templates":
            cmd_templates(api, args.file, args.apply)
        elif args.cmd == "guard":
            cmd_guard(api, args.apply)
        elif args.cmd == "endpoints":   # ENDPOINTS-1
            cmd_endpoints(api, args.apply, sites, args.name)
        elif args.cmd == "sitedown":
            cmd_sitedown(api, args.main_links, args.apply)
        else:
            p = Planner(api, args.apply, args.replace_templates, args.rename)
            rows = read_csv(args.csv)
            if args.main_links != "none" and os.path.exists(args.main_links):
                for host, old, new in main_link_parents(rows, load_main_links(args.main_links)):
                    print(f"main link: {host} parent {old or '-'} -> {new or '(none, LACP site)'}")
            p.run(only_sites(rows, sites))
            print(f"\n{p.changes} change(s) {'made' if args.apply else 'planned; re-run with --apply to make them'}.")
    except ZabbixError as e:
        sys.exit(f"Zabbix API error: {e}")


if __name__ == "__main__":
    main()
