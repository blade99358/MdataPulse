#!/usr/bin/env python3
"""MDataPulse Reports (MDP-BRAND): web server behind the "RCA report" buttons in the Zabbix web UI.

  python3 tools/rca_server.py                  # listens on 0.0.0.0:8081

  /                         home page: health reports, device logs, RCA report
  /rca                      form: pick a site and the incident time, view or download the report
  /rca?site=SITE0008&from=2026-11-02T14:00&to=2026-11-02T15:00[&download=1]
  /rca?eventid=12345        report for the site of that problem, around the problem time
  /rca?host=SITE0008-SW-1    form pre-filled with that host's site
  /reports                  the daily / weekly / monthly health reports and the admin report (zbx_report.py)
  /reports/now/<kind>       that report for the period running now (today, this week...), not saved
  /reports/build            build any finished report the morning run has not built yet

Uses ZABBIX_URL / ZABBIX_TOKEN like the other tools (read-only calls only).
By default only people logged in to Zabbix in the same browser get a report: the
zbx_session cookie is checked with the Zabbix API. Set RCA_AUTH=none to turn that off
(needed only if the Zabbix web UI is on a different host name than this server).
Other settings: RCA_PORT (8081), RCA_BIND (0.0.0.0), RCA_TZ (UTC).
"""
import base64
import html
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mdp_brand  # noqa: E402
import zbx_logs  # noqa: E402
import zbx_rca  # noqa: E402
import zbx_report  # noqa: E402
import zbx_signal  # noqa: E402  MDP-SIGNAL
import zbx_endpoint_signal  # noqa: E402  MDP-ENDPOINT_SIGNAL
import zbx_netmap  # noqa: E402  MDP-NETMAP
import zbx_export  # noqa: E402  MDP-EXPORT
from zbxapi import ZabbixAPI, ZabbixError  # noqa: E402

TZ_NAME = os.environ.get("RCA_TZ", "UTC")
AUTH = os.environ.get("RCA_AUTH", "session")
EVENT_BEFORE = 15 * 60       # report window starts this long before the problem
EVENT_AFTER = 15 * 60        # and ends this long after it resolved (or now)
MAX_WINDOW = 3 * 86400

api = ZabbixAPI()
lock = threading.Lock()      # zbx_rca keeps its time zone in a module global
sessions = {}                # sessionid -> (username, checked at)

PAGE_CSS = zbx_rca.CSS + """
form.rca{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;background:var(--card);
 border:1px solid var(--line);border-radius:8px;padding:16px;margin:16px 0}
form.rca label{display:flex;flex-direction:column;gap:4px;font-weight:600;font-size:13px}
input,select,button,.btn{font:inherit;padding:7px 10px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
button,.btn{background:var(--accent);color:#fff;border-color:var(--accent);cursor:pointer;text-decoration:none;display:inline-block}
.btn.alt{background:transparent;color:var(--accent)}.actions{display:flex;gap:8px;align-items:end;flex-wrap:wrap}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 16px}.err{border-left-color:var(--bad)}
@media print{.toolbar{display:none}body{padding:0}}
"""


BUILD_LOCK = threading.Lock()


def tz():
    return ZoneInfo(TZ_NAME)


def local(epoch):
    return datetime.fromtimestamp(epoch, tz()).strftime("%Y-%m-%dT%H:%M")


