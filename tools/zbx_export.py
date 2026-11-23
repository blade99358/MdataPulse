#!/usr/bin/env python3
"""Zabbix Live Export (MDP-EXPORT): export any Zabbix dashboard window as it is shown, to Excel, CSV or PDF.

Reads the dashboard through the Zabbix API (dashboard.get: every window's hosts, items, columns, sort order and
colour thresholds), then fetches the same live values and builds the same table. Read-only API calls only.

  /export                         pick a dashboard, then a window (or a whole dashboard page)
  /export/view?d=<id>&w=<widgetid>[&period=1h]       one window, on screen (Print / save as PDF from there)
  /export/view?d=<id>&p=<page number>                every window of that dashboard page
  ...&format=xlsx | csv                              the same as an Excel or CSV file

Windows: Top hosts, Top items, Problems, Problems by severity, Host availability, Host navigator, Item navigator,
Honeycomb, Item value, Gauge, Item history, Graph, Graph (classic), Pie chart, Map (its elements and their state),
Geomap (hosts and locations). Graphs export the data behind them plus a chart. Clocks, URL windows and the like
have no data to export.

  python3 tools/zbx_export.py list                     dashboards and windows (check from the shell)
  python3 tools/zbx_export.py csv <dashboardid> <widgetid>
"""
import csv
import fnmatch
import html
import io
import math
import os
import re
import sys
import time
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

URL = "export"
TYPE_NAMES = {
    "tophosts": "Top hosts", "topitems": "Top items", "problems": "Problems", "problemsbysv": "Problems by severity",
    "hostavail": "Host availability", "hostnavigator": "Host navigator", "itemnavigator": "Item navigator",
    "item": "Item value", "itemhistory": "Item history", "plaintext": "Plain text", "svggraph": "Graph",
    "graph": "Graph (classic)", "graphprototype": "Graph prototype", "piechart": "Pie chart", "gauge": "Gauge",
    "honeycomb": "Honeycomb", "map": "Map", "navtree": "Map navigation tree", "geomap": "Geomap", "url": "URL",
    "clock": "Clock", "actionlog": "Action log", "dataover": "Data overview", "trigover": "Trigger overview",
    "discovery": "Discovery status", "favgraphs": "Favorite graphs", "favmaps": "Favorite maps",
    "web": "Web monitoring", "slareport": "SLA report", "systeminfo": "System information",
    "scatterplot": "Scatter plot",
}
# what the export gives for each window type
KIND = {"svggraph": "chart", "graph": "chart", "piechart": "chart", "map": "map"}
NO_DATA = {"url", "clock", "navtree", "favgraphs", "favmaps", "discovery", "web", "slareport", "systeminfo",
           "actionlog", "dataover", "trigover", "graphprototype", "scatterplot"}
SEV_NAME = ["Not classified", "Information", "Warning", "Average", "High", "Disaster"]
SEV_COLOR = ["97AAB3", "7499FF", "FFC859", "FFA059", "E97659", "E45959"]
PERIODS = [("1h", "Last hour", 3600), ("6h", "Last 6 hours", 6 * 3600), ("24h", "Last 24 hours", 86400),
           ("7d", "Last 7 days", 7 * 86400), ("30d", "Last 30 days", 30 * 86400)]
MAX_SERIES = 40
e = html.escape


class ExportError(ValueError):
    pass


# ------------------------------------------------------------------ widget fields
def tree(fields):
    """Widget fields (flat names like columns.2.thresholds.0.color) as nested dicts. The older
    'columnsthresholds.2.color.0' spelling is read as columns.2.thresholds.0.color."""
    t = {}
    for f in fields or []:
        parts = f["name"].split(".")
        if parts[0] == "columnsthresholds" and len(parts) == 4:
            parts = ["columns", parts[1], "thresholds", parts[3], parts[2]]
        node = t
        for p in parts[:-1]:
            nxt = node.get(p)
            if not isinstance(nxt, dict):
                nxt = node[p] = {}
            node = nxt
        last = parts[-1]
        if last in node and not isinstance(node[last], dict):
            prev = node[last]
            node[last] = (prev if isinstance(prev, list) else [prev]) + [f["value"]]
        else:
            node[last] = f["value"]
    return t


def L(v):
    """A field that holds several values (dict with 0, 1, 2... keys, a list or one value) as a list."""
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        keys = [k for k in v if k.isdigit()]
        if keys and len(keys) == len(v):
            return [v[k] for k in sorted(keys, key=int)]
        return [v]
    return [v]


def G(t, path, default=None):
    node = t
    for p in path.split("."):
        if not isinstance(node, dict) or p not in node:
            return default
        node = node[p]
    return node


def I(t, path, default=0):
    try:
        return int(G(t, path, default))
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ value formatting (as the Zabbix frontend)
def num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def fmt_dec(x, d):
    s = f"{x:.{max(d, 0)}f}"
    return "0" if re.fullmatch(r"-0\.?0*", s) else s


def age(sec, ms=False):
    """1d 2h 3m (three biggest units), like Zabbix 's' units and problem durations."""
    sec = float(sec)
    neg, sec = sec < 0, abs(sec)
    if sec < 1 and ms:
        return ("-" if neg else "") + (f"{round(sec * 1000)}ms" if sec else "0")
    units = [("y", 365 * 86400), ("M", 30 * 86400), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]
    out, rest = [], int(round(sec))
    for name, size in units:
        if rest >= size:
            out.append(f"{rest // size}{name}")
            rest %= size
        if len(out) == 3:
            break
    if ms and len(out) < 3 and sec < 60:
        frac = round((sec - int(sec)) * 1000)
        if frac:
            out.append(f"{frac}ms")
    return ("-" if neg else "") + (" ".join(out) or "0")


def uptime(sec):
    sec = int(float(sec))
    days, rest = divmod(abs(sec), 86400)
    h, rest = divmod(rest, 3600)
    m, s = divmod(rest, 60)
    hms = f"{h:02d}:{m:02d}:{s:02d}"
    return (f"{days} day{'s' if days != 1 else ''}, " if days else "") + hms


NO_PREFIX = {"%", "ms", "rpm", "RPM", "dBm", "dB", "°C", "C", "V"}


