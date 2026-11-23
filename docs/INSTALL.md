# Install guide: MDataPulse for Zabbix

Everything below runs on the Zabbix host you SSH into (`root@zabbix-server`). Tested on
Zabbix 7.4.16 with PostgreSQL. Allow about 1 hour for steps 1 to 8, then a week of
data collection before the RCA reports are useful.

Each script makes no change until you add `--apply`. Run it once without `--apply`
first and read what it says it will do.

**Already installed the previous package?** Follow `UPGRADE.md` instead.

---

## Step 1. Get the code on the server

```
cd ~ && git clone https://github.com/blade99358/MdataPulse.git mdatapulse && cd mdatapulse
python3 --version          # must be 3.9 or newer
apt install -y snmp
mkdir -p data              # your private data files go here (never committed, see examples/)
```

## Step 2. Back up the Zabbix database

```
docker ps --format '{{.Names}}'                              # find the PostgreSQL container name
docker exec <postgres-container> pg_dump -U zabbix zabbix | gzip > ~/zabbix_backup_$(date +%F).sql.gz
ls -lh ~/zabbix_backup_*.sql.gz                              # should be several MB, not 0
```
If Zabbix is not in Docker: `sudo -u postgres pg_dump zabbix | gzip > ~/zabbix_backup_$(date +%F).sql.gz`

## Step 3. Create an API token and set the environment

1. In the Zabbix web UI, go to **Users > API tokens > Create API token**, for the Admin user,
   with no expiry. Copy the token and keep it out of chat.
2. On the server (put these lines in `~/.zabbix_env` so you can `source` it later):
```
export ZABBIX_URL=http://localhost:<web-port>     # the port nginx serves Zabbix on, e.g. 80 or 8080
export ZABBIX_TOKEN=<token>
export RCA_TZ=UTC                                  # your time zone, e.g. Europe/Berlin
export SITE_CODE_PREFIX=SITE                       # site codes look like SITE0015
```
All settings are listed in `config.example.env`.
```
source ~/.zabbix_env
python3 tools/zbx_sites.py dashboards              # should print "Zabbix 7.4.x at ..." (dry run)
```

## Step 4. Set the SNMP credentials as global macros

In the web UI, go to **Administration > Macros** and add the following (type **Secret text**):

| Macro | Value |
| --- | --- |
| `{$SNMP_COMMUNITY}` | your v2c community |
| `{$SNMPV3_USER}` | `zabbix` |
| `{$SNMPV3_AUTHPASS}` | the eKit v3 auth password |
| `{$SNMPV3_PRIVPASS}` | the eKit v3 privacy password |

Huawei eKit switches use SNMPv3 (their firmware blocks v2c). Configure each one with:
```
system-view
snmp-agent
snmp-agent mib-view included allview iso
snmp-agent group v3 zbxgroup privacy read-view allview
snmp-agent usm-user v3 zabbix group zbxgroup
snmp-agent usm-user v3 zabbix authentication-mode sha2-256
snmp-agent usm-user v3 zabbix privacy-mode aes128
snmp-agent protocol source-status all-interface
quit
save
```

The UniFi switches use their own community. Add one more global macro, also **Secret text**:

| Macro | Value |
| --- | --- |
| `{$SNMP_COMMUNITY_UNIFI}` | the UniFi community |

`apply` (step 7) points the UniFi hosts at it, and auto-discovery (step 8) tries it too.

## Step 5. Import the templates

```
python3 tools/zbx_sites.py templates --apply
```
This imports `templates/mdp_templates.yaml` and also removes items an older version had and this one dropped.
(The web import, **Data collection > Templates > Import**, works too, but tick *Delete missing* for items,
triggers and discovery rules.)

This creates 6 templates in the group *Templates/MDP network* (and *MDP Zabbix Server Guard* in
*Templates/MDP server*, for the cache and backup alarms: see OPTIMIZE.md step 2):

