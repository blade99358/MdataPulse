#!/usr/bin/env python3
"""Generate the MDP Zabbix templates as a Zabbix import file (YAML).

UUIDs are derived from names (uuid5), so regenerating and re-importing
updates the same templates instead of creating duplicates.

Usage: python3 gen_templates.py > mdp_templates.yaml
"""
import sys
import uuid

import yaml

# store a state value only when it changes, or every 15 min: polling (and alarm speed) stays at 1 min,
# the database keeps ~15x fewer rows. The RCA reads changes, so it sees the same thing.
HEARTBEAT = {"type": "DISCARD_UNCHANGED_HEARTBEAT", "parameters": ["15m"]}

NS = uuid.UUID("6f1c2a52-6d1b-4c7e-9f38-5a0e3d1f0c11")
GROUP = "Templates/MDP network"

BASE = "MDP Network Base SNMP"
EPMP = "MDP Cambium ePMP SNMP"
POE = "MDP PoE Switch SNMP"
UPS = "MDP UPS SNMP"
WAVE = "MDP Ubiquiti Wave SNMP"
UNIFI = "MDP UniFi Switch SNMP"
GUARD = "MDP Zabbix Server Guard"
GUARD_GROUP = "Templates/MDP server"
SITE = "MDP Site Status"
LACP = "MDP LACP Trunk SNMP"


