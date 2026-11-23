#!/usr/bin/env bash
# Read, from one device, the values the MDP templates collect, and print them as a list.
# Read-only (SNMP GET / WALK). Nothing secret is printed.
#
#   bash tools/check_metrics.sh <ip> <type>       type: epmp | wave | unifi | linksys | ups | base
#
# The community is asked for (not echoed), or taken from $C if set: C='...' bash tools/check_metrics.sh ...
# SNMPv3 (eKit): set SNMP_AUTH to the full v3 options, as for snmp_probe.sh.
set -u
IP=${1:?ip}; TYPE=${2:?type}
command -v snmpget >/dev/null || { echo "Needs net-snmp: apt install snmp"; exit 1; }
if [ -n "${SNMP_AUTH:-}" ]; then eval "AUTH=($SNMP_AUTH)"
else
  [ -n "${C:-}" ] || { read -r -s -p "SNMP community: " C; echo; }
  AUTH=(-v2c -c "$C")
fi
g() { printf '%-36s ' "$1"; snmpget "${AUTH[@]}" -On -Oqv -t 3 -r 1 "$IP" "$2" 2>&1 | head -1; }
w() { echo "$1:"; snmpwalk "${AUTH[@]}" -On -Oq -t 3 -r 1 "$IP" "$2" 2>&1 | head -${3:-20} | sed 's/^/    /'; }
echo "== $IP ($TYPE)"
g "sysName" 1.3.6.1.2.1.1.5.0
g "sysUpTime (SNMP agent uptime)" 1.3.6.1.2.1.1.3.0
g "hrSystemUptime (hardware uptime)" 1.3.6.1.2.1.25.1.1.0
E=1.3.6.1.4.1.17713.21
case "$TYPE" in
  epmp)
    g "Mode (1 AP, 2 SM)" $E.3.8.2.1.0
    g "Frequency MHz" $E.3.8.2.18.0
    echo "-- SM / P2P end: its own link"
    g "Link state (3 = connected)" $E.1.2.16.0
    g "Connected AP (SSID)" $E.1.2.8.0
    g "Signal RSSI dBm" $E.1.2.3.0
    g "SNR dB" $E.1.2.18.0
    g "DL / UL MCS" $E.1.2.7.0; g "" $E.1.2.6.0
    g "Distance km" $E.1.2.12.0
    echo "-- AP / P2P master: its stations"
    g "Connected stations" $E.1.2.10.0
    w "Station IPs" $E.1.2.30.1.10
    w "Station UL RSSI dBm" $E.1.2.30.1.4
    w "Station UL SNR dB" $E.1.2.30.1.6 ;;
  wave)
    g "Frequency MHz" 1.3.6.1.4.1.41112.1.11.1.1.2.1
    w "Peer local signal dBm" 1.3.6.1.4.1.41112.1.11.1.3.1.3
    w "Peer local SNR dB" 1.3.6.1.4.1.41112.1.11.1.3.1.4
    w "Peer link uptime" 1.3.6.1.4.1.41112.1.11.1.3.1.17 ;;
  unifi)
    w "Fans" 1.3.6.1.4.1.4413.1.1.43.1.6.1.3
    w "Power supplies" 1.3.6.1.4.1.4413.1.1.43.1.7.1.3
    w "Temperatures" 1.3.6.1.4.1.4413.1.1.43.1.8.1.5 ;;
  linksys)
    w "PoE port status (1 searching, 2 delivering, 4 disabled)" 1.3.6.1.2.1.105.1.1.1.6 30
    g "PoE power used (0.1 W)" 1.3.6.1.2.1.105.1.3.1.1.4.1 ;;
  ups)
    H=1.3.6.1.4.1.2011.6.174.1
    g "Output source (3 normal, 5 battery)" 1.3.6.1.2.1.33.1.4.1.0
    g "Seconds on battery" 1.3.6.1.2.1.33.1.2.2.0
    g "Battery state (4 float, 6 discharging)" $H.2.101.1.3.1
    g "Battery charge %" $H.6.100.1.3.1
    g "Backup time seconds" $H.6.100.1.4.1
    g "Input voltage (0.1 V)" $H.3.100.1.1.1
    g "Output load (0.1 %)" $H.4.100.1.14.1 ;;
  base) ;;
  *) echo "unknown type $TYPE"; exit 1 ;;
esac
w "Interfaces up(1)/down(2)" 1.3.6.1.2.1.2.2.1.8 12