| Template | Used for |
| --- | --- |
| MDP Network Base SNMP | every device: ping, uptime, interfaces |
| MDP Cambium ePMP SNMP | ePMP 4600, Force 4600C, Force 4625: RSSI, SNR, MCS per link |
| MDP PoE Switch SNMP | Linksys and eKit: PoE state per port |
| MDP UPS SNMP | Huawei UPS network cards (standard UPS-MIB): on battery, battery low, runtime |
| MDP Ubiquiti Wave SNMP | Wave Pro 60 GHz: local/remote signal, SNR, MCS, capacity, link uptime |
| MDP UniFi Switch SNMP | UniFi switches: fans, power supply, temperatures |

The base template also holds the **power or connectivity diagnosis**. When a device answers ping again
after an outage of 3 minutes or more, one of two problems opens:

- **Outage cause: POWER (device rebooted)**: its uptime restarted, so it lost power (mains, UPS, PoE) or crashed.
- **Outage cause: CONNECTIVITY (device did not reboot)**: its uptime kept counting, so it had power and the path failed.

Both carry the tag `diagnosis=power` or `diagnosis=connectivity`. They follow the parent chain, so only the
device where the fault started gets one, not every device behind it. They close by themselves after 30 minutes.

## Step 6. Write your host list

Build a CSV with one line per device (see `examples/hosts_example.csv`):

`host,name,ip,site,role,vendor,model,parent,poe,uplink_ports,snmp_community,snmp_version`

- `site` is the site code (`SITE0015`, prefix set by `SITE_CODE_PREFIX`), `parent` the host it depends on
  (the radio or switch upstream), so alarms and RCA follow the real path to the core site.
- Leave `snmp_community` empty to use the global `{$SNMP_COMMUNITY}` macro.

## Step 7. Load the hosts into Zabbix (pilot one site first)

```
source ~/.zabbix_env
python3 tools/zbx_sites.py export current_hosts.csv          # snapshot of what Zabbix has today
head -1 hosts.csv > pilot.csv
grep ',SITE0000,' hosts.csv >> pilot.csv  # the core site only
python3 tools/zbx_sites.py apply pilot.csv                   # dry run: read it
python3 tools/zbx_sites.py apply pilot.csv --apply
```
A device that is already in Zabbix is matched by IP and updated. Its existing groups, tags and
templates are kept. Add `--rename` to also rename hosts to the names in the CSV.

If a host already has a stock template with the same item keys (for example *ICMP Ping*), linking
fails with "duplicate key". Re-run with `--replace-templates`, which removes the stock templates
**and their history**.

Check after 30 minutes: under **Monitoring > Hosts**, filter on *Sites/SITE0000*. The SNMP icon
should be green, and **Latest data** should show ping, uptime and, on radios, RSSI and SNR.

Then load the rest of the sites:
```
python3 tools/zbx_sites.py apply hosts.csv            # dry run
python3 tools/zbx_sites.py apply hosts.csv --apply
```

## Step 8. Dashboards and auto-discovery

```
python3 tools/zbx_sites.py overview --apply                 # the "Network overview" dashboard for all sites
python3 tools/zbx_sites.py dashboards --recreate --apply    # one "Site SITExxxx" dashboard per site
python3 tools/zbx_sites.py discovery                        # dry run
python3 tools/zbx_sites.py discovery --apply
```

**Network overview** (Dashboards > All dashboards) has five pages:

| Page | Shows |
| --- | --- |
| All sites | open problems, SNMP availability, problems per site, current root-cause problems, last diagnosed outages, worst packet loss, most recent reboots |
| Device map | one hexagon per device, red when it does not answer ping |
| Power or connectivity | the diagnosis problems split into POWER and CONNECTIVITY, UPS events, reboots |
| Radios | signal per link (green / amber / red), radio problems, worst loss, recent reboots |
| UPS | output source per UPS (red = on battery), charge, runtime, input voltage, load |

Devices behind a failed device are hidden (trigger dependencies), so the problem lists show where the
fault is, not 30 copies of it. Run `overview --apply` again after adding sites.

Discovery scans the ranges in `DISCOVERY_RANGES` (radios, switches, endpoints) and `DISCOVERY_UPS_RANGE` (UPS) every hour,
or pass `--ranges 10.20.0.0/22,10.20.10.0/24 --ups-range 10.20.10.0/24 --every 1h`.

