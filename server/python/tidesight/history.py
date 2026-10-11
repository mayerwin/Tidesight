"""History imported from another analytics tool: Google Analytics 4, Plausible, Matomo (docs/history.md).

What other tools keep is daily totals, never visits: visitors, visits, pageviews, bounces and visit time per day,
in total and per page, entry and exit page, source, medium, campaign, channel, referrer, country, region, city,
device, browser and operating system. They go into history.db (raw data from elsewhere: never rebuilt, backed up)
in Tidesight's own names (sources and channels as enrichment names them, ISO country codes, Tidesight's browser and
system names), and the reports add them for the days before the property's first day of Tidesight data, so a
switch leaves no gap and counts nothing twice.

An import is a job: queued by the dashboard (or `jobs.py history`), carried out by `jobs.py cron` within a time
budget, step by step (each step can run again: it first deletes what it wrote). A job's days replace those of an
earlier import. Imports run once: when a tool changes its API or its export later, the history already imported
stays.

Limits, said in the reports: figures summed over days count a visitor once per day; filters, segments and goals
cannot apply to imported days (they hold no visits), so a filtered report shows Tidesight's own data only.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import sqlite3
import time
import zipfile
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import urlencode, urlsplit

from .config import Config
from .storage import connect, migrate

log = logging.getLogger("tidesight.history")

DB = "history.db"
SOURCES = {"ga4": "Google Analytics 4", "plausible": "Plausible", "matomo": "Matomo"}
# Imported dimensions: Tidesight's dimension names. '' holds the day's totals.
DIMS = ("page", "entry", "exit", "source", "medium", "campaign", "channel", "referrer", "country", "region", "city",
        "device", "browser", "os")
METRICS = ("visitors", "visits", "pageviews", "bounces", "duration_s")
GA4_API = "https://analyticsdata.googleapis.com/v1beta/"
GA4_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
GA4_FIRST_DAY = "2015-08-14"     # the Data API's earliest date
MATOMO_FIRST_DAY = "2008-01-01"
MAX_UPLOAD = 16 * 1024 * 1024    # what the dashboard accepts (FastCGI keeps 16 MB of a body); larger: jobs.py
MAX_UNZIPPED = 2 * 1024 ** 3     # bytes read out of an export, in all
MAX_TEXT = 1000

HISTORY_MIGRATIONS: List[Tuple[int, List[str]]] = [
    (1, [
        """CREATE TABLE IF NOT EXISTS imported (
            site_id    TEXT NOT NULL,
            day        TEXT NOT NULL,
            dim        TEXT NOT NULL,
            value      TEXT NOT NULL,
            job_id     INTEGER NOT NULL,
            visitors   INTEGER NOT NULL DEFAULT 0,
            visits     INTEGER NOT NULL DEFAULT 0,
            pageviews  INTEGER NOT NULL DEFAULT 0,
            bounces    INTEGER NOT NULL DEFAULT 0,
            duration_s REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (site_id, dim, day, value)
        ) WITHOUT ROWID""",
        "CREATE INDEX IF NOT EXISTS imported_job ON imported(job_id, dim, day)",
        """CREATE TABLE IF NOT EXISTS history_jobs (
            id          INTEGER PRIMARY KEY,
            site_id     TEXT NOT NULL,
            source      TEXT NOT NULL,
            params      TEXT NOT NULL,
            status      TEXT NOT NULL,
            created_at  INTEGER NOT NULL,
            created_by  TEXT,
            started_at  INTEGER,
            finished_at INTEGER,
            progress    TEXT,
            first_day   TEXT,
            last_day    TEXT,
            rows        INTEGER NOT NULL DEFAULT 0,
            error       TEXT,
            retry_at    INTEGER
        )""",
        "CREATE INDEX IF NOT EXISTS history_jobs_site ON history_jobs(site_id, id)",
    ]),
]


class HistoryError(Exception):
    """An import that cannot go on: kind config (what was given), auth, permission, setup, or rate, server,
    network (it may work later)."""

    def __init__(self, message: str, kind: str = "config") -> None:
        super().__init__(message)
        self.kind = kind

    @property
    def transient(self) -> bool:
        return self.kind in ("rate", "server", "network")


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# history.db
# ---------------------------------------------------------------------------

def db_path(data_dir: str) -> str:
    return os.path.join(data_dir, DB)


def uploads_dir(data_dir: str) -> str:
    return os.path.join(data_dir, "imports")


def open_db(cfg: Config) -> sqlite3.Connection:
    s = cfg.server
    conn = connect(db_path(cfg.data_dir), s["journal_mode"], s["synchronous"], max(30000, s["busy_timeout_ms"]))
    migrate(conn, HISTORY_MIGRATIONS)
    conn.row_factory = sqlite3.Row
    return conn


def open_readonly(data_dir: str) -> Optional[sqlite3.Connection]:
    p = db_path(data_dir)
    if not os.path.exists(p):
        return None
    conn = connect(p, readonly=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("SELECT 1 FROM imported LIMIT 1")
    except sqlite3.Error:
        conn.close()
        return None
    return conn


def _days(start: str, end: str) -> Iterator[str]:
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    while d0 <= d1:
        yield d0.isoformat()
        d0 += timedelta(days=1)


def _shift(day: str, n: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def first_tidesight_day(data_dir: str, site_id: str) -> Optional[str]:
    """The property's first day of Tidesight's own data: imported days from then on are not counted."""
    p = os.path.join(data_dir, "derived.db")
    if not os.path.exists(p):
        return None
    conn = connect(p, readonly=True)
    try:
        return conn.execute("SELECT MIN(day) FROM daily WHERE site_id = ? AND sessions > 0", (site_id,)).fetchone()[0]
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def default_last_day(data_dir: str, site_id: str) -> str:
    """The day before Tidesight's first day (yesterday when there is none yet)."""
    first = first_tidesight_day(data_dir, site_id)
    return _shift(first, -1) if first else _shift(time.strftime("%Y-%m-%d", time.gmtime()), -1)


# ---------------------------------------------------------------------------
# Names: Tidesight's, whatever the tool called them
# ---------------------------------------------------------------------------

_DEVICES = {"smartphone": "mobile", "phablet": "mobile", "mobile": "mobile", "feature phone": "mobile",
            "tablet": "tablet", "desktop": "desktop", "laptop": "desktop"}
