# Data retention process

How long MDP monitoring data is kept, where, how it is removed, and how to get it back.
Owner: the Zabbix administrator. Applies to the Zabbix server (8 vCPU, 24 GB RAM, 1 TB HDD).
Tools: `tools/zbx_data.py` (policy, archive, backup, cleanup, status), `tools/zbx_timescale.py` (part 3).

---

## Part 1. The policy

| Data | Kept in Zabbix (database) | Kept in the archive (files on the server) | Why |
| --- | --- | --- | --- |
| **Raw values** (every reading: ping, uptime, signal, ports, PoE, UPS) | **30 days** (90 days if you ever add TimescaleDB, part 3) | **365 days**, then the off-server copy | RCA reports need minute-by-minute readings. Most incidents are investigated within days. Older ones are read from the archive automatically, so the RCA covers a full year either way. |
| **Trends** (hourly min / average / max of every number) | **2 years** | 365 days | Graphs and capacity reports over months and years, and year-on-year comparison |
| **Problems** (alarms, with start, end and acknowledgements) | **2 years** | in the nightly backup | Management reports: outages per site per month, availability, repeat faults |
| Service / SLA data | 1 year | in the nightly backup | SLA reports |
| Audit log (who changed what in Zabbix) | 1 year | in the nightly backup | Accountability |
| Internal, discovery and auto-registration events | 7 days | no | Only useful for troubleshooting Zabbix itself |
| Web sessions | 30 days | no | |
| **Database backup** (configuration, hosts, templates, dashboards, users, problems; no raw values) | | **14 nightly copies** | Rebuild the server after a disk or server loss |

Every RCA report needs raw values: for an incident up to 30 days old it reads Zabbix; for an older one it reads
the archive automatically (part 4). After 365 days only the off-server copy has the raw values.

Why 30 days in the database: on plain PostgreSQL the housekeeper deletes expired values row by row. At full rollout
that is about 50 million rows a day on a spinning disk; with 30 days the database stays around 200 to 250 GB and
the deletes stay manageable. 90 days would mean about 700 GB and three times the work.

**Storage at full rollout** (about 600 values per second; `tools/zbx_health.py` recalculates with real numbers):

| Where | Size |
| --- | --- |
| Database, PostgreSQL (30 days raw + 2 years trends) | about 200 GB after year 1, about 250 GB from year 2 |
| Archive, 365 days | about 165 GB |
| 14 nightly backups | about 15 to 30 GB |
| System, Docker, logs | about 30 GB |
| **Total** | **about 450 to 480 GB of the 1 TB**, stable from year 2 (older data is removed as new data arrives) |

These are estimates before the template savings (OPTIMIZE.md step 6, up to half less); `zbx_health.py` gives the
real numbers once the sites are in.

Apply the policy (sets Administration > Housekeeping; dry run first):
```
cd ~/mdatapulse && source ~/.zabbix_env
python3 tools/zbx_data.py policy             # shows current -> new
python3 tools/zbx_data.py policy --apply
```
The periods apply to all items at once ("Override item history/trend period" on). On plain PostgreSQL
`policy --apply` also adds a small BRIN index on `clock` to each history and trends table (a few MB each, built
once without locking; at full size it takes some minutes per table). Without it the nightly archive reads the whole
table, about 150 GB from the HDD every night, to find one day; with it, only that day. Zabbix ignores these
indexes. If a Zabbix upgrade ever rebuilds a history table, `zbx_health.py` reports the missing index and
`policy --apply` adds it again. The script sets raw values to
30 days on plain PostgreSQL and 90 days on TimescaleDB; `HISTORY_DAYS=45 python3 tools/zbx_data.py policy --apply`
sets another value (more days = more disk and more deletes). To change a period later, edit `POLICY` at the top of `tools/zbx_data.py` and run it again,
and update this table.

---

## Part 2. The daily cycle (automatic)

Install once:
```
mkdir -p /var/backups/zabbix
nano ~/.zabbix_env            # add the lines below
python3 tools/zbx_data.py cron            # shows the schedule
python3 tools/zbx_data.py cron --apply    # installs /etc/cron.d/mdp-zabbix-data
python3 tools/zbx_data.py nightly         # first run by hand: archives the last 7 days and makes a backup
```
Lines for `~/.zabbix_env`:
```
export ARCHIVE_DIR=/var/backups/zabbix      # archive/ and backup/ go here
export ARCHIVE_KEEP_DAYS=365
export BACKUP_KEEP_DAYS=14
export COPY_TO=                             # off-server copy: a mounted NAS path or user@host:/path (rsync)
# export PG_CONTAINER=<name>                # only if the PostgreSQL container is not found automatically
```

What runs, every night at 01:30 (server time, UTC):