def fmt_value(raw, item, decimals=2, tz=None, mapping=True):
    """The value as Zabbix shows it: value map 'Up (1)', units with K/M/G prefixes, s / uptime / unixtime."""
    if raw is None or raw == "":
        return ""
    vt = str(item.get("value_type", "0"))
    if mapping:
        for m in (item.get("valuemap") or {}).get("mappings", []) if isinstance(item.get("valuemap"), dict) else []:
            if str(m.get("type", "0")) == "0" and m.get("value") == str(raw).strip():
                return f"{m['newvalue']} ({raw})"
    if vt not in ("0", "3"):
        return str(raw)
    x = num(raw)
    if x is None:
        return str(raw)
    units = item.get("units", "") or ""
    if units == "s":       # widgets show seconds in the biggest unit that fits: 105.9ms, 2.5s, 1.6m, 1.69h, 11.57d, 1.93M, 1.3y
        if x == 0:
            return "0"
        for name, size in (("y", 31536000), ("M", 2592000), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
            if abs(x) >= size:
                return f"{fmt_dec(x / size, decimals)}{name}"
        return f"{fmt_dec(x * 1000, decimals)}ms"
    if units == "uptime":
        return uptime(x)
    if units == "unixtime":
        return datetime.fromtimestamp(x, tz).strftime("%Y-%m-%d %H:%M:%S") if x > 0 else str(raw)
    if units.startswith("!"):
        return f"{fmt_dec(x, decimals)} {units[1:]}".strip()
    if not units or units in NO_PREFIX:
        return f"{fmt_dec(x, decimals)} {units}".strip()
    step = 1024 if units in ("B", "Bps") else 1000
    prefixes = ["", "K", "M", "G", "T", "P", "E"]
    i = 0
    while abs(x) >= step and i < len(prefixes) - 1:
        x /= step
        i += 1
    return f"{fmt_dec(x, decimals)} {prefixes[i]}{units}".strip()


def parse_threshold(s):
    s = str(s).strip()
    m = re.fullmatch(r"(-?[\d.]+)\s*([KMGTsmhdw]?)", s)
    if not m:
        return num(s)
    mult = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    return float(m.group(1)) * mult[m.group(2)]


def pick_color(value, thresholds, base=None):
    """Colour of the highest threshold the value reaches (Zabbix widget rule), else the base colour."""
    x = num(value)
    best = base or None
    if x is None:
        return best
    th = sorted(((parse_threshold(t.get("threshold")), t.get("color")) for t in thresholds if isinstance(t, dict)
                 and t.get("color")), key=lambda p: (p[0] is None, p[0]))
    for v, c in th:
        if v is not None and x >= v:
            best = c
    return best


def cell(text, bg=None, value=None):
    return {"t": "" if text is None else str(text), "bg": (bg or "").lstrip("#").upper() or None, "v": value}


def fg_for(bg):
    try:
        r, g, b = (int(bg[i:i + 2], 16) for i in (0, 2, 4))
    except (TypeError, ValueError):
        return None
    return "1D2433" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "FFFFFF"


# ------------------------------------------------------------------ context: API access and caches
class Ctx:
    def __init__(self, api, tzname="UTC", zurl="", period=None):
        self.api = api
        self.tzname = tzname
        self.tz = ZoneInfo(tzname)
        self.zurl = zurl
        self.period = period
        self.now = int(time.time())
        self._groups = None
        self._sev = None

    def call(self, method, params):
        return self.api.call(method, params)

    def ts(self, clock):
        return datetime.fromtimestamp(int(clock), self.tz).strftime("%Y-%m-%d %H:%M:%S")

    def severities(self):
        if self._sev is None:
            names, colors = list(SEV_NAME), list(SEV_COLOR)
            try:
                s = self.call("settings.get", {"output": [f"severity_{k}_{i}" for k in ("name", "color") for i in range(6)]})
                names = [s.get(f"severity_name_{i}") or names[i] for i in range(6)]
                colors = [s.get(f"severity_color_{i}") or colors[i] for i in range(6)]
            except Exception:  # noqa: BLE001 - not every API user may read the settings
                pass
            self._sev = (names, colors)
        return self._sev

    def all_groups(self):
        if self._groups is None:
            self._groups = self.call("hostgroup.get", {"output": ["groupid", "name"]})
        return self._groups

    def expand_groups(self, gids):
        """The groups and their subgroups (Zabbix widgets include subgroups)."""
        gids = [str(g) for g in gids]
        if not gids:
            return []
        names = {g["groupid"]: g["name"] for g in self.all_groups()}
        out = set(gids)
        for gid in gids:
            n = names.get(gid)
            if n:
                out |= {g for g, name in names.items() if name.startswith(n + "/")}
        return sorted(out, key=int)

    def hosts(self, t, tags_key="tags", evaltype_key="evaltype", extra=None, maintenance_default=1):
        """Monitored hosts that match the window's host groups, hosts and host tags."""
        p = {"output": ["hostid", "host", "name", "maintenance_status"], "filter": {"status": 0},
             "selectInterfaces": ["type", "main", "useip", "ip", "dns", "available"]}
        gids = self.expand_groups(L(G(t, "groupids")))
        if gids:
            p["groupids"] = gids
        hids = L(G(t, "hostids"))
        if hids:
            p["hostids"] = hids
        tags = [{"tag": x.get("tag", ""), "operator": int(x.get("operator", 0) or 0), "value": x.get("value", "")}
                for x in L(G(t, tags_key)) if isinstance(x, dict) and x.get("tag") is not None]
        if tags:
            p["tags"] = tags
            p["evaltype"] = I(t, evaltype_key, 0)
        p.update(extra or {})
        hosts = self.call("host.get", p)
        exclude = set(self.expand_groups(L(G(t, "exclude_groupids"))))
        if exclude:
            inside = {h["hostid"] for h in self.call("host.get", {"output": ["hostid"], "groupids": sorted(exclude)})}
            hosts = [h for h in hosts if h["hostid"] not in inside]
        if I(t, "maintenance", maintenance_default) == 0:
            hosts = [h for h in hosts if str(h.get("maintenance_status", "0")) != "1"]
        return hosts

    def items(self, hostids, names=None, patterns=None, itemids=None):
        p = {"output": ["itemid", "hostid", "name", "key_", "lastvalue", "lastclock", "units", "value_type",
                        "valuemapid"], "selectValueMap": ["mappings"], "filter": {"status": 0}, "webitems": True}
        if hostids is not None:
            if not hostids:
                return []
            p["hostids"] = hostids
        if itemids:
            p["itemids"] = itemids
            p.pop("filter")
        if names:
            p["filter"]["name"] = sorted(set(names))
        if patterns:
            p["search"] = {"name": patterns}
            p["searchWildcardsEnabled"] = True
            p["searchByAny"] = True
        return self.call("item.get", p)

    def problems_by_host(self, hostids):
        """{hostid: [severity, ...]} for the current problems of these hosts."""
        if not hostids:
            return {}
        probs = self.call("problem.get", {"output": ["eventid", "severity"], "hostids": hostids, "symptom": False})
        out = {}
        if probs:
            sev = {p["eventid"]: int(p["severity"]) for p in probs}
            for ev in self.call("event.get", {"eventids": list(sev), "output": ["eventid"], "selectHosts": ["hostid"]}):
                for h in ev.get("hosts", []):
                    out.setdefault(h["hostid"], []).append(sev[ev["eventid"]])
        return out

    def sev_cell(self, s):
        names, colors = self.severities()
        return cell(names[s], colors[s])


def host_conn(h, what="conn"):
    order = {1: 0, 2: 1, 4: 2, 3: 3}
    ifs = sorted((i for i in h.get("interfaces", []) if str(i.get("main")) == "1"),
                 key=lambda i: order.get(int(i.get("type", 9)), 9))
    if not ifs:
        return ""
    i = ifs[0]
    if what == "ip":
        return i.get("ip", "")
    if what == "dns":
        return i.get("dns", "")
    return i.get("ip", "") if str(i.get("useip", "1")) == "1" else i.get("dns", "")


def macros(text, h):
    rep = {"{HOST.CONN}": host_conn(h), "{HOST.IP}": host_conn(h, "ip"), "{HOST.DNS}": host_conn(h, "dns"),
           "{HOST.NAME}": h.get("name", ""), "{HOST.HOST}": h.get("host", "")}
    return re.sub(r"\{HOST\.(CONN|IP|DNS|NAME|HOST)\}", lambda m: rep[m.group(0)], text or "")


def table(title, columns, rows, note=""):
    return {"kind": "table", "title": title, "columns": columns, "rows": rows, "note": note}


def note_block(title, text, link=None):
    return {"kind": "note", "title": title, "text": text, "link": link}


# ------------------------------------------------------------------ windows
def w_tophosts(ctx, t, title):
    cols = [c if isinstance(c, dict) else {} for c in L(G(t, "columns"))]
    if not cols:
        return [note_block(title, "This window has no columns.")]
    hosts = ctx.hosts(t, maintenance_default=0)
    names = [c.get("item") for c in cols if str(c.get("data", "1")) == "1" and c.get("item")]
    by = {}
    for it in ctx.items([h["hostid"] for h in hosts], names=names) if names else []:
        it["valuemap"] = it.get("valuemap") if isinstance(it.get("valuemap"), dict) else None
        by.setdefault((it["hostid"], it["name"]), it)
    notes = []
    if any(str(c.get("aggregate_function", "0")) not in ("0", "") for c in cols):
        notes.append("A column of this window aggregates over time in Zabbix; the export shows the latest value.")
    sort_i = I(t, "column", 0)
    sort_c = cols[sort_i] if 0 <= sort_i < len(cols) else cols[0]
    order = I(t, "order", 2)       # 2 = top N (highest first), 3 = bottom N (lowest first)
    limit = I(t, "show_lines", 10) or 10
    rows = []
    for h in hosts:
        key_item = by.get((h["hostid"], sort_c.get("item"))) if str(sort_c.get("data", "1")) == "1" else None
        if str(sort_c.get("data", "1")) == "1" and (not key_item or not int(key_item.get("lastclock") or 0)):
            continue      # Zabbix lists only hosts that have a value for the sort column
        cells, raw_sort = [], None
        for c in cols:
            data = str(c.get("data", "1"))
            if data == "2":
                cells.append(cell(h["name"]))
                continue
            if data == "3":
                cells.append(cell(macros(c.get("text", ""), h)))
                continue
            it = by.get((h["hostid"], c.get("item")))
            if not it or not int(it.get("lastclock") or 0):
                cells.append(cell(""))
                continue
            raw = it.get("lastvalue")
            dec = I(c, "decimal_places", 2)
            text_col = str(c.get("display_value_as", "0")) == "1" or str(it.get("value_type")) not in ("0", "3")
            if text_col:
                cells.append(cell(raw))
            else:
                bg = pick_color(raw, L(c.get("thresholds")), c.get("base_color"))
                cells.append(cell(fmt_value(raw, it, dec, ctx.tz), bg, num(raw)))
        if str(sort_c.get("data", "1")) == "1":
            raw_sort = by[(h["hostid"], sort_c.get("item"))].get("lastvalue")
        elif str(sort_c.get("data")) == "2":
            raw_sort = h["name"]
        else:
            raw_sort = macros(sort_c.get("text", ""), h)
        rows.append((raw_sort, int(h["hostid"]), cells))

    def key(r):   # numbers first (highest first for top N, lowest first for bottom N), then text
        x = num(r[0])
        if x is None:
            return (1, 0, str(r[0]).lower(), r[1])
        return (0, -x if order == 2 else x, "", r[1])   # equal values: in host order, as Zabbix lists them
    rows.sort(key=key)
    return [table(title, [c.get("name", "") for c in cols], [r[2] for r in rows[:limit]], " ".join(notes))]


def w_topitems(ctx, t, title):
    cols = [c if isinstance(c, dict) else {} for c in L(G(t, "columns"))]
    hosts = ctx.hosts(t, "host_tags", "evaltype_host", maintenance_default=1)
    hosts.sort(key=lambda h: h["name"].lower())
    hosts = hosts[:I(t, "host_ordering_limit", 10) or 10]
    hid = {h["hostid"]: h for h in hosts}
    patterns = sorted({p for c in cols for p in L(c.get("items"))})
    items = ctx.items(list(hid), patterns=patterns) if patterns and hid else []
    col_of = {}
    for it in items:
        for i, c in enumerate(cols):
            if any(fnmatch.fnmatchcase(it["name"].lower(), p.lower()) for p in L(c.get("items"))):
                col_of[it["itemid"]] = i
                break
    items = [it for it in items if it["itemid"] in col_of and int(it.get("lastclock") or 0)]
    names = sorted({it["name"] for it in items}, key=str.lower)
    by_val = I(t, "item_ordering_order_by", 2) == 3
    desc = I(t, "item_ordering_order", 2) == 2
    if by_val:
        best = {}
        for it in items:
            x = num(it["lastvalue"])
            if x is not None:
                best[it["name"]] = max(best.get(it["name"], -1e300), x) if desc else min(best.get(it["name"], 1e300), x)
        names.sort(key=lambda n: best.get(n, -1e300 if desc else 1e300), reverse=desc)
    names = names[:I(t, "item_ordering_limit", 100) or 100]
    grid = {(it["hostid"], it["name"]): it for it in items}

    def c_for(it):
        c = cols[col_of[it["itemid"]]]
        raw = it["lastvalue"]
        if str(it.get("value_type")) not in ("0", "3"):
            return cell(raw)
        return cell(fmt_value(raw, it, I(c, "decimal_places", 2), ctx.tz),
                    pick_color(raw, L(c.get("thresholds")), c.get("base_color")), num(raw))
    used_hosts = [h for h in hosts if any((h["hostid"], n) in grid for n in names)]
    if I(t, "layout", 0) == 1:     # one row per item, one column per host
        rows = [[cell(n)] + [c_for(grid[(h["hostid"], n)]) if (h["hostid"], n) in grid else cell("")
                             for h in used_hosts] for n in names]
        return [table(title, ["Item"] + [h["name"] for h in used_hosts], rows)]
    rows = [[cell(h["name"])] + [c_for(grid[(h["hostid"], n)]) if (h["hostid"], n) in grid else cell("")
                                 for n in names] for h in used_hosts]
    return [table(title, ["Host"] + names, rows)]


def tag_text(tags, fmt, priority, count):
    if not count:
        return ""
    prio = [p.strip() for p in (priority or "").split(",") if p.strip()]
    tags = sorted(tags, key=lambda x: (prio.index(x["tag"]) if x["tag"] in prio else len(prio), x["tag"], x["value"]))
    out = []
    for x in tags[:count]:
        name = x["tag"] if fmt == 0 else x["tag"][:3] if fmt == 1 else ""
        out.append(f"{name}: {x['value']}" if name and x["value"] else name or x["value"])
    return ", ".join(out)


def problem_params(ctx, t):
    p = {"output": "extend", "selectTags": "extend", "selectAcknowledges": ["clock"], "sortfield": ["eventid"],
         "sortorder": "DESC"}
    gids = ctx.expand_groups(L(G(t, "groupids")))
    if gids:
        p["groupids"] = gids
    if L(G(t, "hostids")):
        p["hostids"] = L(G(t, "hostids"))
    sev = [int(s) for s in L(G(t, "severities"))]
    if sev:
        p["severities"] = sev
    tags = [{"tag": x.get("tag", ""), "operator": int(x.get("operator", 0) or 0), "value": x.get("value", "")}
            for x in L(G(t, "tags")) if isinstance(x, dict)]
    if tags:
        p["tags"] = tags
        p["evaltype"] = I(t, "evaltype", 0)
    if G(t, "problem"):
        p["search"] = {"name": G(t, "problem")}
    if I(t, "show_suppressed", 0) == 0:
        p["suppressed"] = False
    ack = I(t, "acknowledgement_status", 0)
    if ack in (1, 2):
        p["acknowledged"] = ack == 2
    if I(t, "show_symptoms", 0) == 0:
        p["symptom"] = False
    return p


def event_hosts(ctx, eventids):
    out = {}
    for i in range(0, len(eventids), 500):
        for ev in ctx.call("event.get", {"eventids": eventids[i:i + 500], "output": ["eventid"],
                                         "selectHosts": ["hostid", "name"]}):
            out[ev["eventid"]] = ev.get("hosts", [])
    return out


def w_problems(ctx, t, title):
    show = I(t, "show", 3)   # 3 = problems, 1 = recent problems, 2 = history
    limit = I(t, "show_lines", 25) or 25
    p = problem_params(ctx, t)
    if show == 2:
        frm, till = time_window(ctx, t)
        p.pop("selectAcknowledges", None)
        p.pop("symptom", None)
        p.pop("suppressed", None)
        p.update({"source": 0, "object": 0, "value": 1, "time_from": frm, "time_till": till, "limit": limit,
                  "selectAcknowledges": ["clock"]})
        if "search" in p:
            p["search"] = {"name": p["search"]["name"]}
        probs = ctx.call("event.get", p)
        rec = [x["r_eventid"] for x in probs if x.get("r_eventid", "0") != "0"]
        rclock = {r["eventid"]: r["clock"] for r in ctx.call("event.get", {"eventids": rec, "output": ["clock"]})} if rec else {}
        for x in probs:
            x["r_clock"] = rclock.get(x.get("r_eventid"), "0")
    else:
        if show == 1:
            p["recent"] = True
        p["limit"] = limit
        probs = ctx.call("problem.get", p)
    hosts = event_hosts(ctx, [x["eventid"] for x in probs])
    exclude = set(ctx.expand_groups(L(G(t, "exclude_groupids"))))
    if exclude:
        inside = {h["hostid"] for h in ctx.call("host.get", {"output": ["hostid"], "groupids": sorted(exclude)})}
        probs = [x for x in probs if not any(h["hostid"] in inside for h in hosts.get(x["eventid"], []))]
    show_tags = I(t, "show_tags", 0)
    fmt, prio = I(t, "tag_name_format", 0), G(t, "tag_priority", "")
    cols = ["Time", "Severity"] + (["Recovery time", "Status"] if show in (1, 2) else []) + \
        ["Host", "Problem", "Duration", "Ack"] + (["Tags"] if show_tags else [])
    rows = []
    for x in probs[:limit]:
        start, end = int(x["clock"]), int(x.get("r_clock") or 0)
        resolved = x.get("r_eventid", "0") not in ("0", "", None)
        r = [cell(ctx.ts(start)), ctx.sev_cell(int(x["severity"]))]
        if show in (1, 2):
            r += [cell(ctx.ts(end) if resolved and end else ""),
                  cell("RESOLVED", "59DB8F") if resolved else cell("PROBLEM", "E45959")]
        r += [cell(", ".join(h["name"] for h in hosts.get(x["eventid"], []))), cell(x["name"]),
              cell(age((end if resolved and end else ctx.now) - start)),
              cell("Yes" if str(x.get("acknowledged")) == "1" else "No")]
        if show_tags:
            r.append(cell(tag_text(x.get("tags", []), fmt, prio, show_tags)))
        rows.append(r)
    return [table(title, cols, rows)]


def w_problemsbysv(ctx, t, title):
    names, colors = ctx.severities()
    sev = [int(s) for s in L(G(t, "severities"))] or list(range(6))
    sev = sorted(sev, reverse=True)
    p = problem_params(ctx, t)
    p.update({"output": ["eventid", "severity", "acknowledged"]})
    p.pop("selectAcknowledges", None)
    p.pop("selectTags", None)
    p.pop("sortfield", None)
    p.pop("sortorder", None)
    probs = ctx.call("problem.get", p)
    ev_hosts = event_hosts(ctx, [x["eventid"] for x in probs])
    hostids = sorted({h["hostid"] for hs in ev_hosts.values() for h in hs})
    host_groups = {h["hostid"]: [g["groupid"] for g in h.get("hostgroups", [])] for h in
                   ctx.call("host.get", {"output": ["hostid"], "hostids": hostids, "selectHostGroups": ["groupid"]})} if hostids else {}
    sel = ctx.expand_groups(L(G(t, "groupids")))
    exclude = set(ctx.expand_groups(L(G(t, "exclude_groupids"))))
    groups = ctx.call("hostgroup.get", {"output": ["groupid", "name"], "with_monitored_hosts": True,
                                        **({"groupids": sel} if sel else {})})
    groups = sorted((g for g in groups if g["groupid"] not in exclude), key=lambda g: g["name"].lower())
    count = {}
    for x in probs:
        s = int(x["severity"])
        gs = {g for h in ev_hosts.get(x["eventid"], []) for g in host_groups.get(h["hostid"], [])}
        for g in gs:
            count[(g, s)] = count.get((g, s), 0) + 1
        count[("*", s)] = count.get(("*", s), 0) + 1

    def c(g, s):
        n = count.get((g, s), 0)
        return cell(n, colors[s], n) if n else cell("")    # Zabbix leaves a severity with no problems empty
    if I(t, "show_type", 0) == 1:    # totals
        return [table(title, [names[s] for s in sev], [[c("*", s) for s in sev]])]
    rows = []
    for g in groups:
        if I(t, "hide_empty_groups", 0) == 1 and not any(count.get((g["groupid"], s)) for s in sev):
            continue
        rows.append([cell(g["name"])] + [c(g["groupid"], s) for s in sev])
    return [table(title, ["Host group"] + [names[s] for s in sev], rows)]


def w_hostavail(ctx, t, title):
    types = [int(x) for x in L(G(t, "interface_type"))] or [1, 2, 3, 4]
    tname = {1: "Agent", 2: "SNMP", 3: "IPMI", 4: "JMX"}
    hosts = ctx.hosts(t, maintenance_default=1)
    stat = {k: {"Available": 0, "Not available": 0, "Mixed": 0, "Unknown": 0} for k in types}
    for h in hosts:
        for k in types:
            av = {str(i.get("available")) for i in h.get("interfaces", []) if int(i.get("type", 0)) == k}
            if not av:
                continue
            key = "Mixed" if len(av) > 1 else {"1": "Available", "2": "Not available"}.get(av.pop(), "Unknown")
            stat[k][key] += 1
    colors = {"Available": "59DB8F", "Not available": "E45959", "Mixed": "FFA059", "Unknown": "97AAB3"}
    hdr = ["Interface", "Available", "Not available", "Mixed", "Unknown", "Total"]
    rows = [[cell(tname.get(k, k))] + [cell(stat[k][n], colors[n] if stat[k][n] else None, stat[k][n])
                                       for n in hdr[1:5]] + [cell(sum(stat[k].values()), None, sum(stat[k].values()))]
            for k in types]
    return [table(title, hdr, rows)]


def w_hostnavigator(ctx, t, title):
    hosts = ctx.hosts(t, "host_tags", "evaltype", extra={"selectHostGroups": ["name"], "selectTags": "extend"},
                      maintenance_default=1)
    probs = ctx.problems_by_host([h["hostid"] for h in hosts])
    sev_f = {int(s) for s in L(G(t, "severities"))}
    only_problems = I(t, "problems", 0) == 2 or I(t, "status", 0) == 2
    group_by = [g for g in L(G(t, "group_by")) if isinstance(g, dict)]
    attr = int(group_by[0].get("attribute", 0)) if group_by else None
    names, _ = ctx.severities()
    rows = []
    for h in sorted(hosts, key=lambda h: h["name"].lower()):
        ps = [s for s in probs.get(h["hostid"], []) if not sev_f or s in sev_f]
        if only_problems and not ps:
            continue
        if attr == 0:
            grp = ", ".join(sorted(g["name"] for g in h.get("hostgroups", [])))
        elif attr == 1:
            tag = group_by[0].get("tag_name", "")
            grp = ", ".join(sorted({x["value"] for x in h.get("tags", []) if x["tag"] == tag})) or f"({tag} not set)"
        elif attr == 2:
            grp = names[max(ps)] if ps else "No problems"
        else:
            grp = ""
        worst = ctx.sev_cell(max(ps)) if ps else cell("OK", "59DB8F")
        rows.append([grp, cell(h["name"]), cell(len(ps), None, len(ps)), worst])
    rows.sort(key=lambda r: (r[0].lower(), r[1]["t"].lower()))
    limit = I(t, "show_lines", 100) or 100
    rows = rows[:limit]
    if attr is None:
        return [table(title, ["Host", "Problems", "Worst problem"], [r[1:] for r in rows])]
    return [table(title, ["Group", "Host", "Problems", "Worst problem"], [[cell(r[0])] + r[1:] for r in rows])]


def pattern_items(ctx, t, title, host_tags="host_tags"):
    hosts = ctx.hosts(t, host_tags, "evaltype_host", maintenance_default=1)
    hid = {h["hostid"]: h for h in hosts}
    patterns = L(G(t, "items"))
    items = ctx.items(list(hid), patterns=patterns or ["*"]) if hid else []
    if patterns:
        items = [it for it in items if any(fnmatch.fnmatchcase(it["name"].lower(), p.lower()) for p in patterns)]
    items.sort(key=lambda it: (hid[it["hostid"]]["name"].lower(), it["name"].lower()))
    return hid, items


def w_itemlist(ctx, t, title):
    """Item navigator and Honeycomb: every matching item with its latest value."""
    hid, items = pattern_items(ctx, t, title)
    dec = I(t, "decimal_places", 2)
    rows = []
    for it in items:
        if not int(it.get("lastclock") or 0):
            continue
        raw = it["lastvalue"]
        bg = pick_color(raw, L(G(t, "thresholds")), G(t, "bg_color")) if str(it.get("value_type")) in ("0", "3") else None
        rows.append([cell(hid[it["hostid"]]["name"]), cell(it["name"]), cell(fmt_value(raw, it, dec, ctx.tz), bg, num(raw)),
                     cell(ctx.ts(it["lastclock"]))])
    limit = I(t, "show_lines", 0) or I(t, "maximum_items", 0) or 1000
    return [table(title, ["Host", "Item", "Value", "Last check"], rows[:limit])]


def w_item(ctx, t, title):
    ids = L(G(t, "itemid"))
    if not ids:
        return [note_block(title, "This window follows another window or host selection; nothing fixed to export.")]
    items = ctx.call("item.get", {"itemids": ids, "output": ["itemid", "name", "lastvalue", "lastclock", "units",
                                                             "value_type", "description"],
                                  "selectValueMap": ["mappings"], "selectHosts": ["name"]})
    rows = []
    dec = I(t, "decimal_places", 2)
    for it in items:
        raw = it.get("lastvalue")
        bg = pick_color(raw, L(G(t, "thresholds")), G(t, "bg_color")) if str(it.get("value_type")) in ("0", "3") else None
        rows.append([cell(it["hosts"][0]["name"] if it.get("hosts") else ""), cell(it["name"]),
                     cell(fmt_value(raw, it, dec, ctx.tz) if int(it.get("lastclock") or 0) else "No data", bg, num(raw)),
                     cell(ctx.ts(it["lastclock"]) if int(it.get("lastclock") or 0) else "")])
    return [table(title, ["Host", "Item", "Value", "Last check"], rows)]


def w_itemhistory(ctx, t, title):
    cols = [c for c in L(G(t, "columns")) if isinstance(c, dict)]
    ids = [x for c in cols for x in L(c.get("itemid"))]
    if not ids:
        return [note_block(title, "This window has no items.")]
    limit = I(t, "show_lines", 25) or 25
    items = {it["itemid"]: it for it in ctx.call("item.get", {"itemids": ids, "output": ["itemid", "name", "units", "value_type"],
                                                               "selectValueMap": ["mappings"], "selectHosts": ["name"]})}
    label = {x: c.get("name") for c in cols for x in L(c.get("itemid"))}
    vals = []
    for iid, it in items.items():
        for v in ctx.call("history.get", {"history": int(it["value_type"]), "itemids": [iid], "sortfield": "clock",
                                          "sortorder": "DESC", "limit": limit}):
            vals.append((int(v["clock"]), label.get(iid) or it["name"], fmt_value(v["value"], it, 2, ctx.tz)))
    vals.sort(key=lambda v: -v[0])
    return [table(title, ["Time", "Name", "Value"], [[cell(ctx.ts(c)), cell(n), cell(v)] for c, n, v in vals[:limit]])]


# ---- graphs
def parse_rel(s, now, tz):
    """Zabbix time: now, now-1h, now-7d/d, now/d, or 'YYYY-MM-DD HH:MM:SS'."""
    s = (s or "").strip()
    if not s.startswith("now"):
        try:
            return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz).timestamp())
        except ValueError:
            try:
                return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=tz).timestamp())
            except ValueError:
                return None
    t = now
    sizes = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 30 * 86400, "y": 365 * 86400}
    for op, n, u in re.findall(r"([-+/])(\d*)([smhdwMy])", s[3:]):
        if op == "/":
            dt = datetime.fromtimestamp(t, tz)
            if u == "d":
                dt = dt.replace(hour=0, minute=0, second=0)
            elif u == "h":
                dt = dt.replace(minute=0, second=0)
            elif u == "w":
                dt = dt.replace(hour=0, minute=0, second=0)
                t = int(dt.timestamp()) - dt.weekday() * 86400
                continue
            elif u == "M":
                dt = dt.replace(day=1, hour=0, minute=0, second=0)
            elif u == "y":
                dt = dt.replace(month=1, day=1, hour=0, minute=0, second=0)
            t = int(dt.timestamp())
        else:
            t += (1 if op == "+" else -1) * int(n or 1) * sizes[u]
    return t