A new device that answers SNMP is added to *Discovered/Network* with the base template, named from
its sysName. Vendor actions add the Cambium, Wave, UniFi, eKit or Linksys template. Discovery cannot know
the site or the parent, so add the device to the sheet (or to hosts.csv) and run `apply` again. It is
matched by IP.

## Step 9. The RCA report button in Zabbix

A small web service (`tools/rca_server.py`) builds the report when someone clicks the button. It runs
on the Zabbix server, reads Zabbix through the same API token, and only answers people who are logged
in to Zabbix in the same browser.

1. Start it and keep it running:
   ```
   bash tools/install_rca_service.sh      # prints "RCA service running ... on port 8081"
   ```
   It installs a systemd service that runs from the folder you unpacked the package in and reads
   `~/.zabbix_env`. Run it again after moving the folder. If a firewall runs on the server, open TCP 8081
   to the operators' network (`ufw allow 8081/tcp`).
2. Add the buttons. Use the address the operators type to open Zabbix, with port 8081:
   ```
   python3 tools/zbx_sites.py buttons --url http://<zabbix-server-ip>:8081 --apply
   ```
3. Use them:
   - **Problems list**: click a problem name, then **RCA report for this problem**. The report covers
     that problem from 15 minutes before it started to 15 minutes after it ended, for the whole site.
   - **Any host name** (problems, dashboards, maps): click it, then **RCA report for this site**. A form
     opens with the site filled in. Pick the incident date and time and click **View report**.
   - On the report, **Download HTML** saves the file and **Print / save as PDF** gives a PDF.

If the report page says "Log in to Zabbix first" while you are logged in, your Zabbix web UI is on a
different address or path than the RCA service. Add `export RCA_AUTH=none` to `~/.zabbix_env` and run
`systemctl restart rca-server`; then anyone who can reach port 8081 can open reports.

## Step 10. RCA report from the command line

```
source ~/.zabbix_env
python3 tools/zbx_rca.py --site SITE0008 --from "2026-11-02 14:00" --to "2026-11-02 15:00"
```
This writes `rca_SITE0008_20261102_1400.html`. Copy it to your computer to open it:
`scp root@<server>:~/mdatapulse/rca_*.html .`

Other forms:
```
python3 tools/zbx_rca.py --host SITE0008-P2P-4 --from ... --to ...
python3 tools/zbx_rca.py --site SITE0008 --from ... --to ... --json result.json
```
The report also follows the uplink chain into other sites (for example from a branch site to the radio at the core site that feeds it),
so a fault at the far end of a link is found. If the window holds several incidents (a link cut, then a
power cut), each one gets its own finding.

## Step 11. Confirm the vendor readings (once)

Send the probe output back so the Cambium, Wave Pro and UPS readings can be checked:
```
COMM='<community>'
./tools/snmp_probe.sh 10.20.0.24 "$COMM" epmp4600
./tools/snmp_probe.sh 10.20.0.25 "$COMM" epmp4600c
./tools/snmp_probe.sh 10.20.0.26 "$COMM" linksys
./tools/snmp_probe.sh 10.20.10.11   "$COMM" ups
SNMP_AUTH="-v3 -l authPriv -u zabbix -a SHA-256 -A '<auth>' -x AES -X '<priv>'" ./tools/snmp_probe.sh 10.20.0.11 - ekit
wc -l probe_*.txt
```

---

## Undo

- Remove the discovery rule: **Data collection > Discovery**, then *MDP network discovery*, then Delete.
  Delete the *MDP discovery:* actions under **Alerts > Actions > Discovery actions**.
- Dashboards: **Dashboards > All dashboards**, select *Site ...* and *Network overview*, then Delete.
- RCA buttons: **Alerts > Scripts**, delete *RCA report for this problem* and *RCA report for this site*.
  Stop the service with `systemctl disable --now rca-server`.
- Full rollback: stop the Zabbix server, recreate an empty `zabbix` database, then load the
  step 2 backup into it:
  `gunzip -c ~/zabbix_backup_<date>.sql.gz | docker exec -i <postgres-container> psql -U zabbix zabbix`.
  Only do this in a maintenance window. It also discards any data collected after the backup.
