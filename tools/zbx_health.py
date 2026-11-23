#!/usr/bin/env python3
"""Measure the Zabbix server and recommend cache, process and PostgreSQL settings.

Read-only: it reads /proc, zabbix_server.conf, the server logs and the database, and changes nothing.
Run it on the Zabbix server as root:

  python3 tools/zbx_health.py                       # full check (takes about 1 minute)
  python3 tools/zbx_health.py --target-hosts 1000   # size for the full rollout (default 1000)
  python3 tools/zbx_health.py --quick               # skip the 60 s insert-rate measurement

It prints a report and writes a folder health_<date>/ with:
  report.txt                    the report (send this one back for review)
  zabbix_server_tuning.conf     recommended zabbix_server.conf lines
  postgresql_tuning.sql         recommended PostgreSQL settings (ALTER SYSTEM), see OPTIMIZE.md

PostgreSQL is reached with docker exec (container found automatically, or PG_CONTAINER=<name>),
or with the host's psql when PG_LOCAL=1.
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zbxdb import DB, SERVER_CONF, docker, read_conf  # noqa: E402

MB = 1024 ** 2
GB = 1024 ** 3

# zabbix_server.conf keys that matter here, with the 7.4 defaults
DEFAULTS = {
    "CacheSize": "32M", "HistoryCacheSize": "16M", "HistoryIndexCacheSize": "4M", "TrendCacheSize": "4M",
    "TrendFunctionCacheSize": "4M", "ValueCacheSize": "8M",
    "StartPollers": "5", "StartSNMPPollers": "1", "StartAgentPollers": "1", "StartPingers": "1",
    "StartPollersUnreachable": "1", "StartPreprocessors": "16", "StartLLDProcessors": "2",
    "StartDiscoverers": "5", "StartTrappers": "5", "StartDBSyncers": "4", "StartEscalators": "1",
    "HousekeepingFrequency": "1", "MaxHousekeeperDelete": "5000", "Timeout": "3",
    "UnreachablePeriod": "45", "UnavailableDelay": "60", "UnreachableDelay": "15", "LogSlowQueries": "0",
}
CACHES = ["CacheSize", "HistoryCacheSize", "HistoryIndexCacheSize", "TrendCacheSize",
          "TrendFunctionCacheSize", "ValueCacheSize"]

# internal items (Zabbix server health template, or the "Zabbix server guard" host)
INTERNAL = {
    "zabbix[rcache,buffer,pused]": "CacheSize (configuration cache) % used",
    "zabbix[wcache,history,pused]": "HistoryCacheSize % used",
    "zabbix[wcache,index,pused]": "HistoryIndexCacheSize % used",
    "zabbix[wcache,trend,pused]": "TrendCacheSize % used",
    "zabbix[vcache,buffer,pused]": "ValueCacheSize % used",
    "zabbix[vcache,cache,mode]": "Value cache low-memory mode (1 = yes)",
    "zabbix[wcache,values]": "Values processed per second",
    "zabbix[queue,10m]": "Items late by more than 10 min",
    "zabbix[preprocessing_queue]": "Preprocessing queue",
}
BUSY = ["poller", "snmp poller", "agent poller", "icmp pinger", "unreachable poller", "history syncer",
        "preprocessing worker", "lld worker", "discovery worker", "trapper", "housekeeper", "escalator",
        "configuration syncer", "timer", "alerter"]


def size(s):
    """'64M' -> bytes."""
    m = re.fullmatch(r"\s*(\d+)\s*([KMGT]?)B?\s*", str(s), re.I)
    if not m:
        return 0
    return int(m.group(1)) * {"": 1, "K": 1024, "M": MB, "G": GB, "T": GB * 1024}[m.group(2).upper()]


def human(b):
    for unit, d in (("T", GB * 1024), ("G", GB), ("M", MB), ("K", 1024)):
        if b >= d:
            v = b / d
            return f"{v:.0f}{unit}" if v >= 10 or v == int(v) else f"{v:.1f}{unit}"
    return f"{b:.0f}B"


def mb(b):
    return f"{int(b // MB)}MB"


def pow2_mb(b, lo):
    """Round up to a power of two in MB, at least lo bytes."""
    b = max(b, lo)
    return MB * 2 ** math.ceil(math.log2(b / MB))


def seconds(s, macros=None):
    """Item delay ('1m', '30s', '{$IF.INTERVAL}', '1m;wd1-5,9-18') -> seconds, or None."""
    s = (s or "").split(";")[0].strip()
    if macros is not None and s.startswith("{$"):
        s = macros(s)
    m = re.fullmatch(r"(\d+)([smhdw]?)", s or "")
    if not m:
        return None
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]


def sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# ------------------------------------------------------------------ host
def host_facts():
    f = {"cpus": os.cpu_count() or 1}
    mem = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":", 1)
        mem[k] = int(v.split()[0]) * 1024
    f["ram"] = mem.get("MemTotal", 0)
    f["ram_avail"] = mem.get("MemAvailable", 0)
    f["swap"] = mem.get("SwapTotal", 0)
    f["swap_used"] = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
    f["shm_used"] = mem.get("Shmem", 0)
    f["load"] = open("/proc/loadavg").read().split()[:3]
    try:
        f["swappiness"] = open("/proc/sys/vm/swappiness").read().strip()
    except OSError:
        f["swappiness"] = "?"
    return f


def disk_of(path):
    """(total, used, free, rotational) for the filesystem holding path."""
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return None
    src = sh("df", "--output=source", path).splitlines()[-1:] or [""]
    dev = os.path.basename(os.path.realpath(src[0].strip()))
    rot = None
    for cand in (dev, re.sub(r"p?\d+$", "", dev)):
        p = f"/sys/block/{cand}/queue/rotational"
        if cand and os.path.exists(p):
            rot = open(p).read().strip() == "1"
            break
        p = f"/sys/class/block/{cand}/../queue/rotational"
        if cand and os.path.exists(p):
            rot = open(p).read().strip() == "1"
            break
    return {"path": path, "dev": src[0].strip(), "total": u.total, "used": u.used, "free": u.free, "rotational": rot}


# ------------------------------------------------------------------ zabbix server config
def server_config():
    """(where, {key: value}) from zabbix_server.conf or the server container's ZBX_* env."""
    if os.path.exists(SERVER_CONF):
        return SERVER_CONF, read_conf()
    if shutil.which("docker"):
        for row in sh("docker", "ps", "--format", "{{.Names}}\t{{.Image}}").splitlines():
            name, image = row.split("\t")
            if "zabbix-server" in image:
                env = json.loads(sh("docker", "inspect", "--format", "{{json .Config.Env}}", name) or "[]")
                conf = {}
                for e in env:
                    k, _, v = e.partition("=")
                    if k.startswith("ZBX_"):
                        for key in DEFAULTS:
                            if k == "ZBX_" + key.upper():
                                conf[key] = v
                return f"container {name} (ZBX_* environment)", conf
    return None, {}