def time_window(ctx, t):
    if ctx.period:
        sec = dict((k, s) for k, _, s in PERIODS).get(ctx.period, 3600)
        return ctx.now - sec, ctx.now
    if G(t, "time_period._reference") or not G(t, "time_period"):
        return ctx.now - 3600, ctx.now        # the dashboard's own period: Zabbix starts at the last hour
    frm = parse_rel(G(t, "time_period.from", "now-1h"), ctx.now, ctx.tz) or ctx.now - 3600
    till = parse_rel(G(t, "time_period.to", "now"), ctx.now, ctx.tz) or ctx.now
    return frm, max(till, frm + 60)


def graph_items(ctx, t, wtype):
    """[(item, colour)] drawn by a Graph, classic graph or pie chart window."""
    out = []
    if wtype == "graph":
        if I(t, "source_type", 0) == 1:
            ids = L(G(t, "itemid"))
            its = ctx.items(None, itemids=ids) if ids else []
            return [(it, "1A7C11") for it in its]
        gids = L(G(t, "graphid"))
        if not gids:
            return []
        g = ctx.call("graph.get", {"graphids": gids, "output": ["name"], "selectGraphItems": ["itemid", "color"]})
        if not g:
            return []
        color = {gi["itemid"]: gi["color"] for gi in g[0]["gitems"]}
        return [(it, color.get(it["itemid"], "1A7C11")) for it in ctx.items(None, itemids=list(color))]
    palette = ["FF465C", "FFD54F", "0EC9AC", "524BBC", "ED1248", "D1E754", "2AB5FF", "385CC7", "EC1594", "BAE37D",
               "6AC8FF", "EE2B29", "3CA20D", "6F4BBC", "00A1FF", "F3601B", "1D9CA1", "F8B500", "CA4E48", "93B91F"]
    for di, ds in enumerate(d for d in L(G(t, "ds")) if isinstance(d, dict)):
        dtype = int(ds.get("dataset_type", 1) or 1)
        if dtype == 0:       # item list
            ids = L(ds.get("itemids"))
            colors = L(ds.get("color"))
            its = {it["itemid"]: it for it in ctx.items(None, itemids=ids)} if ids else {}
            for k, iid in enumerate(ids):
                if iid in its:
                    out.append((its[iid], colors[k] if k < len(colors) else palette[(di + k) % len(palette)]))
            continue
        hp, ip = L(ds.get("hosts")), L(ds.get("items"))
        if not hp or not ip:
            continue
        hosts = ctx.call("host.get", {"output": ["hostid", "name"], "filter": {"status": 0},
                                      "search": {"name": hp}, "searchWildcardsEnabled": True, "searchByAny": True})
        hosts = [h for h in hosts if any(fnmatch.fnmatchcase(h["name"].lower(), p.lower()) for p in hp)]
        its = ctx.items([h["hostid"] for h in hosts], patterns=ip) if hosts else []
        its = [it for it in its if any(fnmatch.fnmatchcase(it["name"].lower(), p.lower()) for p in ip)
               and str(it.get("value_type")) in ("0", "3")]
        base = ds.get("color") if isinstance(ds.get("color"), str) else palette[di % len(palette)]
        for k, it in enumerate(sorted(its, key=lambda x: (x["hostid"], x["name"]))):
            out.append((it, base if k == 0 else shade(base, k)))
    return out[:MAX_SERIES]


