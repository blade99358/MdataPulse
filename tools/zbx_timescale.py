#!/usr/bin/env python3
"""Move the Zabbix database to TimescaleDB (RETENTION.md part 3). Run as root on the Zabbix server.

  check [--target NEW]       what is installed and missing, and how long it takes (changes nothing)
  migrate --target NEW       stop zabbix-server, copy the database into the new TimescaleDB container NEW,
                             convert history/trends there. The old container is not changed (it is the undo).
  preload / migrate          (in place, only when the current container already runs a TimescaleDB image of the
                             same base system, e.g. both Alpine) add the library, then convert in place

The new container: an image with TimescaleDB for the SAME PostgreSQL major version, e.g.
timescale/timescaledb:2.30.2-pg17. Zabbix 7.4 refuses TimescaleDB newer than 2.30 (the server exits with
"Unsupported DB! timescaledb version ... is newer"), so the extension is created at the newest version
<= TS_MAX (default 2.30; raise it when a newer Zabbix supports more).
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zbxdb import DB, docker  # noqa: E402

SCHEMA_FILES = [
    "/usr/share/zabbix/sql-scripts/postgresql/timescaledb/schema.sql",   # zabbix-sql-scripts package (7.x)
    "/usr/share/zabbix-sql-scripts/postgresql/timescaledb/schema.sql",
    "/usr/share/doc/zabbix-server-postgresql/timescaledb.sql",           # Docker server image
]
BACKUP_DIR = os.environ.get("ARCHIVE_DIR", "/var/backups/zabbix")
TS_MAX = os.environ.get("TS_MAX", "2.30")


def vkey(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def schema_file():
    for p in SCHEMA_FILES + glob.glob("/usr/share/zabbix*/**/timescaledb/schema.sql", recursive=True):
        if os.path.exists(p):
            return p
    return None


def server_running():
    return subprocess.run(["systemctl", "is-active", "--quiet", "zabbix-server"],
                          stderr=subprocess.DEVNULL).returncode == 0


def os_of(db):
    if not db.container:
        return "host"
    out = subprocess.run(["docker", "exec", db.container, "cat", "/etc/os-release"], capture_output=True, text=True)
    m = re.search(r'^ID=("?)(\w+)\1', out.stdout, re.M)
    return m.group(2) if m else "?"


def ts_version(db):
    """Newest available TimescaleDB version Zabbix accepts, or None."""
    vers = [r[0] for r in db.rows("SELECT version FROM pg_available_extension_versions WHERE name='timescaledb'")]
    ok = [v for v in vers if vkey(v)[:2] <= vkey(TS_MAX)[:2]]
    return max(ok, key=vkey) if ok else None


def target_db(name):
    os.environ["PG_CONTAINER"] = name
    t = DB()
    os.environ.pop("PG_CONTAINER", None)
    return t


def describe(db, label):
    ver = db.one("SHOW server_version")
    img = docker("inspect", "--format", "{{.Config.Image}}", db.container).strip() if db.container else "-"
    print(f"{label:<10} {db.where}, image {img}, {os_of(db)}, PostgreSQL {ver}")
    return ver.split(".")[0]


def cmd_check(db, target=None):
    major = describe(db, "Current")
    inst = db.has_timescale()
    size = int(db.one(f"SELECT pg_database_size('{db.dbname}')", 0))
    print(f"           database {size / 1e9:.2f} GB, TimescaleDB in it: {inst or 'no'}, "
          f"db_extension = {db.setting('db_extension') or '(none)'}")
    # converting copies every row about twice (copy + rebuild); a spinning disk does ~15-30 MB/s for this
    print(f"Estimated downtime about {max(3, size / 10e6 / 60):.0f} min on a spinning disk "
          f"(no data is collected meanwhile)")
    sf = schema_file()
    print(f"Zabbix schema file {sf or 'NOT FOUND: apt install zabbix-sql-scripts (same version as the server)'}")
    print(f"zabbix-server      {'running (systemd)' if server_running() else 'not running as a systemd service'}")
    free = shutil.disk_usage(BACKUP_DIR if os.path.isdir(BACKUP_DIR) else "/").free
    print(f"Backup space       {free / 1e9:.0f} GB free in {BACKUP_DIR} (needs about {size / 3e9:.1f} GB)")
    if inst:
        print("\nAlready on TimescaleDB. Nothing to do.")
        return False
    ok = bool(sf)
    if target:
        t = target_db(target)
        tmajor = describe(t, "Target")
        v = ts_version(t)
        print(f"           TimescaleDB usable: {v or 'NONE <= ' + TS_MAX}, preloaded: "
              f"{'yes' if 'timescaledb' in (t.one('SHOW shared_preload_libraries') or '') else 'NO'}")
        n = int(t.one("SELECT count(*) FROM pg_tables WHERE schemaname='public'", 0))
        if tmajor != major:
            ok = False
            print(f"NOT OK: target is PostgreSQL {tmajor}, current is {major}; use a -pg{major} image.")
        if not v:
            ok = False
            print(f"NOT OK: target image has no TimescaleDB <= {TS_MAX}; use e.g. timescale/timescaledb:2.30.2-pg{major}")
        if n:
            ok = False
            print(f"NOT OK: target database {t.dbname} already has {n} tables; it must be empty.")
        print("\nREADY: python3 tools/zbx_timescale.py migrate --target " + target if ok else "")
    else:
        v = ts_version(db)
        pre = "timescaledb" in (db.one("SHOW shared_preload_libraries") or "")
        print(f"           TimescaleDB usable here: {v or 'no'}, preloaded: {'yes' if pre else 'no'}")
        print("\nRecommended: start a new TimescaleDB container next to this one and copy into it (RETENTION.md "
              "part 3):\n  python3 tools/zbx_timescale.py check --target <new-container>")
        if v and not pre:
            print("In place is possible here: python3 tools/zbx_timescale.py preload")
        elif v and pre and ok:
            print("In place is ready: python3 tools/zbx_timescale.py migrate")
        ok = ok and bool(v) and pre
    return ok


def cmd_preload(db):
    pre = db.one("SHOW shared_preload_libraries") or ""
    if "timescaledb" in pre:
        print("timescaledb is already preloaded.")
        return
    if not ts_version(db):
        sys.exit(f"This PostgreSQL has no TimescaleDB <= {TS_MAX}.")
    new = ",".join([x for x in pre.split(",") if x.strip()] + ["timescaledb"])
    db.rows(f"ALTER SYSTEM SET shared_preload_libraries = '{new}'")
    print(f"shared_preload_libraries = '{new}'")
    restart(db)


def restart(db):
    if not db.container:
        print("Now restart PostgreSQL (systemctl restart postgresql).")
        return
    print(f"Restarting container {db.container} ...")
    docker("restart", db.container)
    for _ in range(60):
        time.sleep(2)
        try:
            if "timescaledb" in (db.one("SHOW shared_preload_libraries") or ""):
                return
        except RuntimeError:
            pass
    sys.exit("PostgreSQL did not come back within 2 minutes: docker logs " + db.container)


def convert(t):
    """Create the extension (pinned version) and run Zabbix's TimescaleDB schema script in database t."""
    if "timescaledb" not in (t.one("SHOW shared_preload_libraries") or ""):
        cmd_preload(t)
    v = ts_version(t)
    have = t.has_timescale()
    if have and vkey(have) > vkey(v):
        # TimescaleDB images create the newest version in POSTGRES_DB at first start: replace it while the
        # database has no hypertables yet
        if t.one("SELECT count(*) FROM timescaledb_information.hypertables", "0") != "0":
            raise RuntimeError(f"timescaledb {have} is newer than Zabbix supports and already holds hypertables")
        print(f"Replacing timescaledb {have} (too new for Zabbix) with {v} ...")
        t.rows("DROP EXTENSION timescaledb")
    print(f"Creating extension timescaledb {v} ...")
    t.rows(f"CREATE EXTENSION IF NOT EXISTS timescaledb VERSION '{v}' CASCADE")
    sf = schema_file()
    print(f"Converting history and trends to hypertables ({sf}) ...")
    t0 = time.time()
    r = t.exec_file(sf)
    out = (r.stdout or b"").decode(errors="replace") + (r.stderr or b"").decode(errors="replace")
    if r.returncode or "configured successfully" not in out:
        print(out[-2000:])
        raise RuntimeError("conversion failed")
    hyper = [h[0] for h in t.rows("SELECT hypertable_name FROM timescaledb_information.hypertables ORDER BY 1")]
    print(f"  done in {time.time() - t0:.0f} s; hypertables: {', '.join(hyper)}")


