#!/usr/bin/env python3
"""Data retention for Zabbix: retention policy, daily archive, nightly backup, cleanup, status.

See RETENTION.md for the whole process. Run as root on the Zabbix server, with ~/.zabbix_env loaded.

  policy  [--apply]             set the retention periods in Zabbix (Administration > Housekeeping),
                                and add the small clock indexes the nightly archive reads by
  archive [--day YYYY-MM-DD]    copy each finished day of history and trends to compressed files
  backup                        dump the database without history (configuration, events, users ...)
  prune                         delete archive days and backups older than the keep periods
  status  [--push]              sizes, free space, last archive/backup (--push: to the guard host)
  nightly                       archive + backup + prune + copy off the server + status --push
  load --from D --to D          put archived days back into the database (after a restore)
  cron [--apply]                install the schedule in /etc/cron.d/mdp-zabbix-data

Settings (environment, e.g. in ~/.zabbix_env):
  ARCHIVE_DIR=/var/backups/zabbix   where archive/ and backup/ go
  ARCHIVE_KEEP_DAYS=365             archived history days kept on this server
  BACKUP_KEEP_DAYS=14               nightly database dumps kept on this server
  COPY_TO=                          optional: second copy, a mounted path or rsync target (user@nas:/zabbix)
  RCA_TZ=UTC               day boundaries
PostgreSQL access: see tools/zbxdb.py (PG_CONTAINER, PG_LOCAL).
"""
import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zbxdb import DB  # noqa: E402

ARCHIVE_DIR = os.environ.get("ARCHIVE_DIR", "/var/backups/zabbix")
ARCHIVE_KEEP = int(os.environ.get("ARCHIVE_KEEP_DAYS", "365"))
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP_DAYS", "14"))
COPY_TO = os.environ.get("COPY_TO", "")
TZ = ZoneInfo(os.environ.get("RCA_TZ", "UTC"))
GUARD_HOST = "Zabbix server guard"
LOG = "/var/log/mdp-zabbix-data.log"

# the retention policy (RETENTION.md part 1)
POLICY = {
    "hk_history_mode": 1, "hk_history_global": 1, "hk_history": "90d",   # raw values: 90d TimescaleDB, 30d plain
    "hk_trends_mode": 1, "hk_trends_global": 1, "hk_trends": "730d",     # hourly min/avg/max: 2 years
    "hk_events_mode": 1, "hk_events_trigger": "730d",                    # problems: 2 years of reports
    "hk_events_service": "365d", "hk_events_internal": "7d",
    "hk_events_discovery": "7d", "hk_events_autoreg": "7d",
    "hk_services_mode": 1, "hk_services": "365d",
    "hk_sessions_mode": 1, "hk_sessions": "30d",
    "hk_audit_mode": 1, "hk_audit": "365d",                              # who changed what in Zabbix
}
TIMESCALE_POLICY = {"compression_status": 1, "compress_older": "7d"}

# table -> columns archived (all columns, so a day can be loaded back as it was)
TABLES = {
    "history": "itemid,clock,value,ns",
    "history_uint": "itemid,clock,value,ns",
    "history_str": "itemid,clock,value,ns",
    "history_text": "itemid,clock,value,ns",
    "history_log": "itemid,clock,timestamp,source,severity,value,logeventid,ns",
    "trends": "itemid,clock,num,value_min,value_avg,value_max",
    "trends_uint": "itemid,clock,num,value_min,value_avg,value_max",
}
VALUE_TABLE = {0: "history", 1: "history_str", 2: "history_log", 3: "history_uint", 4: "history_text"}
COPY_STAMP = ".mdp_copy_ok"      # in ARCHIVE_DIR: time of the last successful off-server copy


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    if sys.stdout.isatty():       # under cron stdout already goes to the log
        print(line)
    elif msg.startswith("ERROR"):
        print(line, file=sys.stderr)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def day_bounds(d):
    a = int(datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp())
    n = d + timedelta(days=1)
    return a, int(datetime(n.year, n.month, n.day, tzinfo=TZ).timestamp())