_BROWSERS = {"chrome mobile": "Chrome", "chrome mobile ios": "Chrome", "chrome ios": "Chrome", "mobile safari": "Safari",
             "safari (in-app)": "Safari", "microsoft edge": "Edge", "edge mobile": "Edge", "firefox mobile": "Firefox",
             "firefox mobile ios": "Firefox", "firefox ios": "Firefox", "samsung browser": "Samsung Internet",
             "samsung internet": "Samsung Internet", "opera mobile": "Opera", "android webview": "Android WebView",
             "(not set)": ""}
_SYSTEMS = {"mac": "macOS", "macintosh": "macOS", "mac os x": "macOS", "mac os": "macOS", "os x": "macOS",
            "gnu/linux": "Linux", "chrome os": "ChromeOS", "chromeos": "ChromeOS", "ipados": "iPadOS",
            "windows phone": "Windows Phone", "(not set)": ""}
_NONE = {"", "(not set)", "(none)", "(other)", "unknown", "direct / none", "-"}


def _text(v: Any) -> str:
    s = " ".join(str(v if v is not None else "").split())
    return s[:MAX_TEXT]


def norm(dim: str, value: Any) -> str:
    """A value in Tidesight's terms ('' when the tool did not know it)."""
    v = _text(value)
    if v.lower() in _NONE:
        return ""
    if dim == "device":
        return _DEVICES.get(v.lower(), v.lower())
    if dim == "browser":
        return _BROWSERS.get(v.lower(), v)
    if dim == "os":
        return _SYSTEMS.get(v.lower(), v)
    if dim == "country":
        return v.upper() if re.fullmatch(r"[A-Za-z]{2}", v) else ""
    if dim in ("page", "entry", "exit"):
        return page_path(v)
    return v


def page_path(value: str) -> str:
    """The path of a page as Tidesight names it (an address, a path, or Matomo's "blog/post")."""
    v = value.strip()
    if "://" in v:
        try:
            v = urlsplit(v).path or "/"
        except ValueError:
            return ""
    v = v.split("?", 1)[0].split("#", 1)[0]
    if not v.startswith("/"):
        v = "/" + v
    return v[:MAX_TEXT]


def attribution(name: str = "", referrer: str = "", utm_source: str = "", utm_medium: str = "",
                utm_campaign: str = "") -> Dict[str, str]:
    """Source, medium, campaign, channel and referrer domain as enrichment names them (enrich/sources.py), from a
    tool's source name (Google, Facebook), referrer domain and utm_ tags."""
    from .enrich import sources as src
    name, referrer, utm_source, utm_medium, utm_campaign = (
        _text(x) for x in (name, referrer, utm_source, utm_medium, utm_campaign))
    if referrer or utm_source or utm_medium or utm_campaign:
        host = referrer.split("://", 1)[-1].split("/", 1)[0] if referrer else ""
        query = urlencode([(k, v) for k, v in (("utm_source", utm_source), ("utm_medium", utm_medium),
                                                ("utm_campaign", utm_campaign)) if v])
        c = src.classify("https://imported.invalid/" + ("?" + query if query else ""),
                         "https://%s/" % host if host else None, site_hosts=["imported.invalid"])
        if c.get("source") not in (None, "", "(direct)") or name.lower() in _NONE:
            return {"source": c.get("source") or "", "medium": c.get("medium") or "", "campaign": c.get("campaign") or "",
                    "channel": c.get("channel") or "", "referrer": c.get("referrer_domain") or ""}
    if name.lower() in _NONE:
        return {"source": "(direct)", "medium": "(none)", "campaign": "(direct)", "channel": "Direct", "referrer": ""}
    source = name.lower()
    category = src.source_category(source)
    medium = "organic" if category == "search" else "referral"
    campaign = "(organic)" if medium == "organic" else "(referral)"
    return {"source": source, "medium": medium, "campaign": campaign,
            "channel": src.channel_group(source, medium, campaign, category), "referrer": ""}


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

class Writer:
    """Daily figures of one job, summed per day, dimension and value, written in one transaction per flush."""

    def __init__(self, conn: sqlite3.Connection, site_id: str, job_id: int) -> None:
        self.conn, self.site_id, self.job_id = conn, site_id, job_id
        self.acc: Dict[Tuple[str, str, str], List[float]] = {}
        self.rows = 0

    def add(self, day: str, dim: str, value: str, visitors: Any = 0, visits: Any = 0, pageviews: Any = 0,
            bounces: Any = 0, duration_s: Any = 0) -> None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
            return
        if dim and not value:
            value = "(none)"
        acc = self.acc.setdefault((day, dim, value), [0, 0, 0, 0, 0.0])
        for i, v in enumerate((visitors, visits, pageviews, bounces, duration_s)):
            acc[i] += _num(v)

    def clear(self, dims: Sequence[str], start: Optional[str] = None, end: Optional[str] = None) -> None:
        """What this job wrote for these dimensions (and days): a step that runs again starts clean."""
        for dim in dims:
            sql, args = "DELETE FROM imported WHERE job_id = ? AND dim = ?", [self.job_id, dim]
            if start:
                sql, args = sql + " AND day BETWEEN ? AND ?", args + [start, end]
            self.conn.execute(sql, args)

    def flush(self) -> int:
        if not self.acc:
            return 0
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.executemany(
                "INSERT INTO imported(site_id, day, dim, value, job_id, visitors, visits, pageviews, bounces, duration_s) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(site_id, dim, day, value) DO UPDATE SET "
                "job_id = excluded.job_id, visitors = visitors + excluded.visitors, visits = visits + excluded.visits, "
                "pageviews = pageviews + excluded.pageviews, bounces = bounces + excluded.bounces, "
                "duration_s = duration_s + excluded.duration_s",
                [(self.site_id, d, dim, v, self.job_id, int(a[0]), int(a[1]), int(a[2]), int(a[3]), a[4])
                 for (d, dim, v), a in self.acc.items()])
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        n = len(self.acc)
        self.rows += n
        self.acc = {}
        return n


def _num(v: Any) -> float:
    if isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v) if v == v and abs(v) < 1e15 else 0.0
    try:
        f = float(str(v).strip() or 0)
    except ValueError:
        return 0.0
    return f if f == f and abs(f) < 1e15 else 0.0


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def create_job(cfg: Config, site_id: str, source: str, params: Dict[str, Any], user: str) -> int:
    conn = open_db(cfg)
    try:
        cur = conn.execute("INSERT INTO history_jobs(site_id, source, params, status, created_at, created_by, progress) "
                           "VALUES (?, ?, ?, 'queued', ?, ?, '{}')",
                           (site_id, source, json.dumps(params), _now_ms(), user))
        return int(cur.lastrowid)
    finally:
        conn.close()


