#!/usr/bin/env bash
# Walk one device per model and save what it exposes, so vendor OIDs can be
# confirmed before rollout. Read-only (SNMP GET/WALK).
#
#   ./snmp_probe.sh <ip> <community> <label>     e.g. ./snmp_probe.sh 10.10.0.4 public epmp4600
#
# SNMPv3 devices: set SNMP_AUTH to the full auth options and pass "-" as community, e.g.
#   SNMP_AUTH="-v3 -l authPriv -u zabbix -a SHA-256 -A 'authpass' -x AES -X 'privpass'" ./snmp_probe.sh 10.20.0.11 - ekit
#
# Needs net-snmp (apt install snmp). Send the resulting probe_<label>.txt back.
set -u
IP=$1; COMM=$2; LABEL=$3
OUT="probe_${LABEL}.txt"
if [ -n "${SNMP_AUTH:-}" ]; then eval "AUTH=($SNMP_AUTH)"; else AUTH=(-v2c -c "$COMM"); fi
w() { echo "### $1" >>"$OUT"; snmpwalk "${AUTH[@]}" -On -t 3 -r 1 "$IP" "$1" >>"$OUT" 2>&1; echo >>"$OUT"; }
: >"$OUT"
w 1.3.6.1.2.1.1             # system (descr, uptime, name)
w 1.3.6.1.2.1.25.1.1        # hrSystemUptime
w 1.3.6.1.2.1.31.1.1.1.1    # ifName
w 1.3.6.1.2.1.2.2.1.8       # ifOperStatus
w 1.3.6.1.2.1.105           # POWER-ETHERNET-MIB (PoE)
w 1.3.6.1.2.1.33            # UPS-MIB (RFC 1628)
w 1.3.6.1.4.1               # vendor (enterprise) subtree
echo "Saved $OUT ($(wc -l <"$OUT") lines)"
