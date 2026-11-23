#!/usr/bin/env python3
"""Device log: search any device by name or IP, then show everything Zabbix logged for it.

Served by rca_server.py at /logs (search) and /logs?host=<host name> (one device):
  open problems, the problem history (start, end, duration, severity, cause tag, acknowledgements),
  the "Site SITExxxx DOWN" alarms of its site, notifications sent, items with errors now,
  and configuration changes (audit log). A CSV download holds the same timeline.

Command line (prints the page to stdout or a file):
  python3 tools/zbx_logs.py --search 10.20.0.23
  python3 tools/zbx_logs.py --host SITE0015-WAVE-1 --days 30 --out log.html

Only read-only API calls are made.
"""
import argparse
import csv
import html
import io
import ipaddress
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zbx_rca  # noqa: E402

PERIODS = [1, 7, 30, 90, 365]       # days offered on the page
MAX_EVENTS = 3000
MAX_RESULTS = 200
SEV = ["Not classified", "Information", "Warning", "Average", "High", "Disaster"]
SEV_CSS = ["#97aab3", "#7499ff", "#ffc859", "#ffa059", "#e97659", "#e45959"]
ACK_ACTIONS = [(1, "closed"), (2, "acknowledged"), (4, "commented"), (8, "changed severity"),
               (16, "unacknowledged"), (32, "suppressed"), (64, "unsuppressed"), (128, "changed to cause"),
               (256, "changed to symptom")]
AUDIT_ACTIONS = {"0": "added", "1": "updated", "2": "deleted", "4": "logout", "7": "executed", "8": "login",
                 "9": "failed login", "10": "history cleared", "11": "config refreshed"}
ALERT_STATUS = {"0": "not sent yet", "1": "sent", "2": "failed", "3": "new"}

CSS = zbx_rca.CSS + """
form.find{display:flex;gap:8px;flex-wrap:wrap;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;margin:16px 0}
form.find input[type=search]{flex:1;min-width:240px}
input,select,button,.btn{font:inherit;padding:7px 10px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
button,.btn{background:var(--accent);color:#fff;border-color:var(--accent);cursor:pointer;text-decoration:none;display:inline-block}
.btn.alt{background:transparent;color:var(--accent)}.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 16px}
.kpis{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.sev{display:inline-block;padding:1px 8px;border-radius:4px;color:#1d2433;font-size:12px;font-weight:600;white-space:nowrap}
.tag{display:inline-block;font-size:11px;border:1px solid var(--line);border-radius:4px;padding:0 5px;margin:1px 2px 1px 0;color:var(--muted)}
.open{color:var(--bad);font-weight:700}.okc{color:var(--ok)}.small{font-size:12px}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:4px 24px;margin:12px 0}
.facts div{border-bottom:1px solid var(--line);padding:4px 0}.facts b{color:var(--muted);font-weight:600;display:inline-block;min-width:90px}
tr.site td{background:var(--win)}tr[hidden]{display:none}
.filters{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0}
.filters label{font-size:13px;display:flex;gap:4px;align-items:center}
@media print{form.find,.toolbar,.filters{display:none}}
"""

FILTER_JS = """<script>
function mdp_filter(){var q=document.getElementById('f_text').value.toLowerCase(),
 o=document.getElementById('f_open').checked,s=document.getElementById('f_site').checked,n=0;
 document.querySelectorAll('#timeline tbody tr').forEach(function(r){
  var show=(!q||r.textContent.toLowerCase().indexOf(q)>=0)&&(!o||r.dataset.open=='1')&&(s||r.dataset.src!='site');
  r.hidden=!show;if(show)n++;});
 document.getElementById('f_count').textContent=n+' shown';}
</script>"""

e = html.escape


# --------------------------------------------------------------- helpers
def fmt_ts(clock, tz):
    if not clock:
        return "-"
    return datetime.fromtimestamp(int(clock), tz).strftime("%Y-%m-%d %H:%M:%S")


def fmt_dur(sec):
    sec = int(max(sec, 0))
    if sec < 60:
        return f"{sec}s"
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d}d {h}h"
    return f"{h}h {m:02d}m" if h else f"{m}m"


def sev_badge(sev):
    s = int(sev or 0)
    return f"<span class='sev' style='background:{SEV_CSS[s]}'>{SEV[s]}</span>"


