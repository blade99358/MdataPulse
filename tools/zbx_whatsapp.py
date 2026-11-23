#!/usr/bin/env python3
"""Zabbix side of the WhatsApp NOC alarms: webhook media type, a no-login notify user and the action.

  python3 tools/zbx_whatsapp.py              show what would be created or changed (dry run)
  python3 tools/zbx_whatsapp.py --apply      create / update it (safe to run again)
  python3 tools/zbx_whatsapp.py test         queue one fake High event in the relay (relay -> WhatsApp group)
  python3 tools/zbx_whatsapp.py off | on     pause / resume the WhatsApp action
  python3 tools/zbx_whatsapp.py remove       delete action, user, user group, role and media type

Needs ZABBIX_URL / ZABBIX_TOKEN (source ~/.zabbix_env) and the relay settings in /etc/whatsapp-relay.conf
(python3 tools/whatsapp_relay.py install). Only High and Disaster problems (and their recovery) are sent;
change MIN_SEVERITY below to widen it later (3 = Average).
"""
import json
import os
import secrets
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zbxapi import ZabbixAPI  # noqa: E402
import whatsapp_relay as wr  # noqa: E402

MEDIA = "WhatsApp NOC (WAHA)"
ROLE = "WhatsApp notify (no login)"
GROUP = "WhatsApp notify"
USER = "whatsapp-noc"
ACTION = "WhatsApp NOC: High and Disaster"
MIN_SEVERITY = 4          # 4 = High, 5 = Disaster
SEV_MASK = sum(1 << s for s in range(MIN_SEVERITY, 6))   # user media "use if severity"

SCRIPT = r"""
var p = JSON.parse(value);
var url = p.relay_url, token = p.relay_token;
delete p.relay_url; delete p.relay_token;
var req = new HttpRequest();
req.addHeader('Content-Type: application/json');
req.addHeader('X-Relay-Token: ' + token);
var resp = req.post(url, JSON.stringify(p));
if (req.getStatus() !== 200) {
    throw 'WhatsApp relay ' + url + ' answered HTTP ' + req.getStatus() + ': ' + resp;
}
Zabbix.log(4, '[WhatsApp] relay: ' + resp);
return 'OK';
""".strip()

PARAMS = [
    ("relay_url", None), ("relay_token", None),
    ("event_id", "{EVENT.ID}"), ("event_value", "{EVENT.VALUE}"), ("event_update", "{EVENT.UPDATE.STATUS}"),
    ("severity", "{EVENT.SEVERITY}"), ("nseverity", "{EVENT.NSEVERITY}"), ("name", "{EVENT.NAME}"),
    ("host", "{HOST.NAME}"), ("host_ip", "{HOST.IP}"), ("groups", "{TRIGGER.HOSTGROUP.NAME}"),
    ("mainlink", "{$SITE.MAINLINK}"), ("opdata", "{EVENT.OPDATA}"),
    ("date", "{EVENT.DATE}"), ("time", "{EVENT.TIME}"),
    ("rdate", "{EVENT.RECOVERY.DATE}"), ("rtime", "{EVENT.RECOVERY.TIME}"),
    ("sendto", "{ALERT.SENDTO}"),
]
MSG = "{EVENT.SEVERITY}: {EVENT.NAME} on {HOST.NAME} ({HOST.IP}) at {EVENT.DATE} {EVENT.TIME}"
RMSG = "Resolved {EVENT.RECOVERY.DATE} {EVENT.RECOVERY.TIME}: {EVENT.NAME} on {HOST.NAME}"


def relay_conf():
    c = wr.load_conf()
    if not c.get("RELAY_TOKEN"):
        sys.exit(f"{wr.CONF} has no RELAY_TOKEN: run python3 tools/whatsapp_relay.py install first")
    return c


def one(api, method, flt, **extra):
    r = api.call(method, {"output": "extend", "filter": flt, **extra})
    return r[0] if r else None


