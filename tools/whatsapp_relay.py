#!/usr/bin/env python3
"""WhatsApp relay for Zabbix alarms (standard library only).

Zabbix (webhook media type "WhatsApp NOC (WAHA)") posts each problem / resolved event to this relay on
127.0.0.1. The relay queues them, groups bursts into one message, keeps a calm pace (random gap between
messages, hourly cap, "seen" + typing before each send) and sends them to ONE WhatsApp group through WAHA.
The queue is kept on disk, so nothing is lost when WAHA or the internet is down: it is sent when it comes back.

  python3 tools/whatsapp_relay.py install           write /etc/whatsapp-relay.conf (once) + systemd service, start it
  python3 tools/whatsapp_relay.py set KEY VALUE     change one setting (then the service restarts by itself)
  python3 tools/whatsapp_relay.py session           is the WhatsApp number linked? (WAHA session status)
  python3 tools/whatsapp_relay.py groups            list the WhatsApp groups of the linked number with their chat id
  python3 tools/whatsapp_relay.py test [text]       send one test message to the group now
  python3 tools/whatsapp_relay.py demo              queue a sample burst (2 problems + 1 resolved) through the relay
  python3 tools/whatsapp_relay.py status            queue length, messages sent this hour, last error
  python3 tools/whatsapp_relay.py serve             run the relay (what the systemd service runs)
"""
import datetime as dt
import json
import os
import random
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONF = os.environ.get("WHATSAPP_RELAY_CONF", "/etc/whatsapp-relay.conf")
DEFAULTS = {
    "WAHA_URL": "http://127.0.0.1:3000",       # where WAHA runs (Docker, same machine)
    "WAHA_API_KEY": "",                         # same value as WAHA_API_KEY in WAHA's waha.env
    "WAHA_SESSION": "default",                  # WAHA Core has one session: default
    "WA_CHAT_ID": "",                           # the NOC group, ends with @g.us (see: whatsapp_relay.py groups)
    "RELAY_LISTEN": "127.0.0.1:8095",
    "RELAY_TOKEN": "",                          # shared with the Zabbix media type (filled by install)
    "TZ_NAME": "UTC",
    "GROUP_WINDOW": "15",     # seconds an alarm waits so a problem that clears at once is ONE "down and back"
    "MIN_GAP": "10",          # seconds between two WhatsApp messages (a random 0-5 s is added)
    "MAX_PER_HOUR": "120",    # safety cap only; every alarm is its own message, never a digest
    "MAX_LINES": "25",        # (old digest setting, not used any more)
    "LATE_AFTER": "300",      # an alarm sent this many seconds after it happened is marked "sent late"
    "TYPING": "1",            # 1 = mark the chat seen and show "typing..." before sending
    "PAUSED": "0",            # 1 = queue only, send nothing (e.g. while moving to another SIM)
    "ZABBIX_ENV": "/root/.zabbix_env",          # to look up main-link devices and IPs (optional)
    "SPOOL": "/var/lib/whatsapp-relay",
}
UNIT = "whatsapp-relay"
SEV_ICON = {"5": "\U0001F7E5", "4": "\U0001F7E7", "3": "\U0001F7E8", "2": "\U0001F7E8", "1": "\U0001F7E6", "0": "⬜"}
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from site_conf import CODE_RE  # noqa: E402


# ---------------------------------------------------------------- settings
def read_kv(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:]
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def load_conf():
    c = dict(DEFAULTS)
    c.update(read_kv(CONF))
    return c


def write_conf(c):
    lines = ["# WhatsApp relay for Zabbix alarms. Change with: python3 tools/whatsapp_relay.py set KEY VALUE"]
    for k in DEFAULTS:
        lines.append(f"{k}={c.get(k, DEFAULTS[k])}")
    for k in c:
        if k not in DEFAULTS:
            lines.append(f"{k}={c[k]}")
    tmp = CONF + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONF)


def num(c, k):
    try:
        return float(c.get(k) or DEFAULTS[k])
    except ValueError:
        return float(DEFAULTS[k])


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- WAHA
class WahaError(RuntimeError):
    pass


