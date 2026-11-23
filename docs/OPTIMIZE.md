# Zabbix optimization: caches, processes, PostgreSQL

For the MDP Zabbix server: **8 vCPU, 24 GB RAM, 1 TB HDD**, Zabbix 7.4 (server as a systemd service,
PostgreSQL in Docker), sized for the full rollout of about **1,000 devices** (about 40,000 items and
600 values per second). Data retention, archive and backups are in `RETENTION.md`.

Run everything as root on the Zabbix server, from the package folder, with your settings loaded:
```
cd ~/mdatapulse && source ~/.zabbix_env
```

---

## Why the server crashed, and why it will not happen again

Zabbix keeps its whole configuration (hosts, items, triggers) in a fixed block of memory, `CacheSize`.
The default is 32 MB. When it is full, the server cannot load the configuration and **stops**; systemd starts
it again, it stops again: the restart loop you had. The other caches do not stop the server when full, but they
make it slow (history waits to be written, triggers read from the database).

Three things together prevent it:

1. **Caches sized with large headroom** (step 3). At 1,000 devices the configuration cache needs roughly
   150 to 250 MB; it gets 1 GB.
2. **Alarms long before any cache is full** (step 2): Warning at 70% used, High at 85%. The configuration cache
   only grows when hosts or templates are added, so a warning gives you weeks to react.
3. **Re-checking after each big rollout** (step 1 again): the health check measures real use and recalculates.

Memory budget with the recommendations: Zabbix caches 2.8 GB + PostgreSQL 6 GB + about 3 GB for the system,
web and Docker = about 12 GB. The other 12 GB is used by Linux as file cache for the database, which matters a
lot on a spinning disk.

---

## Step 1. Health check (5 minutes, changes nothing)

```
python3 tools/zbx_health.py
```
It measures the host (CPU, RAM, swap, disks, disk type), the Zabbix configuration, cache and process use, the
items and values per second, PostgreSQL settings and table sizes, and looks for crash messages in the last
30 days. Then it sizes everything for 1,000 devices (`--target-hosts N` to change that) and writes:

| File | What it is |
| --- | --- |
| `health_<date>/report.txt` | the report: **send it back for review** |
| `health_<date>/zabbix_server_tuning.conf` | recommended `zabbix_server.conf` lines |
| `health_<date>/postgresql_tuning.sql` | recommended PostgreSQL settings |

If PostgreSQL is not found automatically: `PG_CONTAINER=<container name> python3 tools/zbx_health.py`.

## Step 2. Cache and job alarms: the guard host (5 minutes)

```
python3 tools/zbx_sites.py templates --apply      # imports the new "MDP Zabbix Server Guard" template
python3 tools/zbx_sites.py guard --apply          # creates the host "Zabbix server guard" with it
```
After 2 minutes, **Monitoring > Latest data**, host *Zabbix server guard*, shows the values; **Monitoring > Hosts >
Zabbix server guard > Graphs** shows cache usage, process load, values per second and disk free space.

| Alarm | Severity | What to do |
| --- | --- | --- |
| Zabbix *configuration / history / index / trend / value* cache over 70% used | Warning | raise that cache at the next maintenance window (step 3) |
| ... over 85% used | High | raise it now and restart zabbix-server |
| Zabbix value cache ran out of memory | High | raise `ValueCacheSize` |
| Zabbix *poller / snmp poller / icmp pinger / ...* processes over 75% busy (15 min) | Warning | raise the matching `Start...` setting (step 4) |
| More than 100 items late by 10 min | Warning | pollers too few, or many devices unreachable |
| Preprocessing queue is growing | Warning | database too slow, or raise `StartPreprocessors` |
| Database / Archive disk below 25% free | Warning | see RETENTION.md, disk space |
| ... below 10% free | High | free space now: PostgreSQL stops when its disk is full |
| Daily history archive / Nightly database backup has not succeeded for 30h | High | read `/var/log/mdp-zabbix-data.log` |
| Off-server copy of archive and backups has not succeeded for 30h | High | only when `COPY_TO` is set: check the NAS or SSH key, then the log |

The thresholds are macros on the template (`{$CACHE.WARN}`, `{$CACHE.HIGH}`, `{$BUSY.WARN}`, `{$QUEUE.MAX}`,
`{$DISK.PFREE.WARN}`, `{$DISK.PFREE.HIGH}`, `{$JOB.MAXAGE}`). The disk and job values are sent every 10 minutes by
`tools/zbx_data.py status --push` once its schedule is installed (RETENTION.md part 2).