| Step | What it does | Result |
| --- | --- | --- |
| 1. **Archive** | copies each finished day of raw values and trends to compressed files, one folder per day. Each file is read back to check it, and a `MANIFEST.json` records rows and checksums. A day already archived is skipped; if a night was missed, the next run catches up (up to 7 days back). | `/var/backups/zabbix/archive/YYYY/MM/YYYY-MM-DD/` |
| 2. **Backup** | `pg_dump` of the database without raw values and trends (those are in the archive), plus `/etc/zabbix` and `~/.zabbix_env` | `/var/backups/zabbix/backup/zabbix_<date>.dump` and `_etc.tgz` |
| 3. **Clean up** | deletes archive days older than 365 days and backups older than 14 days (the newest 3 are always kept) | |
| 4. **Copy off the server** | if `COPY_TO` is set: copies new files there. Nothing is ever deleted on the far side | second copy; alarm on the guard host if it has not worked for 30h |
| 5. **Report** | sends sizes, free space and the time of the last archive and backup to the *Zabbix server guard* host | alarms if a step failed (OPTIMIZE.md step 2) |

Every 10 minutes the report step runs alone, so the disk alarms stay current.
Log: `/var/log/mdp-zabbix-data.log`. The backup folder and files are readable by root only: they hold the
database and `~/.zabbix_env` (API token). Keep the off-server copy just as private.

**Inside Zabbix**, the housekeeper deletes what is older than the policy, every hour. With TimescaleDB it drops whole
days and compresses data older than 7 days; without it, it deletes row by row.

**Data is deleted in this order, so nothing is lost without a copy**: a day is archived the night after it ends,
deleted from Zabbix after 30 days, deleted from the server's archive after 365 days, and stays in the off-server
copy for as long as the company keeps it (recommended: 3 years).

### Off-server copy (strongly recommended)

The archive and backups are on the same disk as the database: if that disk fails, all are lost. Set `COPY_TO`
to another machine or NAS, for example a mounted share (`COPY_TO=/mnt/nas/zabbix`) or rsync over SSH with a key
(`COPY_TO=backup@10.20.100.12:/srv/zabbix`, after `ssh-copy-id backup@10.20.100.12`).

### Routine checks

| When | Who | What |
| --- | --- | --- |
| Daily | automatic | the cycle above; alarms on the guard host if anything fails |
| Weekly | administrator | look at *Zabbix server guard* graphs: cache use, disk free, database size trend |
| Monthly | administrator | **test a restore** (part 4, "Test a backup"), and run an RCA report for an incident 3+ months old |
| Every 3 months, and after each big rollout | administrator | `python3 tools/zbx_health.py`: sizes and growth; adjust caches and keep periods if needed |

---

## Part 3. Optional: TimescaleDB

**Not needed for this setup**; plain PostgreSQL with the 30-day policy above fits the disk. Keep this part for later,
if the database disk or the housekeeper becomes a bottleneck (the guard host shows the *housekeeper* busy graph and
disk free space).

TimescaleDB is a PostgreSQL extension that stores the history tables in one block per day. Old days are dropped
instantly and data older than 7 days is compressed about 10 to 1. Zabbix supports it and switches its housekeeper
to it automatically. Downtime now (database under 1 GB): a few minutes. At full size it would take hours.

The safe way is to copy the database into a **new** TimescaleDB container and leave the old one untouched as the
undo. It needs a short maintenance window (monitoring stops for the duration).

**1. Check.** `python3 tools/zbx_timescale.py check` shows the PostgreSQL version and container. Note the
PostgreSQL major version (e.g. 17) and how the web front end reaches the database (`docker inspect <web container>`,
`DB_SERVER_HOST` / `DB_SERVER_PORT`) and the server (`grep ^DB /etc/zabbix/zabbix_server.conf`).

**2. Start the new database container** next to the old one, same PostgreSQL major version, TimescaleDB **2.30**
(Zabbix 7.4 refuses newer versions), its own volume, and a free port (here 5433):
```
docker run -d --name zabbix-tsdb --restart unless-stopped --shm-size 1g \
  -e POSTGRES_USER=zabbix -e POSTGRES_PASSWORD='<same password as now>' -e POSTGRES_DB=zabbix \
  -v zabbix-tsdb:/var/lib/postgresql/data -p 127.0.0.1:5433:5432 \
  timescale/timescaledb:2.30.2-pg17
```
If the web container is on a Docker network, add `--network <that network>`. With docker compose, add it as a new
service the same way.

**3. Check the target.** `python3 tools/zbx_timescale.py check --target zabbix-tsdb` must end with READY.

**4. Copy and convert.** `python3 tools/zbx_timescale.py migrate --target zabbix-tsdb`
(type MIGRATE to confirm). It stops zabbix-server, makes a full backup of the old database
(`/var/backups/zabbix/before_timescaledb_<date>.dump`), copies it into the new container, sets TimescaleDB 2.30,
and converts history and trends. zabbix-server stays stopped.