def waha(c, method, path, body=None, timeout=30):
    url = c["WAHA_URL"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "X-Api-Key": c.get("WAHA_API_KEY", "")})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise WahaError(f"WAHA {method} {path}: HTTP {e.code} {e.read()[:300].decode(errors='replace')}") from e
    except (urllib.error.URLError, OSError) as e:
        raise WahaError(f"WAHA not reachable at {c['WAHA_URL']}: {e}") from e
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {"raw": raw[:300].decode(errors="replace")}


def session_status(c):
    s = waha(c, "GET", f"/api/sessions/{c['WAHA_SESSION']}", timeout=10)
    return s.get("status", "?"), s


def send_whatsapp(c, text):
    """Seen + typing for a random, length-based time, then the message (WAHA's own advice against bans)."""
    sess, chat = c["WAHA_SESSION"], c["WA_CHAT_ID"]
    if not chat.endswith("@g.us"):
        raise WahaError("WA_CHAT_ID is not set to a group id (…@g.us); run: python3 tools/whatsapp_relay.py groups")
    if c.get("TYPING", "1") == "1":
        for path in ("/api/sendSeen", "/api/startTyping"):
            try:
                waha(c, "POST", path, {"session": sess, "chatId": chat}, timeout=15)
            except WahaError as e:
                log("note:", e)
        time.sleep(min(8.0, 1.5 + len(text) / 120.0) * random.uniform(0.8, 1.3))
        try:
            waha(c, "POST", "/api/stopTyping", {"session": sess, "chatId": chat}, timeout=15)
        except WahaError as e:
            log("note:", e)
    return waha(c, "POST", "/api/sendText", {"session": sess, "chatId": chat, "text": text,
                                             "linkPreview": False})


def list_groups(c):
    sess = c["WAHA_SESSION"]
    try:
        res = waha(c, "GET", f"/api/{sess}/groups", timeout=60)
    except WahaError:
        res = [x for x in waha(c, "GET", f"/api/{sess}/chats?limit=500", timeout=60)
               if "@g.us" in json.dumps(x.get("id"))]
    items = res.values() if isinstance(res, dict) else res
    out = []
    for g in items:
        gid = g.get("id") or g.get("JID") or g.get("jid")
        if isinstance(gid, dict):
            gid = gid.get("_serialized") or f"{gid.get('user')}@{gid.get('server')}"
        name = (g.get("subject") or g.get("name") or g.get("Name")
                or (g.get("groupMetadata") or {}).get("subject") or "")
        if gid and str(gid).endswith("@g.us"):
            out.append((name, str(gid)))
    return sorted(out, key=lambda x: x[0].lower())


# ---------------------------------------------------------------- Zabbix lookups (optional)
_cache = {}