def day_dir(root, d):
    return os.path.join(root, "archive", f"{d:%Y}", f"{d:%m}", f"{d:%Y-%m-%d}")


def archived_days(root=ARCHIVE_DIR):
    out = []
    base = os.path.join(root, "archive")
    for dirpath, dirs, files in os.walk(base):
        if "MANIFEST.json" in files:
            try:
                out.append(date.fromisoformat(os.path.basename(dirpath)))
            except ValueError:
                pass
    return sorted(out)


def today():
    return datetime.now(TZ).date()


# ------------------------------------------------------------------ policy
def cmd_policy(do_apply):
    from zbxapi import ZabbixAPI
    api = ZabbixAPI()
    cur = api.call("housekeeping.get", {"output": "extend"})
    want = dict(POLICY)
    if cur.get("db_extension") == "timescaledb":
        want.update(TIMESCALE_POLICY)
    else:
        # plain PostgreSQL deletes old rows one by one: keep 30 days of raw values in the database (older RCA
        # readings come from the archive), so the disk and the housekeeper stay within what an HDD can do
        want["hk_history"] = "30d"
    if os.environ.get("HISTORY_DAYS"):
        want["hk_history"] = os.environ["HISTORY_DAYS"].rstrip("d") + "d"
    changes = {k: v for k, v in want.items() if str(cur.get(k)) != str(v)}
    for k, v in want.items():
        print(f"  {k:<22} {str(cur.get(k)):<8} -> {v}" + ("" if k in changes else "   (already)"))
    if cur.get("db_extension") != "timescaledb":
        clock_indexes(do_apply)
    if not changes:
        print("Retention policy already applied.")
        return
    if do_apply:
        api.call("housekeeping.update", changes)
        print(f"Applied {len(changes)} setting(s). The housekeeper removes older data at its next run (hourly).")
    else:
        print(f"{len(changes)} setting(s) would change; re-run with --apply.")


