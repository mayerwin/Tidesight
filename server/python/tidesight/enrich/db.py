"""derived.db: everything the enrichment jobs compute from the raw data.

The file can be deleted at any time and rebuilt with `jobs.py enrich --rebuild`.
Tables are documented in docs/schema.md. Schema changes are numbered
migrations below (tracked with PRAGMA user_version).
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..storage import connect, migrate

DERIVED_MIGRATIONS: List[Tuple[int, List[str]]] = [
    (1, [
        """CREATE TABLE IF NOT EXISTS job_state (
            job        TEXT PRIMARY KEY,
            version    TEXT,
            last_id    INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER,
            info       TEXT
        )""",
        # Lookups shared by many hits.
        """CREATE TABLE IF NOT EXISTS ua (
            id              INTEGER PRIMARY KEY,
            key             TEXT NOT NULL UNIQUE,
            ua              TEXT,
            hints           TEXT,
            browser         TEXT,
            browser_version TEXT,
            browser_major   TEXT,
            engine          TEXT,
            os              TEXT,
            os_version      TEXT,
            device_type     TEXT,
            device_brand    TEXT,
            device_model    TEXT,
            is_webview      INTEGER,
            in_app          TEXT,
            is_bot_ua       INTEGER,
            bot_name        TEXT,
            parser          TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS ip_geo (
            ip           TEXT NOT NULL,
            provider     TEXT NOT NULL,
            country      TEXT,
            country_name TEXT,
            continent    TEXT,
            is_eu        INTEGER,
            region       TEXT,
            region_name  TEXT,
            city         TEXT,
            postal       TEXT,
            lat          REAL,
            lon          REAL,
            accuracy_km  INTEGER,
            tz           TEXT,
            asn          INTEGER,
            as_org       TEXT,
            network      TEXT,
            PRIMARY KEY (ip, provider)
        ) WITHOUT ROWID""",
        # One row per raw request.
        """CREATE TABLE IF NOT EXISTS requests (
            request_id  INTEGER PRIMARY KEY,
            received_at INTEGER NOT NULL,
            site_id     TEXT,
            site_ok     INTEGER NOT NULL,
            endpoint    TEXT,
            client_ip   TEXT,
            ip_source   TEXT,
            ua_id       INTEGER,
            ck          TEXT,
            skew_ms     INTEGER,
            try         INTEGER,
            internal    INTEGER NOT NULL DEFAULT 0
        )""",
        # One row per raw hit, with what sessions are built from.
        """CREATE TABLE IF NOT EXISTS hits (
            hit_id      INTEGER PRIMARY KEY,
            request_id  INTEGER NOT NULL,
            site_id     TEXT,
            site_ok     INTEGER NOT NULL,
            session_key TEXT,
            page_key    TEXT,
            type        TEXT,
            name        TEXT,
            ts          INTEGER NOT NULL,
            received_at INTEGER NOT NULL,
            dup         INTEGER NOT NULL DEFAULT 0,
            vid         TEXT,
            uid         TEXT,
            url         TEXT,
            ns          INTEGER,
            sn          INTEGER,
            pn          INTEGER,
            ms          INTEGER,
            sd          INTEGER,
            fp          TEXT,
            extra       TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS hits_session ON hits(session_key, ts)",
        "CREATE INDEX IF NOT EXISTS hits_page ON hits(page_key)",
        "CREATE INDEX IF NOT EXISTS hits_site_time ON hits(site_id, ts)",
        """CREATE TABLE IF NOT EXISTS event_ids (
            site_id  TEXT NOT NULL,
            event_id TEXT NOT NULL,
            hit_id   INTEGER NOT NULL,
            PRIMARY KEY (site_id, event_id)
        ) WITHOUT ROWID""",
        """CREATE TABLE IF NOT EXISTS px_sessions (
            site_id     TEXT NOT NULL,
            ck          TEXT NOT NULL,
            session_key TEXT NOT NULL,
            last_ts     INTEGER NOT NULL,
            PRIMARY KEY (site_id, ck)
        ) WITHOUT ROWID""",
        # Sessions and pageviews, rebuilt from hits.
        """CREATE TABLE IF NOT EXISTS sessions (
            session_key     TEXT PRIMARY KEY,
            site_id         TEXT NOT NULL,
            site_ok         INTEGER NOT NULL,
            visitor_key     TEXT,
            vid             TEXT,
            uid             TEXT,
            started_at      INTEGER NOT NULL,
            ended_at        INTEGER NOT NULL,
            day             TEXT NOT NULL,
            hour            INTEGER,
            duration_ms     INTEGER NOT NULL,
            engaged_ms      INTEGER NOT NULL,
            pageviews       INTEGER NOT NULL,
            events          INTEGER NOT NULL,
            hits            INTEGER NOT NULL,
            engaged         INTEGER NOT NULL,
            bounce          INTEGER NOT NULL,
            is_new          INTEGER,
            session_number  INTEGER,
            entry_path      TEXT,
            entry_url       TEXT,
            entry_title     TEXT,
            exit_path       TEXT,
            source          TEXT,
            medium          TEXT,
            campaign        TEXT,
            term            TEXT,
            content         TEXT,
            channel         TEXT,
            referrer        TEXT,
            referrer_domain TEXT,
            source_category TEXT,
            click_id_type   TEXT,
            country         TEXT,
            region          TEXT,
            region_name     TEXT,
            city            TEXT,
            lat             REAL,
            lon             REAL,
            geo_tz          TEXT,
            asn             INTEGER,
            as_org          TEXT,
            is_hosting      INTEGER,
            browser         TEXT,
            browser_version TEXT,
            os              TEXT,
            os_version      TEXT,
            device_type     TEXT,
            device_brand    TEXT,
            device_model    TEXT,
            screen          TEXT,
            viewport        TEXT,
            language        TEXT,
            timezone        TEXT,
            is_bot          INTEGER NOT NULL DEFAULT 0,
            bot_kind        TEXT,
            bot_name        TEXT,
            bot_score       REAL,
            bot_reasons     TEXT,
            internal        INTEGER NOT NULL DEFAULT 0,
            client_ip       TEXT,
            ck              TEXT,
            fp              TEXT,
            has_js          INTEGER NOT NULL DEFAULT 0,
            has_replay      INTEGER NOT NULL DEFAULT 0,
            errors          INTEGER NOT NULL DEFAULT 0,
            rage_clicks     INTEGER NOT NULL DEFAULT 0,
            dead_clicks     INTEGER NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS sessions_site_day ON sessions(site_id, day)",
        "CREATE INDEX IF NOT EXISTS sessions_site_time ON sessions(site_id, started_at)",
        "CREATE INDEX IF NOT EXISTS sessions_visitor ON sessions(visitor_key)",
        "CREATE INDEX IF NOT EXISTS sessions_vid ON sessions(site_id, vid)",
        """CREATE TABLE IF NOT EXISTS pageviews (
            page_key     TEXT PRIMARY KEY,
            hit_id       INTEGER NOT NULL,
            site_id      TEXT NOT NULL,
            session_key  TEXT NOT NULL,
            ts           INTEGER NOT NULL,
            day          TEXT NOT NULL,
            hour         INTEGER,
            url          TEXT,
            host         TEXT,
            path         TEXT,
            query        TEXT,
            hash         TEXT,
            title        TEXT,
            referrer     TEXT,
            nav          TEXT,
            status       INTEGER,
            seq          INTEGER,
            engaged_ms   INTEGER NOT NULL DEFAULT 0,
            scroll_max   INTEGER,
            duration_ms  INTEGER,
            is_entry     INTEGER NOT NULL DEFAULT 0,
            is_exit      INTEGER NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS pageviews_site_day ON pageviews(site_id, day)",
        "CREATE INDEX IF NOT EXISTS pageviews_session ON pageviews(session_key)",
        # Typed event tables (one row per non-duplicate hit of that family).
        """CREATE TABLE IF NOT EXISTS events (
            hit_id INTEGER PRIMARY KEY, site_id TEXT, session_key TEXT, page_key TEXT, ts INTEGER, day TEXT,
            path TEXT, name TEXT, props TEXT, value REAL
        )""",
        "CREATE INDEX IF NOT EXISTS events_site_day ON events(site_id, day, name)",
        """CREATE TABLE IF NOT EXISTS clicks (
            hit_id INTEGER PRIMARY KEY, site_id TEXT, session_key TEXT, page_key TEXT, ts INTEGER, day TEXT,
            path TEXT, kind TEXT, x REAL, y REAL, vx REAL, vy REAL, pw INTEGER, ph INTEGER, vw INTEGER, vh INTEGER,
            ex REAL, ey REAL, ew REAL, eh REAL, sel TEXT, tag TEXT, txt TEXT, href TEXT, lk TEXT, n INTEGER, msg TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS clicks_site_path ON clicks(site_id, path, day)",
        """CREATE TABLE IF NOT EXISTS vitals (
            hit_id INTEGER PRIMARY KEY, site_id TEXT, session_key TEXT, page_key TEXT, ts INTEGER, day TEXT,
            path TEXT, metric TEXT, value REAL, rating TEXT, el TEXT, nav TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS vitals_site_day ON vitals(site_id, day, metric)",
        """CREATE TABLE IF NOT EXISTS perf (
            hit_id INTEGER PRIMARY KEY, site_id TEXT, session_key TEXT, page_key TEXT, ts INTEGER, day TEXT,
            path TEXT, data TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS perf_site_day ON perf(site_id, day)",
        """CREATE TABLE IF NOT EXISTS errors (
            hit_id INTEGER PRIMARY KEY, site_id TEXT, session_key TEXT, page_key TEXT, ts INTEGER, day TEXT,
            path TEXT, kind TEXT, msg TEXT, src TEXT, line INTEGER, col INTEGER, stack TEXT, tag TEXT, res TEXT,
            n INTEGER, grp TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS errors_site_day ON errors(site_id, day, grp)",
        """CREATE TABLE IF NOT EXISTS forms (
            hit_id INTEGER PRIMARY KEY, site_id TEXT, session_key TEXT, page_key TEXT, ts INTEGER, day TEXT,
            path TEXT, action TEXT, form TEXT, field TEXT, ftype TEXT, ms INTEGER, filled INTEGER, n INTEGER,
            last TEXT, why TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS forms_site_day ON forms(site_id, day, form)",
        """CREATE TABLE IF NOT EXISTS searches (
            hit_id INTEGER PRIMARY KEY, site_id TEXT, session_key TEXT, page_key TEXT, ts INTEGER, day TEXT,
            path TEXT, query TEXT, results INTEGER, category TEXT, origin TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS searches_site_day ON searches(site_id, day)",
        """CREATE TABLE IF NOT EXISTS misc (
            hit_id INTEGER PRIMARY KEY, site_id TEXT, session_key TEXT, page_key TEXT, ts INTEGER, day TEXT,
            path TEXT, type TEXT, data TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS misc_site_day ON misc(site_id, day, type)",
        # Identity stitching.
        """CREATE TABLE IF NOT EXISTS id_edges (
            site_id TEXT NOT NULL,
            a       TEXT NOT NULL,
            b       TEXT NOT NULL,
            kind    TEXT NOT NULL,
            PRIMARY KEY (site_id, a, b, kind)
        ) WITHOUT ROWID""",
        """CREATE TABLE IF NOT EXISTS vid_map (
            site_id     TEXT NOT NULL,
            vid         TEXT NOT NULL,
            visitor_key TEXT NOT NULL,
            confidence  REAL NOT NULL,
            first_seen  INTEGER,
            PRIMARY KEY (site_id, vid)
        ) WITHOUT ROWID""",
        """CREATE TABLE IF NOT EXISTS visitors (
            visitor_key TEXT PRIMARY KEY,
            site_id     TEXT NOT NULL,
            first_seen  INTEGER,
            last_seen   INTEGER,
            sessions    INTEGER,
            pageviews   INTEGER,
            engaged_ms  INTEGER,
            uid         TEXT,
            vids        TEXT,
            confidence  REAL,
            country     TEXT,
            device_type TEXT,
            browser     TEXT,
            os          TEXT,
            is_bot      INTEGER
        )""",
        "CREATE INDEX IF NOT EXISTS visitors_site ON visitors(site_id, last_seen)",
        # Rollups for long-range dashboards.
        """CREATE TABLE IF NOT EXISTS dirty_days (
            site_id TEXT NOT NULL,
            day     TEXT NOT NULL,
            PRIMARY KEY (site_id, day)
        ) WITHOUT ROWID""",
        """CREATE TABLE IF NOT EXISTS daily (
            site_id          TEXT NOT NULL,
            day              TEXT NOT NULL,
            visitors         INTEGER,
            new_visitors     INTEGER,
            sessions         INTEGER,
            pageviews        INTEGER,
            events           INTEGER,
            bounces          INTEGER,
            engaged_sessions INTEGER,
            duration_ms      INTEGER,
            engaged_ms       INTEGER,
            bots             INTEGER,
            PRIMARY KEY (site_id, day)
        ) WITHOUT ROWID""",
        """CREATE TABLE IF NOT EXISTS daily_dim (
            site_id    TEXT NOT NULL,
            day        TEXT NOT NULL,
            dim        TEXT NOT NULL,
            value      TEXT NOT NULL,
            visitors   INTEGER,
            sessions   INTEGER,
            pageviews  INTEGER,
            bounces    INTEGER,
            engaged_ms INTEGER,
            PRIMARY KEY (site_id, day, dim, value)
        ) WITHOUT ROWID""",
    ]),
    # Joins from typed tables to sessions (API filters) need session_key indexes.
    (2, [
        "CREATE INDEX IF NOT EXISTS events_session ON events(session_key)",
        "CREATE INDEX IF NOT EXISTS clicks_session ON clicks(session_key)",
        "CREATE INDEX IF NOT EXISTS vitals_session ON vitals(session_key)",
        "CREATE INDEX IF NOT EXISTS perf_session ON perf(session_key)",
        "CREATE INDEX IF NOT EXISTS errors_session ON errors(session_key)",
        "CREATE INDEX IF NOT EXISTS forms_session ON forms(session_key)",
        "CREATE INDEX IF NOT EXISTS searches_session ON searches(session_key)",
        "CREATE INDEX IF NOT EXISTS misc_session ON misc(session_key)",
        "CREATE INDEX IF NOT EXISTS pageviews_site_path ON pageviews(site_id, path, day)",
    ]),
]


def derived_path(data_dir: str) -> str:
    return os.path.join(data_dir, "derived.db")


def open_derived(path: str, journal_mode: str = "wal", busy_timeout_ms: int = 30000) -> sqlite3.Connection:
    conn = connect(path, journal_mode=journal_mode, synchronous="normal", busy_timeout_ms=busy_timeout_ms)
    migrate(conn, DERIVED_MIGRATIONS)
    conn.row_factory = sqlite3.Row
    return conn


def get_state(conn: sqlite3.Connection, job: str) -> Dict[str, Any]:
    row = conn.execute("SELECT * FROM job_state WHERE job = ?", (job,)).fetchone()
    if row is None:
        return {"job": job, "version": None, "last_id": 0, "info": {}}
    out = dict(row)
    out["info"] = json.loads(out["info"]) if out.get("info") else {}
    return out


def set_state(conn: sqlite3.Connection, job: str, version: Optional[str], last_id: int,
              info: Optional[Dict[str, Any]] = None) -> None:
    conn.execute("INSERT OR REPLACE INTO job_state(job, version, last_id, updated_at, info) VALUES (?, ?, ?, ?, ?)",
                 (job, version, last_id, int(time.time() * 1000), json.dumps(info or {})))


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


class JobLock:
    """An exclusive lock file so overlapping cron runs do not step on each other."""

    def __init__(self, data_dir: str, name: str = "jobs") -> None:
        self.path = os.path.join(data_dir, ".%s.lock" % name)
        self._fh = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self._fh.close()
            self._fh = None
            return False

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "JobLock":
        if not self.acquire():
            raise RuntimeError("another Tidesight job is running (lock: %s)" % self.path)
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()