def shade(hex6, k):
    try:
        r, g, b = (int(hex6[i:i + 2], 16) for i in (0, 2, 4))
    except (TypeError, ValueError):
        return "888888"
    f = 1 - 0.12 * (k % 6)
    return "".join(f"{max(0, min(255, int(c * f))):02X}" for c in (r, g, b))


def fetch_series(ctx, items, frm, till):
    """{itemid: [(clock, value)]}: history for up to two days, hourly trends (average) beyond."""
    out = {it["itemid"]: [] for it in items}
    use_trends = till - frm > 2 * 86400
    by_type = {}
    for it in items:
        by_type.setdefault(int(it["value_type"]), []).append(it["itemid"])
    for vt, ids in by_type.items():
        for i in range(0, len(ids), 20):
            chunk = ids[i:i + 20]
            if use_trends:
                rows = ctx.call("trend.get", {"itemids": chunk, "time_from": frm, "time_till": till,
                                              "output": ["itemid", "clock", "value_avg"]})
                for r in rows:
                    out[r["itemid"]].append((int(r["clock"]), float(r["value_avg"])))
            else:
                rows = ctx.call("history.get", {"history": vt, "itemids": chunk, "time_from": frm, "time_till": till,
                                                "sortfield": "clock", "sortorder": "ASC", "limit": 200000})
                for r in rows:
                    x = num(r["value"])
                    if x is not None:
                        out[r["itemid"]].append((int(r["clock"]), x))
    for v in out.values():
        v.sort()
    return out, use_trends


