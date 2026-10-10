#!/usr/bin/env python3
"""Tidesight Phase 0 hosting test.

Checks whether SQLite is a safe and fast enough store for the Tidesight
collector on this host, and reports what the host offers (Python, SQLite
features, filesystem, process limits, optional modules). Standard library
only; runs on Python 3.6+.

Three ways to run it:

* Over SSH (recommended):
      python3 sqlite_host_test.py
      python3 sqlite_host_test.py --data-dir ~/tidesight-hosttest --procs 8 --rows 2000
      python3 sqlite_host_test.py --json > report.json

* As a CGI script: upload it with execute permission into a web directory,
  set TOKEN below (or the TIDESIGHT_TEST_TOKEN environment variable) and open
      https://your.domain/sqlite_host_test.py?token=YOUR_TOKEN
  Optional query parameters: procs, rows, readers, data_dir, format=json.

* Under Passenger WSGI: import it from passenger_wsgi.py
      from sqlite_host_test import application
  and open the app URL with ?token=YOUR_TOKEN.

Delete the script from the web directory after the test: it reveals details
about the host.

The run ends with a PASS / WARN / FAIL summary and a recommended
`storage_mode` / `journal_mode` for config/tidesight.toml.
"""

from __future__ import print_function

import json
import os
import platform
import random
import shutil
import socket
import sqlite3
import string
import subprocess
import sys
import time
import traceback

try:  # Python 3
    from urllib.parse import parse_qs
    from urllib.request import Request, urlopen
except ImportError:  # pragma: no cover - Python 2 is not supported
    print("This script needs Python 3.")
    sys.exit(2)

# Set this (or the TIDESIGHT_TEST_TOKEN environment variable) before running
# the script as CGI or WSGI. Web requests must pass ?token=<the same value>.
TOKEN = ""

SCRIPT_VERSION = "1.0"

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"

NETWORK_FS = (
    "nfs", "nfs4", "cifs", "smb", "smbfs", "smb2", "smb3", "sshfs", "fuse.sshfs",
    "glusterfs", "fuse.glusterfs", "ceph", "fuse.ceph", "fuse.cephfs", "lustre",
    "9p", "afs", "ncpfs", "davfs", "fuse.s3fs", "gpfs", "beegfs", "panfs", "ocfs2",
    "fuse.rclone", "fuse.gcsfuse",
)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

class Report(object):
    """Collects facts (sections of key/value pairs) and checks (status lines)."""

    def __init__(self, stream=None):
        self.sections = []  # [(title, [(key, value), ...])]
        self.checks = []    # [(name, status, detail)]
        self.data = {}      # machine-readable results
        self.stream = stream  # if set, print progress as we go

    def section(self, title):
        self.sections.append((title, []))
        if self.stream:
            self.stream.write("\n== %s ==\n" % title)
            self.stream.flush()

    def add(self, key, value):
        if not self.sections:
            self.section("General")
        self.sections[-1][1].append((key, value))
        if self.stream:
            self.stream.write("  %-34s %s\n" % (key + ":", _fmt(value)))
            self.stream.flush()

    def check(self, name, status, detail=""):
        self.checks.append((name, status, detail))
        if self.stream:
            self.stream.write("  [%s] %s%s\n" % (status, name, (" - " + detail) if detail else ""))
            self.stream.flush()

    def status_of(self, name):
        for n, s, _ in self.checks:
            if n == name:
                return s
        return None

    # A failure of one of these means Tidesight cannot run safely as tested.
    CRITICAL = ("storage", "data dir writable", "data dir outside web root", "test run")

    def overall(self):
        if any(s == FAIL and n in self.CRITICAL for n, s, _ in self.checks):
            return FAIL
        if any(s in (WARN, FAIL) for _, s, _ in self.checks):
            return WARN
        return PASS

    def render_text(self, include_facts=True):
        out = []
        out.append("Tidesight hosting test v%s" % SCRIPT_VERSION)
        if include_facts:
            for title, items in self.sections:
                out.append("")
                out.append("== %s ==" % title)
                for k, v in items:
                    out.append("  %-34s %s" % (k + ":", _fmt(v)))
        out.append(self.render_summary())
        return "\n".join(out) + "\n"

    def render_summary(self):
        out = ["", "== Summary =="]
        for name, status, detail in self.checks:
            out.append("  [%s] %-28s %s" % (status, name, detail))
        out.append("")
        out.append("OVERALL: %s" % self.overall())
        rec = self.data.get("recommendation")
        if rec:
            out.append("RECOMMENDATION: %s" % rec)
        return "\n".join(out)

    def to_json(self):
        return json.dumps({
            "script_version": SCRIPT_VERSION,
            "overall": self.overall(),
            "recommendation": self.data.get("recommendation"),
            "checks": [{"name": n, "status": s, "detail": d} for n, s, d in self.checks],
            "facts": dict((t, dict((k, v) for k, v in items)) for t, items in self.sections),
            "results": self.data,
        }, indent=2, default=str)


def _fmt(value):
    if isinstance(value, float):
        return "%.3f" % value
    if isinstance(value, (list, tuple)):
        return ", ".join(_fmt(v) for v in value) if value else "(none)"
    if isinstance(value, dict):
        return json.dumps(value, default=str)
    return str(value)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def run_cmd(args, timeout=10):
    """Run a command; return (exit_code, stdout) or (None, error)."""
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True)
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            return None, "timeout"
        return proc.returncode, out.strip()
    except Exception as exc:  # command missing, not permitted, ...
        return None, str(exc)