def media_body(c):
    url = f"http://{c['RELAY_LISTEN']}/alert"
    vals = {"relay_url": url, "relay_token": c["RELAY_TOKEN"]}
    return {
        "name": MEDIA, "type": 4, "status": 0, "script": SCRIPT, "timeout": "30s",
        "parameters": [{"name": n, "value": vals.get(n, v)} for n, v in PARAMS],
        "maxsessions": 1, "maxattempts": 3, "attempt_interval": "30s", "process_tags": 0,
        "description": "Sends High/Disaster alarms to the NOC WhatsApp group through the local relay "
                       "(tools/whatsapp_relay.py -> WAHA). Made by tools/zbx_whatsapp.py.",
        "message_templates": [
            {"eventsource": 0, "recovery": 0, "subject": "Problem: {EVENT.NAME}", "message": MSG},
            {"eventsource": 0, "recovery": 1, "subject": "Resolved: {EVENT.NAME}", "message": RMSG},
        ],
    }


def apply(api, do):
    c = relay_conf()
    say = (lambda m: print(("  " if do else "  would ") + m))
    # 1 media type
    mt = one(api, "mediatype.get", {"name": MEDIA})
    body = media_body(c)
    if mt:
        say(f"update media type '{MEDIA}'")
        if do:
            api.call("mediatype.update", {"mediatypeid": mt["mediatypeid"], **body})
        mtid = mt["mediatypeid"]
    else:
        say(f"create media type '{MEDIA}' (webhook to {body['parameters'][0]['value']})")
        mtid = api.call("mediatype.create", body)["mediatypeids"][0] if do else "NEW"
    # 2 role: super-admin type so it sees every host group (new sites included), but no UI and no API
    role = one(api, "role.get", {"name": ROLE})
    rules = {"ui.default_access": 0, "api.access": 0, "actions.default_access": 0, "modules.default_access": 0}
    if not role:
        say(f"create role '{ROLE}' (sees all hosts, frontend and API switched off)")
        roleid = api.call("role.create", {"name": ROLE, "type": 3, "rules": rules})["roleids"][0] if do else "NEW"
    else:
        roleid = role["roleid"]
    # 3 user group without frontend access
    ug = one(api, "usergroup.get", {"name": GROUP})
    if not ug:
        say(f"create user group '{GROUP}' (no frontend login)")
        ugid = api.call("usergroup.create", {"name": GROUP, "gui_access": 3, "users_status": 0})["usrgrpids"][0] if do else "NEW"
    else:
        ugid = ug["usrgrpid"]
    # 4 user with the WhatsApp media, High + Disaster only
    media = [{"mediatypeid": mtid, "sendto": "NOC WhatsApp group", "active": 0, "severity": SEV_MASK,
              "period": "1-7,00:00-24:00"}]
    u = one(api, "user.get", {"username": USER})
    if not u:
        say(f"create user '{USER}' (random password, cannot log in) with the WhatsApp media")
        uid = api.call("user.create", {"username": USER, "name": "WhatsApp", "surname": "NOC group",
                                       "passwd": secrets.token_urlsafe(24) + "aA1!", "roleid": roleid, "timezone": "UTC",
                                       "usrgrps": [{"usrgrpid": ugid}], "medias": media})["userids"][0] if do else "NEW"
    else:
        uid = u["userid"]
        say(f"update user '{USER}' media (High + Disaster, always)")
        if do:
            # WA-FIX-5: alert times are written in the recipient's time zone; UTC = what the relay expects
            api.call("user.update", {"userid": uid, "roleid": roleid, "timezone": "UTC",
                                     "usrgrps": [{"usrgrpid": ugid}], "medias": media})
    # 5 action
    act = one(api, "action.get", {"name": ACTION, "eventsource": 0})
    abody = {
        "esc_period": "1h", "status": 0, "pause_symptoms": 1, "pause_suppressed": 1, "notify_if_canceled": 0,
        "filter": {"evaltype": 1, "conditions": [
            {"conditiontype": 4, "operator": 5, "value": str(MIN_SEVERITY)},    # severity >= High
            {"conditiontype": 16, "operator": 11},               # not in maintenance
        ]},
        "operations": [{"operationtype": 0, "esc_step_from": 1, "esc_step_to": 1, "esc_period": "0",
                        "opmessage": {"default_msg": 1, "mediatypeid": mtid},
                        "opmessage_usr": [{"userid": uid}]}],
        "recovery_operations": [{"operationtype": 11, "opmessage": {"default_msg": 1}}],
    }
    if act:
        say(f"update action '{ACTION}'")
        if do:
            api.call("action.update", {"actionid": act["actionid"], **abody})
    else:
        say(f"create action '{ACTION}': severity >= High, not in maintenance, problem + resolved, once per event")
        if do:
            api.call("action.create", {"name": ACTION, "eventsource": 0, **abody})
    monitor(api, c, do, say)
    print("Done." if do else "Dry run only. Add --apply to make the changes.")