A separate host is used because the built-in *Zabbix server* host already has some of the same item keys.

## Step 3. Cache sizes (10 minutes, one restart of zabbix-server)

Recommended for this server (`zabbix_server_tuning.conf` from step 1 has the same, adjusted to what it measured):

| Setting | Default | Recommended | What it holds |
| --- | --- | --- | --- |
| `CacheSize` | 32M | **1G** | hosts, items, triggers. Full = server stops. |
| `HistoryCacheSize` | 16M | **512M** | new values waiting to be written; covers about 10 minutes of a slow disk |
| `HistoryIndexCacheSize` | 4M | **128M** | index of the history cache |
| `TrendCacheSize` | 4M | **128M** | the hourly min/avg/max being built |
| `TrendFunctionCacheSize` | 4M | **64M** | results of trend functions in triggers |
| `ValueCacheSize` | 8M | **1G** | recent values that triggers read; full = triggers read the disk |

Apply:
```
cp /etc/zabbix/zabbix_server.conf /etc/zabbix/zabbix_server.conf.$(date +%F)     # keep a copy
nano /etc/zabbix/zabbix_server.conf
```
For each setting, find its line (search for `CacheSize=`, it may start with `#`), remove the `#` and set the value.
Each setting must appear once. Then:
```
zabbix_server -T -c /etc/zabbix/zabbix_server.conf        # "validation successful"
systemctl restart zabbix-server
tail -n 30 /var/log/zabbix/zabbix_server.log            # no "cannot allocate" lines
```
If the server does not start and the log says it cannot allocate shared memory, check
`sysctl kernel.shmmax kernel.shmall` (they must be larger than the caches; current Linux defaults are).

If you run the server in Docker instead, the same settings are environment variables: `ZBX_CACHESIZE=1G`,
`ZBX_HISTORYCACHESIZE=512M`, `ZBX_HISTORYINDEXCACHESIZE=128M`, `ZBX_TRENDCACHESIZE=128M`,
`ZBX_TRENDFUNCTIONCACHESIZE=64M`, `ZBX_VALUECACHESIZE=1G`.

## Step 4. Processes (same restart as step 3)

Zabbix 7.4 polls SNMP asynchronously: one SNMP poller handles up to 1,000 devices at a time, so a few are enough.

| Setting | Default | Recommended | Why |
| --- | --- | --- | --- |
| `StartSNMPPollers` | 1 | **2** | all radios, switches and UPS are SNMP |
| `StartPollers` | 5 | **10** | other checks |
| `StartPingers` | 1 | **10** | 3 ping items per device every minute; ping is what the RCA relies on |
| `StartPollersUnreachable` | 1 | **5** | when a site goes down, many devices become unreachable at once |
| `StartLLDProcessors` | 2 | **4** | port, PoE and radio discovery on 1,000 devices |
| `StartDBSyncers` | 4 | **4** | keep 4 on a spinning disk: more writers compete for the same disk |
| `Timeout` | 3 | **5** | radios on long links answer slowly |
| `LogSlowQueries` | 0 | **3000** | logs database queries slower than 3 s |
| `MaxHousekeeperDelete` | 5000 | **10000** | rows deleted per item per run (hourly) |

Raise a process only when its *busy* alarm fires; more processes use more database connections and memory.

## Step 5. PostgreSQL (10 minutes, one restart of the database container)

Recommended for 24 GB RAM and a spinning disk (`postgresql_tuning.sql` from step 1 has the exact lines):

| Setting | Value | Why |
| --- | --- | --- |
| `shared_buffers` | 6GB | database memory (25% of RAM) |
| `effective_cache_size` | 13GB | tells the planner how much Linux file cache it can count on |
| `work_mem` / `maintenance_work_mem` | 16MB / 1GB | sorts per query / vacuum and index builds |
| `synchronous_commit` | off | the biggest win on a spinning disk. A power cut can lose the last second of values; the database stays consistent |
| `wal_compression`, `max_wal_size`, `checkpoint_timeout` | on, 8GB, 15min | fewer, smoother disk writes |
| `random_page_cost`, `effective_io_concurrency` | 4, 2 | correct costs for a spinning disk (SSD: 1.1, 200) |
| autovacuum settings | 4 workers, 30s, scale 0.05 | keeps the busy tables clean |
| `max_connections` | 300 | Zabbix server processes + web front end |