def tags_html(tags, skip=("site", "role", "vendor", "model", "parent", "mainlink", "scope")):
    return "".join(f"<span class='tag'>{e(t['tag'])}{(': ' + e(t['value'])) if t['value'] else ''}</span>"
                   for t in tags or [] if t["tag"] not in skip)


def union_seconds(spans, t0, t1):
    """Total time covered by [start, end) spans, clipped to t0..t1, overlaps counted once."""
    total, cur_s, cur_e = 0, None, None
    for s, en in sorted((max(s, t0), min(en, t1)) for s, en in spans):
        if en <= s:
            continue
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, en
        else:
            cur_e = max(cur_e, en)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


def is_outage(ev):
    tags = {t["tag"]: t["value"] for t in ev.get("tags", [])}
    return tags.get("alarm") in ("unreachable", "site-down") or "unreachable" in ev["name"].lower() \
        or ev["name"].endswith(" DOWN")


def ack_text(acks, tz):
    out = []
    for a in sorted(acks or [], key=lambda a: int(a["clock"])):
        what = [n for bit, n in ACK_ACTIONS if int(a.get("action", 0)) & bit]
        who = a.get("username") or (" ".join(x for x in (a.get("name"), a.get("surname")) if x)) or \
            f"user {a.get('userid', '?')}"
        msg = f": {a['message']}" if a.get("message") else ""
        out.append(f"{fmt_ts(a['clock'], tz)[5:16]} {who} {', '.join(what) or 'updated'}{msg}")
    return out


# --------------------------------------------------------------- search
def find_hosts(api, q):
    """Hosts whose host name, visible name or interface IP contains q (all hosts, enabled or not)."""
    q = q.strip()
    if not q:
        return []
    out = {"output": ["hostid", "host", "name", "status", "maintenance_status"],
           "selectInterfaces": ["ip", "dns", "type", "main", "available"], "selectTags": "extend",
           "limit": MAX_RESULTS}
    hits = {h["hostid"]: h for h in api.call("host.get", {
        **out, "search": {"host": q, "name": q}, "searchByAny": True, "searchWildcardsEnabled": True})}
    if re.fullmatch(r"[0-9.*]+", q):
        ifs = api.call("hostinterface.get", {"output": ["hostid"], "search": {"ip": q},
                                             "searchWildcardsEnabled": True, "limit": MAX_RESULTS})
        ids = sorted({i["hostid"] for i in ifs} - set(hits))
        if ids:
            hits.update({h["hostid"]: h for h in api.call("host.get", {**out, "hostids": ids})})
    hosts = list(hits.values())

    def rank(h):  # exact name / IP first
        ips = [i["ip"] for i in h["interfaces"]]
        return (0 if q.lower() in (h["host"].lower(), h["name"].lower()) or q in ips else 1, h["host"])
    return sorted(hosts, key=rank)


def open_problem_counts(api, hostids):
    if not hostids:
        return {}
    probs = api.call("problem.get", {"output": ["eventid", "objectid", "severity"], "hostids": hostids,
                                     "source": 0, "object": 0})
    trig = {}
    if probs:
        for t in api.call("trigger.get", {"output": ["triggerid"], "selectHosts": ["hostid"],
                                          "triggerids": sorted({p["objectid"] for p in probs})}):
            trig[t["triggerid"]] = [h["hostid"] for h in t["hosts"]]
    count = {}
    for p in probs:
        for hid in trig.get(p["objectid"], []):
            c = count.setdefault(hid, [0, 0])
            c[0] += 1
            c[1] = max(c[1], int(p["severity"]))
    return count


def main_ip(h):
    ifs = sorted(h.get("interfaces", []), key=lambda i: (i.get("main") != "1", i.get("type")))
    return ifs[0]["ip"] if ifs else ""


def availability(h):
    """'up' / 'down' / 'unknown' from the main interface (1 available, 2 not available)."""
    a = {i.get("available") for i in h.get("interfaces", [])}
    return "down" if "2" in a else "up" if "1" in a else "unknown"


def search_form(q="", days=30):
    opts = "".join(f"<option value='{d}'{' selected' if d == days else ''}>last {d} day{'s' if d > 1 else ''}</option>"
                   for d in PERIODS)
    return (f"<form class='find' action='logs' method='get'><input type='search' name='q' value='{e(q)}' "
            "placeholder='Device name, IP address or site code, e.g. WAVE-1, 10.20.0.23, SITE0003' autofocus>"
            f"<select name='days'>{opts}</select><button type='submit'>Search</button></form>")