def dump(db, tag):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"{tag}_{datetime.now():%Y%m%d_%H%M}.dump")
    print(f"Full backup to {path} ...")
    with open(path, "wb") as f:
        r = subprocess.run(db.cmd("pg_dump", "-d", db.dbname, "-Fc", "-Z", "6"), stdout=f, stderr=subprocess.PIPE)
    if r.returncode:
        raise RuntimeError("backup failed, nothing changed: " + r.stderr.decode(errors="replace")[-400:])
    print(f"  {os.path.getsize(path) / 1e9:.2f} GB")
    return path


def cmd_migrate(db, target, yes):
    if not cmd_check(db, target):
        sys.exit("\nNot ready: fix the NOT OK / NOT FOUND lines above first.")
    if not yes:
        a = input("\nThis stops Zabbix monitoring for the time shown above. Type MIGRATE to continue: ")
        if a.strip() != "MIGRATE":
            sys.exit("Cancelled.")
    was_running = server_running()
    if was_running:
        print("Stopping zabbix-server ...")
        subprocess.run(["systemctl", "stop", "zabbix-server"], check=True)
    ok = False
    try:
        path = dump(db, "before_timescaledb")
        if target:
            t = target_db(target)
            if "timescaledb" not in (t.one("SHOW shared_preload_libraries") or ""):
                cmd_preload(t)
            print(f"Restoring into {target} ...")
            t0 = time.time()
            with open(path, "rb") as f:
                r = subprocess.run(t.cmd("pg_restore", "-d", t.dbname, "--no-owner", "--role", t.user, "-j", "1",
                                         interactive=True), stdin=f, capture_output=True)
            if r.returncode:
                raise RuntimeError("restore failed: " + r.stderr.decode(errors="replace")[-800:])
            print(f"  done in {time.time() - t0:.0f} s")
            convert(t)
        else:
            convert(db)
        ok = True
    except RuntimeError as e:
        print(f"\nFAILED: {e}")
    finally:
        if was_running and (ok and not target or not ok):
            print("Starting zabbix-server ...")
            subprocess.run(["systemctl", "start", "zabbix-server"])
    if not ok:
        sys.exit("Nothing was switched: Zabbix uses the old database as before." if target else
                 "Zabbix is on the old data; restore the dump above if the database looks wrong.")
    if target:
        print(f"\nCopied and converted. zabbix-server is STOPPED. Now point Zabbix to {target}\n"
              "(RETENTION.md part 3, step 5: DBHost/DBPort in zabbix_server.conf and the web container),\n"
              "then: systemctl start zabbix-server && python3 tools/zbx_data.py policy --apply")
    else:
        print("\nConverted in place. Next: python3 tools/zbx_data.py policy --apply")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["check", "preload", "migrate"])
    ap.add_argument("--target", help="new TimescaleDB container to copy into (recommended)")
    ap.add_argument("--yes", action="store_true", help="migrate without asking")
    args = ap.parse_args()
    db = DB()
    if args.cmd == "check":
        cmd_check(db, args.target)
    elif args.cmd == "preload":
        cmd_preload(db)
    else:
        cmd_migrate(db, args.target, args.yes)


if __name__ == "__main__":
    main()