Apply (Zabbix keeps collecting into its history cache during the restart):
```
docker ps --format '{{.Names}}'                                   # the PostgreSQL container name
docker exec -i <postgres-container> psql -U zabbix -d zabbix < health_<date>/postgresql_tuning.sql
# "must be superuser": use the container's admin user instead, e.g. -U postgres
docker restart <postgres-container>
docker exec <postgres-container> psql -U zabbix -d zabbix -c "SHOW shared_buffers"      # 6094MB
```
PostgreSQL installed on the host instead of Docker:
```
runuser -u postgres -- psql -d zabbix < health_<date>/postgresql_tuning.sql
systemctl restart postgresql
runuser -u postgres -- psql -c "SHOW shared_buffers"
```

`ALTER SYSTEM` writes `postgresql.auto.conf` inside the data volume, so the settings survive container updates.
To undo one: `ALTER SYSTEM RESET <name>;` and restart.

Also in Docker (docker compose file, or `docker run` options), for the PostgreSQL container:
- `shm_size: 1g` (Docker gives 64 MB by default; PostgreSQL parallel queries fail with "could not resize shared
  memory segment")
- log rotation for every container: `logging: {driver: json-file, options: {max-size: 50m, max-file: "3"}}`

## Step 6. Less data for the same picture (template changes in this package)

Re-importing the templates (step 2) also brings two changes that cut the database size roughly in half:

- **Port status, port last-change, port speed, PoE port state and UniFi fan/PSU/temperature state** are still read
  every minute, so alarms are just as fast, but a value is stored only when it changes, or every 15 minutes
  (speed: every hour). The RCA works from the changes, so its reports are the same.
- **Port traffic and error counters** are read every 5 minutes instead of every minute (macro
  `{$IF.TRAFFIC.INTERVAL}` on *MDP Network Base SNMP*; set it back to `1m` if you want minute traffic graphs).
  The RCA does not use traffic.

Ping and uptime stay at 1 minute: the power-or-connectivity diagnosis depends on them.

## Other recommendations, by benefit

1. **Keep 30 days of raw values in the database** (RETENTION.md part 1): on plain PostgreSQL and a spinning disk
   this keeps the database around 200 to 250 GB and the daily deletes manageable; older RCA readings come from
   the archive.
2. **An SSD for the PostgreSQL data** (even 250 GB) if one can be added: the single biggest speed-up. The archive
   and backups can stay on the HDD.
3. **Swap**: add 8 GB and set `vm.swappiness=10`, so a memory spike slows the server instead of the kernel killing
   PostgreSQL or Zabbix:
   ```
   fallocate -l 8G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
   echo '/swapfile none swap sw 0 0' >> /etc/fstab
   echo 'vm.swappiness=10' > /etc/sysctl.d/90-zabbix.conf && sysctl --system
   ```
4. **Time sync**: the RCA compares boot times and outage times across devices, so the server clock must be right:
   `timedatectl` should say `System clock synchronized: yes` and time zone UTC.
5. **Monitor the server itself**: install Zabbix agent 2 on the host and keep the built-in *Zabbix server* host with
   *Linux by Zabbix agent*: CPU, memory, disk I/O wait and free space of the host.
6. **Fix or disable unsupported items** (the health check lists them): they are still polled and fill the log.
7. **Disable devices that are off on purpose** (`zbx_sites.py disable`), so their pollers do not wait on timeouts.
8. **Dashboards**: keep auto-refresh at 1 minute or more; many open dashboards refreshing every 10 s load the database.
9. **A Zabbix proxy** only becomes useful if sites end up behind slow or unreliable WAN links: it keeps collecting
   when the link to the server is down. Not needed while all devices are reachable on the local network.
10. **If you ever add TimescaleDB** (RETENTION.md part 3), check which TimescaleDB versions that Zabbix supports. Zabbix 7.4
    refuses anything newer than 2.30 (the server exits). Pin the image tag (e.g. `timescale/timescaledb:2.30.2-pg17`),
    never `latest`.

## After the full rollout

Run `python3 tools/zbx_health.py` again after each large batch of sites. With real devices measured it replaces
the estimates with your numbers; raise any setting it flags, and send the report if anything looks off.