def percentile(sorted_values, p):
    if not sorted_values:
        return None
    k = (len(sorted_values) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def random_token(n=16):
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


def home_dir():
    home = os.environ.get("HOME")
    if home and os.path.isdir(home):
        return home
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except Exception:
        return os.path.expanduser("~")


def mp_context():
    """Prefer fork (works under CGI/Passenger); fall back to spawn (Windows)."""
    import multiprocessing
    methods = multiprocessing.get_all_start_methods()
    if "fork" in methods:
        return multiprocessing.get_context("fork")
    return multiprocessing.get_context("spawn")


def realistic_headers():
    return json.dumps({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/129.0.0.0 Safari/537.36",
        "Accept": "*/*", "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br, zstd", "Origin": "https://www.example.com",
        "Referer": "https://www.example.com/some/page?utm_source=newsletter",
        "Sec-Ch-Ua": "\"Google Chrome\";v=\"129\", \"Not=A?Brand\";v=\"8\", \"Chromium\";v=\"129\"",
        "Sec-Ch-Ua-Mobile": "?0", "Sec-Ch-Ua-Platform": "\"Windows\"", "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-site", "X-Forwarded-For": "203.0.113.7",
        "Content-Type": "text/plain;charset=UTF-8",
    })


def realistic_event(worker, seq):
    return json.dumps({
        "t": "pageview", "id": "%08x%08x" % (worker, seq), "ts": int(time.time() * 1000),
        "url": "https://www.example.com/articles/%d?ref=abc#top" % seq, "title": "An article title",
        "ref": "https://www.google.com/",
        "ctx": {"sw": 1920, "sh": 1080, "vw": 1536, "vh": 730, "dpr": 1.25, "lang": "fr-FR",
                "tz": "Pacific/Tahiti", "tzo": 600, "cs": "dark", "hc": 8, "dm": 8,
                "net": {"type": "wifi", "eff": "4g", "dl": 10, "rtt": 50}},
    })


# --------------------------------------------------------------------------
# 1. Environment
# --------------------------------------------------------------------------

def check_environment(rep, mode, environ):
    rep.section("Environment")
    rep.add("mode", mode)
    rep.add("python", sys.version.replace("\n", " "))
    rep.add("python executable", sys.executable)
    rep.add("platform", platform.platform())
    rep.add("machine", platform.machine())
    rep.add("hostname", socket.gethostname())
    try:
        import getpass
        rep.add("user", getpass.getuser())
    except Exception:
        pass
    rep.add("home", home_dir())
    rep.add("cwd", os.getcwd())
    software = (environ or {}).get("SERVER_SOFTWARE") or os.environ.get("SERVER_SOFTWARE")
    if software:
        rep.add("server software", software)
    passenger = sorted(k for k in os.environ if k.startswith("PASSENGER") or k == "IN_PASSENGER")
    if passenger or (software and "passenger" in software.lower()):
        rep.add("passenger", "detected (%s)" % ", ".join(passenger) if passenger else "detected")
    rep.data["python_version"] = list(sys.version_info[:3])

    v = sys.version_info
    if v >= (3, 9):
        rep.check("python", PASS, "%d.%d.%d" % v[:3])
    else:
        rep.check("python", WARN, "%d.%d.%d; the Tidesight collector needs 3.9+ "
                  "(install a newer Python, e.g. with pyenv)" % v[:3])

    # Other interpreters on PATH (helps pick the one to use for Passenger/cron).
    found = {}
    for d in os.environ.get("PATH", "").split(os.pathsep) + [os.path.join(home_dir(), ".pyenv", "shims")]:
        try:
            names = os.listdir(d)
        except Exception:
            continue
        for name in names:
            base = name[:-4] if name.lower().endswith(".exe") else name
            if base == "python3" or (base.startswith("python3.") and base[8:].isdigit()):
                path = os.path.realpath(os.path.join(d, name))
                found.setdefault(path, base)
    versions = []
    for path in sorted(found)[:12]:
        code, out = run_cmd([path, "-c", "import sys,sqlite3;print('%d.%d.%d' % sys.version_info[:3], "
                                          "sqlite3.sqlite_version)"], timeout=10)
        if code == 0:
            versions.append("%s (python %s)" % (path, out.replace(" ", ", sqlite ")))
    rep.add("python interpreters on PATH", versions)

    try:
        import resource
        limits = {}
        for name in ("RLIMIT_NPROC", "RLIMIT_NOFILE", "RLIMIT_CPU", "RLIMIT_AS", "RLIMIT_FSIZE", "RLIMIT_DATA"):
            if hasattr(resource, name):
                soft, hard = resource.getrlimit(getattr(resource, name))
                fmt = lambda x: "unlimited" if x == resource.RLIM_INFINITY else str(x)
                limits[name[7:].lower()] = "%s/%s" % (fmt(soft), fmt(hard))
        rep.add("resource limits (soft/hard)", limits)
    except ImportError:
        rep.add("resource limits", "n/a on this platform")

    if mode == "cli" and os.name == "posix":
        code, out = run_cmd(["crontab", "-l"], timeout=10)
        if code is None:
            rep.add("crontab", "not available (%s)" % out)
        else:
            rep.add("crontab", "available" if code == 0 or "no crontab" in out.lower() else out[:120])


# --------------------------------------------------------------------------
# 2. SQLite features
# --------------------------------------------------------------------------

def probe(conn, sql_list):
    try:
        for sql in sql_list:
            conn.execute(sql).fetchall()
        return True
    except sqlite3.Error:
        return False


def check_sqlite(rep):
    rep.section("SQLite")
    module_version = getattr(sqlite3, "version", None)  # removed in Python 3.14
    rep.add("sqlite3 module version", module_version or "(not exposed)")
    rep.add("SQLite library version", sqlite3.sqlite_version)
    lib = tuple(int(x) for x in sqlite3.sqlite_version.split("."))
    rep.data["sqlite_version"] = sqlite3.sqlite_version

    conn = sqlite3.connect(":memory:")
    try:
        opts = [r[0] for r in conn.execute("PRAGMA compile_options").fetchall()]
    except sqlite3.Error:
        opts = []
    interesting = [o for o in opts if o.split("=")[0] in (
        "THREADSAFE", "ENABLE_FTS5", "ENABLE_FTS4", "ENABLE_JSON1", "ENABLE_RTREE", "ENABLE_MATH_FUNCTIONS",
        "DEFAULT_WAL_SYNCHRONOUS", "DEFAULT_JOURNAL_SIZE_LIMIT", "MAX_PAGE_SIZE", "DEFAULT_PAGE_SIZE",
        "OMIT_WAL", "DEFAULT_SYNCHRONOUS", "ENABLE_COLUMN_METADATA")]
    rep.add("compile options (selected)", interesting)

    features = {
        "json1": probe(conn, ["SELECT json('{\"a\":1}'), json_extract('{\"a\":1}', '$.a')"]),
        "json ->> operator (3.38)": probe(conn, ["SELECT '{\"a\":1}' ->> '$.a'"]),
        "fts5": probe(conn, ["CREATE VIRTUAL TABLE temp.f5 USING fts5(x)", "DROP TABLE temp.f5"]),
        "upsert (3.24)": probe(conn, ["CREATE TEMP TABLE u(k PRIMARY KEY, n)",
                                      "INSERT INTO u VALUES (1, 1) ON CONFLICT(k) DO UPDATE SET n = n + 1"]),
        "window functions (3.25)": probe(conn, ["SELECT row_number() OVER (ORDER BY 1)"]),
        "returning (3.35)": probe(conn, ["CREATE TEMP TABLE r(x)", "INSERT INTO r VALUES (1) RETURNING x"]),
        "strict tables (3.37)": probe(conn, ["CREATE TEMP TABLE s(x INTEGER) STRICT"]),
        "math functions": probe(conn, ["SELECT sqrt(4), ln(1)"]),
    }
    conn.close()
    for name, ok in features.items():
        rep.add(name, "yes" if ok else "no")
    rep.data["sqlite_features"] = features

    if lib >= (3, 35, 0):
        rep.check("sqlite version", PASS, sqlite3.sqlite_version)
    elif lib >= (3, 24, 0):
        rep.check("sqlite version", WARN, "%s (< 3.35: no RETURNING; Tidesight does not need it)"
                  % sqlite3.sqlite_version)
    else:
        rep.check("sqlite version", WARN, "%s is old (< 3.24); WAL, JSON and UPSERT may be missing"
                  % sqlite3.sqlite_version)
    rep.check("sqlite json1", PASS if features["json1"] else WARN,
              "" if features["json1"] else "JSON functions missing: the dashboard API falls back to Python")
    rep.check("sqlite fts5", PASS if features["fts5"] else INFO,
              "" if features["fts5"] else "not needed yet")


# --------------------------------------------------------------------------
# 3. Filesystem
# --------------------------------------------------------------------------

def _decode_mount_path(path):
    # /proc/mounts escapes spaces and tabs as octal (\040).
    out, i = [], 0
    while i < len(path):
        if path[i] == "\\" and i + 3 < len(path) and path[i + 1:i + 4].isdigit():
            out.append(chr(int(path[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(path[i])
            i += 1
    return "".join(out)


def find_mount(path):
    """Return (mountpoint, fstype, device, options) for path from /proc/mounts."""
    try:
        with open("/proc/mounts") as fh:
            lines = fh.read().splitlines()
    except Exception:
        return None
    real = os.path.realpath(path)
    best = None
    for line in lines:
        parts = line.split()
        if len(parts) < 4:
            continue
        device, mnt, fstype, options = parts[0], _decode_mount_path(parts[1]), parts[2], parts[3]
        if real == mnt or real.startswith(mnt.rstrip("/") + "/") or mnt == "/":
            if best is None or len(mnt) > len(best[0]):
                best = (mnt, fstype, device, options)
    return best


def windows_fs(path):
    try:
        import ctypes
        root = os.path.splitdrive(os.path.realpath(path))[0] + "\\"
        name_buf = ctypes.create_unicode_buffer(64)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), None, 0, None, None, None, name_buf, len(name_buf))
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
        remote = drive_type == 4  # DRIVE_REMOTE
        return (name_buf.value if ok else "unknown"), remote
    except Exception:
        return "unknown", False


def check_filesystem(rep, data_dir):
    """Returns (usable, created) where created lists the directories this run made."""
    rep.section("Data directory and filesystem")
    rep.add("data dir", data_dir)
    rep.add("real path", os.path.realpath(data_dir))
    created = []
    try:
        if not os.path.isdir(data_dir):
            missing, d = [], data_dir
            while d and not os.path.isdir(d):
                missing.append(d)
                parent = os.path.dirname(d)
                if parent == d:
                    break
                d = parent
            os.makedirs(data_dir)
            created = missing  # deepest first
        probe_file = os.path.join(data_dir, ".write-test-" + random_token(6))
        with open(probe_file, "w") as fh:
            fh.write("ok")
        os.remove(probe_file)
        writable = True
    except Exception as exc:
        writable = False
        rep.add("write error", str(exc))
    rep.check("data dir writable", PASS if writable else FAIL, data_dir)
    if not writable:
        return False, created

    fstype, network = "unknown", False
    if os.name == "posix":
        code, out = run_cmd(["stat", "-f", "-c", "%T", data_dir])
        if code == 0:
            rep.add("stat -f type", out)
            fstype = out
        mount = find_mount(data_dir)
        if mount:
            rep.add("mount point", mount[0])
            rep.add("mount type (/proc/mounts)", mount[1])
            rep.add("mount device", mount[2])
            rep.add("mount options", mount[3])
            fstype = mount[1]
        try:
            st = os.statvfs(data_dir)
            rep.add("free space", "%.1f GB" % (st.f_bavail * st.f_frsize / 1e9))
            if st.f_files:
                rep.add("free inodes", "%d of %d" % (st.f_favail, st.f_files))
        except Exception:
            pass
        network = fstype.lower() in NETWORK_FS or fstype.lower().startswith("nfs")
    elif os.name == "nt":
        fstype, network = windows_fs(data_dir)
        rep.add("volume filesystem", fstype)
        try:
            usage = shutil.disk_usage(data_dir)
            rep.add("free space", "%.1f GB" % (usage.free / 1e9))
        except Exception:
            pass

    rep.data["filesystem"] = {"type": fstype, "network": network}
    if network:
        rep.check("filesystem", WARN, "%s is a network filesystem: WAL is unsafe there and locking may be "
                  "unreliable; prefer journal_mode = \"delete\" or storage_mode = \"spool\"" % fstype)
    elif fstype == "unknown":
        rep.check("filesystem", WARN, "could not determine the filesystem type")
    else:
        rep.check("filesystem", PASS, "%s (local)" % fstype)
    return True, created


# --------------------------------------------------------------------------
# 4. Journal modes
# --------------------------------------------------------------------------

def check_journal_modes(rep, data_dir):
    rep.section("Journal modes")
    results = {}
    path = os.path.join(data_dir, "journal-test.db")
    for suffix in ("", "-wal", "-shm", "-journal"):
        _remove(path + suffix)
    try:
        conn = sqlite3.connect(path)
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        conn.execute("CREATE TABLE IF NOT EXISTS t(x)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        shm = os.path.exists(path + "-shm")
        conn.close()
        conn = sqlite3.connect(path)
        persisted = conn.execute("PRAGMA journal_mode").fetchone()[0]
        count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        conn.close()
        ok = mode == "wal" and persisted == "wal" and count == 1
        results["wal"] = ok
        rep.add("WAL: set", mode)
        rep.add("WAL: after reconnect", persisted)
        rep.add("WAL: -shm file created", "yes" if shm else "no")
        rep.check("journal_mode wal", PASS if ok else FAIL,
                  "" if ok else "PRAGMA journal_mode=WAL returned %r, %r after reconnect" % (mode, persisted))
    except sqlite3.Error as exc:
        results["wal"] = False
        rep.check("journal_mode wal", FAIL, str(exc))
    try:
        conn = sqlite3.connect(path)
        mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        conn.execute("INSERT INTO t VALUES (2)")
        conn.commit()
        conn.close()
        conn = sqlite3.connect(path)
        persisted = conn.execute("PRAGMA journal_mode").fetchone()[0]
        count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        conn.close()
        ok = mode == "delete" and persisted == "delete" and count == 2
        results["delete"] = ok
        rep.add("DELETE: set", mode)
        rep.check("journal_mode delete", PASS if ok else FAIL, "" if ok else "returned %r" % mode)
    except sqlite3.Error as exc:
        results["delete"] = False
        rep.check("journal_mode delete", FAIL, str(exc))
    for suffix in ("", "-wal", "-shm", "-journal"):
        _remove(path + suffix)
    rep.data["journal_modes"] = results
    return results


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


# --------------------------------------------------------------------------
# 5-6. Concurrency and durability
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS hits (
  id INTEGER PRIMARY KEY,
  received_at INTEGER NOT NULL,
  site TEXT,
  ip TEXT,
  headers TEXT,
  data TEXT,
  worker INTEGER,
  seq INTEGER
);
CREATE INDEX IF NOT EXISTS hits_site_time ON hits(site, received_at);
"""


def writer_proc(db_path, worker, rows, busy_ms, synchronous, gate, out_q):
    """Insert `rows` rows, one transaction each, like the collector does."""
    result = {"worker": worker, "lat": [], "locked": 0, "other_errors": 0, "failed": 0, "errors": []}
    try:
        conn = sqlite3.connect(db_path, timeout=busy_ms / 1000.0, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=%d" % int(busy_ms))
        conn.execute("PRAGMA synchronous=%s" % synchronous)
        headers = realistic_headers()
        gate.wait(60)
        result["start"] = time.time()
        for seq in range(rows):
            t0 = time.perf_counter()
            ok = False
            for _attempt in range(3):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "INSERT INTO hits(received_at, site, ip, headers, data, worker, seq) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (int(time.time() * 1000), "site%d" % (seq % 3), "203.0.113.%d" % (seq % 250),
                         headers, realistic_event(worker, seq), worker, seq))
                    conn.execute("COMMIT")
                    ok = True
                    break
                except sqlite3.OperationalError as exc:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    msg = str(exc).lower()
                    if "locked" in msg or "busy" in msg:
                        result["locked"] += 1
                    else:
                        result["other_errors"] += 1
                        if len(result["errors"]) < 3:
                            result["errors"].append(str(exc))
            if not ok:
                result["failed"] += 1
            result["lat"].append(time.perf_counter() - t0)
        result["end"] = time.time()
        conn.close()
    except Exception as exc:
        result["fatal"] = "%s: %s" % (type(exc).__name__, exc)
    out_q.put(result)


def reader_proc(db_path, busy_ms, gate, stop, out_q):
    """Simulate an enrichment job reading while the collector writes."""
    result = {"queries": 0, "errors": 0, "lat": []}
    try:
        conn = sqlite3.connect(db_path, timeout=busy_ms / 1000.0, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=%d" % int(busy_ms))
        gate.wait(60)
        while not stop.is_set():
            t0 = time.perf_counter()
            try:
                conn.execute("BEGIN")
                conn.execute("SELECT id, data FROM hits ORDER BY id DESC LIMIT 2000").fetchall()
                conn.execute("SELECT site, COUNT(*) FROM hits GROUP BY site").fetchall()
                conn.execute("COMMIT")
                result["queries"] += 1
            except sqlite3.OperationalError:
                result["errors"] += 1
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            result["lat"].append(time.perf_counter() - t0)
            time.sleep(0.02)
        conn.close()
    except Exception as exc:
        result["fatal"] = "%s: %s" % (type(exc).__name__, exc)
    out_q.put(result)


def run_concurrency(rep, data_dir, journal_mode, procs, rows, readers, busy_ms, synchronous, timeout):
    name = "concurrency %s" % journal_mode
    path = os.path.join(data_dir, "concurrency-%s.db" % journal_mode)
    for suffix in ("", "-wal", "-shm", "-journal"):
        _remove(path + suffix)
    stats = {"journal_mode": journal_mode, "procs": procs, "rows_per_proc": rows, "readers": readers}
    try:
        conn = sqlite3.connect(path)
        mode = conn.execute("PRAGMA journal_mode=%s" % journal_mode).fetchone()[0]
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        if mode != journal_mode:
            rep.check(name, FAIL, "could not enable %s (got %s)" % (journal_mode, mode))
            return stats
    except sqlite3.Error as exc:
        rep.check(name, FAIL, "setup failed: %s" % exc)
        return stats

    ctx = mp_context()
    wq, rq = ctx.Queue(), ctx.Queue()
    gate, stop = ctx.Event(), ctx.Event()
    writers = [ctx.Process(target=writer_proc, args=(path, i, rows, busy_ms, synchronous, gate, wq))
               for i in range(procs)]
    reader_ps = [ctx.Process(target=reader_proc, args=(path, busy_ms, gate, stop, rq)) for _ in range(readers)]
    results, reader_results, timed_out = [], [], False
    t_start = time.time()
    try:
        try:
            for p in writers + reader_ps:
                p.start()
        except Exception as exc:
            rep.check(name, FAIL, "could not start processes: %s" % exc)
            return stats
        time.sleep(0.3)
        t_start = time.time()
        gate.set()
        deadline = t_start + timeout
        for _ in writers:
            remaining = deadline - time.time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                results.append(wq.get(timeout=remaining))
            except Exception:
                timed_out = True
                break
        stop.set()
        for _ in reader_ps:
            try:
                reader_results.append(rq.get(timeout=30))
            except Exception:
                pass
    finally:
        stop.set()
        for p in writers + reader_ps:
            p.join(5)
            if p.is_alive():
                p.terminate()

    fatal = [r["fatal"] for r in results if "fatal" in r] + [r["fatal"] for r in reader_results if "fatal" in r]
    lat = sorted(x for r in results for x in r.get("lat", []))
    locked = sum(r.get("locked", 0) for r in results)
    other = sum(r.get("other_errors", 0) for r in results)
    failed = sum(r.get("failed", 0) for r in results)
    errors = [e for r in results for e in r.get("errors", [])][:3]
    ends = [r["end"] for r in results if "end" in r]
    wall = (max(ends) - t_start) if ends else None
    inserted_ok = sum(len(r.get("lat", [])) for r in results) - failed

    # Durability: integrity check and row count.
    integrity, count = "not run", None
    try:
        conn = sqlite3.connect(path, timeout=busy_ms / 1000.0)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        count = conn.execute("SELECT COUNT(*) FROM hits").fetchone()[0]
        dupes = conn.execute("SELECT COUNT(*) FROM (SELECT worker, seq FROM hits GROUP BY worker, seq "
                             "HAVING COUNT(*) > 1)").fetchone()[0]
        conn.close()
    except sqlite3.Error as exc:
        integrity, dupes = "error: %s" % exc, None
    size = sum(os.path.getsize(path + s) for s in ("", "-wal") if os.path.exists(path + s))
    for suffix in ("", "-wal", "-shm", "-journal"):
        _remove(path + suffix)

    expected = procs * rows
    stats.update({
        "timed_out": timed_out, "fatal": fatal, "wall_seconds": wall,
        "rows_expected": expected, "rows_inserted": inserted_ok, "rows_counted": count, "duplicates": dupes,
        "locked_errors": locked, "other_errors": other, "failed_rows": failed, "error_samples": errors,
        "throughput_rows_per_sec": (inserted_ok / wall) if wall else None,
        "latency_ms": {"p50": _ms(percentile(lat, 50)), "p95": _ms(percentile(lat, 95)),
                       "p99": _ms(percentile(lat, 99)), "max": _ms(lat[-1] if lat else None)},
        "integrity_check": integrity, "db_size_mb": round(size / 1e6, 2),
        "reader_queries": sum(r.get("queries", 0) for r in reader_results),
        "reader_errors": sum(r.get("errors", 0) for r in reader_results),
    })
    rlat = sorted(x for r in reader_results for x in r.get("lat", []))
    if rlat:
        stats["reader_latency_ms"] = {"p50": _ms(percentile(rlat, 50)), "p95": _ms(percentile(rlat, 95))}

    rep.add("%s: wall time" % journal_mode, "%.2f s" % wall if wall else "n/a")
    rep.add("%s: throughput" % journal_mode,
            "%.0f rows/s" % stats["throughput_rows_per_sec"] if stats["throughput_rows_per_sec"] else "n/a")
    rep.add("%s: latency p50/p95/p99/max" % journal_mode, "%s / %s / %s / %s ms" % (
        stats["latency_ms"]["p50"], stats["latency_ms"]["p95"], stats["latency_ms"]["p99"], stats["latency_ms"]["max"]))
    rep.add("%s: 'database is locked'" % journal_mode, locked)
    rep.add("%s: other errors" % journal_mode, other if not errors else "%d (%s)" % (other, "; ".join(errors)))
    rep.add("%s: rows expected/counted" % journal_mode, "%d / %s" % (expected, count))
    rep.add("%s: integrity_check" % journal_mode, integrity)
    if readers:
        rep.add("%s: reader queries (errors)" % journal_mode,
                "%d (%d), p50 %s ms" % (stats["reader_queries"], stats["reader_errors"],
                                        stats.get("reader_latency_ms", {}).get("p50")))

    problems, warnings = [], []
    if timed_out:
        problems.append("timed out after %ss" % timeout)
    if fatal:
        problems.append("worker crashed: %s" % fatal[0])
    if failed:
        problems.append("%d rows failed after 3 attempts" % failed)
    if count != expected:
        problems.append("row count %s != %d" % (count, expected))
    if dupes:
        problems.append("%s duplicated rows" % dupes)
    if integrity != "ok":
        problems.append("integrity_check: %s" % integrity)
    if locked:
        warnings.append("%d lock retries" % locked)
    p99 = stats["latency_ms"]["p99"]
    if p99 is not None and p99 > 250:
        warnings.append("p99 latency %.0f ms" % p99)
    if stats["reader_errors"]:
        warnings.append("%d reader errors" % stats["reader_errors"])
    summary = "%.0f rows/s, p99 %s ms" % (stats["throughput_rows_per_sec"] or 0, p99)
    if problems:
        rep.check(name, FAIL, "; ".join(problems))
    elif warnings:
        rep.check(name, WARN, summary + "; " + "; ".join(warnings))
    else:
        rep.check(name, PASS, summary)
    return stats


def _ms(seconds):
    return None if seconds is None else round(seconds * 1000.0, 2)


# --------------------------------------------------------------------------
# Spool fallback: concurrent O_APPEND writers
# --------------------------------------------------------------------------

def spool_proc(path, worker, lines, gate, out_q):
    result = {"worker": worker}
    try:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        gate.wait(60)
        start = time.time()
        for seq in range(lines):
            line = json.dumps({"w": worker, "s": seq, "pad": "x" * 900}) + "\n"
            os.write(fd, line.encode("utf-8"))
        os.close(fd)
        result["seconds"] = time.time() - start
    except Exception as exc:
        result["fatal"] = str(exc)
    out_q.put(result)


def check_spool(rep, data_dir, procs, lines):
    rep.section("Spool fallback (append-only files)")
    path = os.path.join(data_dir, "spool-test.jsonl")
    _remove(path)
    ctx = mp_context()
    q, gate = ctx.Queue(), ctx.Event()
    ps = [ctx.Process(target=spool_proc, args=(path, i, lines, gate, q)) for i in range(procs)]
    for p in ps:
        p.start()
    time.sleep(0.2)
    t0 = time.time()
    gate.set()
    results = []
    for _ in ps:
        try:
            results.append(q.get(timeout=120))
        except Exception:
            break
    wall = time.time() - t0
    for p in ps:
        p.join(5)
        if p.is_alive():
            p.terminate()
    seen, corrupt = set(), 0
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                try:
                    rec = json.loads(raw.decode("utf-8"))
                    seen.add((rec["w"], rec["s"]))
                except Exception:
                    corrupt += 1
    except Exception as exc:
        rep.check("spool append", FAIL, str(exc))
        return
    _remove(path)
    expected = procs * lines
    rep.add("lines expected/intact", "%d / %d" % (expected, len(seen)))
    rep.add("corrupt lines", corrupt)
    rep.add("throughput", "%.0f lines/s" % (expected / wall if wall else 0))
    rep.data["spool"] = {"expected": expected, "intact": len(seen), "corrupt": corrupt, "seconds": wall}
    if corrupt or len(seen) != expected:
        rep.check("spool append", WARN, "%d corrupt, %d missing with a shared file; Tidesight uses one "
                  "spool file per process, which avoids this" % (corrupt, expected - len(seen)))
    else:
        rep.check("spool append", PASS, "%d concurrent appends intact" % expected)


# --------------------------------------------------------------------------
# 6. Web exposure
# --------------------------------------------------------------------------

def candidate_web_roots(environ, extra_roots):
    roots = []  # (path, base_url or None)
    for r in extra_roots or []:
        roots.append((r, None))
    environ = environ or {}
    doc_root = environ.get("DOCUMENT_ROOT") or os.environ.get("DOCUMENT_ROOT")
    if doc_root:
        host = environ.get("HTTP_HOST") or environ.get("SERVER_NAME")
        scheme = "https" if (environ.get("HTTPS", "").lower() in ("on", "1")
                             or environ.get("wsgi.url_scheme") == "https") else "http"
        roots.append((doc_root, "%s://%s/" % (scheme, host) if host else None))
    home = home_dir()
    try:
        for name in os.listdir(home):
            full = os.path.join(home, name)
            if os.path.isdir(full) and "." in name and not name.startswith(".") and name.count(".") <= 4:
                roots.append((full, "https://%s/" % name))  # Dreamhost layout: ~/example.com
            elif name in ("public_html", "www", "htdocs", "httpdocs"):
                roots.append((full, None))
    except Exception:
        pass
    return roots


def check_web_exposure(rep, data_dir, environ, extra_roots):
    rep.section("Web exposure of the data directory")
    real = os.path.realpath(data_dir)
    roots = candidate_web_roots(environ, extra_roots)
    rep.add("web roots considered", [r for r, _ in roots] or "(none found)")
    inside = [(r, url) for r, url in roots
              if real == os.path.realpath(r) or real.startswith(os.path.realpath(r).rstrip(os.sep) + os.sep)]
    if not inside:
        if roots:
            rep.check("data dir outside web root", PASS, "not inside any web root found")
        else:
            rep.check("data dir outside web root", WARN, "no web root found; pass --web-root to check")
        return
    root, base = inside[0]
    rel = os.path.relpath(real, os.path.realpath(root)).replace(os.sep, "/")
    rep.add("inside web root", root)
    if not base:
        rep.check("data dir outside web root", WARN, "data dir is inside %s; move it outside" % root)
        return
    token = random_token(24)
    probe_name = "ts-probe-%s.txt" % random_token(8)
    probe_path = os.path.join(data_dir, probe_name)
    try:
        with open(probe_path, "w") as fh:
            fh.write(token)
        url = base + rel + "/" + probe_name
        rep.add("probe url", url)
        try:
            body = urlopen(Request(url, headers={"User-Agent": "tidesight-hosttest"}), timeout=10).read()
            served = token.encode() in body
        except Exception as exc:
            served = False
            rep.add("probe result", "not served (%s)" % str(exc)[:80])
    finally:
        _remove(probe_path)
    if served:
        rep.check("data dir outside web root", FAIL, "files in the data dir are served over HTTP: %s" % url)
    else:
        rep.check("data dir outside web root", WARN, "inside the web root but not served; move it outside anyway")


# --------------------------------------------------------------------------
# 7. Runtime capabilities and optional modules
# --------------------------------------------------------------------------

def check_runtime(rep, data_dir):
    rep.section("Runtime capabilities")
    rep.add("os.fork", "yes" if hasattr(os, "fork") else "no")
    try:
        import multiprocessing
        rep.add("multiprocessing start methods", multiprocessing.get_all_start_methods())
        rep.add("cpu count", multiprocessing.cpu_count())
        mp_ok = True
    except Exception as exc:
        rep.add("multiprocessing", "unavailable: %s" % exc)
        mp_ok = False
    rep.check("multiprocessing", PASS if mp_ok else WARN, "" if mp_ok else "tests could not run in parallel")

    try:
        import gzip
        import zlib
        blob = json.dumps({"x": list(range(500))}).encode()
        ok = gzip.decompress(gzip.compress(blob)) == blob and zlib.decompress(zlib.compress(blob)) == blob
        rep.add("zlib version", zlib.ZLIB_RUNTIME_VERSION if hasattr(zlib, "ZLIB_RUNTIME_VERSION") else zlib.ZLIB_VERSION)
        rep.check("gzip/zlib", PASS if ok else FAIL)
    except Exception as exc:
        rep.check("gzip/zlib", FAIL, "%s; the collector cannot accept compressed bodies" % exc)

    if os.name == "posix":
        try:
            import fcntl
            path = os.path.join(data_dir, ".lock-test")
            with open(path, "w") as fh:
                fcntl.lockf(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.lockf(fh, fcntl.LOCK_UN)
            _remove(path)
            rep.check("posix file locks", PASS)
        except Exception as exc:
            rep.check("posix file locks", FAIL, "%s; SQLite relies on them" % exc)

    for mod in ("wsgiref.handlers", "ssl", "tomllib", "tomli", "maxminddb", "geoip2", "ua_parser", "user_agents"):
        try:
            m = __import__(mod)
            extra = getattr(m, "OPENSSL_VERSION", "") if mod == "ssl" else ""
            rep.add("module %s" % mod, ("available " + extra).strip())
        except Exception:
            rep.add("module %s" % mod, "missing")


def check_outbound(rep, url):
    rep.section("Outbound HTTPS (for downloading geo databases from cron)")
    try:
        t0 = time.time()
        resp = urlopen(Request(url, method="HEAD", headers={"User-Agent": "tidesight-hosttest"}), timeout=10)
        rep.add("HEAD %s" % url, "%s in %.2f s" % (resp.status, time.time() - t0))
        rep.check("outbound https", PASS)
    except Exception as exc:
        rep.add("HEAD %s" % url, str(exc)[:120])
        rep.check("outbound https", WARN, "geo databases must then be uploaded manually")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run_all(opts, mode="cli", environ=None, stream=None):
    rep = Report(stream=stream)
    t0 = time.time()
    try:
        check_environment(rep, mode, environ)
        check_sqlite(rep)
        ok, created = check_filesystem(rep, opts["data_dir"])
        if not ok:
            rep.check("storage", FAIL, "the data directory is not writable")
        else:
            jm = check_journal_modes(rep, opts["data_dir"])
            check_runtime(rep, opts["data_dir"])
            rep.section("Concurrency and durability (%d processes x %d rows, %d reader%s)" % (
                opts["procs"], opts["rows"], opts["readers"], "" if opts["readers"] == 1 else "s"))
            conc = {}
            for jmode in ("wal", "delete"):
                if jm.get(jmode):
                    conc[jmode] = run_concurrency(rep, opts["data_dir"], jmode, opts["procs"], opts["rows"],
                                                  opts["readers"], opts["busy_ms"], opts["synchronous"],
                                                  opts["timeout"])
                else:
                    rep.check("concurrency %s" % jmode, FAIL, "journal mode not available")
            rep.data["concurrency"] = conc
            check_spool(rep, opts["data_dir"], opts["procs"], max(100, opts["rows"] // 4))
            check_web_exposure(rep, opts["data_dir"], environ, opts.get("web_roots"))
            if opts.get("outbound_url"):
                check_outbound(rep, opts["outbound_url"])
            recommend(rep)
            if not opts.get("keep"):
                for d in created:  # deepest first; only directories this run created
                    try:
                        os.rmdir(d)
                    except OSError:
                        break
    except Exception:
        rep.section("Unexpected error")
        rep.add("traceback", traceback.format_exc())
        rep.check("test run", FAIL, "unexpected error, see above")
    rep.data["elapsed_seconds"] = round(time.time() - t0, 1)
    return rep


def recommend(rep):
    wal = rep.status_of("concurrency wal")
    delete = rep.status_of("concurrency delete")
    fs = rep.data.get("filesystem", {})
    if wal in (PASS, WARN) and not fs.get("network"):
        rec = 'storage_mode = "sqlite", journal_mode = "wal"'
        status = wal
    elif delete in (PASS, WARN):
        rec = 'storage_mode = "sqlite", journal_mode = "delete"'
        status = delete
    elif wal in (PASS, WARN):
        rec = 'storage_mode = "sqlite", journal_mode = "wal" (network filesystem: watch for corruption)'
        status = WARN
    else:
        rec = 'storage_mode = "spool" (SQLite failed under concurrency on this host)'
        status = FAIL
    rep.data["recommendation"] = rec
    rep.check("storage", status, rec)


def default_data_dir():
    return os.path.join(home_dir(), "tidesight-hosttest")


def parse_args(argv):
    import argparse
    ap = argparse.ArgumentParser(description="Tidesight Phase 0 hosting test (SQLite on this host).")
    ap.add_argument("--data-dir", default=default_data_dir(),
                    help="directory to test (default: ~/tidesight-hosttest)")
    ap.add_argument("--procs", type=int, default=8, help="concurrent writer processes (default 8)")
    ap.add_argument("--rows", type=int, default=2000, help="rows per writer (default 2000)")
    ap.add_argument("--readers", type=int, default=1, help="concurrent reader processes (default 1)")
    ap.add_argument("--busy-ms", type=int, default=10000, help="busy_timeout in ms (default 10000)")
    ap.add_argument("--synchronous", default="NORMAL", choices=["NORMAL", "FULL", "OFF"],
                    help="PRAGMA synchronous for writers (default NORMAL)")
    ap.add_argument("--timeout", type=int, default=600, help="max seconds per concurrency run (default 600)")
    ap.add_argument("--web-root", action="append", dest="web_roots", default=[],
                    help="a web root to check the data dir against (repeatable)")
    ap.add_argument("--outbound-url", default="https://www.sqlite.org/",
                    help="URL for the outbound HTTPS check ('' to skip)")
    ap.add_argument("--quick", action="store_true", help="4 processes x 500 rows")
    ap.add_argument("--keep", action="store_true", help="keep the data dir if it was created")
    ap.add_argument("--json", action="store_true", help="print the report as JSON only")
    args = ap.parse_args(argv)
    if args.quick:
        args.procs, args.rows = 4, 500
    return {
        "data_dir": os.path.abspath(os.path.expanduser(args.data_dir)), "procs": max(1, args.procs),
        "rows": max(1, args.rows), "readers": max(0, args.readers), "busy_ms": args.busy_ms,
        "synchronous": args.synchronous, "timeout": args.timeout, "web_roots": args.web_roots,
        "outbound_url": args.outbound_url, "keep": args.keep, "json": args.json,
    }


def main(argv=None):
    opts = parse_args(sys.argv[1:] if argv is None else argv)
    if opts["json"]:
        rep = run_all(opts, mode="cli")
        print(rep.to_json())
    else:
        print("Tidesight hosting test v%s - testing %s" % (SCRIPT_VERSION, opts["data_dir"]))
        rep = run_all(opts, mode="cli", stream=sys.stdout)
        print(rep.render_summary())
        print("\n(elapsed %.1f s)" % rep.data.get("elapsed_seconds", 0))
    return {PASS: 0, WARN: 0, FAIL: 1}.get(rep.overall(), 1)


# --------------------------------------------------------------------------
# CGI / WSGI entry points
# --------------------------------------------------------------------------

def application(environ, start_response):
    """WSGI entry point (Passenger, or CGI through wsgiref)."""
    qs = parse_qs(environ.get("QUERY_STRING", ""))
    get = lambda k, d: qs.get(k, [d])[0]
    expected = TOKEN or os.environ.get("TIDESIGHT_TEST_TOKEN", "")
    headers = [("Content-Type", "text/plain; charset=utf-8"), ("Cache-Control", "no-store"),
               ("X-Robots-Tag", "noindex, nofollow")]
    if not expected:
        start_response("403 Forbidden", headers)
        return [b"Set TOKEN in the script (or TIDESIGHT_TEST_TOKEN) and pass ?token=... to run the test.\n"]
    if get("token", "") != expected:
        start_response("403 Forbidden", headers)
        return [b"Forbidden.\n"]
    try:
        opts = {
            "data_dir": os.path.abspath(os.path.expanduser(get("data_dir", default_data_dir()))),
            "procs": max(1, min(16, int(get("procs", "4")))),
            "rows": max(1, min(5000, int(get("rows", "500")))),
            "readers": max(0, min(4, int(get("readers", "1")))),
            "busy_ms": int(get("busy_ms", "10000")), "synchronous": "NORMAL",
            "timeout": int(get("timeout", "120")), "web_roots": [],
            "outbound_url": get("outbound_url", "https://www.sqlite.org/"), "keep": False,
        }
    except ValueError:
        start_response("400 Bad Request", headers)
        return [b"Bad parameter.\n"]
    mode = "cgi" if "GATEWAY_INTERFACE" in os.environ else "wsgi"
    rep = run_all(opts, mode=mode, environ=environ)
    if get("format", "text") == "json":
        body = rep.to_json()
        headers[0] = ("Content-Type", "application/json; charset=utf-8")
    else:
        body = rep.render_text()
    start_response("200 OK", headers)
    return [body.encode("utf-8")]


if __name__ == "__main__":
    if "GATEWAY_INTERFACE" in os.environ and "REQUEST_METHOD" in os.environ:
        from wsgiref.handlers import CGIHandler
        CGIHandler().run(application)
    else:
        sys.exit(main())