def crash_evidence(conf):
    """Lines from the last 30 days that show the server or the kernel killed something."""
    pat = re.compile(r"out of memory|cannot allocate|zbx_mem_malloc|__zbx_mem|cache is full|"
                     r"one child process died|zabbix server stopped|killed process|oom-kill|segmentation", re.I)
    found = []
    if shutil.which("journalctl"):
        for args in (["-u", "zabbix-server"], ["-k"]):
            out = sh("journalctl", *args, "--since", "30 days ago", "--no-pager", "-o", "short-iso")
            found += [l for l in out.splitlines() if pat.search(l) and ("-k" not in args or re.search(
                r"killed process|oom", l, re.I))]
    log = conf.get("LogFile", "/var/log/zabbix/zabbix_server.log")
    for p in (log, log + ".1"):
        try:
            with open(p, errors="replace") as f:
                found += [l.rstrip() for l in f if pat.search(l)]
        except OSError:
            pass
    return found


# ------------------------------------------------------------------ database
def db_facts(db, quick):
    d = {"where": db.where}
    d["pg_version"] = db.one("SHOW server_version")
    d["timescaledb"] = db.has_timescale()
    d["db_size"] = int(db.one(f"SELECT pg_database_size('{db.dbname}')", 0))
    d["zbx_db_version"] = db.one("SELECT mandatory FROM dbversion")
    names = ["shared_buffers", "effective_cache_size", "work_mem", "maintenance_work_mem", "max_connections",
             "synchronous_commit", "random_page_cost", "effective_io_concurrency", "max_wal_size",
             "checkpoint_timeout", "checkpoint_completion_target", "wal_compression", "autovacuum_max_workers",
             "autovacuum_vacuum_cost_limit", "max_worker_processes", "shared_preload_libraries", "huge_pages"]
    d["pg"] = dict(db.rows("SELECT name, setting || coalesce(unit, '') FROM pg_settings WHERE name IN (" +
                           ",".join(f"'{n}'" for n in names) + ")"))
    d["pg_shm"] = db.one("SELECT setting FROM pg_settings WHERE name='dynamic_shared_memory_type'")
    d["connections"] = int(db.one("SELECT count(*) FROM pg_stat_activity", 0))
    hk = ["hk_history_global", "hk_history", "hk_trends_global", "hk_trends", "hk_events_mode",
          "hk_events_trigger", "hk_events_internal", "hk_events_discovery", "hk_events_autoreg",
          "hk_events_service", "hk_sessions", "hk_audit", "compression_status", "compress_older", "db_extension"]
    d["housekeeping"] = {k: db.setting(k) for k in hk}
    d["brin"] = int(db.one("SELECT count(*) FROM pg_indexes WHERE schemaname='public' AND indexname LIKE "
                           "'mdp\\_%\\_clock\\_brin'", 0))

    # table sizes: hypertables are summed over their chunks
    if d["timescaledb"]:
        sizes = db.rows("SELECT hypertable_name, hypertable_size(format('%I.%I', hypertable_schema, hypertable_name)"
                        "::regclass) FROM timescaledb_information.hypertables")
    else:
        sizes = []
    known = {n for n, _ in sizes}
    sizes += [r for r in db.rows("SELECT relname, pg_total_relation_size(relid) FROM pg_stat_user_tables "
                                 "WHERE schemaname='public' ORDER BY 2 DESC LIMIT 25") if r[0] not in known]
    d["tables"] = sorted(((n, int(s)) for n, s in sizes), key=lambda x: -x[1])[:12]
    d["rows_est"] = {n: max(0, int(float(r))) for n, r in db.rows(
        "SELECT relname, reltuples FROM pg_class WHERE relname IN ('history','history_uint','history_str',"
        "'history_text','history_log','trends','trends_uint')")}
    if d["timescaledb"]:
        d["rows_est"].update({n: int(r or 0) for n, r in db.rows(
            "SELECT hypertable_name, (SELECT sum(c.reltuples) FROM timescaledb_information.chunks k JOIN pg_class c "
            "ON c.relname = k.chunk_name WHERE k.hypertable_name = h.hypertable_name AND c.reltuples > 0)::bigint "
            "FROM timescaledb_information.hypertables h")})
        comp = db.rows("SELECT hypertable_name, sum(before_compression_total_bytes), "
                       "sum(after_compression_total_bytes) FROM timescaledb_information.hypertables h, "
                       "LATERAL hypertable_compression_stats(format('%I', h.hypertable_name)::regclass) "
                       "GROUP BY 1") if d["timescaledb"] else []
        d["compression"] = {n: (int(b or 0), int(a or 0)) for n, b, a in comp}

    # dead rows: the cost of delete-based housekeeping
    d["dead"] = {n: (int(l), int(dd)) for n, l, dd in db.rows(
        "SELECT relname, n_live_tup, n_dead_tup FROM pg_stat_user_tables WHERE relname IN "
        "('history','history_uint','trends','trends_uint','events','problem','alerts','auditlog')")}

    # hosts and items
    d["hosts"] = int(db.one("SELECT count(*) FROM hosts WHERE status=0 AND flags IN (0,4)", 0))
    d["hosts_disabled"] = int(db.one("SELECT count(*) FROM hosts WHERE status=1 AND flags IN (0,4)", 0))
    d["items"] = int(db.one("SELECT count(*) FROM items i JOIN hosts h USING (hostid) WHERE h.status=0 "
                            "AND i.status=0 AND i.flags IN (0,4)", 0))
    d["items_numeric"] = int(db.one("SELECT count(*) FROM items i JOIN hosts h USING (hostid) WHERE h.status=0 "
                                    "AND i.status=0 AND i.flags IN (0,4) AND i.value_type IN (0,3)", 0))
    d["triggers"] = int(db.one("SELECT count(DISTINCT t.triggerid) FROM triggers t JOIN functions f USING "
                               "(triggerid) JOIN items i USING (itemid) JOIN hosts h USING (hostid) "
                               "WHERE h.status=0 AND t.status=0 AND t.flags IN (0,4)", 0))
    d["unsupported"] = db.rows(
        "SELECT i.key_, count(*) FROM items i JOIN hosts h USING (hostid) JOIN item_rtdata r USING (itemid) "
        "WHERE h.status=0 AND i.status=0 AND i.flags IN (0,4) AND r.state=1 GROUP BY 1 ORDER BY 2 DESC LIMIT 8")
    d["unsupported_total"] = sum(int(c) for _, c in d["unsupported"])
    d["vps_required"], d["delays"], per_host = required_vps(db)
    # network devices = hosts linked to a MDP template; they are what grows with the rollout
    mdp = {r[0] for r in db.rows("SELECT DISTINCT ht.hostid FROM hosts_templates ht JOIN hosts t ON "
                                  "t.hostid=ht.templateid JOIN hosts h ON h.hostid=ht.hostid WHERE "
                                  "t.host LIKE 'MDP %' AND t.host <> 'MDP Zabbix Server Guard' AND h.status=0")}
    items_by_host = dict(db.rows("SELECT i.hostid, count(*) FROM items i JOIN hosts h USING (hostid) WHERE "
                                 "h.status=0 AND i.status=0 AND i.flags IN (0,4) GROUP BY 1"))
    d["mdp_hosts"] = len(mdp)
    d["mdp_items"] = sum(int(items_by_host.get(x, 0)) for x in mdp)
    d["mdp_vps"] = sum(per_host.get(x, 0) for x in mdp)

    # internal metrics: last value and 7-day peak
    keys = ",".join(f"'{k}'" for k in list(INTERNAL) + [f"zabbix[process,{p},avg,busy]" for p in BUSY])
    d["internal"] = {}
    for key, itemid, vt in db.rows(
            "SELECT DISTINCT ON (i.key_) i.key_, i.itemid, i.value_type FROM items i JOIN hosts h USING (hostid) "
            f"WHERE h.status=0 AND i.status=0 AND i.key_ IN ({keys}) ORDER BY i.key_, i.itemid"):
        tbl = "history" if vt == "0" else "history_uint"
        now = int(time.time())
        r = db.rows(f"SELECT (SELECT value FROM {tbl} WHERE itemid={itemid} AND clock>{now - 1800} "
                    f"ORDER BY clock DESC LIMIT 1), (SELECT max(value) FROM {tbl} WHERE itemid={itemid} "
                    f"AND clock>{now - 7 * 86400})")
        last, peak = (r[0] + [None, None])[:2] if r else (None, None)
        if last not in (None, ""):
            d["internal"][key] = (float(last), float(peak) if peak not in (None, "") else None)

    # actual insert rate over 60 s
    if not quick:
        d["vps_measured"] = measure_inserts(db, d["timescaledb"])
    return d