def search_page(api, q, days=30):
    """The search page. Returns (html, single_host_or_None): one exact match is opened directly."""
    head = ("<h1>Device log</h1><div class='muted'>Find a device by name or IP and see every alarm, problem, "
            "acknowledgement, notification and configuration change Zabbix kept for it. "
            "<a href='rca'>RCA report</a> · <a href='reports'>Health reports</a></div>")
    if not q.strip():
        return head + search_form(q, days), None
    hosts = find_hosts(api, q)
    if len(hosts) == 1:
        return None, hosts[0]["host"]
    if not hosts:
        return head + search_form(q, days) + f"<div class='verdict err'>No device matches <b>{e(q)}</b>.</div>", None
    counts = open_problem_counts(api, [h["hostid"] for h in hosts])
    rows = []
    for h in hosts:
        tags = {t["tag"]: t["value"] for t in h["tags"]}
        c = counts.get(h["hostid"])
        state = availability(h)
        state_html = {"up": "<span class='okc'>reachable</span>", "down": "<span class='open'>unreachable</span>",
                      "unknown": "<span class='muted'>unknown</span>"}[state]
        if h["status"] == "1":
            state_html = "<span class='muted'>disabled</span>"
        link = f"logs?{urllib.parse.urlencode({'host': h['host'], 'days': days})}"
        rows.append(f"<tr><td><a href='{e(link)}'><b>{e(h['host'])}</b></a>"
                    f"{('<div class=small muted>' + e(h['name']) + '</div>') if h['name'] != h['host'] else ''}</td>"
                    f"<td>{e(main_ip(h))}</td><td>{e(tags.get('site', ''))}</td><td>{e(tags.get('role', ''))}</td>"
                    f"<td>{e(tags.get('model', ''))}</td><td>{state_html}</td>"
                    f"<td>{(sev_badge(c[1]) + ' ' + str(c[0])) if c else '<span class=muted>none</span>'}</td></tr>")
    more = f" (first {MAX_RESULTS}, narrow the search)" if len(hosts) >= MAX_RESULTS else ""
    return (head + search_form(q, days) + f"<h2>{len(hosts)} devices match <b>{e(q)}</b>{more}</h2><div class='wrap'><table>"
            "<thead><tr><th>Device</th><th>IP</th><th>Site</th><th>Role</th><th>Model</th><th>Status</th>"
            "<th>Open problems</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"), None


# --------------------------------------------------------------- one device
def get_host(api, name):
    out = {"output": ["hostid", "host", "name", "status", "maintenance_status", "description"],
           "selectInterfaces": ["ip", "dns", "type", "main", "available", "error", "port"],
           "selectTags": "extend", "selectHostGroups": ["name"], "selectParentTemplates": ["name"]}
    h = api.call("host.get", {**out, "filter": {"host": [name]}})
    if not h:
        h = api.call("host.get", {**out, "filter": {"name": [name]}})
    return h[0] if h else None


def problem_events(api, hostids, t0):
    """Problem events of these hosts that started after t0, plus older ones still open."""
    out = ["eventid", "clock", "name", "severity", "r_eventid", "acknowledged", "objectid", "suppressed", "opdata"]
    common = {"output": out, "source": 0, "object": 0, "value": 1, "hostids": hostids, "selectTags": "extend",
              "selectAcknowledges": "extend", "selectHosts": ["hostid", "host"]}
    evs = {ev["eventid"]: ev for ev in api.call("event.get", {**common, "time_from": t0, "sortfield": ["clock", "eventid"],
                                                               "sortorder": "DESC", "limit": MAX_EVENTS})}
    still = [p["eventid"] for p in api.call("problem.get", {"output": ["eventid"], "hostids": hostids,
                                                             "source": 0, "object": 0})]
    old = sorted(set(still) - set(evs))
    if old:
        evs.update({ev["eventid"]: ev for ev in api.call("event.get", {**common, "eventids": old})})
    rec_ids = sorted({ev["r_eventid"] for ev in evs.values() if ev["r_eventid"] != "0"})
    rec = {}
    for i in range(0, len(rec_ids), 500):
        for r in api.call("event.get", {"output": ["eventid", "clock", "userid", "c_eventid"],
                                        "eventids": rec_ids[i:i + 500]}):
            rec[r["eventid"]] = r
    for ev in evs.values():
        r = rec.get(ev["r_eventid"])
        ev["end"] = int(r["clock"]) if r else None
        ev["closed_by"] = ("manually" if r.get("userid", "0") != "0" else
                           "by correlation" if r.get("c_eventid", "0") != "0" else "") if r else ""
        ev["open"] = ev["eventid"] in still
    return sorted(evs.values(), key=lambda ev: (-int(ev["clock"]), ev["eventid"]))