MON_HOST = "WhatsApp gateway"


def monitor(api, c, do, say):
    """WA-FIX-4: Zabbix watches the WhatsApp path itself, so the NOC sees it in Problems within minutes:
    relay service down, WhatsApp session not WORKING (logged out, phone, WAHA down), alarms waiting too long."""
    url = f"http://{c['RELAY_LISTEN']}/status"
    port = c["RELAY_LISTEN"].rsplit(":", 1)[1]
    h = one(api, "host.get", {"host": MON_HOST})
    if not h:
        grp = one(api, "hostgroup.get", {"name": "Zabbix servers"})
        say(f"create host '{MON_HOST}' (watches the WhatsApp relay and session)")
        if not do:
            return
        if not grp:
            grp = {"groupid": api.call("hostgroup.create", {"name": "Zabbix servers"})["groupids"][0]}
        hid = api.call("host.create", {"host": MON_HOST, "groups": [{"groupid": grp["groupid"]}],
                                       "interfaces": [{"type": 1, "main": 1, "useip": 1, "ip": "127.0.0.1",
                                                       "dns": "", "port": "10050"}],
                                       "tags": [{"tag": "class", "value": "whatsapp"}]})["hostids"][0]
    else:
        hid = h["hostid"]
        say(f"check items and triggers on host '{MON_HOST}'")
        if not do:
            return
    iface = api.call("hostinterface.get", {"hostids": hid, "output": ["interfaceid"]})[0]["interfaceid"]
    have = {i["key_"]: i["itemid"] for i in api.call("item.get", {"hostids": hid, "output": ["key_"]})}
    svc = f"net.tcp.service[http,127.0.0.1,{port}]"

    def item(key, body):
        if key in have:
            api.call("item.update", {"itemid": have[key], **{k: v for k, v in body.items() if k not in ("type", "hostid", "key_")}})
        else:
            have[key] = api.call("item.create", {"hostid": hid, "key_": key, **body})["itemids"][0]
    item(svc, {"name": "WhatsApp relay service", "type": 3, "value_type": 3, "delay": "1m", "interfaceid": iface})
    item("wa.relay.status", {"name": "WhatsApp relay status (raw)", "type": 19, "value_type": 4, "delay": "1m",
                             "url": url, "timeout": "10s", "history": "1d"})
    for key, name, path, vt, units in (("wa.session", "WhatsApp session", "$.waha_status", 1, ""),
                                       ("wa.oldest", "WhatsApp oldest alarm waiting", "$.oldest_wait", 3, "s"),
                                       ("wa.failing", "WhatsApp sending failing for", "$.failing_for", 3, "s"),
                                       ("wa.queue", "WhatsApp alarms queued", "$.queued", 3, ""),
                                       ("wa.loop", "WhatsApp sender heartbeat age", "$.loop_age", 3, "s")):
        item(key, {"name": name, "type": 18, "value_type": vt, "units": units, "delay": "0",
                   "master_itemid": have["wa.relay.status"],
                   "preprocessing": [{"type": 12, "params": path, "error_handler": 0, "error_handler_params": ""}]})
    h_ = MON_HOST
    want = [
        ("WhatsApp alarms DOWN: relay service not running", f"max(/{h_}/{svc},3m)=0", 5,
         "Zabbix cannot reach the WhatsApp relay on this server: no alarm reaches the NOC group. "
         "Fix: systemctl restart whatsapp-relay ; journalctl -u whatsapp-relay -n 50"),
        ("WhatsApp alarms DOWN: WhatsApp session is {ITEM.LASTVALUE1}", f'count(/{h_}/wa.session,2m,"ne","WORKING")>=2', 5,
         "The WhatsApp link is not WORKING, so alarms queue up and are NOT delivered. The relay restarts the "
         "session by itself; if it says SCAN_QR_CODE the number was logged out: docker logs -f waha and scan again. "
         "Queued alarms go out one by one, marked 'sent late', when it is back."),
        ("WhatsApp alarms delayed: oldest alarm waiting {ITEM.LASTVALUE1}", f"last(/{h_}/wa.oldest)>180", 4,
         "An alarm has waited over 3 minutes to be sent to WhatsApp. See: python3 tools/whatsapp_relay.py status"),
        ("WhatsApp alarms DOWN: sender stopped", f"last(/{h_}/wa.loop)>120", 5,
         "The relay answers but its sender has stopped. Fix: systemctl restart whatsapp-relay"),
    ]
    trig = {t["description"]: t["triggerid"] for t in api.call("trigger.get", {"hostids": hid, "output": ["description"]})}
    for name, expr, prio, desc in want:
        body = {"expression": expr, "priority": prio, "comments": desc,
                "tags": [{"tag": "alarm", "value": "whatsapp"}]}
        if name in trig:
            api.call("trigger.update", {"triggerid": trig[name], **body})
        else:
            api.call("trigger.create", {"description": name, **body})
    say(f"host '{MON_HOST}': 7 items, 4 triggers (relay down, session not WORKING, alarm waiting > 3 min, sender stopped)")