def uid(*parts):
    # Zabbix requires UUIDv4 format: take a stable uuid5 and stamp v4 version bits.
    b = bytearray(uuid.uuid5(NS, "/".join(parts)).bytes)
    b[6] = (b[6] & 0x0F) | 0x40
    b[8] = (b[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(b)).hex


def tags(**kw):
    return [{"tag": k, "value": v} for k, v in kw.items()]


def trigger(tpl, name, expr, priority, desc="", recovery=None, manual_close=False, alarm=None):
    t = {
        "uuid": uid(tpl, "trigger", name),
        "expression": expr,
        "name": name,
        "priority": priority,
        "description": desc,
        # alarm= is what the event correlation in zbx_sites.py sitedown matches on
        "tags": tags(scope="availability", **({"alarm": alarm} if alarm else {})),
    }
    if recovery:
        t["recovery_mode"] = "RECOVERY_EXPRESSION"
        t["recovery_expression"] = recovery
    if manual_close:
        t["manual_close"] = "YES"
    return t


def diag(trig, cause):
    trig["tags"] = trig["tags"] + tags(diagnosis=cause)
    return trig


# ---------------------------------------------------------------- base
def base_template():
    t = BASE
    items = [
        {
            "uuid": uid(t, "icmpping"),
            "name": "ICMP ping",
            "type": "SIMPLE",
            "key": "icmpping",
            "delay": "{$ICMP.INTERVAL}",
            "history": "{$RCA.HISTORY}",
            "valuemap": {"name": "Service state"},
            "tags": tags(component="health", rca="ping"),
            "triggers": [
                trigger(t, "Device unreachable",
                        f"max(/{t}/icmpping,{{$ICMP.DOWN.TIME}})=0",
                        "AVERAGE",  # SEVERITY-1
                        "No ICMP reply. Downstream devices depend on this trigger through the parent tag, "
                        "so only the highest failed device raises a problem.", alarm="unreachable"),
            ],
        },
        {
            "uuid": uid(t, "icmppingloss"),
            "name": "ICMP loss",
            "type": "SIMPLE",
            "key": "icmppingloss",
            "delay": "{$ICMP.INTERVAL}",
            "history": "{$RCA.HISTORY}",
            "value_type": "FLOAT",
            "units": "%",
            "tags": tags(component="health", rca="loss"),
            "triggers": [
                trigger(t, "High ICMP packet loss",
                        f"min(/{t}/icmppingloss,5m)>{{$ICMP.LOSS.WARN}} and min(/{t}/icmppingloss,5m)<100",
                        "WARNING"),
            ],
        },
        {
            "uuid": uid(t, "icmppingsec"),
            "name": "ICMP response time",
            "type": "SIMPLE",
            "key": "icmppingsec",
            "delay": "{$ICMP.INTERVAL}",
            "history": "{$RCA.HISTORY}",
            "value_type": "FLOAT",
            "units": "s",
            "tags": tags(component="health", rca="latency"),
        },
        {
            # HOST-RESOURCES-MIB hrSystemUptime: does not reset when only the SNMP agent restarts.
            "uuid": uid(t, "system.hw.uptime"),
            "name": "Hardware uptime",
            "type": "SNMP_AGENT",
            "snmp_oid": "get[1.3.6.1.2.1.25.1.1.0]",
            "key": "system.hw.uptime",
            "delay": "{$UPTIME.INTERVAL}",
            "history": "{$RCA.HISTORY}",
            "units": "uptime",
            "description": "hrSystemUptime. Used by the RCA script to compute boot time. Devices without it "
                           "(UniFi) store 0, and the triggers and the RCA script use sysUpTime instead.",
            "preprocessing": [
                {"type": "CHECK_NOT_SUPPORTED", "parameters": ["-1"],
                 "error_handler": "CUSTOM_VALUE", "error_handler_params": "0"},
                {"type": "MULTIPLIER", "parameters": ["0.01"]},
            ],
            "tags": tags(component="system", rca="uptime"),
        },
        {
            "uuid": uid(t, "system.net.uptime"),
            "name": "SNMP agent uptime",
            "type": "SNMP_AGENT",
            "snmp_oid": "get[1.3.6.1.2.1.1.3.0]",
            "key": "system.net.uptime",
            "delay": "{$UPTIME.INTERVAL}",
            "history": "{$RCA.HISTORY}",
            "units": "uptime",
            "description": "sysUpTime. Resets on agent restart as well as on reboot.",
            "preprocessing": [
                {"type": "MULTIPLIER", "parameters": ["0.01"]},
            ],
            "tags": tags(component="system", rca="uptime"),
        },
        {
            "uuid": uid(t, "system.name"),
            "name": "System name",
            "type": "SNMP_AGENT",
            "snmp_oid": "get[1.3.6.1.2.1.1.5.0]",
            "key": "system.name",
            "delay": "1h",
            "history": "7d",
            "trends": "0",
            "value_type": "CHAR",
            "inventory_link": "NAME",
            "tags": tags(component="system"),
        },
        {
            "uuid": uid(t, "system.descr"),
            "name": "System description",
            "type": "SNMP_AGENT",
            "snmp_oid": "get[1.3.6.1.2.1.1.1.0]",
            "key": "system.descr",
            "delay": "1h",
            "history": "7d",
            "trends": "0",
            "value_type": "CHAR",
            "inventory_link": "OS",
            "tags": tags(component="system"),
        },
        {
            "uuid": uid(t, "snmp.available"),
            "name": "SNMP agent availability",
            "type": "INTERNAL",
            "key": "zabbix[host,snmp,available]",
            "history": "{$RCA.HISTORY}",
            "valuemap": {"name": "zabbix.host.available"},
            "tags": tags(component="health", rca="snmp"),
        },
    ]

    triggers = [
        trigger(t, "Device rebooted (uptime below 10m)",
                f"(last(/{t}/system.hw.uptime)>0 and last(/{t}/system.hw.uptime)<10m) or (last(/{t}/system.hw.uptime)=0 and last(/{t}/system.net.uptime)<10m)",
                "WARNING",
                "Uptime reset: power loss, PoE loss or crash. See the RCA report for which.",
                manual_close=True),
        # CPE-UPTIME-1: endpoint CPEs that restarted in the last week (power or PoE trouble at the endpoint)
        trigger(t, "CPE uptime below {$CPE.UPTIME.MIN}",
                f"{{$CPE.UPTIME.ALERT}}=1 and ((last(/{t}/system.hw.uptime)>0 and last(/{t}/system.hw.uptime)<{{$CPE.UPTIME.MIN}})"
                f" or (last(/{t}/system.hw.uptime)=0 and last(/{t}/system.net.uptime)<{{$CPE.UPTIME.MIN}}))",
                "WARNING",
                "The endpoint CPE restarted less than {$CPE.UPTIME.MIN} ago (power, PoE or crash). "
                "It clears by itself once the CPE has run {$CPE.UPTIME.MIN} without a restart."),
        # Diagnosis: once a device answers again after an outage, its uptime tells the cause.
        # Hardware uptime is used when the device has it (it survives SNMP agent restarts), else sysUpTime.
        # nodata(...)=0: only judge on a fresh uptime reading (the item stores 0 when unsupported, so it stays fresh).
        # It fires 2 minutes after recovery, so the parent's diagnosis exists first and the child's
        # (which depends on it, see zbx_sites.py) is suppressed: only the root device is diagnosed.
        diag(trigger(t, "Outage cause: CONNECTIVITY (device did not reboot)",
                f"count(/{t}/icmpping,30m,\"eq\",\"0\")>=3 and min(/{t}/icmpping,2m)=1 and nodata(/{t}/system.hw.uptime,3m)=0 and "
                f"((last(/{t}/system.hw.uptime)>0 and last(/{t}/system.hw.uptime)>30m) or (last(/{t}/system.hw.uptime)=0 and last(/{t}/system.net.uptime)>30m))",
                "AVERAGE",
                "The device was unreachable in the last 30 minutes but its uptime did not reset, so it "
                "kept power the whole time. The fault is on the path: upstream link, radio/RF, cable or switch port. "
                "Check the parent device and the RCA report.",
                manual_close=True), "connectivity"),
        diag(trigger(t, "Outage cause: POWER (device rebooted)",
                f"count(/{t}/icmpping,30m,\"eq\",\"0\")>=3 and min(/{t}/icmpping,2m)=1 and nodata(/{t}/system.hw.uptime,3m)=0 and "
                f"((last(/{t}/system.hw.uptime)>0 and last(/{t}/system.hw.uptime)<30m) or (last(/{t}/system.hw.uptime)=0 and last(/{t}/system.net.uptime)<30m))",
                "AVERAGE",
                "The device was unreachable and came back with a fresh uptime, so it lost power or rebooted "
                "(mains, UPS, PoE port or crash). If several devices at the site show this together, it was site power.",
                manual_close=True), "power"),
        trigger(t, "SNMP not responding while ping is up",
                f"{{$SNMP.ALERT}}=1 and max(/{t}/zabbix[host,snmp,available],{{$SNMP.TIMEOUT}})=0 "
                f"and last(/{t}/icmpping)=1",
                "WARNING",
                "Device answers ping but not SNMP: check community, ACL or agent. Devices without SNMP "
                "(e.g. power units) have {$SNMP.ALERT}=0 and are watched by ping only."),
    ]
    # Multi-item triggers live at export top level; single-item ones sit under their item.

    lld = {
        "uuid": uid(t, "net.if.discovery"),
        "name": "Network interfaces discovery",
        "type": "SNMP_AGENT",
        "snmp_oid": "discovery[{#IFOPERSTATUS},1.3.6.1.2.1.2.2.1.8,{#IFADMINSTATUS},1.3.6.1.2.1.2.2.1.7,"
                    "{#IFALIAS},1.3.6.1.2.1.31.1.1.1.18,{#IFNAME},1.3.6.1.2.1.31.1.1.1.1,"
                    "{#IFDESCR},1.3.6.1.2.1.2.2.1.2,{#IFTYPE},1.3.6.1.2.1.2.2.1.3]",
        "key": "net.if.discovery",
        "delay": "1h",
        "filter": {
            "evaltype": "AND",
            "conditions": [
                # 6 = ethernetCsmacd, 71 = ieee80211, 161 = ieee8023adLag
                {"macro": "{#IFTYPE}", "value": "{$NET.IF.IFTYPE.MATCHES}", "formulaid": "A"},
                {"macro": "{#IFADMINSTATUS}", "value": "^1$", "formulaid": "B"},
            ],
        },
        "item_prototypes": [
            {
                "uuid": uid(t, "proto", "status"),
                "name": "Interface {#IFNAME}({#IFALIAS}): Operational status",
                "type": "SNMP_AGENT",
                "snmp_oid": "get[1.3.6.1.2.1.2.2.1.8.{#SNMPINDEX}]",
                "key": "net.if.status[ifOperStatus.{#SNMPINDEX}]",
                "delay": "{$IF.INTERVAL}",
                "history": "{$RCA.HISTORY}",
                "trends": "0",
                "valuemap": {"name": "IF-MIB::ifOperStatus"},
                "preprocessing": [HEARTBEAT],
                "tags": tags(component="network", interface="{#IFNAME}", rca="ifstatus"),
                "trigger_prototypes": [
                    {
                        "uuid": uid(t, "proto", "linkdown"),
                        "expression": f"{{$IF.LINKDOWN.ALERT:\"{{#IFNAME}}\"}}=1 and "
                                      f"last(/{t}/net.if.status[ifOperStatus.{{#SNMPINDEX}}])=2 and "
                                      f"(last(/{t}/net.if.status[ifOperStatus.{{#SNMPINDEX}}],#1)<>"
                                      f"last(/{t}/net.if.status[ifOperStatus.{{#SNMPINDEX}}],#2))=1",
                        "recovery_mode": "RECOVERY_EXPRESSION",
                        "recovery_expression": f"last(/{t}/net.if.status[ifOperStatus.{{#SNMPINDEX}}])<>2",
                        "name": "Interface {#IFNAME}({#IFALIAS}): Link down",
                        "priority": "AVERAGE",
                        "manual_close": "YES",
                        "description": "Alerts only on ports with {$IF.LINKDOWN.ALERT:\"<ifname>\"}=1 "
                                       "(set it on uplinks), to avoid noise from access ports.",
                        "tags": tags(scope="availability"),
                    },
                ],
            },
            {
                "uuid": uid(t, "proto", "lastchange"),
                "name": "Interface {#IFNAME}({#IFALIAS}): Last change",
                "type": "SNMP_AGENT",
                "snmp_oid": "get[1.3.6.1.2.1.2.2.1.9.{#SNMPINDEX}]",
                "key": "net.if.lastchange[ifLastChange.{#SNMPINDEX}]",
                "delay": "{$IF.INTERVAL}",
                "history": "{$RCA.HISTORY}",
                "trends": "0",
                "units": "uptime",
                "description": "sysUpTime at the last status change of this port. A change without an uptime reset = link flap.",
                "preprocessing": [{"type": "MULTIPLIER", "parameters": ["0.01"]}, HEARTBEAT],
                "tags": tags(component="network", interface="{#IFNAME}", rca="iflastchange"),
            },
            {
                "uuid": uid(t, "proto", "speed"),
                "name": "Interface {#IFNAME}({#IFALIAS}): Speed",
                "type": "SNMP_AGENT",
                "snmp_oid": "get[1.3.6.1.2.1.31.1.1.1.15.{#SNMPINDEX}]",
                "key": "net.if.speed[ifHighSpeed.{#SNMPINDEX}]",
                "delay": "5m",
                "history": "{$RCA.HISTORY}",
                "trends": "0",
                "units": "bps",
                "preprocessing": [{"type": "MULTIPLIER", "parameters": ["1000000"]},
                                  {"type": "DISCARD_UNCHANGED_HEARTBEAT", "parameters": ["1h"]}],
                "tags": tags(component="network", interface="{#IFNAME}"),
            },
            {
                "uuid": uid(t, "proto", "in"),
                "name": "Interface {#IFNAME}({#IFALIAS}): Bits received",
                "type": "SNMP_AGENT",
                "snmp_oid": "get[1.3.6.1.2.1.31.1.1.1.6.{#SNMPINDEX}]",
                "key": "net.if.in[ifHCInOctets.{#SNMPINDEX}]",
                "delay": "{$IF.TRAFFIC.INTERVAL}",
                "history": "{$RCA.HISTORY}",
                "units": "bps",
                "preprocessing": [
                    {"type": "CHANGE_PER_SECOND", "parameters": [""]},
                    {"type": "MULTIPLIER", "parameters": ["8"]},
                ],
                "tags": tags(component="network", interface="{#IFNAME}", rca="traffic"),
            },
            {
                "uuid": uid(t, "proto", "out"),
                "name": "Interface {#IFNAME}({#IFALIAS}): Bits sent",
                "type": "SNMP_AGENT",
                "snmp_oid": "get[1.3.6.1.2.1.31.1.1.1.10.{#SNMPINDEX}]",
                "key": "net.if.out[ifHCOutOctets.{#SNMPINDEX}]",
                "delay": "{$IF.TRAFFIC.INTERVAL}",
                "history": "{$RCA.HISTORY}",
                "units": "bps",
                "preprocessing": [
                    {"type": "CHANGE_PER_SECOND", "parameters": [""]},
                    {"type": "MULTIPLIER", "parameters": ["8"]},
                ],
                "tags": tags(component="network", interface="{#IFNAME}", rca="traffic"),
            },
            {
                "uuid": uid(t, "proto", "inerr"),
                "name": "Interface {#IFNAME}({#IFALIAS}): Inbound errors",
                "type": "SNMP_AGENT",
                "snmp_oid": "get[1.3.6.1.2.1.2.2.1.14.{#SNMPINDEX}]",
                "key": "net.if.in.errors[ifInErrors.{#SNMPINDEX}]",
                "delay": "{$IF.TRAFFIC.INTERVAL}",
                "history": "{$RCA.HISTORY}",
                "preprocessing": [{"type": "CHANGE_PER_SECOND", "parameters": [""]}],
                "tags": tags(component="network", interface="{#IFNAME}", rca="errors"),
            },
        ],
    }

    macros = [
        {"macro": "{$ICMP.INTERVAL}", "value": "1m"},
        {"macro": "{$ICMP.DOWN.TIME}", "value": "3m", "description": "No ping reply for this long = unreachable"},
        {"macro": "{$ICMP.LOSS.WARN}", "value": "20"},
        {"macro": "{$CPE.UPTIME.ALERT}", "value": "0", "description": "1 = 'CPE uptime below' alarm (set on EP-* hosts)"},
        {"macro": "{$CPE.UPTIME.MIN}", "value": "7d", "description": "CPE uptime below this = alarm"},
        {"macro": "{$UPTIME.INTERVAL}", "value": "1m", "description": "Keep at 1m: RCA boot-time accuracy depends on it"},
        {"macro": "{$IF.INTERVAL}", "value": "1m", "description": "Port status and last change (RCA link flaps)"},
        {"macro": "{$IF.TRAFFIC.INTERVAL}", "value": "5m",
         "description": "Port traffic and errors (not used by the RCA). 1m = 5x more data"},
        {"macro": "{$RCA.HISTORY}", "value": "30d", "description": "History kept for RCA lookbacks (the global "
         "housekeeping override from zbx_data.py policy sets the real period; older readings come from the archive)"},
        {"macro": "{$SNMP.TIMEOUT}", "value": "5m"},
        {"macro": "{$SNMP.ALERT}", "value": "1", "description": "0 = no 'SNMP not responding' alarm (ping-only devices)"},
        {"macro": "{$NET.IF.IFTYPE.MATCHES}", "value": "^(6|71|117|161)$"},
        {"macro": "{$IF.LINKDOWN.ALERT}", "value": "0", "description": "Set {$IF.LINKDOWN.ALERT:\"<ifname>\"}=1 on uplink ports"},
        # {$SNMP_COMMUNITY} is deliberately not set here: define it once as a global macro
        # (Administration > Macros). A template value would override the global one.
    ]

    return {
        "uuid": uid(t),
        "template": t,
        "name": t,
        "description": "Common base for all MDP network devices: ping, uptime (for RCA), interfaces.",
        "groups": [{"name": GROUP}],
        "items": items,
        "discovery_rules": [lld],
        "macros": macros,
        "tags": [{"tag": "class", "value": "network"}, {"tag": "target", "value": "mdp"}],
        "valuemaps": [
            {"uuid": uid(t, "vm", "service"), "name": "Service state",
             "mappings": [{"value": "0", "newvalue": "Down"}, {"value": "1", "newvalue": "Up"}]},
            {"uuid": uid(t, "vm", "avail"), "name": "zabbix.host.available",
             "mappings": [{"value": "0", "newvalue": "not available"}, {"value": "1", "newvalue": "available"},
                          {"value": "2", "newvalue": "unknown"}]},
            {"uuid": uid(t, "vm", "ifoper"), "name": "IF-MIB::ifOperStatus",
             "mappings": [{"value": v, "newvalue": n} for v, n in
                          [("1", "up"), ("2", "down"), ("3", "testing"), ("4", "unknown"),
                           ("5", "dormant"), ("6", "notPresent"), ("7", "lowerLayerDown")]]},
        ],
    }, triggers


# first / lowest number in an SNMP walk[] result (lines like ".1.3.6... = INTEGER: -57")
WALK_FIRST_JS = """var m = value.match(/=\\s*[A-Za-z0-9]+:\\s*(-?[0-9.]+)/);
if (!m) throw 'no link';
return m[1];"""
WALK_MIN_JS = """var re = /=\\s*[A-Za-z0-9]+:\\s*(-?[0-9.]+)/g, m, best = null;
while ((m = re.exec(value)) !== null) { var v = parseFloat(m[1]); if (best === null || v < best) best = v; }
if (best === null) throw 'no stations';
return best;"""


def walk_item(tpl, key, name, oid, js, units, rca=None, delay="{$WIFI.INTERVAL}"):
    """One value out of a table walk, so every radio has the same item name for dashboards."""
    return {"uuid": uid(tpl, key), "name": name, "type": "SNMP_AGENT", "snmp_oid": f"walk[{oid}]", "key": key,
            "delay": delay, "history": "{$RCA.HISTORY}", "value_type": "FLOAT", "units": units,
            # no table on this radio (e.g. the station table on an SM): discard, do not mark unsupported
            "preprocessing": [{"type": "CHECK_NOT_SUPPORTED", "parameters": ["-1"], "error_handler": "DISCARD_VALUE"},
                              {"type": "JAVASCRIPT", "parameters": [js], "error_handler": "DISCARD_VALUE"}],
            "tags": tags(component="wireless", **({"rca": rca} if rca else {}))}


# ---------------------------------------------------------------- Cambium ePMP
def epmp_template():
    """Cambium ePMP 4600 (AP) and Force 4600C / 4625 (SM / PTP).

    OIDs from CAMBIUM-PMP80211-MIB (enterprise 17713.21):
      wirelessInterfaceMode 21.3.8.2.1.0: 1 = AP, 2 = SM. Every link alarm checks it.
      SM side, scalars of the one AP this radio is linked to (21.1.2.x.0):
        .16 connection state (1 scanning, 2 connecting, 3 connected), .8 connected AP SSID,
        .3 DL RSSI dBm, .18 DL SNR dB, .7 / .6 DL / UL MCS, .12 distance km
      AP side: cambiumAPConnectedSTATable 21.1.2.30.1, one row per connected SM.
    Not used: cambiumSTAConnectedAPTable 21.1.2.20.1. Despite its name it is the SM's scan list of every
    AP it can hear (it has scan age and network-entry columns), so alarms on it fired for foreign radios.
    """
    t = EPMP
    T = "1.3.6.1.4.1.17713.21.1.2.30.1"   # AP: connected stations
    R = "1.3.6.1.4.1.17713.21.1.2"        # RF status scalars
    # SM-only values are discarded on an AP (or while scanning) instead of making the item unsupported
    SKIP = [{"type": "CHECK_NOT_SUPPORTED", "parameters": ["-1"], "error_handler": "DISCARD_VALUE"}]

    def scalar(key, name, oid, units="", value_type="UNSIGNED", rca=None, delay="{$WIFI.INTERVAL}", pre=None,
               valuemap=None):
        d = {"uuid": uid(t, key), "name": name, "type": "SNMP_AGENT", "snmp_oid": f"get[{oid}]", "key": key,
             "delay": delay, "history": "{$RCA.HISTORY}", "value_type": value_type, "units": units,
             "tags": tags(component="wireless", **({"rca": rca} if rca else {}))}
        if pre:
            d["preprocessing"] = pre
        if valuemap:
            d["valuemap"] = {"name": valuemap}
        return d

    def proto(prefix, key, name, oid, units="", value_type="FLOAT", rca=None):
        return {"uuid": uid(t, prefix, key), "name": name, "type": "SNMP_AGENT",
                "snmp_oid": f"get[{oid}.{{#SNMPINDEX}}]", "key": f"{key}[{{#SNMPINDEX}}]",
                "delay": "{$WIFI.INTERVAL}", "history": "{$RCA.HISTORY}", "value_type": value_type, "units": units,
                "tags": tags(component="wireless", **({"rca": rca} if rca else {}))}

    sta = "Station {#STAIP} ({#STAMAC})"
    ap_lld = {
        "uuid": uid(t, "lld", "sta"),
        "name": "Connected stations discovery (AP)",
        "type": "SNMP_AGENT",
        "snmp_oid": f"discovery[{{#STAMAC}},{T}.1,{{#STAIP}},{T}.10]",
        "key": "epmp.sta.discovery",
        "delay": "10m",
        "lifetime": "3d",
        # readings only: the alarms for a weak link are raised once, on the SM end of the link
        "item_prototypes": [
            proto("sta", "wifi.sta.dlrssi", f"{sta}: DL RSSI", f"{T}.5", "dBm"),
            proto("sta", "wifi.sta.ulrssi", f"{sta}: UL RSSI", f"{T}.4", "dBm", rca="rssi"),
            proto("sta", "wifi.sta.dlsnr", f"{sta}: DL SNR", f"{T}.7", "dB"),
            proto("sta", "wifi.sta.ulsnr", f"{sta}: UL SNR", f"{T}.6", "dB", rca="snr"),
            proto("sta", "wifi.sta.dlmcs", f"{sta}: DL MCS", f"{T}.9", value_type="UNSIGNED"),
            proto("sta", "wifi.sta.ulmcs", f"{sta}: UL MCS", f"{T}.8", value_type="UNSIGNED"),
        ],
    }
    items = [
        scalar("wifi.mode", "Wireless: Mode", "1.3.6.1.4.1.17713.21.3.8.2.1.0", delay="10m", valuemap="ePMP mode"),
        scalar("wifi.frequency", "Wireless: Frequency", "1.3.6.1.4.1.17713.21.3.8.2.18.0", "MHz", delay="10m"),
        scalar("wifi.stations", "Wireless: Connected stations (AP)", f"{R}.10.0", rca="stations", pre=SKIP),
        scalar("wifi.link.state", "Wireless: Link state (SM)", f"{R}.16.0", rca="linkstate", pre=SKIP,
               valuemap="ePMP link state"),
        scalar("wifi.link.ssid", "Wireless: Connected AP (SM)", f"{R}.8.0", value_type="CHAR", delay="10m", pre=SKIP),
        scalar("wifi.rssi", "Wireless: Signal (dBm)", f"{R}.3.0", "dBm", value_type="FLOAT", rca="rssi", pre=SKIP),
        scalar("wifi.snr", "Wireless: SNR (dB)", f"{R}.18.0", "dB", value_type="FLOAT", rca="snr", pre=SKIP),
        walk_item(t, "wifi.sta.worst", "Wireless: Weakest station signal (dBm)", f"{T}.4", WALK_MIN_JS, "dBm",
                  rca="rssi"),
        scalar("wifi.dlmcs", "Wireless: DL MCS (SM)", f"{R}.7.0", pre=SKIP),
        scalar("wifi.ulmcs", "Wireless: UL MCS (SM)", f"{R}.6.0", pre=SKIP),
        scalar("wifi.distance", "Wireless: Distance to AP (SM)", f"{R}.12.0", "km", value_type="FLOAT", delay="1h",
               pre=SKIP),
    ]
    sm = f"last(/{t}/wifi.mode)=2"
    triggers = [
        trigger(t, "Radio not linked to its AP (scanning)",
                f"{sm} and max(/{t}/wifi.link.state,{{$WIFI.LINK.DOWN}})<3", "HIGH",
                "The SM answers on the network but its radio link has not been connected for "
                "{$WIFI.LINK.DOWN}. Usually the AP is down, the link is blocked or the radio was re-aimed.",
                recovery=f"last(/{t}/wifi.link.state)=3"),
        trigger(t, "Signal weak: RSSI {ITEM.LASTVALUE2}",
                f"{sm} and max(/{t}/wifi.rssi,15m)<{{$WIFI.RSSI.MIN}} and min(/{t}/wifi.link.state,15m)=3",
                "WARNING", "Signal to its own AP below {$WIFI.RSSI.MIN} dBm for 15 minutes while linked: "
                "alignment, obstruction or interference.",
                recovery=f"min(/{t}/wifi.rssi,10m)>={{$WIFI.RSSI.MIN}}+3 or last(/{t}/wifi.link.state)<>3"),
        trigger(t, "SNR low: {ITEM.LASTVALUE2}",
                f"{sm} and max(/{t}/wifi.snr,15m)<{{$WIFI.SNR.MIN}} and min(/{t}/wifi.link.state,15m)=3",
                "WARNING", "Signal-to-noise below {$WIFI.SNR.MIN} dB for 15 minutes while linked: interference "
                "or noise on the channel.",
                recovery=f"min(/{t}/wifi.snr,10m)>={{$WIFI.SNR.MIN}}+2 or last(/{t}/wifi.link.state)<>3"),
        trigger(t, "AP lost all its stations",
                f"last(/{t}/wifi.mode)=1 and max(/{t}/wifi.stations,5m)=0 and max(/{t}/wifi.stations,2h)>0",
                "HIGH", "This AP had stations connected in the last 2 hours and now has none.",
                recovery=f"last(/{t}/wifi.stations)>0"),
    ]
    for i, tr in enumerate(triggers):
        tr["tags"] = tags(scope="availability" if i in (0, 3) else "performance")
    triggers[3]["priority"] = "AVERAGE"
    macros = [
        {"macro": "{$WIFI.INTERVAL}", "value": "1m"},
        {"macro": "{$WIFI.RSSI.MIN}", "value": "-75"},
        {"macro": "{$WIFI.SNR.MIN}", "value": "15"},
        {"macro": "{$WIFI.LINK.DOWN}", "value": "3m"},
    ]
    return {
        "uuid": uid(t),
        "template": t,
        "name": t,
        "description": "Cambium ePMP 4600 / Force 4600C / Force 4625 wireless metrics (CAMBIUM-PMP80211-MIB). "
                       "Link together with " + BASE + ".",
        "groups": [{"name": GROUP}],
        "items": items,
        "triggers": triggers,
        "discovery_rules": [ap_lld],
        "macros": macros,
        "valuemaps": [
            {"uuid": uid(t, "vm", "mode"), "name": "ePMP mode",
             "mappings": [{"value": "1", "newvalue": "AP"}, {"value": "2", "newvalue": "SM"},
                          {"value": "3", "newvalue": "Spectrum analyzer"}]},
            {"uuid": uid(t, "vm", "state"), "name": "ePMP link state",
             "mappings": [{"value": "1", "newvalue": "scanning"}, {"value": "2", "newvalue": "connecting"},
                          {"value": "3", "newvalue": "connected"}]},
        ],
        "tags": [{"tag": "class", "value": "network"}, {"tag": "target", "value": "cambium"}],
    }


# ---------------------------------------------------------------- PoE switches
POE_MAP_JS = """var map = '{$POE.STATUS.MAP}'.split(',');
for (var i = 0; i < map.length; i++) {
    var kv = map[i].split(':');
    if (kv.length == 2 && kv[0].trim() == value.trim()) return kv[1].trim();
}
return value;"""


def poe_template():
    """POWER-ETHERNET-MIB (RFC 3621), for eKit S310 / Linksys PoE models."""
    t = POE
    lld = {
        "uuid": uid(t, "poe.discovery"),
        "name": "PoE ports discovery",
        "type": "SNMP_AGENT",
        "snmp_oid": "discovery[{#POE.ADMIN},1.3.6.1.2.1.105.1.1.1.3]",
        "key": "poe.port.discovery",
        "delay": "1h",
        "filter": {"evaltype": "AND",
                   "conditions": [{"macro": "{#POE.ADMIN}", "value": "^1$", "formulaid": "A"}]},
        "item_prototypes": [
            {
                "uuid": uid(t, "proto", "detect"),
                "name": "PoE port {#SNMPINDEX}: Detection status",
                "type": "SNMP_AGENT",
                "snmp_oid": "get[1.3.6.1.2.1.105.1.1.1.6.{#SNMPINDEX}]",
                "key": "poe.port.status[{#SNMPINDEX}]",
                # some firmware (Linksys LGS) numbers the states differently: {$POE.STATUS.MAP}
                # translates them to the standard ones, e.g. "1:2,2:3,3:4,4:1"
                "preprocessing": [{"type": "JAVASCRIPT", "parameters": [POE_MAP_JS]}, HEARTBEAT],
                "delay": "1m",
                "history": "{$RCA.HISTORY}",
                "trends": "0",
                "valuemap": {"name": "pethPsePortDetectionStatus"},
                "description": "3 = deliveringPower. A drop from 3 while the switch stays up = PoE/cable fault on that port.",
                "tags": tags(component="poe", rca="poe"),
                "trigger_prototypes": [
                    {
                        "uuid": uid(t, "proto", "poelost"),
                        "expression": f"last(/{t}/poe.port.status[{{#SNMPINDEX}}],#2)=3 and "
                                      f"last(/{t}/poe.port.status[{{#SNMPINDEX}}])<>3",
                        "recovery_mode": "RECOVERY_EXPRESSION",
                        "recovery_expression": f"last(/{t}/poe.port.status[{{#SNMPINDEX}}])=3",
                        "name": "PoE port {#SNMPINDEX}: Stopped delivering power",
                        "priority": "AVERAGE",
                        "manual_close": "YES",
                        "tags": tags(scope="availability"),
                    },
                ],
            },
        ],
    }
    items = [
        {
            "uuid": uid(t, "poe.power.used"),
            "name": "PoE: Total power consumption",
            "type": "SNMP_AGENT",
            "snmp_oid": "get[1.3.6.1.2.1.105.1.3.1.1.4.1]",
            "key": "poe.power.used",
            "value_type": "FLOAT",
            "preprocessing": [{"type": "MULTIPLIER", "parameters": ["{$POE.POWER.MULT}"]}],
            "delay": "5m",
            "history": "{$RCA.HISTORY}",
            "units": "W",
            "tags": tags(component="poe"),
        },
    ]
    return {
        "uuid": uid(t),
        "template": t,
        "name": t,
        "description": "PoE port state (POWER-ETHERNET-MIB). Link together with " + BASE + " on PoE switches.",
        "groups": [{"name": GROUP}],
        "items": items,
        "discovery_rules": [lld],
        "tags": [{"tag": "class", "value": "network"}, {"tag": "target", "value": "poe"}],
        "macros": [
            {"macro": "{$POE.STATUS.MAP}", "value": "",
             "description": "Translate non-standard PoE states, from:to pairs. Linksys LGS: 1:2,2:3,3:4,4:1"},
            {"macro": "{$POE.POWER.MULT}", "value": "1",
             "description": "Total PoE power multiplier to watts. Linksys LGS reports tenths: 0.1"},
        ],
        "valuemaps": [
            {"uuid": uid(t, "vm", "detect"), "name": "pethPsePortDetectionStatus",
             "mappings": [{"value": v, "newvalue": n} for v, n in
                          [("1", "disabled"), ("2", "searching"), ("3", "deliveringPower"),
                           ("4", "fault"), ("5", "test"), ("6", "otherFault")]]},
        ],
    }


# ---------------------------------------------------------------- Ubiquiti Wave Pro (airFiber MIB)
def wave_template():
    """Ubiquiti UISP Wave Pro (60 GHz PTP). OIDs confirmed by snmpwalk of a Wave Pro radio.

    Peer table .1.3.6.1.4.1.41112.1.11.1.3.1, one row per linked radio (index = peer MAC + .1):
      .3 local signal dBm (-57)   .18 remote signal dBm (-62)   .4 / .19 local / remote SNR (16 / 15)
      .5 / .6 TX / RX MCS (9)     .7 / .8 TX / RX capacity kbps (1751750)   .17 link uptime (timeticks)
    Link uptime resetting while the radio's own uptime keeps counting = the link dropped, not the power.
    """
    t = WAVE
    P = "1.3.6.1.4.1.41112.1.11.1.3.1"

    def proto(key, name, col, units="", value_type="FLOAT", rca=None, pre=None, triggers=None, delay="{$WIFI.INTERVAL}"):
        d = {"uuid": uid(t, key), "name": f"Link to {{#PEER}}: {name}", "type": "SNMP_AGENT",
             "snmp_oid": f"get[{P}.{col}.{{#SNMPINDEX}}]", "key": f"{key}[{{#SNMPINDEX}}]",
             "delay": delay, "history": "{$RCA.HISTORY}", "value_type": value_type, "units": units,
             "tags": tags(component="wireless", **({"rca": rca} if rca else {}))}
        if pre:
            d["preprocessing"] = pre
        if triggers:
            d["trigger_prototypes"] = triggers
        return d

    def tp(name, expr, priority, desc="", manual_close=False):
        d = {"uuid": uid(t, "trig", name), "expression": expr, "name": f"Link to {{#PEER}}: {name}",
             "priority": priority, "description": desc, "tags": tags(scope="performance")}
        if manual_close:
            d["manual_close"] = "YES"
        return d

    lld = {
        "uuid": uid(t, "lld"),
        "name": "Wave peers discovery",
        "type": "SNMP_AGENT",
        "snmp_oid": f"discovery[{{#PEER}},{P}.14,{{#PEERMODEL}},{P}.13]",
        "key": "wave.peer.discovery",
        "delay": "10m",
        "lifetime": "7d",
        "item_prototypes": [
            proto("wifi.ap.rssi", "Local signal", 3, "dBm", rca="rssi",
                  triggers=[tp("Signal low", f"max(/{t}/wifi.ap.rssi[{{#SNMPINDEX}}],10m)<{{$WAVE.RSSI.MIN}}",
                               "AVERAGE", "60 GHz signal below threshold for 10 minutes: rain fade, alignment or obstruction.")]),
            proto("wifi.remote.rssi", "Remote signal", 18, "dBm"),
            proto("wifi.ap.snr", "Local SNR", 4, "dB", rca="snr"),
            proto("wifi.remote.snr", "Remote SNR", 19, "dB"),
            proto("wifi.mcs.tx", "TX MCS", 5, value_type="UNSIGNED", rca="mcs"),
            proto("wifi.mcs.rx", "RX MCS", 6, value_type="UNSIGNED", rca="mcs"),
            proto("wifi.capacity.tx", "TX capacity", 7, "bps", value_type="UNSIGNED",
                  pre=[{"type": "MULTIPLIER", "parameters": ["1000"]}]),
            proto("wifi.capacity.rx", "RX capacity", 8, "bps", value_type="UNSIGNED",
                  pre=[{"type": "MULTIPLIER", "parameters": ["1000"]}]),
            proto("wifi.link.uptime", "Link uptime", 17, "uptime", value_type="UNSIGNED", rca="linkuptime",
                  pre=[{"type": "MULTIPLIER", "parameters": ["0.01"]}],
                  triggers=[tp("Link re-established (uptime below 10m)",
                               f"last(/{t}/wifi.link.uptime[{{#SNMPINDEX}}])<10m", "WARNING",
                               "The wireless link dropped and came back. If the radio's own uptime did not reset, "
                               "it was the link, not power.", manual_close=True)]),
        ],
    }
    items = [
        walk_item(t, "wifi.rssi", "Wireless: Signal (dBm)", f"{P}.3", WALK_FIRST_JS, "dBm", rca="rssi"),
        walk_item(t, "wifi.snr", "Wireless: SNR (dB)", f"{P}.4", WALK_FIRST_JS, "dB", rca="snr"),
        {"uuid": uid(t, "wave.frequency"), "name": "Wave: Frequency", "type": "SNMP_AGENT",
         "snmp_oid": "get[1.3.6.1.4.1.41112.1.11.1.1.2.1]", "key": "wave.frequency", "delay": "10m",
         "history": "{$RCA.HISTORY}", "units": "MHz", "tags": tags(component="wireless")},
        {"uuid": uid(t, "wave.width"), "name": "Wave: Channel width", "type": "SNMP_AGENT",
         "snmp_oid": "get[1.3.6.1.4.1.41112.1.11.1.1.3.1]", "key": "wave.width", "delay": "1h",
         "history": "{$RCA.HISTORY}", "units": "MHz", "tags": tags(component="wireless")},
    ]
    return {
        "uuid": uid(t), "template": t, "name": t,
        "description": "Ubiquiti UISP Wave Pro 60 GHz links (airFiber MIB). Link together with " + BASE + ".",
        "groups": [{"name": GROUP}],
        "items": items,
        "discovery_rules": [lld],
        "macros": [{"macro": "{$WIFI.INTERVAL}", "value": "1m"}, {"macro": "{$WAVE.RSSI.MIN}", "value": "-70"}],
        "tags": [{"tag": "class", "value": "network"}, {"tag": "target", "value": "ubiquiti"}],
    }


# ---------------------------------------------------------------- UniFi switches (Broadcom FASTPATH)
def unifi_template():
    """UniFi Pro Aggregation / UniFi switches. OIDs confirmed by snmpwalk of a UniFi Pro Aggregation.

    FASTPATH-BOXSERVICES-PRIVATE-MIB, .1.3.6.1.4.1.4413.1.1.43.1:
      .6.1  fans:          .3 state (2 operational, 3 failed), .4 speed rpm, .5 duty %
      .7.1  power supply:  .3 state (2 operational, 3 failed, 5 no power)
      .8.1  temperatures:  .4 state (1 normal, 2 warning, 3 critical), .5 temperature C
    No hrSystemUptime on these switches: the base template's sysUpTime is used for RCA.
    """
    t = UNIFI
    B = "1.3.6.1.4.1.4413.1.1.43.1"
    item_state = [("1", "notpresent"), ("2", "operational"), ("3", "failed"), ("4", "powering"),
                  ("5", "nopower"), ("6", "notpowering"), ("7", "incompatible")]

    def lld(key, name, disc_col, protos):
        return {"uuid": uid(t, "lld", key), "name": name, "type": "SNMP_AGENT",
                "snmp_oid": f"discovery[{{#STATE}},{B}.{disc_col}]", "key": key, "delay": "1h",
                "lifetime": "7d", "item_prototypes": protos}

    def proto(key, name, oid, units="", valuemap=None, rca=None, triggers=None, delay="1m"):
        d = {"uuid": uid(t, key), "name": name, "type": "SNMP_AGENT", "snmp_oid": f"get[{oid}.{{#SNMPINDEX}}]",
             "key": f"{key}[{{#SNMPINDEX}}]", "delay": delay, "history": "{$RCA.HISTORY}", "units": units,
             "tags": tags(component="hardware", **({"rca": rca} if rca else {}))}
        if valuemap:
            d["valuemap"] = {"name": valuemap}
            d["trends"] = "0"
            d["preprocessing"] = [HEARTBEAT]
        if triggers:
            d["trigger_prototypes"] = triggers
        return d

    def tp(name, expr, priority, desc=""):
        return {"uuid": uid(t, "trig", name), "expression": expr, "name": name, "priority": priority,
                "description": desc, "tags": tags(scope="availability")}

    fans = lld("unifi.fan.discovery", "Fans discovery", "6.1.3", [
        proto("unifi.fan.state", "Fan {#SNMPINDEX}: State", f"{B}.6.1.3", valuemap="FASTPATH item state",
              triggers=[tp("Fan {#SNMPINDEX} failed", f"last(/{t}/unifi.fan.state[{{#SNMPINDEX}}])=3", "AVERAGE")]),
        proto("unifi.fan.speed", "Fan {#SNMPINDEX}: Speed", f"{B}.6.1.4", "rpm", delay="5m"),
    ])
    psus = lld("unifi.psu.discovery", "Power supplies discovery", "7.1.3", [
        proto("unifi.psu.state", "Power supply {#SNMPINDEX}: State", f"{B}.7.1.3", valuemap="FASTPATH item state",
              rca="psu",
              triggers=[tp("Power supply {#SNMPINDEX}: failed or no power",
                           f"last(/{t}/unifi.psu.state[{{#SNMPINDEX}}])=3 or last(/{t}/unifi.psu.state[{{#SNMPINDEX}}])=5",
                           "HIGH", "A power supply lost input power or failed.")]),
    ])
    temps = lld("unifi.temp.discovery", "Temperature sensors discovery", "8.1.4", [
        proto("unifi.temp.value", "Temperature sensor {#SNMPINDEX}", f"{B}.8.1.5", "C",
              triggers=[tp("Temperature sensor {#SNMPINDEX} above {$UNIFI.TEMP.MAX}C",
                           f"min(/{t}/unifi.temp.value[{{#SNMPINDEX}}],5m)>{{$UNIFI.TEMP.MAX}}", "AVERAGE")]),
        proto("unifi.temp.state", "Temperature sensor {#SNMPINDEX}: State", f"{B}.8.1.4", valuemap="FASTPATH temp state",
              triggers=[tp("Temperature sensor {#SNMPINDEX}: critical",
                           f"last(/{t}/unifi.temp.state[{{#SNMPINDEX}}])>=3 and last(/{t}/unifi.temp.state[{{#SNMPINDEX}}])<=4",
                           "HIGH")]),
    ])
    return {
        "uuid": uid(t), "template": t, "name": t,
        "description": "UniFi switches (FASTPATH box services: fans, PSU, temperatures). Link together with " + BASE + ".",
        "groups": [{"name": GROUP}],
        "items": [{"uuid": uid(t, "unifi.model"), "name": "UniFi: Model", "type": "SNMP_AGENT",
                   "snmp_oid": "get[1.3.6.1.4.1.4413.1.1.1.1.1.2.0]", "key": "unifi.model", "delay": "1h",
                   "history": "7d", "trends": "0", "value_type": "CHAR", "inventory_link": "MODEL",
                   "tags": tags(component="system")},
                  {"uuid": uid(t, "unifi.firmware"), "name": "UniFi: Firmware", "type": "SNMP_AGENT",
                   "snmp_oid": "get[1.3.6.1.4.1.4413.1.1.1.1.1.13.0]", "key": "unifi.firmware", "delay": "1h",
                   "history": "7d", "trends": "0", "value_type": "CHAR", "inventory_link": "OS_SHORT",
                   "tags": tags(component="system")}],
        "discovery_rules": [fans, psus, temps],
        "macros": [{"macro": "{$UNIFI.TEMP.MAX}", "value": "75"}],
        "tags": [{"tag": "class", "value": "network"}, {"tag": "target", "value": "unifi"}],
        "valuemaps": [
            {"uuid": uid(t, "vm", "item"), "name": "FASTPATH item state",
             "mappings": [{"value": v, "newvalue": n} for v, n in item_state]},
            {"uuid": uid(t, "vm", "temp"), "name": "FASTPATH temp state",
             "mappings": [{"value": v, "newvalue": n} for v, n in
                          [("0", "low"), ("1", "normal"), ("2", "warning"), ("3", "critical"), ("4", "shutdown"),
                           ("5", "notpresent"), ("6", "notoperational")]]},
        ],
    }


# ---------------------------------------------------------------- UPS (RFC 1628 UPS-MIB)
def ups_template():
    """Huawei UPS2000G network cards (probe of the SITE0011 card, 2026-10-26).

    Standard UPS-MIB (1.3.6.1.2.1.33) answers, but only partly: upsOutputSource (3 normal, 5 battery) and
    secondsOnBattery are right; battery status is missing and charge / runtime read 0. Those come from
    HUAWEI-UPS-MIB (1.3.6.1.4.1.2011.6.174.1, index 1 = the UPS) instead:
      .2.101.1.3 battery state   .6.100.1.3 capacity left %   .6.100.1.4 backup time s
      .3.100.1.1 input voltage 0.1 V   .4.100.1.14 output load 0.1 %
    upsOutputSource = 5 (battery) is direct evidence of a mains power cut; the RCA script uses it.
    """
    t = UPS
    H = "1.3.6.1.4.1.2011.6.174.1"

    def it(key, name, oid, units="", value_type="UNSIGNED", rca=None, valuemap=None, triggers=None, delay="1m",
           mult=None):
        d = {"uuid": uid(t, key), "name": name, "type": "SNMP_AGENT", "snmp_oid": f"get[{oid}]", "key": key,
             "delay": delay, "history": "{$RCA.HISTORY}", "value_type": "FLOAT" if mult else value_type,
             "units": units, "tags": tags(component="power", **({"rca": rca} if rca else {}))}
        if mult:
            d["preprocessing"] = [{"type": "MULTIPLIER", "parameters": [mult]}]
        if valuemap:
            d["valuemap"] = {"name": valuemap}
            d["trends"] = "0"
        if triggers:
            d["triggers"] = triggers
        return d

    items = [
        it("ups.output.source", "UPS: Output source", "1.3.6.1.2.1.33.1.4.1.0", rca="ups.source",
           valuemap="upsOutputSource",
           triggers=[trigger(t, "UPS: On battery (mains power lost)", f"last(/{t}/ups.output.source)=5", "AVERAGE",
                             "The site has lost mains power and is running on battery."),
                     trigger(t, "UPS: On bypass", f"last(/{t}/ups.output.source)=4", "AVERAGE")]),
        it("ups.battery.onbattery", "UPS: Time on battery", "1.3.6.1.2.1.33.1.2.2.0", "s", rca="ups.onbattery"),
        it("ups.battery.status", "UPS: Battery state", f"{H}.2.101.1.3.1", valuemap="Huawei UPS battery state"),
        it("ups.battery.charge", "UPS: Battery charge", f"{H}.6.100.1.3.1", "%"),
        it("ups.battery.minutes", "UPS: Estimated runtime", f"{H}.6.100.1.4.1", "m", mult="0.0166667"),
        it("ups.input.voltage", "UPS: Input voltage", f"{H}.3.100.1.1.1", "V", rca="ups.vin", mult="0.1"),
        it("ups.output.load", "UPS: Output load", f"{H}.4.100.1.14.1", "%", delay="5m", mult="0.1"),
    ]
    on_batt = f"last(/{t}/ups.output.source)=5"
    triggers = [
        trigger(t, "UPS: Battery charge below {$UPS.CHARGE.MIN}% on battery",
                f"last(/{t}/ups.battery.charge)<{{$UPS.CHARGE.MIN}} and {on_batt}", "AVERAGE",
                "Devices at the site will lose power soon."),
        trigger(t, "UPS: Runtime below {$UPS.RUNTIME.MIN} minutes on battery",
                f"last(/{t}/ups.battery.minutes)<{{$UPS.RUNTIME.MIN}} and {on_batt}", "AVERAGE"),
    ]
    return {
        "uuid": uid(t), "template": t, "name": t,
        "description": "Huawei UPS2000G network card (UPS-MIB output source + HUAWEI-UPS-MIB readings). "
                       "Link together with " + BASE + ".",
        "groups": [{"name": GROUP}],
        "items": items,
        "triggers": triggers,
        "macros": [{"macro": "{$UPS.RUNTIME.MIN}", "value": "10"}, {"macro": "{$UPS.CHARGE.MIN}", "value": "30"}],
        "tags": [{"tag": "class", "value": "power"}, {"tag": "target", "value": "ups"}],
        "valuemaps": [
            {"uuid": uid(t, "vm", "src"), "name": "upsOutputSource",
             "mappings": [{"value": v, "newvalue": n} for v, n in
                          [("1", "other"), ("2", "none"), ("3", "normal"), ("4", "bypass"), ("5", "battery"),
                           ("6", "booster"), ("7", "reducer")]]},
            {"uuid": uid(t, "vm", "hwbat"), "name": "Huawei UPS battery state",
             "mappings": [{"value": v, "newvalue": n} for v, n in
                          [("1", "not present"), ("2", "temporary"), ("3", "sleep"), ("4", "float charge"),
                           ("5", "boost charge"), ("6", "discharging")]]},
        ],
    }

# ---------------------------------------------------------------- Zabbix server guard
def guard_template():
    """Alarms before a Zabbix cache fills (a full configuration cache stops the server), when processes are
    saturated, and when the nightly archive / backup (tools/zbx_data.py) did not run or disks fill up.
    Internal items: link it to a host monitored by the server itself (zbx_sites.py guard creates one)."""
    t = GUARD

    def it(key, name, units="%", value_type="FLOAT", delay="1m", triggers=None, pre=None, desc="", typ="INTERNAL",
           history="30d"):
        d = {"uuid": uid(t, key), "name": name, "type": typ, "key": key, "delay": delay if typ != "TRAP" else "",
             "history": history, "value_type": value_type, "units": units, "description": desc,
             "tags": tags(component="zabbix")}
        if pre:
            d["preprocessing"] = pre
        if triggers:
            d["triggers"] = triggers
        return d

    def cache(key, label, conf):
        e = f"/{t}/{key}"
        warn = trigger(t, f"Zabbix {label} over {{$CACHE.WARN}}% used", f"min({e},10m)>{{$CACHE.WARN}}", "WARNING",
                       f"Raise {conf} in zabbix_server.conf (see OPTIMIZE.md) at the next maintenance window.")
        high = trigger(t, f"Zabbix {label} over {{$CACHE.HIGH}}% used", f"min({e},5m)>{{$CACHE.HIGH}}", "HIGH",
                       f"Raise {conf} now and restart zabbix-server." +
                       (" When this cache is full the server stops." if conf == "CacheSize" else ""))
        warn["dependencies"] = [{"name": high["name"], "expression": high["expression"]}]
        for x in (warn, high):
            x["tags"] = tags(scope="capacity")
        return it(key, f"Cache: {label} % used", triggers=[warn, high], desc=f"zabbix_server.conf {conf}")

    def busy(proc, conf):
        e = f"/{t}/zabbix[process,{proc},avg,busy]"
        tr = trigger(t, f"Zabbix {proc} processes over {{$BUSY.WARN}}% busy", f"avg({e},15m)>{{$BUSY.WARN}}",
                     "WARNING", f"Raise {conf} in zabbix_server.conf." if conf else "")
        tr["tags"] = tags(scope="capacity")
        return it(f"zabbix[process,{proc},avg,busy]", f"Busy: {proc} %", triggers=[tr])

    def age(key, name, what, nodata=True):
        e = f"/{t}/{key}"
        tr = trigger(t, f"{what} has not succeeded for {{$JOB.MAXAGE}}",
                     f"now()-last({e})>{{$JOB.MAXAGE}}" + (f" or nodata({e},{{$JOB.MAXAGE}})=1" if nodata else ""),
                     "HIGH", "Check /var/log/mdp-zabbix-data.log on the Zabbix server (RETENTION.md).")
        tr["tags"] = tags(scope="retention")
        return it(key, name, "unixtime", "UNSIGNED", typ="TRAP", history="365d",
                  desc="Pushed by tools/zbx_data.py after each successful run.", triggers=[tr])

    def pfree(key, name):
        e = f"/{t}/{key}"
        w = trigger(t, f"{name} below {{$DISK.PFREE.WARN}}% free", f"last({e})<{{$DISK.PFREE.WARN}}", "WARNING")
        h = trigger(t, f"{name} below {{$DISK.PFREE.HIGH}}% free", f"last({e})<{{$DISK.PFREE.HIGH}}", "HIGH",
                    "PostgreSQL stops when its disk is full. Free space now (RETENTION.md: disk full).")
        w["dependencies"] = [{"name": h["name"], "expression": h["expression"]}]
        for x in (w, h):
            x["tags"] = tags(scope="capacity")
        return it(key, name + " free", "%", typ="TRAP", history="365d", triggers=[w, h])

    vmode = trigger(t, "Zabbix value cache ran out of memory", f"last(/{t}/zabbix[vcache,cache,mode])=1", "HIGH",
                    "The value cache is in low-memory mode: triggers read from the database and get slow. "
                    "Raise ValueCacheSize.")
    queue = trigger(t, "Zabbix: more than {$QUEUE.MAX} items late by 10 min", f"min(/{t}/zabbix[queue,10m],10m)>"
                    "{$QUEUE.MAX}", "WARNING", "Pollers cannot keep up, or many devices are unreachable.")
    prep = trigger(t, "Zabbix preprocessing queue is growing", f"min(/{t}/zabbix[preprocessing_queue],10m)>1000",
                   "WARNING", "Raise StartPreprocessors, or the history syncers cannot write fast enough.")
    items = [
        cache("zabbix[rcache,buffer,pused]", "configuration cache", "CacheSize"),
        cache("zabbix[wcache,history,pused]", "history cache", "HistoryCacheSize"),
        cache("zabbix[wcache,index,pused]", "history index cache", "HistoryIndexCacheSize"),
        cache("zabbix[wcache,trend,pused]", "trend cache", "TrendCacheSize"),
        cache("zabbix[vcache,buffer,pused]", "value cache", "ValueCacheSize"),
        it("zabbix[vcache,cache,mode]", "Cache: value cache low-memory mode", "", "UNSIGNED", triggers=[vmode]),
        it("zabbix[wcache,values]", "Values processed per second", "", pre=[{"type": "CHANGE_PER_SECOND",
                                                                               "parameters": [""]}]),
        it("zabbix[queue,10m]", "Items late by more than 10 min", "", "UNSIGNED", triggers=[queue]),
        it("zabbix[preprocessing_queue]", "Preprocessing queue", "", "UNSIGNED", triggers=[prep]),
        busy("poller", "StartPollers"), busy("snmp poller", "StartSNMPPollers"),
        busy("icmp pinger", "StartPingers"), busy("unreachable poller", "StartPollersUnreachable"),
        busy("history syncer", "StartDBSyncers"), busy("preprocessing worker", "StartPreprocessors"),
        busy("lld worker", "StartLLDProcessors"), busy("discovery worker", "StartDiscoverers"),
        busy("trapper", "StartTrappers"), busy("configuration syncer", ""),
        it("zabbix[process,housekeeper,avg,busy]", "Busy: housekeeper %"),
        it("mdp.db.size", "Database size", "B", "UNSIGNED", typ="TRAP", history="365d",
           desc="Pushed by tools/zbx_data.py status (every 10 min)."),
        it("mdp.history.days", "Oldest history kept (days)", "d", typ="TRAP", history="365d"),
        pfree("mdp.disk.pfree[db]", "Database disk"),
        pfree("mdp.disk.pfree[archive]", "Archive disk"),
        age("mdp.archive.last", "Last successful history archive", "Daily history archive"),
        age("mdp.backup.last", "Last successful database backup", "Nightly database backup"),
        # only sent when COPY_TO is set, so no nodata(): an unset COPY_TO is not an alarm
        age("mdp.copy.last", "Last successful off-server copy", "Off-server copy of archive and backups", nodata=False),
    ]
    caches = ["zabbix[rcache,buffer,pused]", "zabbix[wcache,history,pused]", "zabbix[wcache,index,pused]",
              "zabbix[wcache,trend,pused]", "zabbix[vcache,buffer,pused]"]
    procs = ["poller", "snmp poller", "icmp pinger", "unreachable poller", "history syncer", "preprocessing worker",
             "lld worker"]
    colors = ["1A7C11", "F63100", "2774A4", "A54F10", "FC6EA3", "6C59DC", "AC8C14"]

    def graph(name, keys, ymax=None):
        g = {"uuid": uid(t, "graph", name), "name": name,
             "graph_items": [{"sortorder": str(i), "color": colors[i % len(colors)], "item": {"host": t, "key": k}}
                             for i, k in enumerate(keys)]}
        if ymax:
            g.update({"ymin_type_1": "FIXED", "ymax_type_1": "FIXED", "yaxismax": ymax})
        return g

    graphs = [graph("Zabbix cache usage", caches, "100"),
              graph("Zabbix process busy", [f"zabbix[process,{p},avg,busy]" for p in procs], "100"),
              graph("Zabbix values per second", ["zabbix[wcache,values]"]),
              graph("Disk free", ["mdp.disk.pfree[db]", "mdp.disk.pfree[archive]"], "100")]
    return {
        "uuid": uid(t), "template": t, "name": t,
        "description": guard_template.__doc__.strip(),
        "groups": [{"name": GUARD_GROUP}],
        "items": items,
        "macros": [
            {"macro": "{$CACHE.WARN}", "value": "70", "description": "% used: plan a cache increase"},
            {"macro": "{$CACHE.HIGH}", "value": "85", "description": "% used: raise the cache now"},
            {"macro": "{$BUSY.WARN}", "value": "75"},
            {"macro": "{$QUEUE.MAX}", "value": "100"},
            {"macro": "{$DISK.PFREE.WARN}", "value": "25"},
            {"macro": "{$DISK.PFREE.HIGH}", "value": "10"},
            {"macro": "{$JOB.MAXAGE}", "value": "30h", "description": "Daily jobs: alarm when older than this"},
        ],
        "tags": [{"tag": "class", "value": "software"}, {"tag": "target", "value": "zabbix-server"}],
    }, graphs


# ---------------------------------------------------------------- site status
def site_template():
    """Linked to one host per site ("Site SITExxxx", made by zbx_sites.py sitedown from data/main_links.txt).
    Watches the site's main link and counts how many of the site's devices answer ping, and raises one
    "Site SITExxxx DOWN" problem when the main link is lost and most of the site went with it. The device
    alarms of the site, and the Site down alarms of the sites behind it, depend on it.
    Host macros set by sitedown: {$SITE.CODE}, {$SITE.MAINLINK} (text), {$SITE.CUTOFF} (text)."""
    t = SITE
    grp = 'group="Sites/{$SITE.CODE}"'

    def calc(key, name, formula, units="", value_type="FLOAT", desc=""):
        return {"uuid": uid(t, key), "name": name, "type": "CALCULATED", "key": key, "delay": "30s",
                "history": "{$RCA.HISTORY}", "value_type": value_type, "units": units, "params": formula,
                "description": desc, "tags": tags(component="site")}

    e = f"/{t}"
    down = trigger(t, "Site {$SITE.CODE} DOWN: main link {$SITE.MAINLINK} lost{$SITE.CUTOFF}",
                   f"last({e}/site.mainlink.up)=0 and last({e}/site.devices.down.pct)>={{$SITE.DOWN.PCT}}"
                   " and {$SITE.DISASTER}=0",
                   "HIGH",
                   "The site's main link has not answered ping for {$SITE.DOWN.TIME} and most of the site's devices "
                   "are down with it, so the whole site is cut off. The device alarms at this site and the sites "
                   "behind it are held back: this is the one problem to work on. Open the RCA report for the cause "
                   "(power: the far-end radio rebooted; link: it kept its uptime).",
                   recovery=f"last({e}/site.mainlink.up)=1", alarm="site-down")
    down["event_name"] = ("Site {$SITE.CODE} DOWN: main link {$SITE.MAINLINK} lost, "
                          "{?last(//site.devices.down)} of {?last(//site.devices)} devices down"
                          "{$SITE.CUTOFF}")
    down["opdata"] = "{ITEM.LASTVALUE2} of devices down"
    # SEVERITY-1: the same alarm at Disaster for key sites ({$SITE.DISASTER}=1 on the Site host, zbx_sites.py)
    dis = {**down, "uuid": uid(t, "trigger", down["name"], "disaster"), "priority": "DISASTER",
           "expression": down["expression"].replace("{$SITE.DISASTER}=0", "{$SITE.DISASTER}=1")}
    # the Site DOWN above waits on the upstream site's (one root alarm), so a site cut off behind it would stay
    # green: this one shows it on the map and in Problems as "down via upstream", lower severity, not the cause
    cut = trigger(t, "Site {$SITE.CODE} DOWN via upstream {$SITE.UPSTREAM} (cut off, not the cause)",
                  f"last({e}/site.upstream.down)>=1 and last({e}/site.mainlink.up)=0 "
                  f"and last({e}/site.devices.down.pct)>={{$SITE.DOWN.PCT}}",
                  "HIGH",
                  "This site is down because the site it hangs off ({$SITE.UPSTREAM}) lost its main link. Work on "
                  "the upstream Site DOWN alarm; this one clears by itself when the path comes back. If it turns "
                  "into a 'Site DOWN' alarm after the upstream recovers, this site has its own fault too.",
                  recovery=f"last({e}/site.upstream.down)=0 or last({e}/site.mainlink.up)=1", alarm="site-cutoff")
    cut["event_name"] = ("Site {$SITE.CODE} DOWN via upstream {$SITE.UPSTREAM}: cut off, "
                         "{?last(//site.devices.down)} of {?last(//site.devices)} devices down")
    items = [
        calc("site.devices", "Devices monitored", f"count(last_foreach(/*/icmpping?[{grp}]))", value_type="UNSIGNED"),
        calc("site.devices.down", "Devices down", f'count(last_foreach(/*/icmpping?[{grp}]),"eq",0)',
             value_type="UNSIGNED"),
        calc("site.devices.down.pct", "Devices down %", "last(//site.devices.down)*100/max(last(//site.devices),1)", "%"),
        # max over a window shorter than {$ICMP.DOWN.TIME}: the site alarm comes before the devices' own alarms
        calc("site.mainlink.up", "Main link up",
             'max(max_foreach(/*/icmpping?[tag="mainlink:{$SITE.CODE}"],{$SITE.DOWN.TIME}))',
             desc="1 while any far-end radio of the main link (host tag mainlink=<site>) answered ping in the last "
                  "{$SITE.DOWN.TIME}. Two radios = LACP: the link is down only when both are."),
        # own main link is always in the filter, so the item never runs out of data at a site with no upstream
        calc("site.upstream.down", "Upstream site main link down",
             'count(last_foreach(/*/site.mainlink.up?[tag="sitedown:{$SITE.UPSTREAM}" or tag="sitedown:{$SITE.CODE}"]),"eq",0)'
             '-(last(//site.mainlink.up)=0)', value_type="UNSIGNED",
             desc="1 while the main link of the upstream site ({$SITE.UPSTREAM}, host 'Site <upstream>') is down."),
    ]
    items[3]["valuemap"] = {"name": "Link state"}
    return {
        "uuid": uid(t), "template": t, "name": t,
        "description": site_template.__doc__.strip(),
        "groups": [{"name": GROUP}],
        "items": items,
        "valuemaps": [{"uuid": uid(t, "valuemap", "Link state"), "name": "Link state",
                       "mappings": [{"value": "0", "newvalue": "Down"}, {"value": "1", "newvalue": "Up"}]}],
        "macros": [
            {"macro": "{$SITE.CODE}", "value": "", "description": "Site code, e.g. SITE0007 (set per host)"},
            {"macro": "{$SITE.MAINLINK}", "value": "", "description": "Main link text for the alarm (set per host)"},
            {"macro": "{$SITE.CUTOFF}", "value": "", "description": "'. Also cut off: ...' sites behind this one"},
            {"macro": "{$SITE.UPSTREAM}", "value": "", "description": "Site this one hangs off (set per host)"},
            {"macro": "{$SITE.DISASTER}", "value": "0", "description": "1 = Site DOWN is Disaster, 0 = High (set per host)"},
            {"macro": "{$SITE.DOWN.TIME}", "value": "2m",
             "description": "Main link silent this long = site down. Keep below {$ICMP.DOWN.TIME} (3m)"},
            {"macro": "{$SITE.DOWN.PCT}", "value": "80", "description": "% of the site's devices that must be down too"},
            {"macro": "{$RCA.HISTORY}", "value": "30d"},
        ],
        "tags": [{"tag": "class", "value": "site"}],
    }, [down, dis, cut]


# ---------------------------------------------------------------- LACP trunks (IEEE8023-LAG-MIB)
LAG = "1.2.840.10006.300.43.1"
# walk text -> {"aggs": {ifIndex: {"n": name, "k": admin key}}, "ports": {ifIndex: {"n", "k", "a": actor state, "o": oper}}}
LACP_PARSE = r"""
var cols = {'1.3.6.1.2.1.31.1.1.1.1': 'name', '1.3.6.1.2.1.2.2.1.8': 'oper',
            '1.2.840.10006.300.43.1.1.1.1.6': 'aggkey', '1.2.840.10006.300.43.1.2.1.1.4': 'portkey',
            '1.2.840.10006.300.43.1.2.1.1.21': 'actor'};
var t = {name: {}, oper: {}, aggkey: {}, portkey: {}, actor: {}};
value.split('\n').forEach(function (line) {
    var m = line.match(/^\.?([0-9.]+)\.([0-9]+) = ([A-Za-z0-9-]+): ?(.*)$/);
    if (!m || !cols[m[1]]) return;
    var v = m[4].replace(/^"|"\s*$/g, '').trim();
    if (m[3] === 'Hex-STRING') v = parseInt(v.split(' ')[0], 16);
    else if (m[3] === 'STRING' && cols[m[1]] === 'actor') v = v.length ? v.charCodeAt(0) : 0;
    t[cols[m[1]]][m[2]] = v;
});
var out = {aggs: {}, ports: {}};
Object.keys(t.aggkey).forEach(function (i) {
    out.aggs[i] = {n: t.name[i] || ('trunk ' + i), k: String(t.aggkey[i])};
});
Object.keys(t.portkey).forEach(function (i) {
    out.ports[i] = {n: t.name[i] || ('port ' + i), k: String(t.portkey[i]),
                    a: Number(t.actor[i] || 0), o: Number(t.oper[i] || 0)};
});
return JSON.stringify(out);
"""
# a port is in use when the link is up and LACP has it in sync, collecting and distributing (state bits 0x1C)
LACP_PORTS = r"""
var d = JSON.parse(value), agg = d.aggs['{#AGGINDEX}'];
if (!agg) throw 'trunk {#AGGNAME} is gone';
var all = [], used = [], idle = [];
Object.keys(d.ports).forEach(function (i) {
    var p = d.ports[i];
    if (p.k !== agg.k) return;
    all.push(p.n);
    if (p.o === 1 && (p.a & 0x1C) === 0x1C) used.push(p.n); else idle.push(p.n);
});
"""


def lacp_template():
    """LACP trunks (Eth-Trunk in LACP mode) on Huawei eKit switches. OIDs confirmed by snmpwalk of two
    eKit switches, 2026-10-29.

    The radios inside an LACP trunk cannot be reached reliably (the trunk's hashing sends their management
    packets down the other link), so the trunk is watched here instead. A radio that loses its air link keeps
    its cable up: only the LACP state shows it (dot3adAggPortActorOperState, bits sync/collecting/distributing).
    One SNMP walk a minute feeds every item. A port belongs to a trunk by its LACP admin key; static trunks
    (no LACP) have no ports in the LAG MIB and are not discovered.
    Host macro {$LACP.LABEL:"Eth-Trunk1"} (set by zbx_sites.py sitedown) names what the trunk carries."""
    t = LACP
    e = f"/{t}"
    oids = ",".join(["1.3.6.1.2.1.31.1.1.1.1", "1.3.6.1.2.1.2.2.1.8", f"{LAG}.1.1.1.6", f"{LAG}.2.1.1.4",
                     f"{LAG}.2.1.1.21"])
    master = {"uuid": uid(t, "lacp.walk"), "name": "LACP: raw table", "type": "SNMP_AGENT",
              "snmp_oid": f"walk[{oids}]", "key": "lacp.walk", "delay": "1m", "history": "0", "trends": "0",
              "value_type": "TEXT", "preprocessing": [{"type": "JAVASCRIPT", "parameters": [LACP_PARSE]}],
              "tags": tags(component="lacp")}

    def dep(key, name, js, value_type="UNSIGNED", units="", heartbeat=True):
        d = {"uuid": uid(t, key), "name": name, "type": "DEPENDENT", "key": f"{key}[{{#AGGINDEX}}]",
             "history": "{$RCA.HISTORY}", "value_type": value_type, "units": units,
             "master_item": {"key": "lacp.walk"},
             "preprocessing": [{"type": "JAVASCRIPT", "parameters": [LACP_PORTS + js]}] + ([HEARTBEAT] if heartbeat else []),
             "tags": tags(component="lacp", trunk="{#AGGNAME}", rca="lacp")}
        if value_type == "TEXT":
            d["trends"] = "0"
        return d

    k = lambda key: f"{e}/{key}[{{#AGGINDEX}}]"
    label = '{$LACP.LABEL:"{#AGGNAME}"}'

    def tp(name, expr, priority, desc, recovery=None):
        d = {"uuid": uid(t, "trig", name), "expression": expr, "name": name, "priority": priority,
             "description": desc, "opdata": "In use: {ITEM.LASTVALUE1}", "tags": tags(scope="availability", alarm="lacp")}
        if recovery:
            d["recovery_mode"] = "RECOVERY_EXPRESSION"
            d["recovery_expression"] = recovery
        return d

    trunks = {
        "uuid": uid(t, "lld", "lacp.trunk.discovery"), "name": "LACP trunks discovery", "type": "DEPENDENT",
        "key": "lacp.trunk.discovery", "master_item": {"key": "lacp.walk"}, "lifetime": "30d",
        "preprocessing": [{"type": "JAVASCRIPT", "parameters": [r"""
var d = JSON.parse(value), out = [];
Object.keys(d.aggs).forEach(function (i) {
    var n = 0;
    Object.keys(d.ports).forEach(function (p) { if (d.ports[p].k === d.aggs[i].k) n++; });
    if (n) out.push({'{#AGGINDEX}': i, '{#AGGNAME}': d.aggs[i].n, '{#MEMBERS}': n});
});
return JSON.stringify(out);"""]},
            {"type": "DISCARD_UNCHANGED_HEARTBEAT", "parameters": ["1h"]}],
        "item_prototypes": [
            # every value kept (no heartbeat): the alarms take max/min over {$LACP.TIME}, which needs values in the window
            dep("lacp.active", "LACP {#AGGNAME}: links in use", "return used.length;", heartbeat=False),
            dep("lacp.members", "LACP {#AGGNAME}: links configured", "return all.length;"),
            dep("lacp.idle", "LACP {#AGGNAME}: links not in use", "return idle.length ? idle.join(', ') : 'none';",
                value_type="CHAR"),
        ],
        "trigger_prototypes": [
            tp("LACP {#AGGNAME} degraded: {ITEM.LASTVALUE4} not in use" + label,
               f"max({k('lacp.active')},{{$LACP.TIME}})<last({k('lacp.members')}) and "
               f"last({k('lacp.active')})>0 and last({k('lacp.idle')})<>\"none\"",
               "AVERAGE",
               "One link of the trunk is down (port down, or the radio on it lost its air link: LACP stops using it "
               "within 3 s with the fast timer). Traffic runs on the other link, at half the capacity and with no "
               "spare. Check the radio pair on the port named in the problem.",
               recovery=f"min({k('lacp.active')},{{$LACP.TIME}})=last({k('lacp.members')})"),
            tp("LACP {#AGGNAME}: all links down" + label,
               f"max({k('lacp.active')},{{$LACP.TIME}})=0", "HIGH",
               "No link of the trunk is in use: everything behind it is cut off. The Site down alarm of that site "
               "follows; this is its cause on the near side.",
               recovery=f"last({k('lacp.active')})>0"),
            tp("LACP {#AGGNAME}: links flapping" + label,
               f"changecount({k('lacp.active')},1h)>={{$LACP.FLAPS}}", "WARNING",
               "The number of links in use changed {$LACP.FLAPS} or more times in an hour: a radio link that keeps "
               "dropping (interference, alignment, power).",
               recovery=f"changecount({k('lacp.active')},1h)=0"),
        ],
    }
    # degraded: functions in order max(active), last(members), last(active), last(idle) -> {ITEM.LASTVALUE4} = idle ports
    trunks["trigger_prototypes"][0]["dependencies"] = [
        {"name": "LACP {#AGGNAME}: all links down" + label, "expression": trunks["trigger_prototypes"][1]["expression"],
         "recovery_expression": trunks["trigger_prototypes"][1]["recovery_expression"]}]
    return {
        "uuid": uid(t), "template": t, "name": t,
        "description": lacp_template.__doc__.strip(),
        "groups": [{"name": GROUP}],
        "items": [master],
        "discovery_rules": [trunks],
        "macros": [
            {"macro": "{$LACP.TIME}", "value": "2m", "description": "a link must stay out this long to alarm"},
            {"macro": "{$LACP.FLAPS}", "value": "4", "description": "changes per hour that count as flapping"},
            {"macro": "{$LACP.LABEL}", "value": "",
             "description": 'what a trunk carries, per trunk: {$LACP.LABEL:"Eth-Trunk1"} = " (SITE0008 main link)"'},
            {"macro": "{$RCA.HISTORY}", "value": "30d"},
        ],
        "tags": [{"tag": "class", "value": "network"}, {"tag": "target", "value": "lacp"}],
    }


def prune(o):
    if isinstance(o, dict):
        return {k: prune(v) for k, v in o.items() if v != ""}
    if isinstance(o, list):
        return [prune(v) for v in o]
    return o


def main():
    base, base_triggers = base_template()
    epmp = epmp_template()
    epmp_triggers = epmp.pop("triggers")  # multi-item triggers go to the export's top level
    ups = ups_template()
    ups_triggers = ups.pop("triggers")
    guard, guard_graphs = guard_template()
    site, site_triggers = site_template()
    export = {
        "zabbix_export": {
            "version": "7.0",
            "template_groups": [{"uuid": uid("group", GROUP), "name": GROUP},
                                {"uuid": uid("group", GUARD_GROUP), "name": GUARD_GROUP}],
            "templates": [base, epmp, poe_template(), ups, wave_template(), unifi_template(), lacp_template(), guard, site],
            "triggers": base_triggers + epmp_triggers + ups_triggers + site_triggers,
            "graphs": guard_graphs,
        }
    }
    # multi-line strings (JavaScript) as literal blocks: a folded double-quoted line can split an escape like '\\.'
    yaml.SafeDumper.add_representer(str, lambda d, v: d.represent_scalar(
        "tag:yaml.org,2002:str", v, style="|" if "\n" in v and not v.endswith(" ") else None))
    yaml.safe_dump(prune(export), sys.stdout, sort_keys=False, allow_unicode=True, width=200)


if __name__ == "__main__":
    main()