def jobs(cfg: Config, site_id: str) -> List[Dict[str, Any]]:
    """A property's imports, newest first (never their secrets)."""
    conn = open_readonly(cfg.data_dir)
    if conn is None:
        return []
    try:
        out = []
        for r in conn.execute("SELECT * FROM history_jobs WHERE site_id = ? ORDER BY id DESC LIMIT 50", (site_id,)):
            params = _loads(r["params"])
            shown = {k: v for k, v in params.items() if k in ("property", "url", "id_site", "from", "to", "name")}
            kept = conn.execute("SELECT MIN(day), MAX(day), COUNT(DISTINCT day) FROM imported WHERE job_id = ? AND dim = ''",
                                (r["id"],)).fetchone()
            out.append({"id": r["id"], "source": r["source"], "label": SOURCES.get(r["source"], r["source"]),
                        "status": r["status"], "params": shown, "created_at": r["created_at"],
                        "created_by": r["created_by"], "started_at": r["started_at"], "finished_at": r["finished_at"],
                        "first_day": r["first_day"], "last_day": r["last_day"], "rows": r["rows"], "error": r["error"],
                        "retry_at": r["retry_at"], "progress": _loads(r["progress"]),
                        "days_kept": kept[2], "kept_from": kept[0], "kept_to": kept[1]})
        return out
    finally:
        conn.close()


def delete_job(cfg: Config, site_id: str, job_id: int) -> bool:
    """Forget an import: its figures, its uploaded file, the job (a running one stops at its next step)."""
    if not os.path.exists(db_path(cfg.data_dir)):
        return False
    conn = open_db(cfg)
    try:
        row = conn.execute("SELECT params FROM history_jobs WHERE id = ? AND site_id = ?", (job_id, site_id)).fetchone()
        if row is None:
            return False
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM imported WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM history_jobs WHERE id = ?", (job_id,))
        conn.execute("COMMIT")
        _remove_upload(cfg, _loads(row["params"]))
        return True
    finally:
        conn.close()


def purge_site(cfg: Config, site_id: str, apply: bool) -> Dict[str, int]:
    out: Dict[str, int] = {}
    if not os.path.exists(db_path(cfg.data_dir)):
        return out
    conn = open_db(cfg)
    try:
        for table in ("imported", "history_jobs"):
            n = conn.execute("SELECT COUNT(*) FROM %s WHERE site_id = ?" % table, (site_id,)).fetchone()[0]
            if n:
                out["history.db:" + table] = n
        if apply:
            for r in conn.execute("SELECT params FROM history_jobs WHERE site_id = ?", (site_id,)).fetchall():
                _remove_upload(cfg, _loads(r["params"]))
            conn.execute("DELETE FROM imported WHERE site_id = ?", (site_id,))
            conn.execute("DELETE FROM history_jobs WHERE site_id = ?", (site_id,))
    finally:
        conn.close()
    return out


def _remove_upload(cfg: Config, params: Dict[str, Any]) -> None:
    name = params.get("file")
    if isinstance(name, str) and re.fullmatch(r"[0-9a-f]{32}\.zip", name):
        try:
            os.remove(os.path.join(uploads_dir(cfg.data_dir), name))
        except OSError:
            pass


def _loads(raw: Optional[str]) -> Dict[str, Any]:
    try:
        v = json.loads(raw or "{}")
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def run(cfg: Config, max_seconds: float = 25.0, job_id: Optional[int] = None) -> Dict[str, Any]:
    """Carry imports forward within a time budget (the rest at the next run)."""
    from .enrich.db import JobLock
    p = db_path(cfg.data_dir)
    if not os.path.exists(p):
        return {}
    conn = open_db(cfg)
    try:
        sql = "SELECT id FROM history_jobs WHERE status IN ('queued', 'running')"
        args: List[Any] = []
        if job_id is not None:
            sql, args = sql + " AND id = ?", [job_id]
        else:
            sql, args = sql + " AND (retry_at IS NULL OR retry_at <= ?)", [_now_ms()]
        todo = [r[0] for r in conn.execute(sql + " ORDER BY id", args)]
        if not todo:
            return {}
        lock = JobLock(cfg.data_dir, "history")
        if not lock.acquire():
            return {"skipped": "another history import is running"}
        out: Dict[str, Any] = {}
        deadline = time.monotonic() + max_seconds
        try:
            for jid in todo:
                if time.monotonic() > deadline:
                    break
                out[str(jid)] = _run_job(cfg, conn, jid, deadline)
        finally:
            lock.release()
        return out
    finally:
        conn.close()


def _run_job(cfg: Config, conn: sqlite3.Connection, jid: int, deadline: float) -> Dict[str, Any]:
    row = conn.execute("SELECT * FROM history_jobs WHERE id = ?", (jid,)).fetchone()
    if row is None:
        return {"gone": True}
    if cfg.site(row["site_id"]) is None:
        _finish(conn, jid, "failed", "the property no longer exists")
        return {"error": "the property no longer exists"}
    params, progress = _loads(row["params"]), _loads(row["progress"])
    if row["status"] == "queued":
        conn.execute("UPDATE history_jobs SET status = 'running', started_at = ?, error = NULL WHERE id = ?",
                     (_now_ms(), jid))
    job = Job(cfg, conn, dict(row), params, progress, deadline)
    try:
        done = {"ga4": job.ga4, "plausible": job.plausible, "matomo": job.matomo}[row["source"]]()
    except HistoryError as exc:
        job.save()
        if exc.transient:
            conn.execute("UPDATE history_jobs SET error = ?, retry_at = ? WHERE id = ?",
                         (str(exc), _now_ms() + 10 * 60000, jid))
        else:
            _finish(conn, jid, "failed", str(exc))
            _forget_secrets(cfg, conn, jid, params)
        return {"error": str(exc)}
    except Exception as exc:  # a file or an answer nobody expected: the job fails, cron goes on
        log.exception("history import %s failed", jid)
        job.save()
        _finish(conn, jid, "failed", "unexpected error: %s" % exc)
        _forget_secrets(cfg, conn, jid, params)
        return {"error": str(exc)}
    job.save()
    if done:
        _finish(conn, jid, "done", None)
        _forget_secrets(cfg, conn, jid, params)
    return {"rows": job.writer.rows, "done": bool(done)}