def page(title, body, active="", base="./"):
    return mdp_brand.decorate(f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{PAGE_CSS}</style></head><body><main>{body}</main></body></html>", active, base)


def sites():
    groups = api.call("hostgroup.get", {"output": ["name"], "search": {"name": "Sites/"}, "startSearch": True})
    return sorted(g["name"].split("/", 1)[1] for g in groups)


def host_site(hostname=None, hostid=None):
    flt = {"filter": {"host": [hostname]}} if hostname else {"hostids": [hostid]}
    h = api.call("host.get", {"output": ["host"], "selectTags": "extend", **flt})
    if not h:
        return None, None
    tags = {t["tag"]: t["value"] for t in h[0]["tags"]}
    return h[0]["host"], tags.get("site")


def form(site="", t_from=None, t_to=None, note=""):
    now = int(time.time())
    t_to = t_to or now
    t_from = t_from or t_to - 2 * 3600
    opts = "".join(f"<option{' selected' if s == site else ''}>{html.escape(s)}</option>" for s in sites())
    note = f"<div class='verdict'>{note}</div>" if note else ""
    return page("RCA report", f"""
<h1>RCA report</h1>
<div class='muted'>Pick the site and the time the incident happened ({TZ_NAME}). The report reads ping, uptime,
radio, PoE and UPS history from Zabbix and says whether it was power, connectivity, upstream or radio.
<a href='reports'>Network health reports</a> · <a href='logs'>Device log</a></div>
{note}
<form class='rca' action='rca' method='get'>
 <label>Site<select name='site' required>{opts}</select></label>
 <label>From<input type='datetime-local' name='from' value='{local(t_from)}' required></label>
 <label>To<input type='datetime-local' name='to' value='{local(t_to)}' required></label>
 <div class='actions'><button type='submit'>View report</button>
  <button type='submit' name='download' value='1' class='btn alt'>Download</button></div>
</form>""", "rca")


def toolbar(query):
    q = dict(query)
    q["download"] = "1"
    return (f"<style>{PAGE_CSS}</style><div class='toolbar'>"
            f"<a class='btn' href='rca?{html.escape(urllib.parse.urlencode(q))}'>Download HTML</a>"
            "<a class='btn alt' href='#' onclick='window.print();return false'>Print / save as PDF</a>"
            "<a class='btn alt' href='rca'>New report</a></div>")


def session_user(cookie_header):
    """Return the Zabbix user name for the browser's zbx_session cookie, or None."""
    if AUTH == "none":
        return "-"
    c = SimpleCookie(cookie_header or "")
    if "zbx_session" not in c:
        return None
    try:
        raw = urllib.parse.unquote(c["zbx_session"].value)
        sid = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))["sessionid"]
    except (ValueError, KeyError, TypeError):
        return None
    hit = sessions.get(sid)
    if hit and time.time() - hit[1] < 60:
        return hit[0]
    try:
        u = api.call("user.checkAuthentication", {"sessionid": sid})
    except ZabbixError:
        sessions.pop(sid, None)
        return None
    sessions[sid] = (u.get("username", "?"), time.time())
    return sessions[sid][0]