def w_graph(ctx, t, title, wtype):
    gi = graph_items(ctx, t, wtype)
    if not gi:
        return [note_block(title, "No items match this graph.")]
    hosts = {h["hostid"]: h["name"] for h in ctx.call("host.get", {"output": ["hostid", "name"],
                                                                   "hostids": sorted({it["hostid"] for it, _ in gi})})}
    names = {it["itemid"]: f"{hosts.get(it['hostid'], '')}: {it['name']}" for it, _ in gi}
    if wtype == "piechart":
        vals = [(it, c, num(it.get("lastvalue")) or 0) for it, c in gi if int(it.get("lastclock") or 0)]
        total = sum(abs(v) for _, _, v in vals) or 1
        rows = [[cell(names[it["itemid"]], c), cell(fmt_value(v, it, 2, ctx.tz), None, v),
                 cell(f"{100 * abs(v) / total:.1f}%", None, round(100 * abs(v) / total, 1))] for it, c, v in vals]
        return [table(title, ["Item", "Value", "Share"], rows)]
    frm, till = time_window(ctx, t)
    series, trends = fetch_series(ctx, [it for it, _ in gi], frm, till)
    summary, data = [], []
    for it, c in gi:
        pts = series.get(it["itemid"], [])
        vs = [v for _, v in pts]
        f = (lambda v: fmt_value(v, it, 2, ctx.tz))
        summary.append([cell(names[it["itemid"]]), cell(f(vs[-1]) if vs else "", None, vs[-1] if vs else None),
                        cell(f(min(vs)) if vs else "", None, min(vs) if vs else None),
                        cell(f(sum(vs) / len(vs)) if vs else "", None, round(sum(vs) / len(vs), 4) if vs else None),
                        cell(f(max(vs)) if vs else "", None, max(vs) if vs else None), cell(len(vs), None, len(vs))])
        for clock, v in pts:
            data.append([cell(ctx.ts(clock)), cell(hosts.get(it["hostid"], "")), cell(it["name"]),
                         cell(round(v, 6), None, v), cell(it.get("units", ""))])
    data.sort(key=lambda r: r[0]["t"])
    span = f"{ctx.ts(frm)} to {ctx.ts(till)} ({ctx.tzname})" + (", hourly averages" if trends else "")
    chart = {"kind": "chart", "title": title, "series": [(names[it["itemid"]], c, series.get(it["itemid"], []), it)
                                                           for it, c in gi], "frm": frm, "till": till, "note": span}
    return [chart, table(title + ": summary", ["Item", "Last", "Min", "Avg", "Max", "Values"], summary, span),
            table(title + ": data", ["Time", "Host", "Item", "Value", "Units"], data, span)]


def w_map(ctx, t, title):
    ids = L(G(t, "sysmapid"))
    if not ids:
        return [note_block(title, "This window follows a map navigation tree; open the map itself to export it.")]
    m = ctx.call("map.get", {"sysmapids": ids, "output": ["sysmapid", "name"], "selectSelements": "extend"})
    if not m:
        return [note_block(title, "Map not found.")]
    m = m[0]
    els = m.get("selements", [])
    hostids = sorted({e2["hostid"] for el in els if str(el["elementtype"]) == "0" for e2 in el.get("elements", [])})
    groupids = sorted({e2["groupid"] for el in els if str(el["elementtype"]) == "3" for e2 in el.get("elements", [])})
    hosts = {h["hostid"]: h for h in ctx.call("host.get", {"output": ["hostid", "host", "name"], "hostids": hostids,
                                                           "selectInterfaces": ["type", "main", "useip", "ip", "dns"]})} if hostids else {}
    ghosts = {}
    if groupids:
        for h in ctx.call("host.get", {"output": ["hostid"], "groupids": groupids, "selectHostGroups": ["groupid"]}):
            for g in h.get("hostgroups", []):
                ghosts.setdefault(g["groupid"], []).append(h["hostid"])
    gname = {g["groupid"]: g["name"] for g in ctx.all_groups()}
    probs = ctx.problems_by_host(sorted(set(hostids) | {h for v in ghosts.values() for h in v}))
    subs = sorted({e2["sysmapid"] for el in els if str(el["elementtype"]) == "1" for e2 in el.get("elements", [])})
    sname = {s["sysmapid"]: s["name"] for s in ctx.call("map.get", {"sysmapids": subs, "output": ["name"]})} if subs else {}
    trig = sorted({e2["triggerid"] for el in els if str(el["elementtype"]) == "2" for e2 in el.get("elements", [])})
    tinfo = {x["triggerid"]: x for x in ctx.call("trigger.get", {"triggerids": trig, "output": ["description", "value", "priority"],
                                                                 "expandDescription": True})} if trig else {}
    tn = {0: "Host", 1: "Map", 2: "Trigger", 3: "Host group", 4: "Image"}
    rows = []
    for el in sorted(els, key=lambda x: (int(x.get("y", 0)), int(x.get("x", 0)))):
        typ = int(el["elementtype"])
        if typ == 4 and not (el.get("label") or "").strip():
            continue
        label, what, sevs = el.get("label", ""), "", []
        if typ == 0:
            h = hosts.get(el["elements"][0]["hostid"]) if el.get("elements") else None
            what = h["name"] if h else ""
            sevs = probs.get(h["hostid"], []) if h else []
            label = macros(label, h or {})
        elif typ == 3:
            g = el["elements"][0]["groupid"] if el.get("elements") else ""
            what = gname.get(g, "")
            sevs = [s for hid in ghosts.get(g, []) for s in probs.get(hid, [])]
        elif typ == 1:
            what = sname.get(el["elements"][0]["sysmapid"], "") if el.get("elements") else ""
        elif typ == 2:
            ts = [tinfo[e2["triggerid"]] for e2 in el.get("elements", []) if e2["triggerid"] in tinfo]
            what = "; ".join(x["description"] for x in ts)
            sevs = [int(x["priority"]) for x in ts if str(x["value"]) == "1"]
        label = re.sub(r"\{[A-Z.]+\}", "", label).strip()
        state = ctx.sev_cell(max(sevs)) if sevs else (cell("OK", "59DB8F") if typ in (0, 2, 3) else cell(""))
        rows.append([cell(label or what), cell(tn.get(typ, typ)), cell(what), cell(len(sevs), None, len(sevs)) if typ in (0, 2, 3) else cell(""), state])
    link = f"{ctx.zurl}/zabbix.php?action=map.view&sysmapid={m['sysmapid']}" if ctx.zurl else None
    return [{"kind": "map", "title": title, "map": m["name"], "link": link},
            table(title + ": elements", ["Element", "Type", "Host / group / map", "Problems", "State"], rows,
                  "A map is a picture: the export lists every element on it with its live state.")]