def zabbix_call(c, method, params):
    env = read_kv(c.get("ZABBIX_ENV") or "")
    url, token = env.get("ZABBIX_URL"), env.get("ZABBIX_TOKEN")
    if not url or not token:
        return None
    url = url.rstrip("/")
    if not url.endswith("api_jsonrpc.php"):
        url += "/api_jsonrpc.php"
    import ssl
    ctx = ssl._create_unverified_context() if env.get("ZABBIX_INSECURE") == "1" else None
    req = urllib.request.Request(url, json.dumps({"jsonrpc": "2.0", "method": method, "params": params,
                                                  "id": 1}).encode(),
                                 {"Content-Type": "application/json-rpc", "Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, context=ctx, timeout=8) as r:
        return json.loads(r.read()).get("result")


def main_link_devices(c, code):
    """'Site SITExxxx' hosts have no IP: show the devices tagged as the site's main link instead."""
    hit = _cache.get(code)
    if hit and hit[0] > time.time() - 600:
        return hit[1]
    val = ""
    try:
        hosts = zabbix_call(c, "host.get", {"output": ["name"], "selectInterfaces": ["ip", "main"],
                                            "tags": [{"tag": "mainlink", "value": code, "operator": 1}]}) or []
        parts = []
        for h in hosts[:4]:
            ip = next((i["ip"] for i in h.get("interfaces", []) if i.get("main") == "1"), "")
            parts.append(f"{h['name']} ({ip})" if ip else h["name"])
        val = ", ".join(parts)
    except Exception as e:  # lookups are a nice-to-have, never block a message
        log("note: Zabbix lookup failed:", e)
    _cache[code] = (time.time(), val)
    return val


# ---------------------------------------------------------------- event handling
def clean(v):
    v = (v or "").strip()
    return "" if (not v or v.startswith("{") or v.startswith("*UNKNOWN")) else v


def to_epoch(date, tm):
    """WA-FIX-5: {EVENT.DATE} {EVENT.TIME} follow the RECIPIENT user's time zone in Zabbix; zbx_whatsapp.py sets
    the whatsapp-noc user to UTC, so read them as UTC. (Only a fallback: the event clock from the API wins.)"""
    try:
        return dt.datetime.strptime(f"{date} {tm}", "%Y.%m.%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return None


def api_times(c, e):
    """WA-FIX-5: exact start/resolve time of the event from Zabbix itself (epoch, no time-zone guessing)."""
    if e.get("api_ok") or not str(e.get("id", "")).isdigit():
        return
    try:
        ev = zabbix_call(c, "event.get", {"eventids": [e["id"]], "output": ["clock", "r_eventid"]})
        if not ev:
            return
        e["start"] = int(ev[0]["clock"])
        if e["kind"] in ("resolved", "blip"):
            if ev[0]["r_eventid"] == "0":
                return   # recovery not stored yet: try again on the next attempt
            r = zabbix_call(c, "event.get", {"eventids": [ev[0]["r_eventid"]], "output": ["clock"]})
            if not r:
                return
            e["end"] = int(r[0]["clock"])
        e["api_ok"] = True
    except Exception as x:
        log("note: event time lookup failed:", x)


def fmt_time(c, epoch, with_date=True):
    if not epoch:
        return "?"
    try:
        from zoneinfo import ZoneInfo
        d = dt.datetime.fromtimestamp(epoch, ZoneInfo(c.get("TZ_NAME") or "UTC"))
    except Exception:
        d = dt.datetime.fromtimestamp(epoch)
    return d.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M")


def fmt_dur(sec):
    sec = int(max(0, sec or 0))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return f"{s}s"


def normalize(p):
    """Webhook payload -> event dict kept in the queue."""
    value = str(p.get("event_value", "1")).strip()
    kind = "resolved" if value == "0" else "problem"
    if str(p.get("event_update", "0")).strip() == "1":
        kind = "update"
    groups = p.get("groups", "") or ""
    host = clean(p.get("host")) or "?"
    code = next(iter(CODE_RE.findall(groups)), None) or next(iter(CODE_RE.findall(host)), "")
    start = to_epoch(p.get("date", ""), p.get("time", ""))
    end = to_epoch(p.get("rdate", ""), p.get("rtime", "")) if kind == "resolved" else None
    return {"id": str(p.get("event_id", "")), "kind": kind, "sev": clean(p.get("severity")) or "?",
            "nsev": str(p.get("nseverity", "")), "name": clean(p.get("name")) or "?", "host": host,
            "ip": clean(p.get("host_ip")), "site": code, "mainlink": clean(p.get("mainlink")),
            "opdata": clean(p.get("opdata")), "start": start, "end": end, "received": time.time()}


def merge(events):
    """Problem + its resolve in the same batch -> one 'down and back' entry; drop repeats."""
    by = {}
    order = []
    for e in events:
        if e["kind"] == "update":
            continue
        k = e["id"] or str(id(e))
        if k not in by:
            by[k] = dict(e)
            order.append(k)
            continue
        cur = by[k]
        if cur["kind"] != e["kind"]:
            cur.update({"kind": "blip", "end": e.get("end") or cur.get("end"), "start": cur.get("start") or e.get("start")})
    return [by[k] for k in order]


def device_line(c, e):
    if e["host"].startswith("Site ") and e["site"]:
        ml = main_link_devices(c, e["site"]) or e["mainlink"]
        return f"Main link: {ml}" if ml else ""
    return f"Device: {e['host']}" + (f" ({e['ip']})" if e["ip"] else "")


def render(c, batch):
    if len(batch) == 1:
        e = batch[0]
        icon = SEV_ICON.get(e["nsev"], "\U0001F534")
        head = {"problem": f"{icon} *PROBLEM* | {e['sev']}",
                "resolved": f"✅ *RESOLVED* | {e['sev']}",
                "blip": f"⚠️ *DOWN AND BACK* | {e['sev']}"}[e["kind"]]
        lines = [head, f"*{e['name']}*"]
        if e["site"]:
            lines.append(f"Site: {e['site']}")
        dl = device_line(c, e)
        if dl:
            lines.append(dl)
        lines.append(f"Started: {fmt_time(c, e['start'])}")
        if e["kind"] in ("resolved", "blip"):
            dur = (e["end"] - e["start"]) if (e.get("end") and e.get("start")) else None
            lines.append(f"Resolved: {fmt_time(c, e['end'])}" + (f" (after {fmt_dur(dur)})" if dur is not None else ""))
        late = time.time() - ((e.get("end") if e["kind"] != "problem" else e.get("start")) or e["received"])
        if late > num(c, "LATE_AFTER"):
            lines.append(f"\u23F1 Sent late ({fmt_dur(late)} after it happened): WhatsApp delivery was interrupted")
        lines.append(f"Event: {e['id']}")
        return "\n".join(lines)
    maxl = int(num(c, "MAX_LINES"))
    probs = sum(1 for e in batch if e["kind"] == "problem")
    res = sum(1 for e in batch if e["kind"] == "resolved")
    blips = sum(1 for e in batch if e["kind"] == "blip")
    parts = [f"{probs} new"] if probs else []
    if res:
        parts.append(f"{res} resolved")
    if blips:
        parts.append(f"{blips} down and back")
    t0 = min((e["start"] or e["received"]) for e in batch)
    out = [f"*Zabbix: {len(batch)} alarm updates* ({', '.join(parts)})", f"Since {fmt_time(c, t0)}", ""]
    rank = {"problem": 0, "blip": 1, "resolved": 2}
    batch = sorted(batch, key=lambda e: (rank[e["kind"]], -int(e["nsev"] or 0), e["start"] or 0))
    for e in batch[:maxl]:
        dl = device_line(c, e)
        dl = (" | " + dl.split(": ", 1)[1]) if dl else ""
        if e["kind"] == "problem":
            icon, tail = SEV_ICON.get(e["nsev"], "\U0001F534"), f"down {fmt_time(c, e['start'], False)}"
        else:
            dur = (e["end"] - e["start"]) if (e.get("end") and e.get("start")) else None
            icon = "✅" if e["kind"] == "resolved" else "⚠️"
            tail = (f"back {fmt_time(c, e['end'], False)}" + (f" after {fmt_dur(dur)}" if dur is not None else ""))
        out.append(f"{icon} *{e['sev']}* {e['name']}{dl} | {tail}")
    if len(batch) > maxl:
        out.append(f"+{len(batch) - maxl} more, open Zabbix Problems")
    return "\n".join(out)


# ---------------------------------------------------------------- relay service
class Relay:
    def __init__(self):
        self.lock = threading.Lock()
        self.c = load_conf()
        self.conf_mtime = self._mtime()
        os.makedirs(self.c["SPOOL"], exist_ok=True)
        self.qfile = os.path.join(self.c["SPOOL"], "queue.json")
        self.sfile = os.path.join(self.c["SPOOL"], "sent.json")
        self.queue = self._load(self.qfile, [])
        st = self._load(self.sfile, {})
        self.sent_times = st.get("times", [])
        self.sent_keys = st.get("keys", {})
        self.last_send = st.get("last_send", 0)
        self.next_gap = 0
        self.last_error = ""
        self.retry_at = 0
        self.backoff = 30
        self.fail_since = 0          # first failed send of the current outage (0 = sending works)
        self.waha_status = "UNKNOWN"
        self.waha_checked = 0
        self.last_restart = 0
        self.loop_beat = time.time()

    def _mtime(self):
        try:
            return os.path.getmtime(CONF)
        except OSError:
            return 0

    @staticmethod
    def _load(path, default):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    @staticmethod
    def _save(path, data):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)

    def save(self):
        self._save(self.qfile, self.queue)
        now = time.time()
        self.sent_times = [t for t in self.sent_times if t > now - 3600]
        self.sent_keys = {k: t for k, t in self.sent_keys.items() if t > now - 7 * 86400}
        self._save(self.sfile, {"times": self.sent_times, "keys": self.sent_keys, "last_send": self.last_send})

    def add(self, payload):
        e = normalize(payload)
        if e["kind"] == "update":
            return "ignored (update)"
        key = f"{e['id']}:{e['kind']}"
        with self.lock:
            if e["id"] and (key in self.sent_keys or any(f"{q['id']}:{q['kind']}" == key for q in self.queue)):
                return "duplicate"
            self.queue.append(e)
            self.save()
        log(f"queued {e['kind']} event {e['id']} [{e['sev']}] {e['name']}")
        return "queued"

    def status(self):
        now = time.time()
        with self.lock:
            return {"waha_status": self.waha_status, "waha_checked_age": int(now - self.waha_checked) if self.waha_checked else -1,
                    "oldest_wait": int(now - min(q["received"] for q in self.queue)) if self.queue else 0,
                    "failing_for": int(now - self.fail_since) if self.fail_since else 0,
                    "loop_age": int(now - self.loop_beat),
                    "queued": len(self.queue), "sent_last_hour": len([t for t in self.sent_times if t > now - 3600]),
                    "max_per_hour": int(num(self.c, "MAX_PER_HOUR")), "paused": self.c.get("PAUSED") == "1",
                    "last_send": fmt_time(self.c, self.last_send) if self.last_send else "never",
                    "last_error": self.last_error, "chat_id": self.c.get("WA_CHAT_ID"), "waha": self.c.get("WAHA_URL")}

    def pick(self, now):
        """WA-FIX-2: the next ONE alarm to send (with its own resolve if that is queued too), oldest first.
        Never a digest: a backlog goes out one message per alarm, in the normal format."""
        if not self.queue or self.c.get("PAUSED") == "1" or now < self.retry_at:
            return None
        if now < self.last_send + self.next_gap:
            return None
        if len([t for t in self.sent_times if t > now - 3600]) >= num(self.c, "MAX_PER_HOUR"):
            return None
        hold = num(self.c, "GROUP_WINDOW")
        ready = [q for q in self.queue if now - q["received"] >= hold]
        if not ready:
            return None
        first = min(ready, key=lambda q: (q.get("start") or q["received"], q["received"]))
        return [q for q in self.queue if q is first or (first["id"] and q["id"] == first["id"])]

    def health(self):
        """WA-FIX-3: every 30 s ask WAHA if the WhatsApp session is WORKING; restart it when it stopped or failed."""
        while True:
            try:
                st, _ = session_status(self.c)
            except Exception as e:
                st = "WAHA UNREACHABLE" if "not reachable" in str(e) else ("NOT CREATED" if "404" in str(e) else "ERROR")
            if st != self.waha_status:
                log(f"WhatsApp session status: {self.waha_status} -> {st}")
            self.waha_status, self.waha_checked = st, time.time()
            if st in ("STOPPED", "FAILED", "NOT CREATED") and time.time() - self.last_restart > 300:
                self.last_restart = time.time()
                for path in (f"/api/sessions/{self.c['WAHA_SESSION']}/restart", f"/api/sessions/{self.c['WAHA_SESSION']}/start"):
                    try:
                        waha(self.c, "POST", path, {}, timeout=60)
                        log(f"asked WAHA to {path.rsplit('/', 1)[1]} the session")
                        break
                    except Exception as e:
                        log("note:", e)
            time.sleep(30)

    def loop(self):
        while True:
            time.sleep(1)
            self.loop_beat = time.time()
            try:
                self.step()
            except Exception as e:   # WA-FIX-1: never let the sender thread die silently
                import traceback
                self.last_error = f"{time.strftime('%H:%M:%S')} internal: {e}"
                log("ERROR in sender loop:", traceback.format_exc())

    def step(self):
        m = self._mtime()
        if m != self.conf_mtime:
            self.conf_mtime, self.c = m, load_conf()
            log("settings reloaded")
        now = time.time()
        with self.lock:
            batch = self.pick(now)
        if not batch:
            return
        for x in batch:
            api_times(self.c, x)
        text = render(self.c, merge(batch))
        try:
            send_whatsapp(self.c, text)
        except Exception as e:
            self.last_error = f"{time.strftime('%H:%M:%S')} {e}"
            self.fail_since = self.fail_since or time.time()
            log("send failed, retry in", self.backoff, "s:", e)
            self.retry_at = time.time() + self.backoff
            self.backoff = min(120, self.backoff * 2)
            return
        with self.lock:
            ids = {(x["id"], x["kind"], x["received"]) for x in batch}
            self.queue = [q for q in self.queue if (q["id"], q["kind"], q["received"]) not in ids]
            t = time.time()
            for x in batch:
                self.sent_keys[f"{x['id']}:{x['kind']}"] = t
            self.sent_times.append(t)
            self.last_send = t
            self.next_gap = num(self.c, "MIN_GAP") + random.uniform(0, 5)
            self.last_error, self.backoff, self.retry_at, self.fail_since = "", 30, 0, 0
            self.save()
        log(f"sent {batch[0]['kind'] if len(batch) == 1 else 'down-and-back'} event {batch[0]['id']} ({len(self.queue)} left)")


def serve():
    relay = Relay()
    c = relay.c
    host, port = c["RELAY_LISTEN"].rsplit(":", 1)
    token = c.get("RELAY_TOKEN", "")

    class H(BaseHTTPRequestHandler):
        def _out(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                return self._out(200, {"ok": True})
            if self.path == "/status":
                return self._out(200, relay.status())
            self._out(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/alert":
                return self._out(404, {"error": "not found"})
            if token and self.headers.get("X-Relay-Token", "") != token:
                return self._out(403, {"error": "bad token"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._out(400, {"error": "bad json"})
            self._out(200, {"result": relay.add(payload)})

        def log_message(self, *a):
            pass

    threading.Thread(target=relay.loop, daemon=True).start()
    threading.Thread(target=relay.health, daemon=True).start()
    log(f"WhatsApp relay listening on {host}:{port}, sending to {c.get('WA_CHAT_ID') or '(WA_CHAT_ID not set)'} via {c['WAHA_URL']}")
    ThreadingHTTPServer((host, int(port)), H).serve_forever()


# ---------------------------------------------------------------- CLI
def install():
    if os.geteuid() != 0:
        sys.exit("Run as root.")
    c = load_conf()
    if not os.path.exists(CONF):
        c["RELAY_TOKEN"] = secrets.token_hex(16)
        write_conf(c)
        print(f"created {CONF}")
    elif not c.get("RELAY_TOKEN"):
        c["RELAY_TOKEN"] = secrets.token_hex(16)
        write_conf(c)
    else:
        print(f"kept {CONF}")
    os.makedirs(c["SPOOL"], exist_ok=True)
    me = os.path.abspath(__file__)
    with open(f"/etc/systemd/system/{UNIT}.service", "w") as f:
        f.write(f"""[Unit]
Description=WhatsApp relay for Zabbix alarms (to WAHA)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 {me} serve
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
""")
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", UNIT], check=True)
    subprocess.run(["systemctl", "restart", UNIT], check=True)
    time.sleep(2)
    try:
        st = local(c, "/status")
        print(f"{UNIT} running on {c['RELAY_LISTEN']} (queue {st['queued']})")
    except Exception as e:
        print(f"{UNIT} did not answer: {e}")
        subprocess.run(["journalctl", "-u", UNIT, "-n", "20", "--no-pager"])


def local(c, path, body=None, token=""):
    url = f"http://{c['RELAY_LISTEN']}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data, {"Content-Type": "application/json", "X-Relay-Token": token})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def cli_set(key, value):
    c = load_conf()
    key = key.upper()
    if key not in DEFAULTS:
        sys.exit(f"Unknown setting {key}. Known: {', '.join(DEFAULTS)}")
    c[key] = value
    write_conf(c)
    print(f"{key} set (the relay picks it up within a second)")
    if key in ("RELAY_LISTEN", "SPOOL"):
        subprocess.run(["systemctl", "restart", UNIT])


def demo():
    c = load_conf()
    now = dt.datetime.now()
    d, t = now.strftime("%Y.%m.%d"), now.strftime("%H:%M:%S")
    base = {"date": d, "time": t, "groups": "Sites/SITE9999", "opdata": ""}
    evs = [
        dict(base, event_id="T1", event_value="1", severity="Disaster", nseverity="5",
             name="TEST ONLY: Site SITE9999 DOWN: main link TEST-A ~ TEST-B lost, 12 of 14 devices down",
             host="Site SITE9999", host_ip=""),
        dict(base, event_id="T2", event_value="1", severity="High", nseverity="4",
             name="TEST ONLY: Site SITE9998 DOWN via upstream SITE9999: cut off", host="Site SITE9998", host_ip="",
             groups="Sites/SITE9998"),
        dict(base, event_id="T3", event_value="0", severity="High", nseverity="4",
             name="TEST ONLY: Site SITE9997 DOWN: main link TEST-C lost", host="Site SITE9997", host_ip="",
             groups="Sites/SITE9997", rdate=d, rtime=t, time=(now - dt.timedelta(minutes=4)).strftime("%H:%M:%S")),
    ]
    for e in evs:
        e["event_id"] = e["event_id"] + "-" + str(int(time.time() * 1000))
        print(e["event_id"], local(c, "/alert", e, c.get("RELAY_TOKEN", ""))["result"])
    print(f"Expect ONE WhatsApp message with 3 lines in about {int(num(c, 'GROUP_WINDOW')) + 10} s.")


def main():
    a = sys.argv[1:]
    cmd = a[0] if a else "help"
    if cmd == "serve":
        return serve()
    if cmd == "install":
        return install()
    if cmd == "set" and len(a) >= 3:
        return cli_set(a[1], " ".join(a[2:]))
    c = load_conf()
    try:
        if cmd == "session":
            try:
                st, raw = session_status(c)
            except WahaError as e:
                if "404" not in str(e):
                    raise
                st, raw = "NOT CREATED", {}
            me = (raw.get("me") or {})
            print(f"WAHA session '{c['WAHA_SESSION']}': {st}" + (f" (linked number {me.get('id', '')})" if me else ""))
            if st != "WORKING":
                print("Run:  docker logs -f waha   and scan the QR code with WhatsApp > Linked devices > "
                      "Link a device (Ctrl+C to leave the log). If no QR shows:  docker restart waha")
        elif cmd == "groups":
            gs = list_groups(c)
            if not gs:
                print("No groups found (is the number linked and a member of the NOC group?)")
            for name, gid in gs:
                print(f"{gid:<34} {name}")
            print("\nThen: python3 tools/whatsapp_relay.py set WA_CHAT_ID <id of the NOC group>")
        elif cmd == "test":
            text = " ".join(a[1:]) or (f"✅ Test from Zabbix WhatsApp relay, {fmt_time(c, time.time())}. "
                                       "High and Disaster alarms will be posted in this group.")
            send_whatsapp(c, text)
            print("sent")
        elif cmd == "demo":
            demo()
        elif cmd == "status":
            print(json.dumps(local(c, "/status"), indent=2))
        else:
            print(__doc__)
    except WahaError as e:
        sys.exit(str(e))
    except urllib.error.URLError as e:
        sys.exit(f"relay not reachable on {c['RELAY_LISTEN']} ({e}); is it running? systemctl status {UNIT}")


if __name__ == "__main__":
    main()