def required_vps(db):
    """Values per second the enabled items ask for, resolving {$MACRO} delays through templates."""
    gm = dict(db.rows("SELECT macro, value FROM globalmacro"))
    hm = {}
    for hid, macro, value in db.rows("SELECT hostid, macro, value FROM hostmacro"):
        hm.setdefault(hid, {})[macro] = value
    links = {}
    for hid, tid in db.rows("SELECT hostid, templateid FROM hosts_templates"):
        links.setdefault(hid, []).append(tid)
    cache = {}

    def lookup(hid, macro, depth=0):
        if macro in hm.get(hid, {}):
            return hm[hid][macro]
        if depth < 6:
            for t in links.get(hid, []):
                v = lookup(t, macro, depth + 1)
                if v is not None:
                    return v
        return None

    total, by_delay, per_host = 0.0, {}, {}
    for hid, delay, n in db.rows(
            "SELECT i.hostid, i.delay, count(*) FROM items i JOIN hosts h USING (hostid) WHERE h.status=0 "
            "AND i.status=0 AND i.flags IN (0,4) AND i.type NOT IN (2,17,18) GROUP BY 1,2"):
        key = (hid, delay)
        if key not in cache:
            cache[key] = seconds(delay, lambda m: lookup(hid, m) or gm.get(m) or "")
        sec = cache[key]
        if sec:
            total += int(n) / sec
            per_host[hid] = per_host.get(hid, 0) + int(n) / sec
            by_delay[sec] = by_delay.get(sec, 0) + int(n)
    return total, sorted(by_delay.items()), per_host