def w_geomap(ctx, t, title):
    hosts = ctx.hosts(t, "tags", "evaltype", extra={"selectInventory": ["location_lat", "location_lon"]},
                      maintenance_default=1)
    hosts = [h for h in hosts if isinstance(h.get("inventory"), dict) and h["inventory"].get("location_lat")]
    probs = ctx.problems_by_host([h["hostid"] for h in hosts])
    rows = []
    for h in sorted(hosts, key=lambda h: h["name"].lower()):
        ps = probs.get(h["hostid"], [])
        rows.append([cell(h["name"]), cell(h["inventory"]["location_lat"]), cell(h["inventory"]["location_lon"]),
                     cell(len(ps), None, len(ps)), ctx.sev_cell(max(ps)) if ps else cell("OK", "59DB8F")])
    return [table(title, ["Host", "Latitude", "Longitude", "Problems", "State"], rows)]


def w_url(ctx, t, title):
    url = G(t, "url", "")
    if "/netmap" in url:
        return [note_block(title, "This window shows the MDataPulse network map (a live picture). Open it to see "
                                  "or print it.", "netmap")]
    return [note_block(title, "This window shows another web page; there is no Zabbix data to export.", url or None)]


HANDLERS = {
    "tophosts": w_tophosts, "topitems": w_topitems, "problems": w_problems, "problemsbysv": w_problemsbysv,
    "hostavail": w_hostavail, "hostnavigator": w_hostnavigator, "itemnavigator": w_itemlist, "honeycomb": w_itemlist,
    "item": w_item, "gauge": w_item, "itemhistory": w_itemhistory, "map": w_map, "geomap": w_geomap, "url": w_url,
}


def widget_title(w):
    return (w.get("name") or "").strip() or TYPE_NAMES.get(w["type"], w["type"])


def export_widget(ctx, w):
    t = tree(w.get("fields"))
    title = widget_title(w)
    if w["type"] in ("svggraph", "graph", "piechart"):
        return w_graph(ctx, t, title, w["type"])
    fn = HANDLERS.get(w["type"])
    if not fn:
        return [note_block(title, f"A {TYPE_NAMES.get(w['type'], w['type'])} window has no table data to export.")]
    return fn(ctx, t, title)


# ------------------------------------------------------------------ dashboards
def dashboards(api):
    ds = api.call("dashboard.get", {"output": ["dashboardid", "name"], "selectPages": "extend"})
    return sorted(ds, key=lambda d: d["name"].lower())


def dashboard(api, did):
    ds = api.call("dashboard.get", {"output": ["dashboardid", "name"], "dashboardids": [did], "selectPages": "extend"})
    if not ds:
        raise ExportError("Dashboard not found (it may have been deleted, or this account cannot see it).")
    return ds[0]


def ordered(widgets):
    return sorted(widgets, key=lambda w: (int(w.get("y", 0)), int(w.get("x", 0))))


def page_name(d, i):
    p = d["pages"][i]
    return p.get("name") or (f"Page {i + 1}" if len(d["pages"]) > 1 else "")


def select(api, q):
    """(dashboard, title, [widgets]) for ?d=&w= or ?d=&p=."""
    d = dashboard(api, q.get("d", ""))
    if q.get("w"):
        for i, p in enumerate(d["pages"]):
            for w in p["widgets"]:
                if str(w.get("widgetid")) == q["w"]:
                    return d, widget_title(w), [w], i
        raise ExportError("That window is no longer on the dashboard. Pick it again from the list.")
    i = int(q.get("p", "0") or 0) if str(q.get("p", "0")).isdigit() else 0
    if not 0 <= i < len(d["pages"]):
        raise ExportError("That dashboard page does not exist.")
    return d, page_name(d, i) or d["name"], ordered(d["pages"][i]["widgets"]), i


def run(ctx, widgets):
    blocks = []
    for w in widgets:
        try:
            blocks += export_widget(ctx, w)
        except Exception as ex:  # noqa: BLE001 - one bad window must not break a whole page
            blocks.append(note_block(widget_title(w), f"Could not read this window: {ex}"))
    return blocks


# ------------------------------------------------------------------ outputs
def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")[:60] or "export"


def to_csv(blocks, head):
    out = io.StringIO()
    out.write("﻿")
    w = csv.writer(out)
    w.writerow([head])
    for b in blocks:
        if b["kind"] == "note":
            w.writerow([])
            w.writerow([b["title"]])
            w.writerow([b["text"]])
            continue
        if b["kind"] != "table":
            continue
        w.writerow([])
        w.writerow([b["title"]])
        w.writerow(b["columns"])
        for r in b["rows"]:
            w.writerow([c["t"] for c in r])
    return out.getvalue().encode("utf-8")


def xml(s):
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(s))
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def col_letter(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def to_xlsx(blocks, head):
    """An .xlsx file (Office Open XML, standard library only): one sheet per table, coloured cells as on screen."""
    fills, styles = ["none", "gray125"], [(0, 0)]       # styles = (fill index, font index); fonts 0 normal 1 bold 2 title

    def style(bg=None, font=0):
        fi = 0
        if bg:
            key = bg.upper()
            if key not in fills:
                fills.append(key)
            fi = fills.index(key)
        font_i = font if not bg else (3 if fg_for(bg) == "FFFFFF" else font)
        k = (fi, font_i)
        if k not in styles:
            styles.append(k)
        return styles.index(k)
    sheets, used = [], set()
    for b in [b for b in blocks if b["kind"] == "table"] or [table(head, ["Nothing to export"], [])]:
        name = re.sub(r"\s+", " ", re.sub(r"[\[\]:*?/\\]", " - ", b["title"])).strip(" -")[:28] or "Sheet"
        base, n = name, 2
        while name.lower() in used:
            name = f"{base[:25]} {n}"
            n += 1
        used.add(name.lower())
        rows_xml = []
        widths = [len(c) for c in b["columns"]]

        def row(ri, cells):
            out = []
            for ci, (text, st, v) in enumerate(cells):
                ref = f"{col_letter(ci)}{ri}"
                if ci < len(widths):
                    widths[ci] = max(widths[ci], min(len(str(text)), 60))
                if v is not None and isinstance(v, (int, float)) and str(text).strip() == fmt_plain(v):
                    out.append(f'<c r="{ref}" s="{st}"><v>{v}</v></c>')
                else:
                    out.append(f'<c r="{ref}" s="{st}" t="inlineStr"><is><t xml:space="preserve">{xml(text)}</t></is></c>')
            return f'<row r="{ri}">{"".join(out)}</row>'
        rows_xml.append(row(1, [(b["title"], style(font=2), None)]))
        rows_xml.append(row(2, [(head + (" · " + b["note"] if b.get("note") else ""), style(), None)]))
        rows_xml.append(row(4, [(c, style(font=1), None) for c in b["columns"]]))
        for i, r in enumerate(b["rows"]):
            rows_xml.append(row(5 + i, [(c["t"], style(c["bg"]), c["v"]) for c in r]))
        cols_xml = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{min(max(w, 6) + 2, 62)}" customWidth="1"/>'
                           for i, w in enumerate(widths))
        sheets.append((name, f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                             f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                             f'<sheetViews><sheetView workbookViewId="0"><pane ySplit="4" topLeftCell="A5" '
                             f'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
                             f'{"<cols>" + cols_xml + "</cols>" if cols_xml else ""}'
                             f'<sheetData>{"".join(rows_xml)}</sheetData>'
                             + (f'<autoFilter ref="A4:{col_letter(len(b["columns"]) - 1)}{4 + len(b["rows"])}"/>'
                                if b["rows"] and b["columns"] else "") + '</worksheet>'))
    fill_xml = "".join('<fill><patternFill patternType="none"/></fill>' if f == "none" else
                       '<fill><patternFill patternType="gray125"/></fill>' if f == "gray125" else
                       f'<fill><patternFill patternType="solid"><fgColor rgb="FF{f}"/><bgColor indexed="64"/></patternFill></fill>'
                       for f in fills)
    apply_fill, apply_font = ' applyFill="1"', ' applyFont="1"'
    xf = "".join(f'<xf numFmtId="0" fontId="{fo}" fillId="{fi}" borderId="0" xfId="0"'
                 f'{apply_fill if fi else ""}{apply_font if fo else ""}/>' for fi, fo in styles)
    styles_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                  '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                  '<fonts count="4"><font><sz val="11"/><name val="Calibri"/></font>'
                  '<font><b/><sz val="11"/><name val="Calibri"/></font>'
                  '<font><b/><sz val="14"/><name val="Calibri"/></font>'
                  '<font><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts>'
                  f'<fills count="{len(fills)}">{fill_xml}</fills>'
                  '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                  '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                  f'<cellXfs count="{len(styles)}">{xf}</cellXfs>'
                  '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
          + "".join(f'<sheet name="{xml(n)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, (n, _) in enumerate(sheets))
          + '</sheets></workbook>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                      f'relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>' for i in range(len(sheets)))
            + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
              'relationships/styles" Target="styles.xml"/></Relationships>')
    ctypes = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="xml" ContentType="application/xml"/>'
              '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.'
              'spreadsheetml.sheet.main+xml"/>'
              '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.'
              'spreadsheetml.styles+xml"/>'
              + "".join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/'
                        f'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(len(sheets)))
              + '</Types>')
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                 'officeDocument" Target="xl/workbook.xml"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ctypes)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/styles.xml", styles_xml)
        for i, (_, s) in enumerate(sheets):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", s)
    return buf.getvalue()


