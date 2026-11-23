"""Run SQL against the Zabbix PostgreSQL database (standard library only).

Works whether PostgreSQL runs in Docker or on the host:
  PG_CONTAINER=<name>   use `docker exec` into that container (found automatically if unset)
  PG_LOCAL=1            use the host's psql (PGHOST, PGPORT, PGPASSWORD as usual)
  PGUSER / PGDATABASE   default: DBUser / DBName from zabbix_server.conf, else zabbix / zabbix
"""
import os
import re
import shutil
import subprocess
import sys

SERVER_CONF = os.environ.get("ZBX_SERVER_CONF", "/etc/zabbix/zabbix_server.conf")


def read_conf(path=SERVER_CONF):
    """zabbix_server.conf as {Key: value}, following Include= lines."""
    out = {}

    def load(p):
        try:
            lines = open(p, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "Include":
                if v.endswith("/"):
                    v += "*.conf"
                import glob
                for f in sorted(glob.glob(v) if any(c in v for c in "*?") else
                                (glob.glob(v + "/*.conf") if os.path.isdir(v) else [v])):
                    load(f)
            else:
                out[k] = v
    load(path)
    return out


def docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check).stdout


def find_container():
    if not shutil.which("docker"):
        return None
    try:
        rows = docker("ps", "--format", "{{.Names}}\t{{.Image}}").splitlines()
    except (subprocess.CalledProcessError, OSError):
        return None
    hits = [r.split("\t")[0] for r in rows if re.search(r"postgres|timescale|pgsql-db|postgis", r.split("\t")[-1])]
    hits = [h for h in hits if not re.search(r"zabbix-(server|web|proxy)", h)]
    return hits[0] if len(hits) == 1 else (hits if hits else None)


class DB:
    def __init__(self):
        conf = read_conf()
        self.user = os.environ.get("PGUSER") or conf.get("DBUser") or "zabbix"
        self.dbname = os.environ.get("PGDATABASE") or conf.get("DBName") or "zabbix"
        self.container = None
        if os.environ.get("PG_LOCAL") != "1":
            c = os.environ.get("PG_CONTAINER") or find_container()
            if isinstance(c, list):
                sys.exit(f"Several PostgreSQL containers run ({', '.join(c)}): set PG_CONTAINER=<name>.")
            if not c and not shutil.which("psql"):
                sys.exit("No PostgreSQL container found and no psql on this host: set PG_CONTAINER=<name>.")
            self.container = c
        self.prefix = []
        if not self.container:   # connect like the Zabbix server does
            host = os.environ.get("PGHOST") or conf.get("DBHost", "")
            if host and not host.startswith("/"):
                os.environ["PGHOST"] = host
                if conf.get("DBPort") and not os.environ.get("PGPORT"):
                    os.environ["PGPORT"] = conf["DBPort"]
                if conf.get("DBPassword") and not os.environ.get("PGPASSWORD"):
                    os.environ["PGPASSWORD"] = conf["DBPassword"]
            elif os.geteuid() == 0 and shutil.which("runuser"):
                self.prefix = ["runuser", "-u", self.user, "--"]   # socket "peer" login needs that OS user
        self.where = f"container {self.container}" if self.container else "local psql " + (os.environ.get("PGHOST") or "socket")

    def cmd(self, prog="psql", *args, interactive=False):
        base = [prog, "-U", self.user]
        if self.container:
            pw = os.environ.get("PGPASSWORD")
            env = ["-e", f"PGPASSWORD={pw}"] if pw else []
            return ["docker", "exec", *(["-i"] if interactive else []), *env, self.container, *base, *args]
        return self.prefix + base + list(args)

    def psql(self, *args, interactive=False):
        return self.cmd("psql", "-d", self.dbname, "-X", "-v", "ON_ERROR_STOP=1", *args, interactive=interactive)

    def rows(self, sql):
        """Run one query, return rows as lists of strings."""
        r = subprocess.run(self.psql("-At", "-F", "\t", "-c", sql), capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"SQL failed ({self.where}): {r.stderr.strip()}\n  {sql[:200]}")
        return [line.split("\t") for line in r.stdout.splitlines() if line != ""]

    def one(self, sql, default=None):
        r = self.rows(sql)
        return r[0][0] if r and r[0] else default

    def exec_file(self, path):
        with open(path, "rb") as f:
            return subprocess.run(self.psql("-q", interactive=True), stdin=f, capture_output=True, text=False)

    def copy_out(self, sql):
        """Popen streaming `COPY (sql) TO STDOUT` as CSV on .stdout."""
        return subprocess.Popen(self.psql("-c", f"COPY ({sql}) TO STDOUT WITH (FORMAT csv)"),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def copy_in(self, table, columns):
        """Popen accepting CSV on .stdin for `COPY table(columns) FROM STDIN`."""
        return subprocess.Popen(self.psql("-c", f"COPY {table}({columns}) FROM STDIN WITH (FORMAT csv)",
                                          interactive=True),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def has_timescale(self):
        return self.one("SELECT extversion FROM pg_extension WHERE extname='timescaledb'")

    def setting(self, name):
        """Zabbix frontend setting from the settings table (7.x)."""
        r = self.rows(f"SELECT value_str, value_int FROM settings WHERE name='{name}'")
        if not r:
            return None
        s, i = r[0]
        return s if s != "" else i