def device_data(api, name, days):
    h = get_host(api, name)
    if not h:
        raise ValueError(f"No device called {name}.")
    now = int(time.time())
    t0 = now - days * 86400
    tags = {t["tag"]: t["value"] for t in h["tags"]}
    events = problem_events(api, [h["hostid"]], t0)
    for ev in events:
        ev["src"] = "device"
    site_host = None
    if tags.get("site") and not h["host"].startswith("Site "):
        sh = api.call("host.get", {"output": ["hostid", "host"], "filter": {"host": ["Site " + tags["site"]]}})
        if sh:
            site_host = sh[0]
            for ev in problem_events(api, [site_host["hostid"]], t0):
                ev["src"] = "site"
                events.append(ev)
    events.sort(key=lambda ev: (-int(ev["clock"]), ev["eventid"]))
    alerts = api.call("alert.get", {"output": ["clock", "sendto", "subject", "status", "error", "retries", "eventid"],
                                    "hostids": [h["hostid"]], "time_from": t0, "selectMediatypes": ["name"],
                                    "sortfield": "clock", "sortorder": "DESC", "limit": 500})
    bad_items = api.call("item.get", {"output": ["name", "key_", "error", "lastclock", "state"],
                                      "hostids": [h["hostid"]], "filter": {"state": 1, "status": 0},
                                      "sortfield": "name", "limit": 200})
    try:
        audit = api.call("auditlog.get", {"output": ["clock", "username", "action", "resourcename", "resourcetype",
                                                     "ip", "details"],
                                          "filter": {"resourceid": h["hostid"]}, "time_from": t0,
                                          "sortfield": "clock", "sortorder": "DESC", "limit": 100})
    except Exception:  # noqa: BLE001 - a token without audit rights still gets the rest
        audit = None
    parent = None
    if tags.get("parent"):
        p = api.call("host.get", {"output": ["host"], "selectInterfaces": ["ip", "available", "main", "type"],
                                  "filter": {"host": [tags["parent"]]}})
        parent = p[0] if p else None
    return {"host": h, "tags": tags, "events": events, "alerts": alerts, "bad_items": bad_items, "audit": audit,
            "site_host": site_host, "parent": parent, "t0": t0, "now": now, "days": days}


def timeline_rows(d, tz):
    rows = []
    for ev in d["events"]:
        start = int(ev["clock"])
        end = ev["end"]
        dur = (end or d["now"]) - start
        tags = {t["tag"]: t["value"] for t in ev["tags"]}
        rows.append({"start": fmt_ts(start, tz), "end": fmt_ts(end, tz) if end else "", "duration": fmt_dur(dur),
                     "dur_s": dur, "severity": SEV[int(ev["severity"])], "sev": int(ev["severity"]),
                     "problem": ev["name"], "status": "OPEN" if ev["open"] else ("resolved " + ev["closed_by"]).strip(),
                     "source": "site alarm" if ev["src"] == "site" else "device",
                     "cause": tags.get("diagnosis", ""), "acknowledged": "yes" if ev["acknowledged"] == "1" else "no",
                     "history": " | ".join(ack_text(ev.get("acknowledges"), tz)), "eventid": ev["eventid"],
                     "triggerid": ev["objectid"], "tags": ev["tags"], "open": ev["open"], "src": ev["src"],
                     "suppressed": ev.get("suppressed") == "1", "opdata": ev.get("opdata", "")})
    return rows