def _finish(conn: sqlite3.Connection, jid: int, status: str, error: Optional[str]) -> None:
    conn.execute("UPDATE history_jobs SET status = ?, finished_at = ?, error = ?, retry_at = NULL WHERE id = ?",
                 (status, _now_ms(), error, jid))


def _forget_secrets(cfg: Config, conn: sqlite3.Connection, jid: int, params: Dict[str, Any]) -> None:
    """A finished import keeps no token, and no uploaded file."""
    _remove_upload(cfg, params)
    kept = {k: v for k, v in params.items() if k not in ("token", "file")}
    conn.execute("UPDATE history_jobs SET params = ? WHERE id = ?", (json.dumps(kept), jid))


class Job:
    """One import being carried out: its steps, its progress (saved after each step), its writer."""

    def __init__(self, cfg: Config, conn: sqlite3.Connection, row: Dict[str, Any], params: Dict[str, Any],
                 progress: Dict[str, Any], deadline: float) -> None:
        self.cfg, self.conn, self.row, self.params, self.progress, self.deadline = cfg, conn, row, params, progress, deadline
        self.id, self.site_id = row["id"], row["site_id"]
        self.writer = Writer(conn, self.site_id, self.id)

    def save(self) -> None:
        self.writer.flush()
        self.conn.execute("UPDATE history_jobs SET progress = ?, rows = rows + ?, first_day = ?, last_day = ? WHERE id = ?",
                          (json.dumps(self.progress), self.writer.rows, self.progress.get("first"),
                           self.progress.get("last"), self.id))
        self.writer.rows = 0

    def out_of_time(self) -> bool:
        return time.monotonic() > self.deadline

    def claim_days(self, first: str, last: str) -> None:
        """This import's days replace those of earlier imports of the property."""
        self.progress.update(first=first, last=last)
        self.conn.execute("DELETE FROM imported WHERE site_id = ? AND job_id != ? AND day BETWEEN ? AND ?",
                          (self.site_id, self.id, first, last))

    def range(self, first_default: str) -> Tuple[str, str]:
        start = self.params.get("from") or first_default
        end = self.params.get("to") or default_last_day(self.cfg.data_dir, self.site_id)
        if start > end:
            raise HistoryError("nothing to import: the period ends (%s) before it starts (%s)" % (end, start))
        return start, end

    # -- Google Analytics 4 ---------------------------------------------------------------------------------

    GA4_STEPS: List[Tuple[str, List[str], List[str]]] = [  # (step, GA4 dimensions after date, metrics)
        ("page", ["pagePath"], ["screenPageViews", "totalUsers", "sessions"]),
        ("entry", ["landingPage"], ["totalUsers", "sessions", "screenPageViews", "engagedSessions", "averageSessionDuration"]),
        ("source", ["sessionSource", "sessionMedium", "sessionCampaignName"],
         ["totalUsers", "sessions", "screenPageViews", "engagedSessions", "averageSessionDuration"]),
        ("channel", ["sessionDefaultChannelGroup"], ["totalUsers", "sessions", "screenPageViews", "engagedSessions", "averageSessionDuration"]),
        ("country", ["countryId"], ["totalUsers", "sessions", "screenPageViews", "engagedSessions", "averageSessionDuration"]),
        ("region", ["region"], ["totalUsers", "sessions", "screenPageViews", "engagedSessions", "averageSessionDuration"]),
        ("city", ["city"], ["totalUsers", "sessions", "screenPageViews", "engagedSessions", "averageSessionDuration"]),
        ("device", ["deviceCategory"], ["totalUsers", "sessions", "screenPageViews", "engagedSessions", "averageSessionDuration"]),
        ("browser", ["browser"], ["totalUsers", "sessions", "screenPageViews", "engagedSessions", "averageSessionDuration"]),
        ("os", ["operatingSystem"], ["totalUsers", "sessions", "screenPageViews", "engagedSessions", "averageSessionDuration"]),
    ]

    def ga4(self) -> bool:
        from . import searchengines as se
        cred = se.credentials(self.cfg.data_dir).get("google")
        if not cred:
            raise HistoryError("connect the Google service account first (the property's Search engines settings)")
        client = se.Google(cred, scope=GA4_SCOPE)
        prop = str(self.params.get("property") or "")
        if "first" not in self.progress:  # the days with data, and the day totals
            start, end = self.range(GA4_FIRST_DAY)
            rows = _ga4_rows(client, prop, start, end, [], ["totalUsers", "sessions", "screenPageViews",
                                                             "engagedSessions", "averageSessionDuration"])
            days = sorted(r[0] for r in rows if _num(r[2]) > 0 or _num(r[3]) > 0)
            if not days:
                raise HistoryError("Google Analytics has no visits for this property between %s and %s" % (start, end))
            self.writer.clear([""])
            self.claim_days(days[0], days[-1])
            for d, users, sessions, views, engaged, avg in rows:
                self.writer.add(d, "", "", users, sessions, views, _num(sessions) - _num(engaged), _num(avg) * _num(sessions))
            self.progress["done"] = []
            self.save()
        for step, dims, metrics in self.GA4_STEPS:
            if step in self.progress["done"]:
                continue
            for start, end in _chunks(self.progress["first"], self.progress["last"], 366):
                if self.out_of_time():
                    return False
                key = "%s:%s" % (step, start)
                if key in self.progress["done"]:
                    continue
                written = ["source", "medium", "campaign", "referrer"] if step == "source" else [step]
                self.writer.clear(written, start, end)
                for r in _ga4_rows(client, prop, start, end, dims, metrics):
                    self._ga4_add(step, r)
                self.progress["done"].append(key)
                self.save()
            self.progress["done"] = [k for k in self.progress["done"] if not k.startswith(step + ":")] + [step]
            self.save()
        return True

    def _ga4_add(self, step: str, r: Sequence[str]) -> None:
        d = r[0]
        if step == "page":
            self.writer.add(d, "page", norm("page", r[1]), r[3], r[4], r[2])
            return
        users, sessions, views, engaged, avg = r[-5:]
        bounces, duration = _num(sessions) - _num(engaged), _num(avg) * _num(sessions)
        if step == "source":  # GA4 names them as enrichment does: google / organic / (organic), (direct) / (none)
            for dim, value in (("source", r[1]), ("medium", r[2]), ("campaign", r[3])):
                self.writer.add(d, dim, "" if value == "(not set)" else _text(value), users, sessions, views, bounces,
                                duration)
            return
        self.writer.add(d, step, norm(step, r[1]), users, sessions, views, bounces, duration)

    # -- Plausible ------------------------------------------------------------------------------------------

    def plausible(self) -> bool:
        name = self.params.get("file")
        path = name if os.path.isabs(str(name)) else os.path.join(uploads_dir(self.cfg.data_dir), str(name))
        try:
            z = zipfile.ZipFile(path)
        except (OSError, zipfile.BadZipFile) as exc:
            raise HistoryError("the export cannot be read (%s): download it again from Plausible" % exc)
        with z:
            tables = _plausible_tables(z)
            if "imported_visitors" not in tables:
                raise HistoryError("this ZIP holds no imported_visitors CSV: is it Plausible's export?")
            budget = [MAX_UNZIPPED]
            days = []
            for row in _csv_rows(z, tables["imported_visitors"], budget):
                if _num(row.get("visits")) > 0 or _num(row.get("pageviews")) > 0:
                    days.append(row.get("date", ""))
            days = sorted(d for d in days if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d))
            if not days:
                raise HistoryError("the export holds no visits")
            start, end = self.range(days[0])
            within = [d for d in days if start <= d <= end]
            if not within:
                raise HistoryError("the export has no day between %s and %s (Tidesight's own data starts after)"
                                   % (start, end))
            first, last = within[0], within[-1]
            self.writer.clear([""] + list(DIMS))
            self.claim_days(first, last)
            for table, fn in _PLAUSIBLE.items():
                if table not in tables:
                    continue
                for row in _csv_rows(z, tables[table], budget):
                    d = row.get("date", "")
                    if first <= d <= last:
                        fn(self.writer, d, row)
                if len(self.writer.acc) > 200000:
                    self.writer.flush()
        return True

    # -- Matomo -----------------------------------------------------------------------------------------------

    def matomo(self) -> bool:
        api = Matomo(str(self.params.get("url") or ""), str(self.params.get("id_site") or ""),
                     str(self.params.get("token") or ""))
        if "first" not in self.progress:
            start, end = self.range(MATOMO_FIRST_DAY)
            visits = api.call("VisitsSummary.get", start, end)
            actions = api.call("Actions.get", start, end)
            days = sorted(d for d, v in visits.items() if isinstance(v, dict) and _num(v.get("nb_visits")) > 0)
            if not days:
                raise HistoryError("Matomo has no visits for this site between %s and %s" % (start, end))
            self.writer.clear([""])
            self.claim_days(days[0], days[-1])
            for d in days:
                v = visits[d]
                a = actions.get(d) if isinstance(actions.get(d), dict) else {}
                self.writer.add(d, "", "", v.get("nb_uniq_visitors") or v.get("nb_visits"), v.get("nb_visits"),
                                a.get("nb_pageviews", v.get("nb_actions")), v.get("bounce_count"), v.get("sum_visit_length"))
            self.progress["done"] = []
            self.save()
        for step, method, written in MATOMO_STEPS:
            if step in self.progress["done"]:
                continue
            for start, end in _chunks(self.progress["first"], self.progress["last"], 92):
                if self.out_of_time():
                    return False
                key = "%s:%s" % (step, start)
                if key in self.progress["done"]:
                    continue
                self.writer.clear(written, start, end)
                extra = {"flat": "1"} if method.startswith("Actions.") else {}
                for d, rows in api.call(method, start, end, **extra).items():
                    for r in rows if isinstance(rows, list) else []:
                        if isinstance(r, dict):
                            _matomo_add(self.writer, step, d, r)
                self.progress["done"].append(key)
                self.save()
            self.progress["done"] = [k for k in self.progress["done"] if not k.startswith(step + ":")] + [step]
            self.save()
        return True


