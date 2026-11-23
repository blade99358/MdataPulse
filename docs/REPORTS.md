# Network health reports

Three reports for management (daily, weekly, monthly) and one for the Zabbix admin. They are built
every morning on the Zabbix server by `tools/zbx_report.py`, saved as HTML pages, and shown at
`http://<zabbix-server>:8081/reports` (the RCA web service; Zabbix login required). Each page prints
or saves as PDF from the browser.

Every report keeps a **snapshot** of its numbers next to the page. The next report of the same kind
compares itself with it: arrows ▲▼ show what is better or worse than the day, week or month before,
and the score chart shows the last reports. On the very first run the previous period is computed from
Zabbix trends, so the first report already has a comparison.

## What is in the management reports

| Section | What it shows | Where the numbers come from |
| --- | --- | --- |
| Health score | 0 to 100 for the network and each site, GOOD (90+) / FAIR (75-89) / POOR, change vs last period | the sections below, see "How the score works" |
| What changed | 3 to 8 plain sentences: site outages and downtime, availability, sites whose score fell and why | snapshot comparison |
| Sites | score, availability, site outages and their downtime, single-device outages, times cut off by an upstream site, radios, UPS, busy links | per site tag |
| Outages and their impact | each outage once, where it started: SITE DOWN or the devices, POWER or CONNECTIVITY, how long, devices affected and the sites behind it | `Device unreachable` and `Site DOWN` problems, `Outage cause` problems, the parent tags and Site hosts |
| Radio links getting worse | links that lost 3+ dB of signal or SNR, 1.5+ modulation steps or 20%+ capacity vs last period; weak links | hourly trends of the radio items |
| Power and UPS | power outages per site; each UPS: times on battery, backup time (and its trend: battery aging), mains voltage range, peak load | UPS items and `UPS: On battery` problems |
| Capacity | the 10 busiest ports and Wave radios: peak 5-minute traffic against port speed or radio capacity | interface traffic trends |
| Top 10 | sites and devices with the most downtime, marked REPEAT when they also failed last period; repeat offenders | outages |
| Recommended actions | a to-do list built from the findings (replace UPS battery, realign link, add capacity, visit device) | all of the above |

Monthly adds a bar chart of availability per day. Daily lists every outage of the day.

### How outages are counted (since 2026-11-01)

An outage is counted **once**, at the site where it started, however many devices went down with it:

- The alarms of one site that start within 10 minutes of each other, or during that site's `Site SITExxxx DOWN`
  alarm, are one outage. Its downtime is from the first device down to the last one back (the union), not the
  sum: a site of 4 devices down for 30 minutes is 1 outage of 30 minutes, not 4 outages and 2 hours.
- It is a **site outage** when the site's `Site DOWN` alarm fired or 80% or more of its devices went down
  together; otherwise a **single-device outage** (one or a few devices).
- Alarms in a site that was cut off by an outage upstream at the same time belong to that upstream outage:
  the site shows "cut off by upstream", the outage counts at the upstream site.
- Devices hidden by the dependencies (or whose alarm was closed by the Site down event correlation) still
  lose availability for the whole site outage.
- Endpoint devices (`EP-*`: CPEs and power units) are left out: the reports cover the site sites only
  (`REPORT_EXCLUDE` in `~/.zabbix_env` to change).

## How the score works

Each site starts at 100. Every problem costs points once (an outage costs its downtime and its count,
nothing else):

| Reason | Points | Cap |
| --- | --- | --- |
| downtime: device-time lost in outages that started at the site | 3 per 1% (whole site down 30 min: 6 on a day, 0.9 on a week, 0.2 on a month) | 50 |
| outages that started at the site | 4 per site outage, 1 per single-device outage (per week) | 20 |
| UPS | 5 per UPS to check, 1 per time on battery (per week) | 10 |
| radio links | 3 per link that got weaker, 1 per weak link | 10 |
| capacity | 4 per full link (90%+), 1 per busy link (70%+) | 10 |

A site cut off by an upstream site keeps its score (its availability column still shows the cut). Network
score = average of the site scores weighted by devices, with the downtime points taken from the
availability the users really got (cut-offs included), so a core outage weighs by the devices it took
down. Examples: a whole site down 30 minutes on a day = 79 (FAIR); the same in a week = 94 (GOOD).
All thresholds are at the top of `tools/zbx_report.py`.

## The admin report

Not for management. It lists what stops the monitoring from seeing the network:

- devices whose SNMP does not answer (since when, and the error), devices with no ping data for 15
  minutes, ping-only devices, devices found by discovery that have no site
- items not supported, by key and by device
- problems open for more than 7 days that nobody acknowledged
- Zabbix server: caches and process busy (average and highest of the period), database size and growth
  per day, free disk, time of the last archive and backup (from the "Zabbix server guard" host, see
  OPTIMIZE.md), server problems in the period

## Install

Needs the package's `~/.zabbix_env` (ZABBIX_URL, ZABBIX_TOKEN) and access to PostgreSQL as for
`zbx_data.py`. Run as root on the Zabbix server, from the package folder:

```
source ~/.zabbix_env
python3 tests/test_report.py                         # offline checks, all PASS
python3 tools/zbx_report.py daily                    # yesterday; prints the score and the file
python3 tools/zbx_report.py weekly                   # last full week (Sunday to Saturday)
python3 tools/zbx_report.py monthly                  # last month
python3 tools/zbx_report.py admin                    # yesterday, and the state right now
python3 tools/zbx_report.py cron --apply             # every morning at 04:15 server time
python3 tools/zbx_report.py rebuild                  # build the kept reports again (after an update)
bash tools/install_rca_service.sh                    # restart the web service so /reports appears
```

Open `http://<zabbix-server>:8081/reports`. A report for an earlier period: `--date 2026-10-20`
(weekly: any day of that week; monthly: `--date 2026-09`). `--out file.html` writes a copy elsewhere;
`--no-save` builds it without keeping a snapshot.

The cron job runs `zbx_report.py scheduled`: it builds every report whose period has finished and is
not built yet (daily every day, weekly on the first day of the week, monthly on the 1st, admin every
day). It logs to `/var/log/mdp-zabbix-reports.log`.

Settings in `~/.zabbix_env` (all optional):

| Setting | Default | |
| --- | --- | --- |
| `REPORT_DIR` | `/var/lib/mdp-reports` | reports and snapshots, about 40 MB a year |
| `REPORT_WEEK_START` | `sun` | first day of the report week |
| `RCA_TZ` | `UTC` | day boundaries |

## Notes

- Radio, UPS and traffic numbers come from hourly trends (kept 2 years) and outages from problems
  (kept 2 years), so a report for any period of the last 2 years can be built again, including after
  the raw history (30 days) is gone.
- Availability is the time a device was not cut off by an outage, its own or one of a device in front
  of it. Only `Device unreachable` problems count, so:
  - outages shorter than 3 minutes (`{$ICMP.DOWN.TIME}`) raise no problem and do not lower it;
  - a Zabbix server stop does not lower it either; it shows in the admin report instead.
- A device that has been down for a long time still counts as down: outages are read back up to 90
  days before the period, and any outage still open, however old. A device that is off on purpose
  should be disabled (`zbx_sites.py disable`) so it leaves the reports.
- A report for a period that has not finished yet (for example `weekly --date` of this week) is shown
  but not saved, so the morning job still builds the full period.
- An outage whose cause is "NOT KNOWN" had no `Outage cause` problem after it: usually SNMP did not
  answer after the device came back. The RCA report for that time can say more.
- Radio and UPS trends compare averages: one bad hour does not mark a link as degraded, a week of
  lower signal does.