def device_page(api, name, days=30, tz_name="UTC", web=""):
    tz = ZoneInfo(tz_name)
    days = days if days in PERIODS else 30
    d = device_data(api, name, days)
    h, tags = d["host"], d["tags"]
    rows = timeline_rows(d, tz)
    own = [ev for ev in d["events"] if ev["src"] == "device"]
    out_spans = [(int(ev["clock"]), ev["end"] or d["now"]) for ev in own if is_outage(ev)]
    down = union_seconds(out_spans, d["t0"], d["now"])
    site_down = union_seconds([(int(ev["clock"]), ev["end"] or d["now"]) for ev in d["events"]
                               if ev["src"] == "site" and is_outage(ev)], d["t0"], d["now"])
    opened = [ev for ev in own if ev["open"]]
    in_period = [ev for ev in own if int(ev["clock"]) >= d["t0"]]
    q = urllib.parse.urlencode
    period = f"last {days} day{'s' if days > 1 else ''}"
    zl = (lambda path: f"<a class='btn alt' target='_blank' href='{e(web + path)}'>") if web else None

    state = availability(h)
    state_txt = "disabled in Zabbix" if h["status"] == "1" else {"up": "reachable", "down": "UNREACHABLE",
                                                                  "unknown": "not checked yet"}[state]
    ips = ", ".join(sorted({i["ip"] for i in h["interfaces"] if i["ip"]})) or "-"
    ifs_err = [i["error"] for i in h["interfaces"] if i.get("error")]
    par = d["parent"]
    par_html = "-"
    if tags.get("parent"):
        par_html = (f"<a href='{e('logs?' + q({'host': tags['parent'], 'days': days}))}'>{e(tags['parent'])}</a>"
                    + (f" ({e(main_ip(par))}, {availability(par)})" if par else ""))
    facts = ([("Name", e(h["name"]))] if h["name"] != h["host"] else []) + [("IP", e(ips)), ("Site", e(tags.get("site", "-"))),
             ("Role", e(tags.get("role", "-"))), ("Model", e(" ".join(x for x in (tags.get("vendor"), tags.get("model")) if x) or "-")),
             ("Depends on", par_html), ("Status", f"<span class='{'open' if state == 'down' else 'okc'}'>{state_txt}</span>"
                                        + (" · in maintenance" if h.get("maintenance_status") == "1" else "")),
             ("Templates", e(", ".join(t["name"] for t in h.get("parentTemplates", [])) or "-"))]
    if ifs_err:
        facts.append(("Last error", e(ifs_err[0])))

    body = [search_form("", days),
            f"<div class='rhead'><div><div class='kicker'>Device log · {e(period)}</div><h1>{e(h['host'])}</h1></div>"
            f"<div class='muted'>{fmt_ts(d['t0'], tz)[:16]} to {fmt_ts(d['now'], tz)[:16]} ({e(tz_name)})</div></div>",
            "<div class='toolbar'>"
            + "".join(f"<a class='btn{'' if p == days else ' alt'}' href='{e('logs?' + q({'host': h['host'], 'days': p}))}'>"
                      f"{p}d</a>" for p in PERIODS)
            + f"<a class='btn alt' href='{e('logs?' + q({'host': h['host'], 'days': days, 'format': 'csv'}))}'>Download CSV</a>"
            + f"<a class='btn alt' href='{e('rca?' + q({'host': h['host']}))}'>RCA report</a>"
            + (zl(f"/zabbix.php?action=problem.view&filter_set=1&hostids%5B%5D={h['hostid']}") + "Problems in Zabbix</a>"
               + zl(f"/zabbix.php?action=latest.view&filter_set=1&hostids%5B%5D={h['hostid']}") + "Latest data</a>"
               if zl else "")
            + "<a class='btn alt' href='#' onclick='window.print();return false'>Print / PDF</a></div>",
            "<div class='facts'>" + "".join(f"<div><b>{k}</b> {v}</div>" for k, v in facts) + "</div>",
            "<div class='kpis'>"
            f"<div><b class='{'open' if opened else ''}'>{len(opened)}</b><span>open problems now</span></div>"
            f"<div><b>{len(in_period)}</b><span>problems in the {e(period)}</span></div>"
            f"<div><b>{fmt_dur(down) if down else '0'}</b><span>time unreachable (own alarms)</span></div>"
            + (f"<div><b>{fmt_dur(site_down) if site_down else '0'}</b><span>site {e(tags.get('site', ''))} down</span></div>"
               if d["site_host"] else "")
            + f"<div><b>{len(d['alerts'])}</b><span>notifications sent</span></div>"
            f"<div><b class='{'open' if d['bad_items'] else ''}'>{len(d['bad_items'])}</b><span>items with errors</span></div>"
            "</div>"]
    if d["parent"] or d["site_host"]:
        body.append("<div class='muted small'>When an upstream device or the whole site is down, Zabbix hides this "
                    "device's own \"unreachable\" alarm (it depends on the upstream one). The site alarms are listed "
                    "in the timeline (shaded) so the full story is in one place.</div>")

    # open problems
    if opened:
        body.append("<h2>Open problems</h2><div class='wrap'><table><thead><tr><th>Since</th><th>For</th><th>Severity</th>"
                    "<th>Problem</th><th>Acknowledged</th></tr></thead><tbody>")
        for r in [r for r in rows if r["open"] and r["src"] == "device"]:
            body.append(f"<tr><td>{r['start']}</td><td>{r['duration']}</td><td>{sev_badge(r['sev'])}</td>"
                        f"<td>{e(r['problem'])}{' <span class=tag>suppressed</span>' if r['suppressed'] else ''}</td>"
                        f"<td>{r['acknowledged']}</td></tr>")
        body.append("</tbody></table></div>")

    # timeline
    body.append(f"<h2>Problem timeline ({len(rows)})</h2>")
    if rows:
        body.append("<div class='filters'><input type='search' id='f_text' placeholder='Filter: text, date, severity...' "
                    "oninput='mdp_filter()'><label><input type='checkbox' id='f_open' onchange='mdp_filter()'>"
                    "open only</label>" + ("<label><input type='checkbox' id='f_site' checked onchange='mdp_filter()'>"
                                           "site alarms</label>" if d["site_host"] else
                                           "<input type='checkbox' id='f_site' checked hidden>")
                    + f"<span class='muted small' id='f_count'>{len(rows)} shown</span></div>")
        body.append("<div class='wrap'><table id='timeline'><thead><tr><th>Started</th><th>Ended</th><th>Duration</th>"
                    "<th>Severity</th><th>Problem</th><th>Cause / tags</th><th>Acknowledgements</th><th></th></tr></thead><tbody>")
        for r in rows:
            end = r["end"] or "<span class='open'>still open</span>"
            how = f"<div class='small muted'>{e(r['status'])}</div>" if r["end"] and r["status"] != "resolved" else ""
            src = "<span class='tag'>site alarm</span> " if r["src"] == "site" else ""
            acks = "<br>".join(e(a) for a in r["history"].split(" | ")) if r["history"] else \
                "<span class='muted'>-</span>"
            links = f"<a class='small' href='{e('rca?' + q({'eventid': r['eventid']}))}'>RCA</a>"
            if web:
                links += (f" · <a class='small' target='_blank' href='"
                          f"{e(web + '/tr_events.php?' + q({'triggerid': r['triggerid'], 'eventid': r['eventid']}))}'>event</a>")
            body.append(f"<tr class='{'site' if r['src'] == 'site' else ''}' data-open='{1 if r['open'] else 0}' "
                        f"data-src='{r['src']}'><td>{r['start']}</td><td>{end}{how}</td><td>{r['duration']}</td>"
                        f"<td>{sev_badge(r['sev'])}</td><td>{src}{e(r['problem'])}"
                        f"{('<div class=small muted>' + e(r['opdata']) + '</div>') if r['opdata'] else ''}</td>"
                        f"<td>{tags_html(r['tags'])}</td><td class='small'>{acks}</td><td>{links}</td></tr>")
        body.append("</tbody></table></div>" + FILTER_JS)
    else:
        body.append(f"<div class='verdict'>No problems in the {e(period)}.</div>")

    # items with errors
    if d["bad_items"]:
        body.append("<h2>Items with errors now</h2><div class='muted small'>Zabbix cannot read these values from the "
                    "device (wrong OID, SNMP credentials, or the device does not support it).</div><div class='wrap'><table>"
                    "<thead><tr><th>Item</th><th>Key</th><th>Error</th></tr></thead><tbody>"
                    + "".join(f"<tr><td>{e(i['name'])}</td><td class='small'>{e(i['key_'])}</td>"
                              f"<td class='small'>{e(i['error'])}</td></tr>" for i in d["bad_items"])
                    + "</tbody></table></div>")

    # notifications
    body.append(f"<h2>Notifications sent ({len(d['alerts'])})</h2>")
    if d["alerts"]:
        body.append("<div class='wrap'><table><thead><tr><th>Time</th><th>Via</th><th>To</th><th>Subject</th>"
                    "<th>Status</th></tr></thead><tbody>")
        for a in d["alerts"]:
            via = ", ".join(m["name"] for m in a.get("mediatypes", [])) or "-"
            st = ALERT_STATUS.get(a["status"], a["status"])
            err = f"<div class='small open'>{e(a['error'])}</div>" if a.get("error") else ""
            body.append(f"<tr><td>{fmt_ts(a['clock'], tz)}</td><td>{e(via)}</td><td>{e(a['sendto'])}</td>"
                        f"<td>{e(a['subject'])}</td><td>{e(st)}{err}</td></tr>")
        body.append("</tbody></table></div>")
    else:
        body.append(f"<div class='muted'>No e-mail / message notifications for this device in the {e(period)}.</div>")

    # audit
    body.append("<h2>Configuration changes</h2>")
    if d["audit"] is None:
        body.append("<div class='muted'>The API token cannot read the audit log.</div>")
    elif d["audit"]:
        body.append("<div class='wrap'><table><thead><tr><th>Time</th><th>User</th><th>Action</th><th>Details</th>"
                    "</tr></thead><tbody>")
        for a in d["audit"]:
            det = (a.get("details") or "")
            det = det[:300] + ("..." if len(det) > 300 else "")
            body.append(f"<tr><td>{fmt_ts(a['clock'], tz)}</td><td>{e(a.get('username', ''))}</td>"
                        f"<td>{e(AUDIT_ACTIONS.get(a['action'], a['action']))}</td><td class='small'>{e(det)}</td></tr>")
        body.append("</tbody></table></div>")
    else:
        body.append(f"<div class='muted'>No configuration changes to this device in the {e(period)}.</div>")
    return "".join(body)