def _chunks(first: str, last: str, days: int) -> Iterator[Tuple[str, str]]:
    start = first
    while start <= last:
        end = min(last, _shift(start, days - 1))
        yield start, end
        start = _shift(end, 1)


# ---- GA4 -------------------------------------------------------------------------------------------------------

def _ga4_none(v: str) -> str:
    return "" if v in ("(not set)", "(none)", "(direct)", "(organic)", "(referral)", "(other)") else v


def _ga4_rows(client: Any, prop: str, start: str, end: str, dims: List[str],
              metrics: List[str]) -> List[List[str]]:
    """Rows [day, *dimension values, *metric values] of a Data API report, page after page."""
    from .searchengines import SearchEngineError, _google_error, _json, http_request
    out: List[List[str]] = []
    offset = 0
    while True:
        body = {"dateRanges": [{"startDate": start, "endDate": end}],
                "dimensions": [{"name": "date"}] + [{"name": d} for d in dims],
                "metrics": [{"name": m} for m in metrics], "limit": 100000, "offset": offset,
                "keepEmptyRows": False}
        try:
            token = client.token()
            status, raw = http_request("POST", GA4_API + "properties/%s:runReport" % prop, json.dumps(body).encode("utf-8"),
                                       {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        except SearchEngineError as exc:
            raise HistoryError(str(exc), exc.kind)
        doc = _json(raw)
        if status != 200 or not isinstance(doc, dict):
            err = _google_error(status, doc, "Google Analytics refused", client.email)
            msg = str(err)
            if "sufficient permission" in msg or status == 403 and "Analytics Data API" not in msg:
                msg += " In Google Analytics, Admin → Property access management: add %s as a Viewer." % client.email
            if "Search Console API" in msg:
                msg = msg.replace("Enable the Google Search Console API", "Enable the Google Analytics Data API")
            raise HistoryError(msg, err.kind)
        rows = doc.get("rows") or []
        for r in rows if isinstance(rows, list) else []:
            dv = [x.get("value", "") if isinstance(x, dict) else "" for x in r.get("dimensionValues") or []]
            mv = [x.get("value", "0") if isinstance(x, dict) else "0" for x in r.get("metricValues") or []]
            if len(dv) != len(dims) + 1 or len(mv) != len(metrics) or not re.fullmatch(r"\d{8}", dv[0]):
                continue
            out.append(["%s-%s-%s" % (dv[0][:4], dv[0][4:6], dv[0][6:])] + dv[1:] + mv)
        offset += len(rows) if isinstance(rows, list) else 0
        if not rows or offset >= int(_num(doc.get("rowCount"))):
            return out


# ---- Plausible -------------------------------------------------------------------------------------------------

def _plausible_tables(z: zipfile.ZipFile) -> Dict[str, zipfile.ZipInfo]:
    """{table: the CSV in the ZIP}: imported_visitors_20230101_20231231.csv and the like (the last of a table)."""
    out = {}
    for info in z.infolist():
        m = re.fullmatch(r"(?:.*/)?(imported_[a-z_]+?)(?:_\d{8}_\d{8})?\.csv", info.filename)
        if m:
            out[m.group(1)] = info
    return out


def _csv_rows(z: zipfile.ZipFile, info: zipfile.ZipInfo, budget: List[int]) -> Iterator[Dict[str, str]]:
    """The rows of a CSV of the export, read as a stream (a bounded number of bytes in all: no ZIP bomb)."""
    class Bounded(io.RawIOBase):
        def __init__(self, raw: Any) -> None:
            self.raw = raw

        def readable(self) -> bool:
            return True

        def readinto(self, b: Any) -> int:
            data = self.raw.read(min(len(b), 1 << 20))
            budget[0] -= len(data)
            if budget[0] < 0:
                raise HistoryError("the export is larger than %d GB once unpacked" % (MAX_UNZIPPED // 1024 ** 3))
            b[:len(data)] = data
            return len(data)

    with z.open(info) as raw:
        text = io.TextIOWrapper(io.BufferedReader(Bounded(raw)), encoding="utf-8-sig", errors="replace", newline="")
        csv.field_size_limit(1 << 20)
        for row in csv.DictReader(text):
            yield {str(k).strip().lower(): (v or "") for k, v in row.items() if k is not None}


def _metrics(row: Dict[str, str], visits_keys: Sequence[str] = ("visits",)) -> Tuple[Any, ...]:
    visits = next((row[k] for k in visits_keys if row.get(k) not in (None, "")), 0)
    return row.get("visitors", 0), visits, row.get("pageviews", 0), row.get("bounces", 0), row.get("visit_duration", 0)


def _p_visitors(w: Writer, d: str, row: Dict[str, str]) -> None:
    w.add(d, "", "", *_metrics(row))


def _p_sources(w: Writer, d: str, row: Dict[str, str]) -> None:
    a = attribution(row.get("source", ""), row.get("referrer", ""), row.get("utm_source", ""),
                    row.get("utm_medium", ""), row.get("utm_campaign", ""))
    for dim in ("source", "medium", "campaign", "channel", "referrer"):
        if dim == "referrer" and not a["referrer"]:
            continue
        w.add(d, dim, a[dim], *_metrics(row))


def _p_pages(w: Writer, d: str, row: Dict[str, str]) -> None:
    w.add(d, "page", norm("page", row.get("page", "")), row.get("visitors", 0), row.get("visits", 0),
          row.get("pageviews", 0))


def _p_entry(w: Writer, d: str, row: Dict[str, str]) -> None:
    w.add(d, "entry", norm("entry", row.get("entry_page", "")), *_metrics(row, ("entrances", "visits")))


def _p_exit(w: Writer, d: str, row: Dict[str, str]) -> None:
    w.add(d, "exit", norm("exit", row.get("exit_page", "")), row.get("visitors", 0),
          next((row[k] for k in ("exits", "visits") if row.get(k)), 0))


def _p_dim(dim: str, column: str) -> Callable[[Writer, str, Dict[str, str]], None]:
    def add(w: Writer, d: str, row: Dict[str, str]) -> None:
        w.add(d, dim, norm(dim, row.get(column, "")), *_metrics(row))
    return add


_PLAUSIBLE: Dict[str, Callable[[Writer, str, Dict[str, str]], None]] = {
    "imported_visitors": _p_visitors, "imported_sources": _p_sources, "imported_pages": _p_pages,
    "imported_entry_pages": _p_entry, "imported_exit_pages": _p_exit,
    "imported_locations": _p_dim("country", "country"), "imported_devices": _p_dim("device", "device"),
    "imported_browsers": _p_dim("browser", "browser"), "imported_operating_systems": _p_dim("os", "operating_system"),
}


# ---- Matomo ----------------------------------------------------------------------------------------------------

MATOMO_STEPS: List[Tuple[str, str, List[str]]] = [  # (step, Reporting API method, dimensions it writes)
    ("pages", "Actions.getPageUrls", ["page", "entry", "exit"]),
    ("referrers", "Referrers.getAll", ["source", "medium", "campaign", "channel", "referrer"]),
    ("country", "UserCountry.getCountry", ["country"]),
    ("city", "UserCountry.getCity", ["city", "region"]),
    ("device", "DevicesDetection.getType", ["device"]),
    ("browser", "DevicesDetection.getBrowsers", ["browser"]),
    ("os", "DevicesDetection.getOsFamilies", ["os"]),
]
_MATOMO_TYPES = {"1": "direct", "2": "search", "3": "website", "6": "campaign", "7": "social",
                 "direct": "direct", "search": "search", "website": "website", "campaign": "campaign", "social": "social"}


class Matomo:
    """Matomo's Reporting API (a token with view access), one day at a time over a range."""

    def __init__(self, url: str, id_site: str, token: str) -> None:
        self.url, self.id_site, self.token = url.rstrip("/") + "/", id_site, token

    def call(self, method: str, start: str, end: str, **extra: str) -> Dict[str, Any]:
        from .searchengines import SearchEngineError, _json, http_request
        body = urlencode(dict({"module": "API", "method": method, "idSite": self.id_site, "period": "day",
                               "date": "%s,%s" % (start, end), "format": "JSON", "filter_limit": "-1",
                               "token_auth": self.token}, **extra)).encode("ascii")
        try:
            status, raw = http_request("POST", self.url + "index.php", body,
                                       {"Content-Type": "application/x-www-form-urlencoded"})
        except SearchEngineError as exc:
            raise HistoryError("Matomo: %s" % exc, exc.kind)
        doc = _json(raw)
        if isinstance(doc, dict) and doc.get("result") == "error":
            msg = str(doc.get("message") or "")[:300]
            kind = "auth" if "token" in msg.lower() or "access" in msg.lower() else "config"
            raise HistoryError("Matomo refused: %s" % msg, kind)
        if status != 200 or not isinstance(doc, dict):
            raise HistoryError("Matomo answered HTTP %d%s" % (status, "" if status != 200 else " without JSON: is this "
                                                             "Matomo's address?"), "server" if status >= 500 else "config")
        return doc


def check_matomo(url: str, id_site: str, token: str) -> Dict[str, Any]:
    """The site's name, asked of Matomo: the address, the site id and the token work."""
    if not re.fullmatch(r"https?://[^\s/?#]+(/[^\s?#]*)?", url or ""):
        raise HistoryError("Matomo's address is like https://matomo.example.com/")
    if not re.fullmatch(r"\d{1,9}", id_site or ""):
        raise HistoryError("the site id is a number (Matomo: Administration → Websites → Manage)")
    if not re.fullmatch(r"[A-Za-z0-9]{16,128}", token or ""):
        raise HistoryError("this is not a Matomo token (Administration → Personal → Security → Auth tokens)")
    from .searchengines import SearchEngineError, _json, http_request
    body = urlencode({"module": "API", "method": "SitesManager.getSiteFromId", "idSite": id_site, "format": "JSON",
                      "token_auth": token}).encode("ascii")
    try:
        status, raw = http_request("POST", url.rstrip("/") + "/index.php", body,
                                   {"Content-Type": "application/x-www-form-urlencoded"})
    except SearchEngineError as exc:
        raise HistoryError("Matomo: %s" % exc, exc.kind)
    doc = _json(raw)
    if isinstance(doc, dict) and doc.get("result") == "error":
        raise HistoryError("Matomo refused: %s" % str(doc.get("message"))[:300], "auth")
    if status != 200 or not isinstance(doc, dict) or "name" not in doc:
        raise HistoryError("no Matomo answered at this address (HTTP %d)" % status)
    return {"name": str(doc.get("name"))[:200], "ts_created": str(doc.get("ts_created") or "")[:10]}


def _matomo_add(w: Writer, step: str, d: str, r: Dict[str, Any]) -> None:
    visitors = r.get("nb_uniq_visitors") or r.get("sum_daily_nb_uniq_visitors") or r.get("nb_visits")
    if step == "pages":
        path = norm("page", r.get("url") or r.get("label") or "")
        if path.endswith("/index"):
            path = path[:-5]
        w.add(d, "page", path, visitors, r.get("nb_visits"), r.get("nb_hits"))
        if _num(r.get("entry_nb_visits")):
            w.add(d, "entry", path, r.get("entry_nb_uniq_visitors") or r.get("entry_nb_visits"), r.get("entry_nb_visits"),
                  r.get("entry_nb_actions"), r.get("entry_bounce_count"), r.get("entry_sum_visit_length"))
        if _num(r.get("exit_nb_visits")):
            w.add(d, "exit", path, r.get("exit_nb_uniq_visitors") or r.get("exit_nb_visits"), r.get("exit_nb_visits"))
        return
    metrics = (visitors, r.get("nb_visits"), r.get("nb_actions"), r.get("bounce_count"), r.get("sum_visit_length"))
    label = _text(r.get("label"))
    if step == "referrers":
        kind = _MATOMO_TYPES.get(str(r.get("referer_type", r.get("referrer_type", ""))).lower(), "")
        if kind == "direct":
            a = attribution()
        elif kind == "website":
            a = attribution("", label)
        elif kind == "campaign":  # Matomo keeps campaigns apart from their source
            from .enrich.sources import channel_group
            a = {"source": "(not set)", "medium": "(not set)", "campaign": label,
                 "channel": channel_group("(not set)", "(not set)", label, None), "referrer": ""}
        else:
            a = attribution(label)
        for dim in ("source", "medium", "campaign", "channel", "referrer"):
            if dim == "referrer" and not a["referrer"]:
                continue
            w.add(d, dim, a[dim], *metrics)
    elif step == "country":
        w.add(d, "country", norm("country", r.get("code") or ""), *metrics)
    elif step == "city":
        w.add(d, "city", _text(r.get("city_name") or label.split(",")[0]), *metrics)
        w.add(d, "region", _text(r.get("region_name") or ""), *metrics)
    else:
        w.add(d, step, norm(step, label), *metrics)


# ---------------------------------------------------------------------------
# Reports (api.py): what the imported days add
# ---------------------------------------------------------------------------

class Imported:
    """The imported days a report covers: before the property's first day of Tidesight data, within its period;
    none when filters, a goal or the bots-only traffic apply (imported days hold no visits)."""

    def __init__(self, cfg: Config, site_id: str, start: str, end: str, filtered: bool = False) -> None:
        self.cfg, self.site_id = cfg, site_id
        self.start, self.end = start, end
        self.conn: Optional[sqlite3.Connection] = None
        self.reason: Optional[str] = None
        first = first_tidesight_day(cfg.data_dir, site_id)
        if first:
            self.end = min(end, _shift(first, -1))
        if self.start > self.end:
            return
        conn = open_readonly(cfg.data_dir)
        if conn is None:
            return
        found = conn.execute("SELECT MIN(day), MAX(day) FROM imported WHERE site_id = ? AND dim = '' AND day BETWEEN ? AND ?",
                             (site_id, self.start, self.end)).fetchone()
        if not found[0]:
            conn.close()
            return
        if filtered:
            self.reason = "filters"
            conn.close()
            return
        self.conn = conn
        self.first, self.last = found[0], found[1]

    @property
    def active(self) -> bool:
        return self.conn is not None

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def info(self) -> Optional[Dict[str, Any]]:
        if self.reason:
            return {"applied": False, "reason": self.reason}
        if not self.active:
            return None
        assert self.conn is not None
        sources = [SOURCES.get(r[0], r[0]) for r in self.conn.execute(
            "SELECT DISTINCT j.source FROM imported i JOIN history_jobs j ON j.id = i.job_id WHERE i.site_id = ? "
            "AND i.dim = '' AND i.day BETWEEN ? AND ?", (self.site_id, self.first, self.last))]
        return {"applied": True, "from": self.first, "to": self.last, "sources": sources}

    def daily(self, dim: str = "", values: Optional[Sequence[str]] = None) -> Dict[Tuple[str, str], Dict[str, float]]:
        """{(day, value): figures}."""
        assert self.conn is not None
        sql = ("SELECT day, value, visitors, visits, pageviews, bounces, duration_s FROM imported WHERE site_id = ? "
               "AND dim = ? AND day BETWEEN ? AND ?")
        args: List[Any] = [self.site_id, dim, self.start, self.end]
        out: Dict[Tuple[str, str], Dict[str, float]] = {}
        chunks = [list(values)[i:i + 300] for i in range(0, len(values), 300)] if values is not None else [None]
        for chunk in chunks:
            extra, extra_args = "", []  # type: Tuple[str, List[Any]]
            if chunk is not None:
                if not chunk:
                    continue
                extra, extra_args = " AND value IN (%s)" % ",".join("?" * len(chunk)), list(chunk)
            for r in self.conn.execute(sql + extra, args + extra_args):
                out[(r["day"], r["value"])] = {m: r[m] for m in METRICS}
        return out

    def totals(self) -> Dict[str, float]:
        assert self.conn is not None
        r = self.conn.execute("SELECT SUM(visitors), SUM(visits), SUM(pageviews), SUM(bounces), SUM(duration_s) FROM imported "
                              "WHERE site_id = ? AND dim = '' AND day BETWEEN ? AND ?",
                              (self.site_id, self.start, self.end)).fetchone()
        return dict(zip(METRICS, (x or 0 for x in r)))

    def rows(self, dim: str, search: Optional[str] = None, values: Optional[Sequence[str]] = None) -> Dict[str, Dict[str, float]]:
        """{value: figures summed over the days}."""
        assert self.conn is not None
        sql = ("SELECT value, SUM(visitors) AS visitors, SUM(visits) AS visits, SUM(pageviews) AS pageviews, "
               "SUM(bounces) AS bounces, SUM(duration_s) AS duration_s FROM imported WHERE site_id = ? AND dim = ? "
               "AND day BETWEEN ? AND ?")
        args: List[Any] = [self.site_id, dim, self.start, self.end]
        if search:
            sql += " AND value LIKE ? ESCAPE '\\'"
            args.append("%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        out: Dict[str, Dict[str, float]] = {}
        if values is not None:
            vals = list(values)
            for i in range(0, len(vals), 300):
                part = vals[i:i + 300]
                for r in self.conn.execute(sql + " AND value IN (%s) GROUP BY value" % ",".join("?" * len(part)), args + part):
                    out[r["value"]] = {m: r[m] for m in METRICS}
            return out
        for r in self.conn.execute(sql + " GROUP BY value", args):
            out[r["value"]] = {m: r[m] for m in METRICS}
        return out


def add_totals(t: Dict[str, Any], imp: Dict[str, float]) -> Dict[str, Any]:
    """A period's totals (query.totals) with imported figures added."""
    sessions = (t.get("sessions") or 0) + int(imp["visits"])
    dur = (t.get("avg_duration") or 0) * (t.get("sessions") or 0) + imp["duration_s"] * 1000
    bounces = (t.get("bounces") or 0) + int(imp["bounces"])
    out = dict(t, visitors=(t.get("visitors") or 0) + int(imp["visitors"]), sessions=sessions,
               pageviews=(t.get("pageviews") or 0) + int(imp["pageviews"]), bounces=bounces)
    out["bounce_rate"] = round(bounces / sessions, 4) if sessions else None
    out["views_per_session"] = round(out["pageviews"] / sessions, 2) if sessions else None
    out["avg_duration"] = int(dur / sessions) if sessions else None
    return out


def bucket(day: str, interval: str) -> str:
    if interval == "week":
        d = date.fromisoformat(day)
        return (d - timedelta(days=d.weekday())).isoformat()
    if interval == "month":
        return day[:8] + "01"
    return day


def add_series(points: List[Dict[str, Any]], interval: str, daily: Dict[Tuple[str, str], Dict[str, float]]) -> None:
    """query.series' points with the imported days added in their buckets (hours: none)."""
    if interval == "hour":
        return
    per: Dict[str, Dict[str, float]] = {}
    for (day, _v), m in daily.items():
        acc = per.setdefault(bucket(day, interval), dict.fromkeys(METRICS, 0.0))
        for k in METRICS:
            acc[k] += m[k]
    for p in points:
        m = per.get(p["t"])
        if m:
            p.update(add_totals(p, m))


def add_rows(rows: List[Dict[str, Any]], scope: str, imported: Dict[str, Dict[str, float]]) -> List[Dict[str, Any]]:
    """A breakdown's rows (every one of them) with the imported rows added, by value."""
    by_value = {r["value"]: r for r in rows}
    out = list(rows)
    for value, m in imported.items():
        r = by_value.get(value)
        if r is None:
            r = {"value": value, "visitors": 0, "sessions": 0, "pageviews": 0}
            if scope == "session":
                r.update(views_per_session=None, bounce_rate=None, avg_duration=None, avg_engaged=None, new_visitors=None)
            out.append(r)
            by_value[value] = r
        if scope == "session":
            sessions0 = r.get("sessions") or 0
            bounces = (r.get("bounce_rate") or 0) * sessions0 + m["bounces"]
            dur = (r.get("avg_duration") or 0) * sessions0 + m["duration_s"] * 1000
            sessions = sessions0 + int(m["visits"])
            r.update(visitors=(r.get("visitors") or 0) + int(m["visitors"]), sessions=sessions,
                     pageviews=(r.get("pageviews") or 0) + int(m["pageviews"]))
            r["bounce_rate"] = round(bounces / sessions, 4) if sessions else None
            r["avg_duration"] = int(dur / sessions) if sessions else None
            r["views_per_session"] = round(r["pageviews"] / sessions, 2) if sessions else None
        else:
            r.update(visitors=(r.get("visitors") or 0) + int(m["visitors"]),
                     sessions=(r.get("sessions") or 0) + int(m["visits"]),
                     pageviews=(r.get("pageviews") or 0) + int(m["pageviews"]))
    return out


def sum_rows(rows: Iterable[Dict[str, Any]], scope: str, base: Dict[str, Any]) -> Dict[str, Any]:
    """Totals of merged rows (the counts summed, the rates weighted by visits)."""
    out = dict(base)
    rows = list(rows)
    for k in ("visitors", "sessions", "pageviews"):
        out[k] = sum(r.get(k) or 0 for r in rows)
    if scope == "session":
        s = out["sessions"]
        out["bounce_rate"] = round(sum((r.get("bounce_rate") or 0) * (r.get("sessions") or 0) for r in rows) / s, 4) if s else None
        out["avg_duration"] = int(sum((r.get("avg_duration") or 0) * (r.get("sessions") or 0) for r in rows) / s) if s else None
        out["views_per_session"] = round(out["pageviews"] / s, 2) if s else None
    return out