def measure_inserts(db, ts):
    q = ("SELECT coalesce(sum(n_tup_ins),0) FROM pg_stat_user_tables WHERE relname IN "
         "('history','history_uint','history_str','history_text','history_log')")
    if ts:
        q += (" OR relname IN (SELECT chunk_name FROM timescaledb_information.chunks WHERE hypertable_name IN "
              "('history','history_uint','history_str','history_text','history_log'))")
    a = int(db.one(q, 0))
    t0 = time.time()
    print("Measuring the insert rate for 60 s ...", file=sys.stderr)
    time.sleep(60)
    b = int(db.one(q, 0))
    return max(0, b - a) / (time.time() - t0)


def pg_data_dir(db):
    if db.container:
        try:
            mounts = json.loads(docker("inspect", "--format", "{{json .Mounts}}", db.container))
        except Exception:  # noqa: BLE001
            mounts = []
        datadir = db.one("SHOW data_directory") or "/var/lib/postgresql/data"
        best = None
        for m in mounts:
            if datadir.startswith(m.get("Destination", "~")) and (not best or len(m["Destination"]) > len(best[0])):
                best = (m["Destination"], m.get("Source"))
        return best[1] if best else "/var/lib/docker"
    try:
        return db.one("SHOW data_directory") or "/"
    except RuntimeError:   # needs pg_read_all_settings; the usual place of a host PostgreSQL
        return "/var/lib/postgresql" if os.path.isdir("/var/lib/postgresql") else "/"


