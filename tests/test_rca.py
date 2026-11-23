"""Offline checks of the RCA rules with synthetic data: python3 tests/test_rca.py"""
import os
os.environ.setdefault("RCA_TZ", "Etc/GMT-3")  # tests assume a UTC+3 day
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import zbx_rca as r  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

r.TZ = ZoneInfo("UTC")
T0, T1 = 0, 4 * 3600
W = (3600, 7200)


def host(name, parent="", role="", down=None, reboot_at=None, rssi=None, poe_drop=None, battery=None,
         external=False):
    """down=(start, end) or a list of them; reboot_at = boot clock after which uptime restarts."""
    h = r.Host(name, name, name, role=role, parent=parent)
    boot = -30 * 86400
    for c in range(T0, T1, 60):
        spans = down if isinstance(down, list) else [down] if down else []
        is_down = any(a <= c < b for a, b in spans)
        if reboot_at is not None and c >= reboot_at:
            boot = reboot_at
        h.ping.append((c, 0 if is_down else 1))
        if not is_down:
            h.uptime.append((c, c - boot))
        if rssi:
            h.rssi.append((c, rssi[1] if W[0] <= c <= W[1] else rssi[0]))
        if poe_drop:
            h.poe.setdefault("5", []).append((c, 2 if poe_drop[0] <= c < poe_drop[1] else 3))
        if battery is not None:
            h.ups.append((c, 5 if battery[0] <= c < battery[1] else 3))
    h.uptime_src = "hrSystemUptime"
    h.external = external
    return h


def wave_link_drop(h, at=5000):
    h.link_uptime = [(c, (c - at) if c >= at else c + 86400) for c, _ in h.uptime]
    return h


def run(hosts):
    return r.analyze(hosts, *W)


def check(name, findings, cause, root=None):
    top = findings[0] if findings else {"cause": "none", "root": ""}
    ok = top["cause"] == cause and (root is None or root in top["root"])
    print(("PASS " if ok else "FAIL ") + f"{name}: {top['cause']} at {top['root']}")
    return ok


D = (4000, 5800)
results = [
    check("connectivity on backhaul link", run([
        host("AGG"), host("BH", "AGG", "backhaul", down=D), host("AP", "BH", "ap", down=D),
        host("SM", "AP", "sm", down=D)]), "Connectivity fault", "AGG and BH"),
    check("site power", run([
        host("AGG", down=D, reboot_at=D[1] - 60), host("BH", "AGG", down=D, reboot_at=D[1] - 60),
        host("AP", "BH", down=D, reboot_at=D[1] - 30)]), "Site power outage"),
    check("upstream beyond site", run([
        host("AGG", down=D), host("BH", "AGG", down=D)]), "Upstream failure beyond the site", "AGG"),
    check("PoE fault on one AP", run([
        host("SW", poe_drop=(D[0] - 30, D[0] + 120)), host("AP", "SW", "ap", down=D, reboot_at=D[0] + 150),
        host("SM", "AP", "sm", down=D)]), "PoE / cable fault", "AP"),
    check("single device reboot", run([
        host("SW"), host("AP", "SW", "ap", down=D, reboot_at=D[0] + 150)]), "Device power loss or reboot", "AP"),
    check("RF degradation", run([
        host("SW"), host("AP", "SW", "ap", rssi=(-58, -74))]), "RF degradation", "AP"),
    check("site power with UPS evidence", run([
        host("UPS", role="ups", down=(4100, 5800), battery=(4000, 4100), reboot_at=5800 - 60),
        host("AGG", down=D, reboot_at=D[1] - 60), host("BH", "AGG", down=D, reboot_at=D[1] - 60)]),
        "Site power outage"),
    check("mains cut, UPS held", run([
        host("UPS", role="ups", battery=(4000, 5000)), host("AGG"), host("BH", "AGG")]),
        "Mains power failure (UPS on battery)"),
    check("link fault in the upstream site", run([
        host("CORE0", external=True), host("RADIO0", "CORE0", external=True, down=D),
        host("RADIO3", "RADIO0", down=D), host("CORE3", "RADIO3", down=D), host("AP3", "CORE3", down=D)]),
        "Connectivity fault", "CORE0 and RADIO0"),
    check("Wave link dropped, radio stayed up", run([host("SW"), wave_link_drop(host("BH", "SW", "backhaul"))]),
          "Wireless link drop", "BH"),
    check("link cut, then power cut, in one window", two := run([
        host("AGG", down=[(3700, 4300), (5500, 6300)], reboot_at=6240),
        host("BH", "AGG", down=[(3700, 4300), (5500, 6300)], reboot_at=6240)]),
        "Upstream failure beyond the site", "AGG") and check("  ... second incident", two[1:], "Site power outage"),
    check("quiet window", run([host("SW"), host("AP", "SW")]), "none"),
]


def check_text(name, got, want):
    ok = want in got
    print(("PASS " if ok else "FAIL ") + f"{name}: {got}")
    return ok


ap = host("AP", "SW", "ap", rssi=(-60, -70))
ap.rssi, ap.sta_rssi = [], [ap.rssi, [(c, v - 5) for c, v in ap.rssi]]
ap.rssi, ap.rf_src = r.worst(ap.sta_rssi), "weakest station"
ap.stations = [(c, 3 if W[0] <= c <= W[1] else 5) for c, _ in ap.ping]
run([host("SW"), ap])
sw = host("SW2")
sw.model = "UniFi Pro Aggregation"
mute = host("SM", "AP", "sm")
results += [
    check_text("AP readings from its stations", r.signal_text(ap), "RSSI -65.0 → -75.0 dBm; stations 5 → 3 (weakest station)"),
    check_text("switch has no radio readings", r.signal_text(sw), "not a radio"),
    check_text("radio without readings says so", r.signal_text(mute), "check SNMP"),
]
sys.exit(0 if all(results) else 1)