def set_status(api, status):
    act = one(api, "action.get", {"name": ACTION, "eventsource": 0})
    if not act:
        sys.exit(f"No action '{ACTION}' (run --apply first)")
    api.call("action.update", {"actionid": act["actionid"], "status": status})
    print(f"WhatsApp action {'ON' if status == 0 else 'OFF'}")


def remove(api):
    for method, key, flt in (("action", "actionid", {"name": ACTION}), ("user", "userid", {"username": USER}),
                             ("usergroup", "usrgrpid", {"name": GROUP}), ("role", "roleid", {"name": ROLE}),
                             ("mediatype", "mediatypeid", {"name": MEDIA})):
        x = one(api, f"{method}.get", flt)
        if x:
            api.call(f"{method}.delete", [x[key]])
            print(f"deleted {method} {list(flt.values())[0]}")


def test(api):
    """Queue one fake High event (with the media type's fields) in the relay: proves relay -> WAHA -> group."""
    c = relay_conf()
    mt = one(api, "mediatype.get", {"name": MEDIA}, selectParameters="extend")
    if not mt:
        sys.exit("Media type missing: run --apply first")
    import time
    t = time.localtime()
    fake = {"event_id": f"ZBXTEST-{int(time.time() * 1000)}", "event_value": "1", "event_update": "0",
            "severity": "High", "nseverity": "4", "name": "TEST ONLY: Zabbix to WhatsApp path check (ignore)",
            "host": "Zabbix server", "host_ip": "127.0.0.1", "groups": "Zabbix servers",
            "date": time.strftime("%Y.%m.%d", t), "time": time.strftime("%H:%M:%S", t)}
    body = {p["name"]: fake.get(p["name"], "") for p in mt["parameters"] if p["name"] not in ("relay_url", "relay_token")}
    print("relay:", wr.local(c, "/alert", body, c["RELAY_TOKEN"])["result"])
    print(f"Expect the WhatsApp message in about {int(wr.num(c, 'GROUP_WINDOW')) + 10} s.")


def main():
    a = sys.argv[1:]
    api = ZabbixAPI()
    if not a or a == ["--apply"]:
        return apply(api, bool(a))
    cmd = a[0]
    if cmd == "test":
        return test(api)
    if cmd in ("off", "on"):
        return set_status(api, 1 if cmd == "off" else 0)
    if cmd == "remove":
        return remove(api)
    print(__doc__)


if __name__ == "__main__":
    main()