# ------------------------------------------------------------------ recommendations
def recommend(h, d, conf, target_hosts, hdd):
    rec = {}
    ram = h["ram"]
    big = ram >= 15 * GB          # "16 GB" hosts report a little less
    # growth comes from network devices: per-device load measured on the MDP hosts when there are
    # enough of them, else ~40 items and 0.6 values/s per device (estimate from the MDP templates)
    n = d["mdp_hosts"]
    per_items = d["mdp_items"] / n if n >= 10 else 40
    per_vps = d["mdp_vps"] / n if n >= 10 else 0.6
    add = max(target_hosts - n, 0)
    vps_now = d.get("vps_measured") or d["vps_required"]
    items_t = d["items"] + add * per_items
    vps_t = vps_now + add * per_vps
    scale = vps_t / vps_now if vps_now else 1.0
    iscale = items_t / d["items"] if d["items"] else 1.0
    cur = {k: size(conf.get(k, DEFAULTS[k])) for k in CACHES}
    pct = {k: d["internal"].get(key, (None, None)) for k, key in (
        ("CacheSize", "zabbix[rcache,buffer,pused]"), ("HistoryCacheSize", "zabbix[wcache,history,pused]"),
        ("HistoryIndexCacheSize", "zabbix[wcache,index,pused]"), ("TrendCacheSize", "zabbix[wcache,trend,pused]"),
        ("ValueCacheSize", "zabbix[vcache,buffer,pused]"))}

    def from_use(k, per_unit_guess, units_now, units_t, lo):
        """Use at full rollout = measured peak + a generous per-item cost for the new items; size for 2x that."""
        last, peak = pct.get(k, (None, None))
        use = peak if peak is not None else last
        used = cur[k] * use / 100 if use is not None and cur[k] else per_unit_guess * units_now
        need = used + per_unit_guess * max(units_t - units_now, 0)
        return pow2_mb(need * 2, lo)

    # configuration cache: a full cache stops the server, so it gets the most headroom
    rec["CacheSize"] = min(from_use("CacheSize", 4096, d["items"], items_t, 1 * GB if big else 512 * MB), 4 * GB)
    # history write cache: holds values while the database is slow (HDD, housekeeping, backups)
    rec["HistoryCacheSize"] = min(pow2_mb(vps_t * 600 * 100, 512 * MB if big else 256 * MB), 2 * GB)
    rec["HistoryIndexCacheSize"] = max(rec["HistoryCacheSize"] // 4, 64 * MB)
    rec["TrendCacheSize"] = min(from_use("TrendCacheSize", 200, d["items_numeric"], d["items_numeric"] * iscale,
                                         128 * MB), 1 * GB)
    rec["TrendFunctionCacheSize"] = 64 * MB
    rec["ValueCacheSize"] = min(from_use("ValueCacheSize", 8192, d["items"], items_t, 1 * GB if big else 512 * MB),
                                4 * GB)
    zbx_mem = sum(rec[k] for k in CACHES)

    snmp = int(conf.get("StartSNMPPollers", DEFAULTS["StartSNMPPollers"]))
    rec["StartSNMPPollers"] = max(snmp, 2 if vps_t < 1500 else 4)
    rec["StartPollers"] = max(int(conf.get("StartPollers", 5)), 10)
    rec["StartPingers"] = max(int(conf.get("StartPingers", 1)), min(20, math.ceil(target_hosts / 100)))
    rec["StartPollersUnreachable"] = max(int(conf.get("StartPollersUnreachable", 1)), 5)
    rec["StartLLDProcessors"] = max(int(conf.get("StartLLDProcessors", 2)), 4)
    rec["StartDBSyncers"] = 4 if hdd else max(4, min(8, h["cpus"]))
    rec["Timeout"] = max(int(conf.get("Timeout", 3)), 5)
    rec["HousekeepingFrequency"] = 1
    rec["MaxHousekeeperDelete"] = 0 if d.get("timescaledb") else 10000
    rec["LogSlowQueries"] = 3000
    # raise any process that is already busy
    for proc, key in (("StartSNMPPollers", "snmp poller"), ("StartPollers", "poller"),
                      ("StartPingers", "icmp pinger"), ("StartPollersUnreachable", "unreachable poller"),
                      ("StartLLDProcessors", "lld worker"), ("StartDBSyncers", "history syncer")):
        busy = d["internal"].get(f"zabbix[process,{key},avg,busy]")
        if busy and (busy[1] or busy[0]) > 60:
            rec[proc] = max(rec[proc], math.ceil(int(conf.get(proc, DEFAULTS[proc])) * 2))

    # PostgreSQL: what is left after Zabbix caches and ~3 GB for OS, nginx/php, RCA, Docker
    left = max(ram - zbx_mem - 3 * GB, ram // 3)
    pg = {
        "shared_buffers": mb(min(ram // 4, 8 * GB, left // 2)),
        "effective_cache_size": mb(int(left * 0.75)),
        "work_mem": "16MB",
        "maintenance_work_mem": mb(min(1 * GB, ram // 24)),
        "autovacuum_work_mem": mb(min(512 * MB, ram // 48)),
        "max_connections": "300",
        "synchronous_commit": "off",
        "wal_compression": "on",
        "wal_buffers": "64MB",
        "min_wal_size": "2GB",
        "max_wal_size": "8GB",
        "checkpoint_timeout": "15min",
        "checkpoint_completion_target": "0.9",
        "random_page_cost": "4" if hdd else "1.1",
        "effective_io_concurrency": "2" if hdd else "200",
        "autovacuum_max_workers": "4",
        "autovacuum_naptime": "30s",
        "autovacuum_vacuum_cost_limit": "1000" if hdd else "2000",
        "autovacuum_vacuum_scale_factor": "0.05",
        "autovacuum_analyze_scale_factor": "0.02",
        "max_worker_processes": str(max(16, h["cpus"] * 2)),
        "max_parallel_workers": str(max(2, h["cpus"] // 2)),
        "max_parallel_workers_per_gather": "2",
        "huge_pages": "try",
    }
    if d.get("timescaledb"):
        pg["timescaledb.max_background_workers"] = "8"
    return rec, pg, zbx_mem, scale, iscale, vps_now, vps_t, items_t


def storage_plan(d, scale, iscale, vps_now, hist_days=90, trend_days=730):
    """Bytes for the retention plan, plain PostgreSQL vs TimescaleDB with compression."""
    # measured bytes per stored row when the tables are big enough to tell
    rows = sum(d["rows_est"].get(t, 0) for t in ("history", "history_uint"))
    tsize = sum(s for n, s in d["tables"] if n in ("history", "history_uint"))
    bpr = tsize / rows if rows > 200000 else 90
    ratio = 10.0
    comp = d.get("compression") or {}
    b, a = sum(v[0] for v in comp.values()), sum(v[1] for v in comp.values())
    if a and b > 50 * MB:
        ratio = b / a
    vps = vps_now * scale
    per_day = vps * 86400 * bpr
    trend_rows_day = d["items_numeric"] * iscale * 24
    trend_day = trend_rows_day * 110
    plain = per_day * 30 * 1.3 + trend_day * trend_days * 1.2           # 30 days raw; bloat from row deletes
    tsdb = per_day * 7 + per_day * (hist_days - 7) / ratio + trend_day * 30 + trend_day * (trend_days - 30) / ratio
    archive_day = vps * 86400 * 9 + trend_rows_day * 20                 # gzip CSV
    return {"bytes_per_row": bpr, "ratio": ratio, "vps": vps, "per_day": per_day, "plain": plain,
            "timescale": tsdb, "archive_day": archive_day, "rows_day": vps * 86400}


# ------------------------------------------------------------------ report
def build(args):
    out = []
    p = out.append
    now = datetime.now()
    h = host_facts()
    where, conf = server_config()
    db = DB()
    d = db_facts(db, args.quick)
    datadir = pg_data_dir(db)
    disks = [x for x in {disk_of("/")["dev"]: disk_of("/"), (disk_of(datadir) or {}).get("dev"): disk_of(datadir)}
             .values() if x]
    rot = any(x["rotational"] for x in disks if x["rotational"] is not None)
    hdd = True if args.hdd else False if args.ssd else rot
    rec, pg, zbx_mem, scale, iscale, vps_now, vps_t, items_t = recommend(h, d, conf, args.target_hosts, hdd)
    sp = storage_plan(d, scale, iscale, vps_now)

    p(f"Zabbix health check  {now:%Y-%m-%d %H:%M}  (sizing target: {args.target_hosts} hosts)")
    p("=" * 78)
    p("\n## Host")
    p(f"CPUs {h['cpus']}   RAM {human(h['ram'])} (available {human(h['ram_avail'])})   "
      f"swap {human(h['swap'])} (used {human(h['swap_used'])}, swappiness {h['swappiness']})   load {' '.join(h['load'])}")
    for x in disks:
        kind = "HDD" if x["rotational"] else "SSD" if x["rotational"] is False else "disk type unknown"
        p(f"Disk {x['path']:<28} {x['dev']:<22} {human(x['total'])} total, {human(x['free'])} free "
          f"({100 * x['free'] / x['total']:.0f}%), {kind}")
    if args.hdd or args.ssd:
        p(f"(disk type set by hand: {'HDD' if hdd else 'SSD'})")

    if shutil.which("docker"):
        rows = sh("docker", "ps", "-a", "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}").splitlines()
        if rows:
            p("\n## Containers")
            for r in rows:
                n, img, st, ports = (r.split("\t") + ["", "", ""])[:4]
                p(f"  {n:<24} {img:<44} {st[:22]:<22} {ports[:60]}")
            comp = sh("docker", "inspect", "--format", '{{index .Config.Labels "com.docker.compose.project.config_files"}}',
                      *[r.split("\t")[0] for r in rows]).split()
            comp = sorted({c for c in comp if c and c != "<no" and c != "value>"})
            p("  compose files: " + (", ".join(comp) if comp else "none (containers started with docker run)"))

    p("\n## Zabbix server settings  (" + (where or "zabbix_server.conf not found") + ")")
    for k in CACHES:
        p(f"  {k:<24} {conf.get(k, DEFAULTS[k] + ' (default)'):<16} -> recommended {human(rec[k])}")
    for k in ("StartPollers", "StartSNMPPollers", "StartPingers", "StartPollersUnreachable", "StartLLDProcessors",
              "StartDBSyncers", "Timeout", "MaxHousekeeperDelete"):
        p(f"  {k:<24} {conf.get(k, DEFAULTS[k] + ' (default)'):<16} -> recommended {rec[k]}")

    p("\n## Load")
    p(f"Monitored hosts {d['hosts']} (disabled {d['hosts_disabled']}), enabled items {d['items']} "
      f"({d['items_numeric']} numeric), triggers {d['triggers']}")
    p(f"Values per second asked for by item intervals: {d['vps_required']:.0f}" +
      (f"; measured inserts: {d['vps_measured']:.0f}/s" if d.get("vps_measured") is not None else ""))
    p("Items by interval: " + ", ".join(f"{s}s x{n}" for s, n in d["delays"][:10]))
    if d["unsupported_total"]:
        p(f"Unsupported items still polled: {d['unsupported_total']} (top: " +
          ", ".join(f"{k} x{n}" for k, n in d["unsupported"][:5]) + ")")
    p(f"Network devices (MDP templates): {d['mdp_hosts']} with {d['mdp_items']} items, {d['mdp_vps']:.0f} values/s")
    p(f"Full rollout ({args.target_hosts} devices): about {items_t:.0f} items, {vps_t:.0f} values/s")

    p("\n## Caches and processes now (last value / 7-day peak)")
    if not d["internal"]:
        p("  No internal items found. Create the guard host (OPTIMIZE.md step 2), wait one hour, run this again.")
    for key, (last, peak) in sorted(d["internal"].items()):
        label = INTERNAL.get(key) or key.replace("zabbix[process,", "busy % ").replace(",avg,busy]", "")
        flag = "  <-- HIGH" if (peak or last) >= 70 and ("pused" in key or "busy" in key) else ""
        if key == "zabbix[vcache,cache,mode]" and (peak or last) >= 1:
            flag = "  <-- value cache ran out of memory"
        p(f"  {label:<44} {last:>8.1f} / {peak if peak is not None else float('nan'):>8.1f}{flag}")

    p("\n## Crash evidence, last 30 days")
    ev = crash_evidence(conf)
    p(f"  {len(ev)} matching log line(s)" + (":" if ev else " (none found)"))
    for line in ev[-12:]:
        p("  " + line[:180])

    p("\n## PostgreSQL  (" + d["where"] + f", PostgreSQL {d['pg_version']}, TimescaleDB "
      f"{d['timescaledb'] or 'not installed'})")
    p(f"Database size {human(d['db_size'])}, connections {d['connections']}, data in {datadir}")
    for n, s in d["tables"][:8]:
        p(f"  {n:<28} {human(s):>8}")
    for n, (live, dead) in sorted(d["dead"].items()):
        if live + dead > 10000:
            p(f"  dead rows {n:<20} {dead} of {live + dead} ({100 * dead / (live + dead):.0f}%)")
    if d.get("compression"):
        for n, (b, a) in d["compression"].items():
            if b:
                p(f"  compressed {n:<20} {human(b)} -> {human(a)}")
    p("Settings now -> recommended:")
    for k, v in pg.items():
        p(f"  {k:<34} {d['pg'].get(k, '?'):<14} -> {v}")
    p(f"  dynamic_shared_memory_type {d['pg_shm']} (in Docker give the container shm_size: 1g)")

    p("\n## Housekeeping (Administration > Housekeeping)")
    p("  " + ", ".join(f"{k}={v}" for k, v in d["housekeeping"].items()))

    p("\n## Storage for the retention plan (raw values 30 days plain / 90 days TimescaleDB, trends 2 years)")
    p(f"  At {sp['vps']:.0f} values/s: {sp['rows_day'] / 1e6:.1f} M rows/day, {human(sp['per_day'])}/day raw "
      f"({sp['bytes_per_row']:.0f} bytes/row)")
    p(f"  Plain PostgreSQL, 30 d:  about {human(sp['plain'])} on disk (deletes ~{sp['rows_day'] / 1e6:.0f} M rows/day)")
    p(f"  TimescaleDB, 90 d:       about {human(sp['timescale'])} (old days dropped whole, ratio {sp['ratio']:.0f}:1)")
    p(f"  Daily archive files:     about {human(sp['archive_day'])}/day, {human(sp['archive_day'] * 365)} for 365 days")

    p("\n## Memory budget with the recommendations")
    sb = size(pg["shared_buffers"])
    p(f"  Zabbix caches {human(zbx_mem)} + PostgreSQL shared_buffers {human(sb)} + ~3G OS/web/Docker = "
      f"{human(zbx_mem + sb + 3 * GB)} of {human(h['ram'])}; the rest is file cache for PostgreSQL")
    if zbx_mem + sb + 3 * GB > h["ram"] * 0.8:
        p("  WARNING: that is more than 80% of RAM; lower ValueCacheSize and shared_buffers")

    p("\n## Findings")
    for f in findings(h, d, conf, rec, hdd, disks, sp, ev):
        p("  - " + f)
    report = "\n".join(out) + "\n"

    tuning = ["# Recommended by tools/zbx_health.py on " + f"{now:%Y-%m-%d}" + f" for {args.target_hosts} hosts",
              "# Put these in /etc/zabbix/zabbix_server.conf (replace existing lines), then:",
              "#   zabbix_server -T -c /etc/zabbix/zabbix_server.conf && systemctl restart zabbix-server"]
    tuning += [f"{k}={human(rec[k])}" for k in CACHES]
    tuning += [f"{k}={rec[k]}" for k in rec if k not in CACHES]
    sql = ["-- Recommended by tools/zbx_health.py on " + f"{now:%Y-%m-%d}" + f" ({human(h['ram'])} RAM, "
           f"{'HDD' if hdd else 'SSD'}). Apply: see OPTIMIZE.md step 5. Needs a PostgreSQL restart."]
    sql += [f"ALTER SYSTEM SET {k} = '{v}';" for k, v in pg.items()]
    return report, "\n".join(tuning) + "\n", "\n".join(sql) + "\n"


def findings(h, d, conf, rec, hdd, disks, sp, ev):
    f = []
    cs = size(conf.get("CacheSize", DEFAULTS["CacheSize"]))
    if cs < rec["CacheSize"]:
        f.append(f"CacheSize is {human(cs)}: raise it to {human(rec['CacheSize'])}. When this cache fills the "
                 "server stops, which is the crash you had.")
    for k in CACHES[1:]:
        if size(conf.get(k, DEFAULTS[k])) < rec[k] // 2:
            f.append(f"{k} {human(size(conf.get(k, DEFAULTS[k])))} is small for the full rollout: use {human(rec[k])}.")
    if any("out of memory" in l.lower() or "zbx_mem" in l.lower() for l in ev):
        f.append("The logs show Zabbix shared memory running out (see Crash evidence).")
    if any("killed process" in l.lower() or "oom" in l.lower() for l in ev):
        f.append("The kernel killed processes for lack of RAM (OOM). Check the memory budget and add swap.")
    if not d["internal"]:
        f.append("Cache usage is not being recorded: create the guard host so you are alerted before a cache fills.")
    if hdd:
        f.append("The database is on a spinning disk: keep synchronous_commit=off and the 30-day raw history "
                 "(RETENTION.md). An SSD for the database would help most.")
    if not d["timescaledb"]:
        f.append(f"Plain PostgreSQL: keep 30 days of raw values in the database (about {human(sp['plain'])} at full "
                 f"rollout; the housekeeper deletes ~{sp['rows_day'] / 1e6:.0f} M rows a day). Older RCA readings "
                 "come from the archive.")
    hk = d["housekeeping"]
    if not d["timescaledb"] and d.get("brin", 0) < 7:
        f.append("The nightly archive reads whole history tables: run python3 tools/zbx_data.py policy --apply "
                 "(adds small clock indexes, RETENTION.md part 1).")
    if str(hk.get("hk_trends_global")) != "1" or str(hk.get("hk_history_global")) != "1":
        f.append("History/trend periods are per item. Apply the retention policy: python3 tools/zbx_data.py policy --apply")
    if h["swap"] == 0:
        f.append("No swap: add 4-8 GB so a memory spike slows the server instead of killing a process.")
    for x in disks:
        if x["free"] / x["total"] < 0.25:
            f.append(f"{x['path']} has only {100 * x['free'] / x['total']:.0f}% free.")
    if d["unsupported_total"] > 50:
        f.append(f"{d['unsupported_total']} unsupported items are still polled: fix or disable them.")
    busy = [(k, v) for k, v in d["internal"].items() if "busy" in k and (v[1] or v[0]) > 75]
    for k, v in busy:
        f.append(f"Process {k.split(',')[1]} reached {v[1] or v[0]:.0f}% busy: start more of them.")
    if d["connections"] > int(re.sub(r"\D", "", d["pg"].get("max_connections", "100")) or 100) * 0.8:
        f.append("PostgreSQL is near max_connections.")
    return f or ["Nothing urgent found."]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target-hosts", type=int, default=1000, help="size for this many hosts (default 1000)")
    ap.add_argument("--quick", action="store_true", help="skip the 60 s insert-rate measurement")
    ap.add_argument("--hdd", action="store_true", help="treat the database disk as a spinning disk")
    ap.add_argument("--ssd", action="store_true", help="treat the database disk as an SSD")
    ap.add_argument("--out", default=None, help="output folder (default health_<date>)")
    args = ap.parse_args()
    report, tuning, sql = build(args)
    print(report)
    out = args.out or f"health_{datetime.now():%Y%m%d_%H%M}"
    os.makedirs(out, exist_ok=True)
    for name, text in (("report.txt", report), ("zabbix_server_tuning.conf", tuning), ("postgresql_tuning.sql", sql)):
        with open(os.path.join(out, name), "w") as fh:
            fh.write(text)
    print(f"Written to {out}/ (send report.txt back for review)")


if __name__ == "__main__":
    main()