class Handler(BaseHTTPRequestHandler):
    server_version = "MDataPulse/1.0"

    def send(self, code, body, filename=None, ctype="text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        path = url.path.rstrip("/").rsplit("/", 1)[-1]
        try:
            if path == "health":
                return self.send(200, "ok", ctype="text/plain")
            user = session_user(self.headers.get("Cookie"))
            if not user:
                return self.send(401, page("Log in to Zabbix", "<h1>Log in to Zabbix first</h1><div class='verdict err'>"
                                 "Open the Zabbix web UI in this browser, log in, then click the RCA button again.</div>"))
            if path in ("", "index.html"):
                return self.send(200, page(mdp_brand.NAME, mdp_brand.home(zbx_logs.web_base(self.headers.get("Host"))),
                                           "home"))
            if path == "logs":
                return self.logs(q)
            if url.path.rstrip("/").endswith("/reports") or "/reports/" in url.path:
                return self.reports(url.path)
            if url.path.rstrip("/").endswith("/export") or "/export/" in url.path:  # MDP-EXPORT
                return self.export(url.path, q)
            if url.path.rstrip("/").endswith("/netmap") or "/netmap/" in url.path:  # MDP-NETMAP
                return self.netmap(url.path, q)
            if url.path.rstrip("/").endswith("/endpoint-signal") or "/endpoint-signal/" in url.path:  # MDP-ENDPOINT_SIGNAL
                return self.endpoint_signal(url.path, q)
            if url.path.rstrip("/").endswith("/signal") or "/signal/" in url.path:  # MDP-SIGNAL
                return self.signal(url.path, q)
            if path != "rca":
                return self.send(404, page("Not found", "<h1>Not found</h1>"))
            self.report(q, user)
        except (ZabbixError, zbx_rca.RCAError, ValueError) as e:
            self.send(400, page("RCA error", f"<h1>Could not build the report</h1><div class='verdict err'>{html.escape(str(e))}</div>"
                                               "<p><a class='btn alt' href='rca'>Back</a></p>"))
        except Exception:
            traceback.print_exc()
            self.send(500, page("RCA error", "<h1>Internal error</h1><div class='verdict err'>See the rca_server log.</div>"))

    def logs(self, q):
        """/logs: search a device by name or IP; /logs?host=<name>[&days=30][&format=csv]: its log."""
        days = int(q["days"]) if q.get("days", "").isdigit() else 30
        if q.get("host"):
            if q.get("format") == "csv":
                text, name = zbx_logs.device_csv(api, q["host"], days, TZ_NAME)
                return self.send(200, text, filename=name, ctype="text/csv; charset=utf-8")
            body = zbx_logs.device_page(api, q["host"], days, TZ_NAME, zbx_logs.web_base(self.headers.get("Host")))
            return self.send(200, mdp_brand.decorate(zbx_logs.wrap("Device log " + q["host"], body), "logs"))
        body, one = zbx_logs.search_page(api, q.get("q", ""), days)
        if one:
            self.send_response(302)
            self.send_header("Location", "logs?" + urllib.parse.urlencode({"host": one, "days": days}))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        return self.send(200, mdp_brand.decorate(zbx_logs.wrap("Device log", body), "logs"))

    def reports(self, path):
        """/reports: list of the health reports; /reports/<kind>/<id>.html: one report;
        /reports/now/<kind>: built on the spot for the running period; /reports/build: build the missing ones."""
        parts = [p for p in path.split("/") if p]
        parts = parts[parts.index("reports") + 1:]
        back = "<p><a class='btn alt' href='" + "../" * len(parts) + "reports'>Back to the reports</a></p>"
        up = "../" * len(parts) or "./"
        if len(parts) == 2 and parts[0] == "now" and parts[1] in zbx_report.KINDS:
            try:
                with BUILD_LOCK:  # one build at a time: each one reads a lot of history
                    _, page_html, _ = zbx_report.build(parts[1], zbx_report.today(), store=False)
            except SystemExit as ex:  # build() stops with a message, e.g. a period that has not started
                return self.send(400, page("Report", f"<h1>No report</h1><div class='verdict err'>{html.escape(str(ex))}</div>" + back,
                                           "reports", up))
            return self.send(200, mdp_brand.decorate(page_html, "reports", up, retitle=False))
        if parts == ["build"]:
            done = []
            with BUILD_LOCK:
                for kind in zbx_report.KINDS:
                    p = zbx_report.due(kind)
                    if os.path.exists(zbx_report.snap_path(kind, p.id, "html")):
                        done.append(f"<li>{kind} {html.escape(p.id)}: already built</li>")
                        continue
                    try:
                        zbx_report.build(kind)
                        done.append(f"<li><b>{kind} {html.escape(p.id)}: built</b></li>")
                    except Exception as ex:  # noqa: BLE001 - show it, keep building the others
                        traceback.print_exc()
                        done.append(f"<li class='err'>{kind} {html.escape(p.id)}: failed ({html.escape(str(ex))})</li>")
            return self.send(200, page("Reports built", "<h1>Reports built</h1><ul>" + "".join(done) + "</ul>" + back, "reports", up))
        if len(parts) == 2 and parts[0] in zbx_report.KINDS and re.fullmatch(r"[0-9-]+\.html", parts[1]):
            f = os.path.join(zbx_report.REPORT_DIR, parts[0], parts[1])
            if not os.path.isfile(f):
                return self.send(404, page("Not found", "<h1>Report not found</h1>", "reports", up))
            return self.send(200, mdp_brand.decorate(open(f, encoding="utf-8").read(), "reports", up, retitle=False))
        names = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly", "admin": "Zabbix admin"}
        cols = []
        for kind, ids in zbx_report.list_reports().items():
            links = "".join(f"<li><a href='reports/{kind}/{i}.html'>{html.escape(i)}</a></li>" for i in ids[:60])
            cols.append(f"<div class='finding'><b>{names[kind]}</b><ul>{links or '<li class=muted>none yet</li>'}</ul></div>")
        base = "<base href='" + html.escape(path[:path.index("/reports")] + "/") + "'>"
        self.send(200, page("Network health reports", base + "<h1>Network health reports</h1><div class='muted'>"
                            "Built every morning by tools/zbx_report.py. Newest first. "
                            "<a href='rca'>RCA report</a></div><p>"
                            "<a class='btn' href='reports/now/daily'>Today so far</a> "
                            "<a class='btn' href='reports/now/weekly'>This week so far</a> "
                            "<a class='btn' href='reports/now/monthly'>This month so far</a> "
                            "<a class='btn' href='reports/now/admin'>Zabbix admin now</a> "
                            "<a class='btn alt' href='reports/build' title='Build finished reports the morning run missed'>"
                            "Build now</a> <span class='muted'>A report can take a minute.</span></p>"
                            "<div style='display:grid;grid-template-columns:"
                            "repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-top:16px'>" + "".join(cols) + "</div>", "reports"))

    def export(self, path, q):
        """MDP-EXPORT: /export pick a dashboard window; /export/view?d=&w= (or &p=) [&format=xlsx|csv]."""
        parts = [p for p in path.split("/") if p]
        parts = parts[parts.index("export") + 1:]
        r = zbx_export.web(api, parts, q, zbx_logs.web_base(self.headers.get("Host")), TZ_NAME)
        if r["page"]:
            base = "<base href='" + html.escape(path[:path.index("/export")] + "/") + "'>"
            return self.send(r["code"], page(r["title"], base + r["body"], "export"))  # base = the server root
        data = r["body"] if isinstance(r["body"], bytes) else r["body"].encode()
        self.send_response(r["code"])
        self.send_header("Content-Type", r["ctype"])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition", f'attachment; filename="{r["filename"]}"')
        self.end_headers()
        self.wfile.write(data)

    def netmap(self, path, q):
        """MDP-NETMAP: /netmap live geo map, /netmap/data (JSON), /netmap/leaflet.js."""
        parts = [p for p in path.split("/") if p]
        parts = parts[parts.index("netmap") + 1:]
        up = "../" * len(parts) or "./"
        code, ctype, body = zbx_netmap.web(api, parts, q, zbx_logs.web_base(self.headers.get("Host")),
                                           bar=mdp_brand.bar("netmap", up), head=mdp_brand.head_bits())
        return self.send(code, body, ctype=ctype)

    def endpoint_signal(self, path, q):
        """MDP-ENDPOINT_SIGNAL: /endpoint-signal list, /endpoint-signal/<kind>/<day>.html, /endpoint-signal/now/<kind>[?site=|?host=]."""
        parts = [p for p in path.split("/") if p]
        parts = parts[parts.index("endpoint-signal") + 1:]
        up = "../" * len(parts) or "./"
        with BUILD_LOCK:
            code, title, body, is_report = zbx_endpoint_signal.web(api, parts, q, path[:path.index("/endpoint-signal")] + "/")
        if is_report:
            return self.send(code, mdp_brand.decorate(body, "endpoint_signal", up, retitle=False))
        return self.send(code, page(title, body, "endpoint_signal", up))

    def signal(self, path, q):
        """MDP-SIGNAL: /signal list, /signal/<kind>/<day>.html, /signal/now/<kind>, /signal/view, /signal/build."""
        parts = [p for p in path.split("/") if p]
        parts = parts[parts.index("signal") + 1:]
        up = "../" * len(parts) or "./"
        with BUILD_LOCK:
            code, title, body, is_report = zbx_signal.web(api, parts, q, path[:path.index("/signal")] + "/")
        if is_report:
            return self.send(code, mdp_brand.decorate(body, "signal", up, retitle=False))
        return self.send(code, page(title, body, "signal", up))

    def report(self, q, user):
        if not (q.get("site") or q.get("eventid") or q.get("host")):
            return self.send(200, form())
        if q.get("host") and not q.get("from"):
            _, site = host_site(hostname=q["host"])
            return self.send(200, form(site or "", note=f"Pre-filled from host <b>{html.escape(q['host'])}</b>. "
                                                            "Set the incident time and click View report."))
        host = None
        if q.get("eventid"):
            ev = api.call("event.get", {"eventids": [q["eventid"]], "output": ["clock", "r_eventid", "name"],
                                        "selectHosts": ["hostid", "host"]})
            if not ev or not ev[0]["hosts"]:
                raise ValueError(f"Problem event {q['eventid']} not found.")
            ev = ev[0]
            start = int(ev["clock"])
            end = int(time.time())
            if ev["r_eventid"] != "0":
                rec = api.call("event.get", {"eventids": [ev["r_eventid"]], "output": ["clock"]})
                if rec:
                    end = int(rec[0]["clock"])
            hostname, site = host_site(hostid=ev["hosts"][0]["hostid"])
            if not site:
                host = hostname
            w_from, w_till = start - EVENT_BEFORE, min(end + EVENT_AFTER, int(time.time()))
            if w_till - w_from > MAX_WINDOW:
                w_till = w_from + MAX_WINDOW
            q = {"site": site, "from": local(w_from), "to": local(w_till)} if site else \
                {"host": host, "from": local(w_from), "to": local(w_till)}
        else:
            site = q.get("site")
            if not site:
                raise ValueError("Pick a site.")
            w_from = zbx_rca.parse_local(q.get("from", ""), TZ_NAME)
            w_till = zbx_rca.parse_local(q.get("to", ""), TZ_NAME)
            if w_till - w_from > MAX_WINDOW:
                raise ValueError("Pick a window of 3 days or less.")
        host = q.get("host") if not site else None
        with lock:
            label, html_doc, findings = zbx_rca.generate(api, site=site, host=host, w_from=w_from, w_till=w_till,
                                                          tz=TZ_NAME)
        self.log_message("report %s %s..%s for %s: %s", label, q.get("from"), q.get("to"), user,
                         findings[0]["cause"] if findings else "nothing found")
        name = f"rca_{label}_{datetime.fromtimestamp(w_from, tz()):%Y%m%d_%H%M}.html"
        if q.get("download") == "1":
            return self.send(200, html_doc, filename=name)
        self.send(200, mdp_brand.decorate(html_doc.replace("<main>", "<main>" + toolbar(q), 1), "rca", retitle=False))


def main():
    port = int(os.environ.get("RCA_PORT", "8081"))
    bind = os.environ.get("RCA_BIND", "0.0.0.0")
    print(f"Zabbix {api.version()} at {api.url}; {mdp_brand.NAME} on http://{bind}:{port}/ (auth: {AUTH}, tz: {TZ_NAME})")
    ThreadingHTTPServer((bind, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