def fmt_plain(v):
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v)


# ------------------------------------------------------------------ HTML
CSS = """
.xp-head{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:flex-end;margin-bottom:6px}
.xp-crumb{font-size:13px;color:var(--muted)}.xp-crumb a{color:var(--muted)}
.xp-dash{border:1px solid var(--line);border-radius:12px;margin:12px 0;background:var(--card);overflow:hidden}
.xp-dash>summary{cursor:pointer;padding:12px 16px;font-weight:700;font-size:15px;display:flex;gap:10px;align-items:center;list-style:none}
.xp-dash>summary::-webkit-details-marker{display:none}.xp-dash>summary:before{content:"\\25B8";color:var(--muted)}
.xp-dash[open]>summary:before{content:"\\25BE"}.xp-dash>summary .muted{font-weight:400;font-size:13px;margin-left:auto}
.xp-page{padding:4px 16px 14px;border-top:1px solid var(--line)}.xp-page h3{font-size:13px;margin:10px 0 8px;display:flex;gap:10px;align-items:center}
.xp-page h3 a{font-weight:600;font-size:12px}
.xp-wins{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:8px}
.xp-win{display:flex;flex-direction:column;gap:2px;padding:9px 12px;border:1px solid var(--line);border-radius:9px;
 background:var(--bg);text-decoration:none;color:var(--fg)}
.xp-win:hover{border-color:var(--accent)}.xp-win b{font-weight:600;font-size:13.5px}.xp-win span{font-size:12px;color:var(--muted)}
.xp-win.none{opacity:.55}.xp-tag{display:inline-block;font-size:11px;padding:0 7px;border-radius:9px;border:1px solid var(--line);margin-left:4px}
.xp-tag.t{color:#0d9488;border-color:#0d948866}.xp-tag.c{color:#7c3aed;border-color:#7c3aed66}.xp-tag.m{color:#0891b2;border-color:#0891b266}
.xp-find{display:flex;gap:8px;margin:14px 0}.xp-find input{flex:1;max-width:420px}
.xp-block{margin:22px 0;break-inside:auto}.xp-block h2{margin:0 0 4px;font-size:17px}
.xp-block .muted{font-size:12.5px;margin-bottom:8px}
.xp-tbl td.c{font-weight:600}.xp-tbl th{position:sticky;top:0;background:var(--bg)}
.xp-tbl tr:nth-child(even) td:not(.c){background:color-mix(in srgb,var(--card) 70%,transparent)}
.xp-chart{border:1px solid var(--line);border-radius:10px;padding:8px;background:var(--bg)}
.xp-chart svg{width:100%;height:auto;display:block}.xp-chart text{fill:var(--muted);font-size:11px}
.xp-chart .ax{stroke:var(--line)}.xp-legend{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;margin-top:6px}
.xp-legend i{display:inline-block;width:12px;height:3px;margin-right:5px;vertical-align:middle}
.xp-note{border:1px dashed var(--line);border-radius:10px;padding:12px 14px;color:var(--muted)}
.xp-period{display:inline-flex;gap:6px;align-items:center}
.toolbar .btn,.toolbar button{font-size:13px}
details.xp-raw>summary{cursor:pointer;color:var(--accent);font-size:13px;margin:6px 0}
@media print{@page{size:A4 landscape;margin:12mm}.xp-block{break-inside:avoid-page}.xp-tbl th{position:static}
 details.xp-raw{display:none}.xp-tbl td,.xp-tbl th{padding:3px 6px;font-size:11px}
 .xp-tbl td.c{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
"""


