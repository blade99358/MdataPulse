# MDataPulse

Network monitoring and root cause analysis for wireless backhaul networks, built on **Zabbix 7.4** and
PostgreSQL. MDataPulse adds what plain Zabbix does not give you out of the box: one clear alarm when a whole
site goes down, a report that tells you *why* an outage happened (power or connectivity), daily health
reports anyone can read, a live map, and alarms in WhatsApp.

Everything runs inside your own network against your own Zabbix. Python 3.9+ standard library only
(PyYAML is needed only to regenerate the templates).

## Features

| | |
| --- | --- |
| **Device templates** | SNMP templates for Ubiquiti Wave Pro, Cambium ePMP, UniFi and Huawei eKit switches, Linksys PoE switches and UPS cards (UPS-MIB), plus a Zabbix server guard template |
| **Site model** | Every device belongs to a site and depends on its parent device, so alarms follow the real path to the core site |
| **Site DOWN alarm** | One problem per site when its main link is lost (with LACP pairs and "down via upstream" for sites behind it) instead of dozens of "unreachable" lines |
| **Outage cause** | After an outage, each device is judged: uptime restarted = power, uptime kept counting = connectivity |
| **RCA reports** | Pick a site and a time window: device readings, the chain of events and the inferred cause, in a printable page |
| **Health reports** | Daily, weekly and monthly reports with every outage listed, power failures, capacity and a site score, plus an admin report for the Zabbix server itself |
| **Radio signal check** | Busy-hour RSSI and SNR of every radio and endpoint CPE compared with the previous day or week |
| **Live geo map** | All sites, links and endpoints on a map, coloured live from Zabbix |
| **Device logs** | Full history of one device: problems, reboots, notifications, item errors |
| **Live export** | Any Zabbix dashboard window to Excel, CSV or PDF, exactly as it shows |
| **WhatsApp alarms** | High and Disaster problems sent to a WhatsApp group through a free self-hosted WAHA instance |
| **Data retention** | Retention policy, nightly archive and backup, sizing and PostgreSQL tuning advice |

## Layout

```
tools/       scripts and the MDataPulse Reports web service (rca_server.py)
templates/   Zabbix template import file and its generator
tests/       offline tests (python3 tests/test_report.py, ...)
docs/        install, site down, reports, signal, optimization and retention guides
examples/    sample host list and sample formats of the private data files
```

## Quick start

```
git clone https://github.com/blade99358/MdataPulse.git mdatapulse && cd mdatapulse
cp config.example.env ~/.zabbix_env && chmod 600 ~/.zabbix_env    # fill in ZABBIX_URL, ZABBIX_TOKEN, ...
source ~/.zabbix_env
python3 tools/zbx_sites.py templates --apply          # import the templates
python3 tools/zbx_sites.py apply hosts.csv            # dry run, then add --apply
python3 tools/zbx_sites.py dashboards --apply
bash tools/install_rca_service.sh                     # reports web service on port 8081
```

Every script makes no change until you add `--apply`. The full guide is in [docs/INSTALL.md](docs/INSTALL.md).

## Private data stays out of the repository

Your device lists, site locations, display names and main links are read at run time from the `data/` folder
(or `MDP_DATA_DIR`), which is ignored by git. `examples/` shows the format of each file:

| File | Used by |
| --- | --- |
| `data/main_links.txt` | site DOWN alarm and parents (`zbx_sites.py sitedown`) |
| `data/endpoints.txt` | endpoint names and sites (`zbx_endpoints.py`) |
| `data/device_names.txt` | display names (`zbx_rename.py`) |
| `data/site_locations.csv`, `data/endpoint_locations.csv` | live geo map (`zbx_netmap.py`) |

Credentials (SNMP communities, SNMPv3 passwords) live only in Zabbix global macros, and the API token and other
settings in `~/.zabbix_env` (see `config.example.env`). Nothing secret is stored in the code.

## Tests

```
for t in tests/test_*.py; do python3 "$t" || break; done
```