def device_csv(api, name, days=30, tz_name="UTC"):
    tz = ZoneInfo(tz_name)
    d = device_data(api, name, days if days in PERIODS else 30)
    buf = io.StringIO()
    cols = ["start", "end", "duration", "dur_s", "severity", "problem", "status", "source", "cause", "acknowledged",
            "history", "eventid"]
    w = csv.writer(buf)
    w.writerow(["device", "ip"] + cols)
    ip = main_ip(d["host"])
    for r in timeline_rows(d, tz):
        w.writerow([d["host"]["host"], ip] + [r[c] for c in cols])
    return buf.getvalue(), f"log_{re.sub(r'[^A-Za-z0-9_.-]', '_', d['host']['host'])}_{days}d.csv"


def wrap(title, body):
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,"
            f"initial-scale=1'><title>{e(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>")


def web_base(host_header):
    """Zabbix web UI address for links in the page: ZABBIX_WEB_URL, else this server's host name on port 80."""
    if os.environ.get("ZABBIX_WEB_URL"):
        return os.environ["ZABBIX_WEB_URL"].rstrip("/")
    if not host_header:
        return ""
    hostname = host_header.rsplit(":", 1)[0] if not host_header.endswith("]") else host_header
    try:
        ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9.-]+", hostname):
            return ""
    return "http://" + hostname


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--search", help="name or IP (part of it is fine)")
    g.add_argument("--host", help="exact host name")
    ap.add_argument("--days", type=int, default=30, choices=PERIODS)
    ap.add_argument("--tz", default=os.environ.get("RCA_TZ", "UTC"))
    ap.add_argument("--csv", action="store_true", help="with --host: CSV instead of HTML")
    ap.add_argument("--out", help="write to this file instead of stdout")
    a = ap.parse_args()
    from zbxapi import ZabbixAPI
    api = ZabbixAPI()
    if a.search:
        hosts = find_hosts(api, a.search)
        for h in hosts:
            tags = {t["tag"]: t["value"] for t in h["tags"]}
            print(f"{h['host']:<32} {main_ip(h):<16} {tags.get('site', ''):<9} {availability(h)}")
        print(f"{len(hosts)} match(es)")
        return
    text = device_csv(api, a.host, a.days, a.tz)[0] if a.csv else wrap(a.host, device_page(api, a.host, a.days, a.tz))
    if a.out:
        open(a.out, "w", encoding="utf-8").write(text)
        print(f"wrote {a.out}")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