def chart_svg(b, tz):
    W, H, L0, R0, T0, B0 = 1000, 300, 64, 12, 12, 30
    pts = [(c, v) for _, _, s, _ in b["series"] for c, v in s]
    if not pts:
        return "<div class='xp-note'>No data in this period.</div>"
    frm, till = b["frm"], b["till"]
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    if hi == lo:
        hi, lo = hi + 1, lo - 1
    pad = (hi - lo) * 0.06
    lo, hi = lo - pad, hi + pad
    X = lambda c: L0 + (c - frm) / max(till - frm, 1) * (W - L0 - R0)
    Y = lambda v: T0 + (hi - v) / (hi - lo) * (H - T0 - B0)
    it0 = b["series"][0][3]
    out = [f"<svg viewBox='0 0 {W} {H}' xmlns='http://www.w3.org/2000/svg' role='img'>"]
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = Y(v)
        out.append(f"<line class='ax' x1='{L0}' x2='{W - R0}' y1='{y:.1f}' y2='{y:.1f}'/>"
                   f"<text x='{L0 - 6}' y='{y + 4:.1f}' text-anchor='end'>{e(fmt_value(v, it0, 1, tz))}</text>")
    span = till - frm
    fmt = "%H:%M" if span <= 86400 else "%m-%d %H:%M" if span <= 3 * 86400 else "%m-%d"
    for k in range(7):
        c = frm + span * k / 6
        out.append(f"<text x='{X(c):.1f}' y='{H - 8}' text-anchor='middle'>{datetime.fromtimestamp(c, tz).strftime(fmt)}</text>")
    for name, color, s, _ in b["series"]:
        if not s:
            continue
        step = max(1, len(s) // 1200)
        d = " ".join(f"{'M' if i == 0 else 'L'}{X(c):.1f},{Y(v):.1f}" for i, (c, v) in enumerate(s[::step]))
        out.append(f"<path d='{d}' fill='none' stroke='#{e(color)}' stroke-width='1.6'><title>{e(name)}</title></path>")
    out.append("</svg>")
    legend = "".join(f"<span><i style='background:#{e(c)}'></i>{e(n)}</span>" for n, c, _, _ in b["series"])
    return f"<div class='xp-chart'>{''.join(out)}<div class='xp-legend'>{legend}</div></div>"


def html_table(b, limit=None):
    rows = b["rows"] if limit is None else b["rows"][:limit]
    head = "".join(f"<th>{e(c)}</th>" for c in b["columns"])
    body = []
    for r in rows:
        tds = []
        for c in r:
            if c["bg"]:
                tds.append(f"<td class='c' style='background:#{e(c['bg'])};color:#{fg_for(c['bg'])}'>{e(c['t'])}</td>")
            else:
                tds.append(f"<td>{e(c['t'])}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    if not rows:
        body.append(f"<tr><td colspan='{max(len(b['columns']), 1)}' class='muted'>No data found.</td></tr>")
    return f"<div class='wrap'><table class='xp-tbl'><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"


def render_blocks(blocks, ctx):
    out = []
    for b in blocks:
        if b["kind"] == "chart":
            out.append(f"<section class='xp-block'><h2>{e(b['title'])}</h2><div class='muted'>{e(b['note'])}</div>"
                       f"{chart_svg(b, ctx.tz)}</section>")
        elif b["kind"] == "map":
            link = f" <a href='{e(b['link'])}' target='_blank'>Open the map in Zabbix</a>" if b.get("link") else ""
            out.append(f"<section class='xp-block'><h2>{e(b['title'])}</h2><div class='muted'>Map "
                       f"<b>{e(b['map'])}</b>.{link}</div></section>")
        elif b["kind"] == "note":
            link = ""
            if b.get("link"):
                link = f" <a href='{e(b['link'])}'>Open it</a>" if b["link"] == "netmap" else \
                    f" <a href='{e(b['link'])}' target='_blank' rel='noopener'>Open it</a>"
            out.append(f"<section class='xp-block'><h2>{e(b['title'])}</h2><div class='xp-note'>{e(b['text'])}{link}"
                       "</div></section>")
        else:
            data = b["title"].endswith(": data")
            note = f"<div class='muted'>{len(b['rows'])} row{'s' if len(b['rows']) != 1 else ''}" + \
                   (f" · {e(b['note'])}" if b.get("note") else "") + "</div>"
            if data:   # the raw values behind a graph: in the files, a short preview on screen
                out.append(f"<section class='xp-block'><details class='xp-raw'><summary>Show the values "
                           f"({len(b['rows'])} rows, all of them are in the Excel and CSV files)</summary>"
                           f"{html_table(b, 300)}</details></section>")
            else:
                out.append(f"<section class='xp-block'><h2>{e(b['title'])}</h2>{note}{html_table(b)}</section>")
    return "".join(out)


def kind_tag(wtype):
    if wtype in NO_DATA:
        return "<span class='xp-tag'>no data</span>"
    k = KIND.get(wtype, "table")
    return {"chart": "<span class='xp-tag c'>chart + data</span>", "map": "<span class='xp-tag m'>map elements</span>"
            }.get(k, "<span class='xp-tag t'>table</span>")


def index_page(api, zurl):
    ds = dashboards(api)
    cards = []
    for d in ds:
        pages = []
        n = 0
        for i, p in enumerate(d["pages"]):
            wins = []
            for w in ordered(p["widgets"]):
                n += 1
                cls = " none" if w["type"] in NO_DATA else ""
                wins.append(f"<a class='xp-win{cls}' href='{URL}/view?d={e(d['dashboardid'])}&amp;w={e(str(w['widgetid']))}'>"
                            f"<b>{e(widget_title(w))}</b><span>{e(TYPE_NAMES.get(w['type'], w['type']))}"
                            f"{kind_tag(w['type'])}</span></a>")
            pname = page_name(d, i)
            pages.append(f"<div class='xp-page'><h3>{e(pname) if pname else 'Windows'}"
                         f"<a href='{URL}/view?d={e(d['dashboardid'])}&amp;p={i}'>Export the whole page</a></h3>"
                         f"<div class='xp-wins'>{''.join(wins) or '<span class=muted>No windows.</span>'}</div></div>")
        cards.append(f"<details class='xp-dash' data-name='{e(d['name'].lower())}'><summary>{e(d['name'])}"
                     f"<span class='muted'>{len(d['pages'])} page{'s' if len(d['pages']) != 1 else ''}, {n} window"
                     f"{'s' if n != 1 else ''}</span></summary>{''.join(pages)}</details>")
    js = ("<script>const f=document.getElementById('xpf');f.addEventListener('input',()=>{const v=f.value.toLowerCase();"
          "document.querySelectorAll('.xp-dash').forEach(d=>{const hit=!v||d.dataset.name.includes(v)||"
          "[...d.querySelectorAll('.xp-win b')].some(b=>b.textContent.toLowerCase().includes(v));"
          "d.style.display=hit?'':'none';if(v&&hit)d.open=true;});});</script>")
    return (f"<style>{CSS}</style><h1>Zabbix Live Export</h1>"
            "<div class='muted'>Pick a dashboard, then a window. The export reads the window's settings from Zabbix "
            "(hosts, items, columns, sorting and colours) and the live values, so it matches what the window shows "
            "now. Save it as Excel, CSV or PDF.</div>"
            "<div class='xp-find'><input id='xpf' placeholder='Find a dashboard or window, e.g. Endpoints or UPS' "
            "autocomplete='off'></div>" + ("".join(cards) or "<p class='muted'>No dashboards found.</p>") + js)


def view_page(ctx, q, d, title, widgets, page_i, blocks):
    qs = {k: v for k, v in q.items() if k in ("d", "w", "p", "period")}
    link = lambda **kw: e(URL + "/view?" + "&".join(f"{k}={v}" for k, v in {**qs, **kw}.items()))
    has_time = any(w["type"] in ("svggraph", "graph") or (w["type"] == "problems" and tree(w.get("fields")).get("show") == "2")
                   for w in widgets)
    period = ""
    if has_time:
        opts = "".join(f"<option value='{k}'{' selected' if ctx.period == k else ''}>{e(n)}</option>" for k, n, _ in PERIODS)
        hidden = "".join(f"<input type='hidden' name='{k}' value='{e(v)}'>" for k, v in qs.items() if k != "period")
        period = (f"<form class='xp-period' action='{URL}/view' method='get'>{hidden}<select name='period' "
                  f"onchange='this.form.submit()'><option value=''>Window's own period</option>{opts}</select></form>")
    zlink = (f"<a class='btn alt' href='{e(ctx.zurl)}/zabbix.php?action=dashboard.view&amp;dashboardid="
             f"{e(d['dashboardid'])}' target='_blank'>Open in Zabbix</a>") if ctx.zurl else ""
    where = e(d["name"]) + ("" if q.get("w") else f" › {e(title)}" if title != d["name"] else "")
    crumb = (f"<div class='xp-crumb'><a href='{URL}'>Live export</a> › {where}"
             + (f" › {e(page_name(d, page_i))}" if q.get("w") and page_name(d, page_i) else "") + "</div>")
    stamp = datetime.fromtimestamp(ctx.now, ctx.tz).strftime("%Y-%m-%d %H:%M:%S")
    tables = sum(1 for b in blocks if b["kind"] == "table")
    files = (f"<a class='btn' href='{link(format='xlsx')}'>Excel (.xlsx)</a>"
             f"<a class='btn alt' href='{link(format='csv')}'>CSV</a>") if tables else ""
    return (f"<style>{CSS}</style>{crumb}<div class='xp-head'><div><h1>{e(title)}</h1>"
            f"<div class='muted'>Live from Zabbix at {stamp} ({e(ctx.tzname)}) · dashboard {e(d['name'])}"
            f"{' · ' + str(len(widgets)) + ' windows' if len(widgets) > 1 else ''}</div></div></div>"
            f"<div class='toolbar' style='margin-top:12px'>{files}"
            "<a class='btn alt' href='#' onclick='window.print();return false'>Print / save as PDF</a>"
            f"<a class='btn alt' href='{link()}'>Refresh</a>{zlink}{period}</div>"
            + render_blocks(blocks, ctx))


def web(api, parts, q, zabbix_url="", tzname="UTC"):
    """dict(code, title, body, ctype, filename, page) for /export...; page=True: body is HTML to wrap in the site page."""
    try:
        if not parts:
            return {"code": 200, "title": "Zabbix Live Export", "body": index_page(api, zabbix_url), "page": True}
        if parts == ["view"]:
            period = q.get("period") if q.get("period") in dict((k, 1) for k, _, _ in PERIODS) else None
            ctx = Ctx(api, tzname, zabbix_url, period)
            d, title, widgets, page_i = select(api, q)
            blocks = run(ctx, widgets)
            stamp = datetime.fromtimestamp(ctx.now, ctx.tz)
            name = safe_name(f"{d['name']}_{title}") + f"_{stamp:%Y%m%d_%H%M}"
            head = f"{d['name']} · {title} · live from Zabbix {stamp:%Y-%m-%d %H:%M:%S} ({tzname})"
            if q.get("format") == "csv":
                return {"code": 200, "body": to_csv(blocks, head), "ctype": "text/csv; charset=utf-8",
                        "filename": name + ".csv", "page": False}
            if q.get("format") == "xlsx":
                return {"code": 200, "body": to_xlsx(blocks, head), "filename": name + ".xlsx", "page": False,
                        "ctype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
            return {"code": 200, "title": title, "body": view_page(ctx, q, d, title, widgets, page_i, blocks), "page": True}
    except ExportError as ex:
        return {"code": 404, "title": "Live export", "page": True,
                "body": f"<h1>Live export</h1><div class='verdict err'>{e(str(ex))}</div>"
                        f"<p><a class='btn alt' href='{URL}'>Back to the dashboards</a></p>"}
    return {"code": 404, "title": "Not found", "body": "<h1>Not found</h1>", "page": True}


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from zbxapi import ZabbixAPI
    api = ZabbixAPI()
    if len(sys.argv) >= 2 and sys.argv[1] == "list":
        for d in dashboards(api):
            print(f"{d['dashboardid']:>6}  {d['name']}")
            for i, p in enumerate(d["pages"]):
                for w in ordered(p["widgets"]):
                    print(f"        page {i}  window {w['widgetid']:>7}  {TYPE_NAMES.get(w['type'], w['type']):<20} {widget_title(w)}")
        return
    if len(sys.argv) == 4 and sys.argv[1] == "csv":
        r = web(api, ["view"], {"d": sys.argv[2], "w": sys.argv[3], "format": "csv"},
                tzname=os.environ.get("RCA_TZ", "UTC"))
        sys.stdout.write(r["body"].decode("utf-8") if isinstance(r["body"], bytes) else r["body"])
        return
    print(__doc__)


if __name__ == "__main__":
    main()