def clock_indexes(do_apply):
    """BRIN index on clock for each history and trends table (plain PostgreSQL).

    The Zabbix indexes start with itemid, so "all rows of one day" reads the whole table: at full rollout about
    150 GB from the HDD every night. A BRIN index is a few MB, costs almost nothing on insert, and lets the archive
    read only that day's part of the table. Zabbix ignores it; built once, without locking the table."""
    db = DB()
    have = {r[0]: r[1] == "t" for r in db.rows("SELECT c.relname, x.indisvalid FROM pg_index x JOIN pg_class c ON "
                                                 "c.oid = x.indexrelid WHERE c.relname LIKE 'mdp\\_%\\_clock\\_brin'")}
    for t in TABLES:
        name = f"mdp_{t}_clock_brin"
        if have.get(name):
            print(f"  index {name:<30} (already)")
            continue
        print(("  APPLY " if do_apply else "  would ") + f"create index {name} (BRIN on {t}.clock)")
        if do_apply:
            t0 = time.time()
            if name in have:          # left invalid by an interrupted run
                db.rows(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
            db.rows(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {t} USING brin (clock)")
            log(f"index {name} created in {time.time() - t0:.0f} s")


# ------------------------------------------------------------------ archive
def archive_day(db, d, root, force=False):
    """Write every table's rows of day d to <root>/archive/YYYY/MM/YYYY-MM-DD/. True if written."""
    final = day_dir(root, d)
    if os.path.exists(os.path.join(final, "MANIFEST.json")) and not force:
        return False
    a, b = day_bounds(d)
    tmp = final + ".partial"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    manifest = {"day": d.isoformat(), "tz": str(TZ), "from": a, "till": b, "created": int(time.time()),
                "tables": {}}
    t0 = time.time()
    # items map: lets a later RCA match rows to hosts even if item ids changed
    jobs = [("items", "SELECT i.itemid, h.host, i.key_, i.value_type FROM items i JOIN hosts h USING (hostid) "
                      "WHERE h.status IN (0,1) AND i.flags IN (0,4) ORDER BY 1")]
    jobs += [(t, f"SELECT {cols} FROM {t} WHERE clock >= {a} AND clock < {b} ORDER BY itemid, clock")
             for t, cols in TABLES.items()]
    for name, sql in jobs:
        path = os.path.join(tmp, f"{name}.csv.gz")
        p = db.copy_out(sql)
        h = hashlib.sha256()
        lines = 0
        with open(path, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as gz:
                for chunk in iter(lambda: p.stdout.read(1 << 20), b""):
                    gz.write(chunk)
                    h.update(chunk)
                    lines += chunk.count(b"\n")
        err = p.stderr.read().decode(errors="replace")
        if p.wait() != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError(f"archive {d} {name}: {err.strip()}")
        manifest["tables"][name] = {"lines": lines, "bytes": os.path.getsize(path), "sha256": h.hexdigest()}
    # read every file back once: a truncated gzip fails here, not on the day it is needed
    for name in manifest["tables"]:
        with gzip.open(os.path.join(tmp, f"{name}.csv.gz"), "rb") as f:
            while f.read(1 << 20):
                pass
    with open(os.path.join(tmp, "MANIFEST.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    shutil.rmtree(final, ignore_errors=True)
    os.rename(tmp, final)
    rows = sum(v["lines"] for k, v in manifest["tables"].items() if k != "items")
    size = sum(v["bytes"] for v in manifest["tables"].values())
    log(f"archive {d}: {rows} rows, {size / 1e6:.1f} MB in {time.time() - t0:.0f} s -> {final}")
    return True


def cmd_archive(day=None, days_back=7, force=False):
    """Archive the given day, or every finished day of the last days_back days not archived yet."""
    db = DB()
    os.makedirs(os.path.join(ARCHIVE_DIR, "archive"), exist_ok=True)
    if day:
        todo = [date.fromisoformat(day)]
    else:
        todo = [today() - timedelta(days=i) for i in range(days_back, 0, -1)]
    done = 0
    for d in todo:
        if d >= today():
            print(f"  {d} is not finished yet: skipped")
            continue
        if archive_day(db, d, ARCHIVE_DIR, force):
            done += 1
    if not done:
        print("Nothing new to archive.")


# ------------------------------------------------------------------ backup
def cmd_backup():
    """pg_dump of everything except history and trends rows (those are in the archive)."""
    db = DB()
    out_dir = os.path.join(ARCHIVE_DIR, "backup")
    os.makedirs(out_dir, exist_ok=True)
    os.chmod(out_dir, 0o700)          # the dump and _etc.tgz hold the database and the API token
    stamp = datetime.now(TZ).strftime("%Y-%m-%d_%H%M")
    path = os.path.join(out_dir, f"zabbix_{stamp}.dump")
    excl = ["--exclude-table-data=public.history*", "--exclude-table-data=public.trends*"]
    if db.has_timescale():
        # hypertable rows live in chunk tables (_hyper_<id>_<n>_chunk, compressed: compress_hyper_<cid>_<n>_chunk):
        # leave out those of history and trends; auditlog keeps its rows
        for hid, cid in db.rows("SELECT id, coalesce(compressed_hypertable_id, 0) FROM _timescaledb_catalog.hypertable "
                                "WHERE table_name LIKE 'history%' OR table_name LIKE 'trends%'"):
            excl.append(f"--exclude-table-data=_timescaledb_internal._hyper_{hid}_*")
            if cid != "0":
                excl.append(f"--exclude-table-data=_timescaledb_internal.compress_hyper_{cid}_*")
    t0 = time.time()
    cmd = db.cmd("pg_dump", "-d", db.dbname, "-Fc", "-Z", "6", *excl)
    with open(os.open(path + ".partial", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
        r = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE)
    if r.returncode:
        os.remove(path + ".partial")
        raise RuntimeError("pg_dump failed: " + r.stderr.decode(errors="replace").strip()[-500:])
    os.rename(path + ".partial", path)
    # the files needed to rebuild the server next to the dump
    extra = [p for p in ("/etc/zabbix/zabbix_server.conf", os.path.expanduser("~/.zabbix_env"),
                         "/etc/cron.d/mdp-zabbix-data") if os.path.exists(p)]
    if os.path.isdir("/etc/zabbix"):
        extra.append("/etc/zabbix")
    if extra:
        etc = path.replace(".dump", "_etc.tgz")
        subprocess.run(["tar", "-czf", etc, *sorted(set(extra))], stderr=subprocess.DEVNULL)
        if os.path.exists(etc):
            os.chmod(etc, 0o600)
    log(f"backup {path}: {os.path.getsize(path) / 1e6:.1f} MB in {time.time() - t0:.0f} s")
    return path


# ------------------------------------------------------------------ prune and copy
def cmd_prune(do_apply=True):
    cut = today() - timedelta(days=ARCHIVE_KEEP)
    for d in archived_days():
        if d < cut:
            p = day_dir(ARCHIVE_DIR, d)
            log(("delete " if do_apply else "would delete ") + f"archive {d} (older than {ARCHIVE_KEEP} days)")
            if do_apply:
                shutil.rmtree(p)
    bdir = os.path.join(ARCHIVE_DIR, "backup")
    if os.path.isdir(bdir):
        dumps = sorted(f for f in os.listdir(bdir) if f.endswith(".dump"))
        limit = time.time() - BACKUP_KEEP * 86400
        for f in dumps[:-3]:      # always keep the newest 3, whatever their age
            p = os.path.join(bdir, f)
            if os.path.getmtime(p) < limit:
                log(("delete " if do_apply else "would delete ") + f"backup {f} (older than {BACKUP_KEEP} days)")
                if do_apply:
                    os.remove(p)
                    etc = p.replace(".dump", "_etc.tgz")
                    if os.path.exists(etc):
                        os.remove(etc)
    # empty month/year folders
    for dirpath, dirs, files in sorted(os.walk(os.path.join(ARCHIVE_DIR, "archive")), reverse=True):
        if not dirs and not files and dirpath != os.path.join(ARCHIVE_DIR, "archive") and do_apply:
            os.rmdir(dirpath)


def cmd_copy():
    """Second copy away from this disk. Files are added, never deleted on the far side."""
    if not COPY_TO:
        return
    src = ARCHIVE_DIR.rstrip("/") + "/"
    if shutil.which("rsync"):
        r = subprocess.run(["rsync", "-a", "--exclude", "*.partial", src, COPY_TO.rstrip("/") + "/"],
                           capture_output=True, text=True)
    else:
        os.makedirs(COPY_TO, exist_ok=True)
        r = subprocess.run(["cp", "-rnp", src + ".", COPY_TO], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"copy to {COPY_TO} failed: {r.stderr.strip()[-300:]}")
    with open(os.path.join(ARCHIVE_DIR, COPY_STAMP), "w") as f:
        f.write(str(int(time.time())))
    log(f"copied {ARCHIVE_DIR} to {COPY_TO}")


# ------------------------------------------------------------------ status
def pfree(path):
    try:
        u = shutil.disk_usage(path)
        return round(100.0 * u.free / u.total, 1)
    except OSError:
        return None


def cmd_status(push=False):
    db = DB()
    from zbx_health import pg_data_dir
    st = {"db_size": int(db.one(f"SELECT pg_database_size('{db.dbname}')", 0))}
    st["db_pfree"] = pfree(pg_data_dir(db))
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    st["archive_pfree"] = pfree(ARCHIVE_DIR)
    days = archived_days()
    st["archive_days"] = len(days)
    st["archive_last"] = None
    if days:
        m = json.load(open(os.path.join(day_dir(ARCHIVE_DIR, days[-1]), "MANIFEST.json")))
        st["archive_last"] = m["created"]
        st["archive_newest_day"] = days[-1].isoformat()
        st["archive_oldest_day"] = days[0].isoformat()
    bdir = os.path.join(ARCHIVE_DIR, "backup")
    dumps = sorted(f for f in os.listdir(bdir) if f.endswith(".dump")) if os.path.isdir(bdir) else []
    st["backup_last"] = int(os.path.getmtime(os.path.join(bdir, dumps[-1]))) if dumps else None
    st["backup_count"] = len(dumps)
    if COPY_TO:
        try:
            st["copy_last"] = int(open(os.path.join(ARCHIVE_DIR, COPY_STAMP)).read().strip())
        except (OSError, ValueError):
            st["copy_last"] = 0       # set but never succeeded: the guard alarms
    # oldest raw value still in the database (one item, so it is an index lookup)
    old = db.one("SELECT min(clock) FROM history_uint WHERE itemid = (SELECT i.itemid FROM items i JOIN hosts h "
                 "USING (hostid) WHERE i.key_='icmpping' AND h.status=0 ORDER BY 1 LIMIT 1)")
    st["history_days"] = round((time.time() - int(old)) / 86400, 1) if old else 0
    for k, v in st.items():
        if k.endswith("_last") and v:
            v = f"{datetime.fromtimestamp(v, TZ):%Y-%m-%d %H:%M}"
        if k == "db_size":
            v = f"{v / 1e9:.2f} GB"
        print(f"  {k:<20} {v}")
    if push:
        from zbxapi import ZabbixAPI
        api = ZabbixAPI()
        now = int(time.time())
        vals = {"mdp.db.size": st["db_size"], "mdp.disk.pfree[db]": st["db_pfree"],
                "mdp.disk.pfree[archive]": st["archive_pfree"], "mdp.history.days": st["history_days"],
                "mdp.archive.last": st["archive_last"], "mdp.backup.last": st["backup_last"],
                "mdp.copy.last": st.get("copy_last")}
        data = [{"host": GUARD_HOST, "key": k, "value": str(v), "clock": now} for k, v in vals.items() if v is not None]
        r = api.call("history.push", data)
        bad = [x for x in r.get("data", []) if x.get("error")]
        if bad:
            print(f"  push: {len(bad)} value(s) refused, e.g. {bad[0]['error']} (run zbx_sites.py guard --apply)")
        else:
            print(f"  pushed {len(data)} value(s) to {GUARD_HOST}")
    return st


# ------------------------------------------------------------------ load (restore)
def cmd_load(d_from, d_to, tables=None):
    """Load archived days back into the database; rows already there are skipped."""
    db = DB()
    tables = tables or list(TABLES)
    d = date.fromisoformat(d_from)
    end = date.fromisoformat(d_to)
    while d <= end:
        folder = day_dir(ARCHIVE_DIR, d)
        if not os.path.exists(os.path.join(folder, "MANIFEST.json")):
            print(f"  {d}: not in the archive")
            d += timedelta(days=1)
            continue
        for t in tables:
            path = os.path.join(folder, f"{t}.csv.gz")
            if not os.path.exists(path):
                continue
            cols = TABLES[t]
            p = subprocess.Popen(db.psql("-q", interactive=True), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            p.stdin.write(f"CREATE TEMP TABLE load_tmp (LIKE {t} INCLUDING DEFAULTS);\n"
                          f"COPY load_tmp({cols}) FROM STDIN WITH (FORMAT csv);\n".encode())
            with gzip.open(path, "rb") as f:
                shutil.copyfileobj(f, p.stdin, 1 << 20)
            p.stdin.write(f"\\.\nINSERT INTO {t} SELECT * FROM load_tmp ON CONFLICT DO NOTHING;\n".encode())
            out, err = p.communicate()
            if p.returncode:
                raise RuntimeError(f"load {d} {t}: {err.decode(errors='replace').strip()}")
            print(f"  {d} {t}: loaded")
        d += timedelta(days=1)
    print("Done. Note: the housekeeper deletes loaded rows again if they are older than the history period.")


# ------------------------------------------------------------------ archive reader (used by zbx_rca.py)
def read_archive(root, wanted, t_from, t_till):
    """Series from archived days for the window.

    wanted: {(host, key): (current_itemid, value_type)} -> {current_itemid: [(clock, float)]}.
    Rows are matched by host and key, so items recreated since then still match."""
    out = {}
    d = datetime.fromtimestamp(t_from, TZ).date()
    end = datetime.fromtimestamp(t_till, TZ).date()
    while d <= end:
        folder = day_dir(root, d)
        d += timedelta(days=1)
        if not os.path.exists(os.path.join(folder, "MANIFEST.json")):
            continue
        idmap, tables = {}, set()
        with gzip.open(os.path.join(folder, "items.csv.gz"), "rt", newline="") as f:
            import csv
            for old_id, host, key, vt in csv.reader(f):
                hit = wanted.get((host, key))
                if hit:
                    idmap[old_id] = hit[0]
                    tables.add(VALUE_TABLE.get(int(vt), "history"))
        for t in tables & {"history", "history_uint"}:
            with gzip.open(os.path.join(folder, f"{t}.csv.gz"), "rt") as f:
                for line in f:
                    itemid, clock, value = line.split(",", 3)[:3]
                    new = idmap.get(itemid)
                    if new and t_from <= int(clock) <= t_till:
                        out.setdefault(new, []).append((int(clock), float(value)))
    for s in out.values():
        s.sort()
    return out


# ------------------------------------------------------------------ cron
def cmd_cron(do_apply):
    here = os.path.dirname(os.path.abspath(__file__))
    env = os.path.expanduser("~/.zabbix_env")
    run = f"bash -c 'source {env} && python3 {here}/zbx_data.py"
    text = (f"# MDP Zabbix data retention (tools/zbx_data.py, RETENTION.md). Times are server local time.\n"
            f"SHELL=/bin/bash\n"
            f"30 1 * * * root {run} nightly' >> {LOG} 2>&1\n"
            f"*/10 * * * * root {run} status --push' > /dev/null 2>&1\n")
    print(text)
    if not os.path.exists(env):
        print(f"WARNING: {env} not found; create it first (INSTALL.md step 3).")
    if do_apply:
        with open("/etc/cron.d/mdp-zabbix-data", "w") as f:
            f.write(text)
        os.chmod("/etc/cron.d/mdp-zabbix-data", 0o644)
        print("Installed /etc/cron.d/mdp-zabbix-data")
    else:
        print("Dry run; re-run with --apply to install.")


def cmd_nightly():
    failed = []
    for name, fn in (("archive", cmd_archive), ("backup", cmd_backup), ("prune", cmd_prune), ("copy", cmd_copy)):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - keep going, the guard host alarms on the missing step
            failed.append(name)
            log(f"ERROR {name}: {e}")
    try:
        cmd_status(push=True)
    except Exception as e:  # noqa: BLE001
        failed.append("status")
        log(f"ERROR status: {e}")
    if failed:
        log("nightly finished with errors in: " + ", ".join(failed))
        sys.exit(1)
    log("nightly finished OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("policy")
    p.add_argument("--apply", action="store_true")
    a = sub.add_parser("archive")
    a.add_argument("--day", help="one day, YYYY-MM-DD (default: every unarchived day of the last --days-back)")
    a.add_argument("--days-back", type=int, default=7)
    a.add_argument("--force", action="store_true", help="rewrite a day that is already archived")
    sub.add_parser("backup")
    pr = sub.add_parser("prune")
    pr.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("status")
    s.add_argument("--push", action="store_true", help="send the values to the guard host")
    sub.add_parser("nightly")
    ld = sub.add_parser("load")
    ld.add_argument("--from", dest="d_from", required=True)
    ld.add_argument("--to", dest="d_to", required=True)
    ld.add_argument("--tables", help="comma-separated (default all)")
    c = sub.add_parser("cron")
    c.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    try:
        if args.cmd == "policy":
            cmd_policy(args.apply)
        elif args.cmd == "archive":
            cmd_archive(args.day, args.days_back, args.force)
        elif args.cmd == "backup":
            cmd_backup()
        elif args.cmd == "prune":
            cmd_prune(not args.dry_run)
        elif args.cmd == "status":
            cmd_status(args.push)
        elif args.cmd == "nightly":
            cmd_nightly()
        elif args.cmd == "load":
            cmd_load(args.d_from, args.d_to, args.tables.split(",") if args.tables else None)
        elif args.cmd == "cron":
            cmd_cron(args.apply)
    except RuntimeError as e:
        log(f"ERROR {args.cmd}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