**5. Point Zabbix to the new database.**
- server: in `/etc/zabbix/zabbix_server.conf` set `DBHost=127.0.0.1` and `DBPort=5433`
- web front end: recreate its container with `DB_SERVER_HOST`/`DB_SERVER_PORT` pointing to the new container
  (on a Docker network: `DB_SERVER_HOST=zabbix-tsdb`, port 5432)
- the package: `export PG_CONTAINER=zabbix-tsdb` in `~/.zabbix_env`

**6. Start and verify.**
```
systemctl start zabbix-server
grep -i timescale /var/log/zabbix/zabbix_server.log | tail -3     # no "Unsupported DB" line
python3 tools/zbx_data.py policy --apply                          # turns compression on (7d)
```
In the web UI: Latest data updates, Administration > Housekeeping shows "TimescaleDB" options.

**7. Apply the PostgreSQL tuning** (OPTIMIZE.md step 5) to the new container. After a week without problems,
stop the old container (`docker stop <old>`); keep its volume one more month, then remove it.

**Undo** (before step 7): stop zabbix-server, put `DBHost`/`DBPort` and the web container back to the old
database, start zabbix-server. The old database is exactly as it was before step 4.

---

## Part 4. Procedures

### RCA for an old incident
Use the RCA button or `tools/zbx_rca.py` as usual. If Zabbix no longer has raw values for that window, the report
reads them from the archive and says so at the bottom ("Readings for this window came from the history archive").
Problems are kept for 2 years, so the timeline is complete. For incidents older than 365 days, copy the day
folders back from the off-server copy into `/var/backups/zabbix/archive/YYYY/MM/` first. If `rca-server` runs as a
service, add `ARCHIVE_DIR=...` to `~/.zabbix_env` only if you changed it from the default.

### Test a backup (monthly, 10 minutes, does not touch production)
Use the same PostgreSQL major version as production (here 17):
```
docker run -d --name zbx-restore-test -e POSTGRES_USER=zabbix -e POSTGRES_PASSWORD=test -e POSTGRES_DB=zabbix postgres:17
sleep 15
B=$(ls -t /var/backups/zabbix/backup/*.dump | head -1)
docker exec -i zbx-restore-test pg_restore -U zabbix -d zabbix --no-owner < "$B"
docker exec zbx-restore-test psql -U zabbix -d zabbix -c "SELECT count(*) AS hosts FROM hosts;"
PG_CONTAINER=zbx-restore-test python3 tools/zbx_data.py load --from <a recent day> --to <same day>
docker exec zbx-restore-test psql -U zabbix -d zabbix -c "SELECT count(*) FROM history_uint;"
docker rm -f zbx-restore-test
```
Pass: the host count matches Zabbix and the history rows match that day's `MANIFEST.json`. Write the date and result
in the operations log. (On TimescaleDB: image `timescale/timescaledb:2.30.2-pg17`, and before `pg_restore` run
`DROP EXTENSION IF EXISTS timescaledb`, then `CREATE EXTENSION timescaledb VERSION '2.30.2'; SELECT
timescaledb_pre_restore();`, and after it `SELECT timescaledb_post_restore();`.)

### Rebuild after losing the server or the disk
1. Install the server as before (Zabbix server, Docker, PostgreSQL container) from INSTALL.md.
2. Get the newest `zabbix_<date>.dump` and `_etc.tgz` and the archive folders from the off-server copy.
3. Restore the dump into the new database as in "Test a backup" (into the real container), and `/etc/zabbix` from
   `_etc.tgz`.
4. Load the recent raw values back (as far back as needed, newest first):
   `python3 tools/zbx_data.py load --from <date> --to <date>`
5. Start zabbix-server and check the dashboards.
Lost at most: the raw values of the day the failure happened (archived nightly), and changes made since the last backup.

### Disk running out of space
1. See what grew: `du -sh /var/backups/zabbix/* /var/lib/docker/volumes/*` and the *Database size* graph on the guard host.
2. Quick relief: lower `BACKUP_KEEP_DAYS` (e.g. 7) or `ARCHIVE_KEEP_DAYS` (e.g. 180, once those days are in the
   off-server copy), then `python3 tools/zbx_data.py prune`.
3. If the database itself grew: run `zbx_health.py` (unexpected items, unsupported items, intervals), or shorten
   the raw period in `POLICY` (e.g. 60d) and `policy --apply`.
4. Never delete files inside the PostgreSQL data folder by hand.

### Change a retention period
Edit `POLICY` in `tools/zbx_data.py` (or `ARCHIVE_KEEP_DAYS` / `BACKUP_KEEP_DAYS`), run `policy --apply`, update
the table in part 1, and note the change and the reason in the operations log. Shortening deletes data at the next
housekeeper run: make sure those days are archived first (`zbx_data.py status` shows the oldest archived day).
