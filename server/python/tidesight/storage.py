"""Raw storage for the collector (standard library only).

Two backends share one interface:

* SQLiteStore appends to events.db (raw_requests + raw_hits) and replay.db
  (chunks), one short transaction per request. The per-IP rate limit is
  applied inside the same transaction.
* SpoolStore appends one JSON line per request to a per-process file under
  <data_dir>/spool/. `load_spool()` (run from cron) imports the files into
  SQLite idempotently. It is the Phase 0 fallback, and also where the
  SQLite backend writes when the database is unavailable.

Schema changes are numbered migrations below, applied automatically and
idempotently (tracked with PRAGMA user_version). Document them in
docs/schema.md.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import socket
import sqlite3
import threading
import time
import zlib
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from .errorlog import ERRORS

log = logging.getLogger("tidesight.storage")

# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

EVENTS_MIGRATIONS: List[Tuple[int, List[str]]] = [
    (1, [
        """CREATE TABLE IF NOT EXISTS raw_requests (
            id            INTEGER PRIMARY KEY,
            received_at   INTEGER NOT NULL,
            site_id       TEXT,
            site_status   TEXT NOT NULL,
            endpoint      TEXT NOT NULL,
            method        TEXT NOT NULL,
            ip            TEXT,
            headers       TEXT NOT NULL,
            cookies       TEXT,
            query         TEXT,
            content_type  TEXT,
            body_bytes    INTEGER NOT NULL DEFAULT 0,
            body_encoding TEXT,
            envelope      TEXT,
            body          BLOB,
            error         TEXT,
            proto_v       INTEGER,
            event_count   INTEGER NOT NULL DEFAULT 0,
            server        TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS raw_requests_time ON raw_requests(received_at)",
        """CREATE TABLE IF NOT EXISTS raw_hits (
            id          INTEGER PRIMARY KEY,
            request_id  INTEGER NOT NULL,
            seq         INTEGER NOT NULL,
            received_at INTEGER NOT NULL,
            site_id     TEXT,
            site_ok     INTEGER NOT NULL,
            type        TEXT,
            name        TEXT,
            client_ts   INTEGER,
            event_id    TEXT,
            visitor_id  TEXT,
            session_id  TEXT,
            page_id     TEXT,
            user_id     TEXT,
            url         TEXT,
            data        TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS raw_hits_site_time ON raw_hits(site_id, received_at)",
        "CREATE INDEX IF NOT EXISTS raw_hits_request ON raw_hits(request_id)",
        """CREATE TABLE IF NOT EXISTS rate_limits (
            key    TEXT NOT NULL,
            minute INTEGER NOT NULL,
            hits   INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (key, minute)
        ) WITHOUT ROWID""",
        """CREATE TABLE IF NOT EXISTS counters (
            day     TEXT NOT NULL,
            site_id TEXT NOT NULL,
            name    TEXT NOT NULL,
            n       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, site_id, name)
        ) WITHOUT ROWID""",
        """CREATE TABLE IF NOT EXISTS spool_progress (
            path   TEXT PRIMARY KEY,
            offset INTEGER NOT NULL,
            done   INTEGER NOT NULL DEFAULT 0
        )""",
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    ]),
    # Privacy annotations: the geolocation derived before an IP was anonymised (JSON {provider: record}).
    (2, [
        "ALTER TABLE raw_requests ADD COLUMN geo TEXT",
    ]),
    # Old allowance minutes are deleted by minute: without an index, a flood's rows made that a full scan.
    (3, [
        "CREATE INDEX IF NOT EXISTS rate_limits_minute ON rate_limits(minute)",
    ]),
]

REPLAY_MIGRATIONS: List[Tuple[int, List[str]]] = [
    (1, [
        """CREATE TABLE IF NOT EXISTS chunks (
            id          INTEGER PRIMARY KEY,
            received_at INTEGER NOT NULL,
            site_id     TEXT,
            site_ok     INTEGER NOT NULL,
            kind        TEXT NOT NULL,
            visitor_id  TEXT,
            session_id  TEXT,
            page_id     TEXT,
            seq         INTEGER,
            client_ts   INTEGER,
            url         TEXT,
            ip          TEXT,
            meta        TEXT NOT NULL,
            encoding    TEXT NOT NULL,
            payload     BLOB NOT NULL,
            error       TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS chunks_session ON chunks(site_id, session_id, page_id, seq)",
        "CREATE INDEX IF NOT EXISTS chunks_time ON chunks(received_at)",
        """CREATE TABLE IF NOT EXISTS rate_limits (
            key    TEXT NOT NULL,
            minute INTEGER NOT NULL,
            hits   INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (key, minute)
        ) WITHOUT ROWID""",
        """CREATE TABLE IF NOT EXISTS spool_progress (
            path   TEXT PRIMARY KEY,
            offset INTEGER NOT NULL,
            done   INTEGER NOT NULL DEFAULT 0
        )""",
    ]),
    (2, [
        "CREATE INDEX IF NOT EXISTS rate_limits_minute ON rate_limits(minute)",
    ]),
]


def connect(path: str, journal_mode: str = "wal", synchronous: str = "normal",
            busy_timeout_ms: int = 10000, readonly: bool = False) -> sqlite3.Connection:
    """Open a SQLite connection in autocommit mode with Tidesight's pragmas."""
    if readonly:
        uri = "file:%s?mode=ro" % path.replace("\\", "/").replace("?", "%3f").replace("#", "%23")
        conn = sqlite3.connect(uri, uri=True, timeout=busy_timeout_ms / 1000.0, isolation_level=None)
    else:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(path, timeout=busy_timeout_ms / 1000.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=%d" % int(busy_timeout_ms))
    if not readonly:
        try:
            mode = _journal_mode(conn, "wal" if journal_mode == "wal" else "delete")
        except BaseException:
            conn.close()
            raise
        if mode != journal_mode:
            log.warning("journal_mode %s requested for %s, got %s", journal_mode, path, mode)
        conn.execute("PRAGMA synchronous=%s" % ("FULL" if synchronous == "full" else "NORMAL"))
    return conn


def _journal_mode(conn: sqlite3.Connection, wanted: str) -> str:
    """Switch to `wanted` unless the database is in that mode already. When several processes switch a new
    database at once, SQLite answers "database is locked" at once to all but one (it does not wait there, which
    could deadlock): they try again."""
    for attempt in range(100):
        try:
            mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if mode != wanted:
                mode = str(conn.execute("PRAGMA journal_mode=%s" % wanted.upper()).fetchone()[0]).lower()
            return mode
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 99:
                raise
            time.sleep(0.01 + random.random() * 0.02)
    return wanted  # not reached


_ADD_COLUMN_RE = re.compile(r"^ALTER TABLE (\w+) ADD COLUMN (\w+)", re.IGNORECASE)


def _column_exists(conn: sqlite3.Connection, sql: str) -> bool:
    """True for an ADD COLUMN whose column is already there: a database that lost its user_version (e.g. a
    Cloudflare D1 export) then migrates cleanly instead of failing on a duplicate column."""
    m = _ADD_COLUMN_RE.match(sql.strip())
    if not m:
        return False
    return any(row[1] == m.group(2) for row in conn.execute("PRAGMA table_info(%s)" % m.group(1)))


def migrate(conn: sqlite3.Connection, migrations: Sequence[Tuple[int, List[str]]]) -> int:
    """Apply pending migrations; safe to call concurrently from several processes."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    target = migrations[-1][0] if migrations else 0
    if current >= target:
        return current
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = conn.execute("PRAGMA user_version").fetchone()[0]  # re-check under the lock
        for version, statements in migrations:
            if version <= current:
                continue
            for sql in statements:
                if not _column_exists(conn, sql):
                    conn.execute(sql)
            conn.execute("PRAGMA user_version=%d" % version)
            current = version
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return current


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def _json(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class HitRecord:
    """One event of a batch, with the typed columns extracted at ingest."""

    __slots__ = ("seq", "type", "name", "client_ts", "event_id", "visitor_id", "session_id",
                 "page_id", "user_id", "url", "data")

    def __init__(self, seq: int, data: str, type: Optional[str] = None, name: Optional[str] = None,
                 client_ts: Optional[int] = None, event_id: Optional[str] = None,
                 visitor_id: Optional[str] = None, session_id: Optional[str] = None,
                 page_id: Optional[str] = None, user_id: Optional[str] = None,
                 url: Optional[str] = None) -> None:
        self.seq = seq
        self.data = data
        self.type = type
        self.name = name
        self.client_ts = client_ts
        self.event_id = event_id
        self.visitor_id = visitor_id
        self.session_id = session_id
        self.page_id = page_id
        self.user_id = user_id
        self.url = url

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HitRecord":
        return cls(**{k: d.get(k) for k in cls.__slots__})


class RequestRecord:
    """Everything stored about one HTTP request to /collect or /p.gif."""

    def __init__(self, received_at: int, endpoint: str, method: str, site_id: Optional[str],
                 site_status: str, ip: Optional[str], headers: Dict[str, str],
                 cookies: Optional[Dict[str, Any]] = None, query: Optional[str] = None,
                 content_type: Optional[str] = None, body_bytes: int = 0,
                 body_encoding: Optional[str] = None, envelope: Optional[Dict[str, Any]] = None,
                 body: Optional[bytes] = None, error: Optional[str] = None,
                 proto_v: Optional[int] = None, server: Optional[Dict[str, Any]] = None,
                 hits: Optional[List[HitRecord]] = None) -> None:
        self.received_at = received_at
        self.endpoint = endpoint
        self.method = method
        self.site_id = site_id
        self.site_status = site_status
        self.ip = ip
        self.headers = headers
        self.cookies = cookies
        self.query = query
        self.content_type = content_type
        self.body_bytes = body_bytes
        self.body_encoding = body_encoding
        self.envelope = envelope
        self.body = body
        self.error = error
        self.proto_v = proto_v
        self.server = server
        self.hits = hits or []

    @property
    def site_ok(self) -> int:
        return 1 if self.site_status == "ok" else 0

    def to_dict(self) -> Dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k not in ("hits", "body")}
        d["body"] = base64.b64encode(self.body).decode("ascii") if self.body is not None else None
        d["hits"] = [h.to_dict() for h in self.hits]
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RequestRecord":
        body = d.get("body")
        return cls(
            received_at=d["received_at"], endpoint=d["endpoint"], method=d["method"],
            site_id=d.get("site_id"), site_status=d["site_status"], ip=d.get("ip"),
            headers=d.get("headers") or {}, cookies=d.get("cookies"), query=d.get("query"),
            content_type=d.get("content_type"), body_bytes=d.get("body_bytes") or 0,
            body_encoding=d.get("body_encoding"), envelope=d.get("envelope"),
            body=base64.b64decode(body) if body is not None else None, error=d.get("error"),
            proto_v=d.get("proto_v"), server=d.get("server"),
            hits=[HitRecord.from_dict(h) for h in d.get("hits") or []])


class ChunkRecord:
    """One heavy-data chunk for replay.db (stored without parsing the payload)."""

    def __init__(self, received_at: int, kind: str, site_id: Optional[str], site_status: str,
                 payload: bytes, encoding: str, meta: Dict[str, Any], visitor_id: Optional[str] = None,
                 session_id: Optional[str] = None, page_id: Optional[str] = None,
                 seq: Optional[int] = None, client_ts: Optional[int] = None, url: Optional[str] = None,
                 ip: Optional[str] = None, error: Optional[str] = None) -> None:
        self.received_at = received_at
        self.kind = kind
        self.site_id = site_id
        self.site_status = site_status
        self.payload = payload
        self.encoding = encoding
        self.meta = meta
        self.visitor_id = visitor_id
        self.session_id = session_id
        self.page_id = page_id
        self.seq = seq
        self.client_ts = client_ts
        self.url = url
        self.ip = ip
        self.error = error

    @property
    def site_ok(self) -> int:
        return 1 if self.site_status == "ok" else 0

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["payload"] = base64.b64encode(self.payload).decode("ascii")
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ChunkRecord":
        d = dict(d)
        d["payload"] = base64.b64decode(d["payload"])
        return cls(**d)


# Outcomes of a write.
STORED = "stored"
RATE_LIMITED = "rate_limited"
SPOOLED = "spooled"


class StorageError(RuntimeError):
    """The record could not be stored anywhere."""


class Budget(NamedTuple):
    """A per-minute allowance, counted in the rate_limits table in the same transaction as the write."""

    key: str        # "<client>" (requests), "kb:<client>" (kilobytes), "f:<client>" and "f:*" (flagged requests)
    amount: int     # what this request consumes: 1, or its size in KB
    limit: int      # per clock minute; 0 = unlimited
    counter: str    # the counters name recorded when the allowance is exceeded


class Limits(NamedTuple):
    """Everything a write must fit in. The first budget exceeded refuses the record (RATE_LIMITED)."""

    budgets: Tuple[Budget, ...] = ()
    site: str = ""          # counters site_id: a configured site, or "" (never an id taken from the request)
    quota: int = 0          # the site's stored hits per UTC day; 0 = unlimited
    hits: int = 0           # hits this request adds (counted per site and day as the counter "hits")


NO_LIMITS = Limits()


def is_flagged(rec: "RequestRecord") -> bool:
    """A request stored only as a flagged record: wrong or unknown site or origin, unreadable, no events or too
    many. A batch with a few invalid events or a bad version still holds good hits and is not flagged."""
    if rec.site_status != "ok":
        return True
    if rec.endpoint == "pixel":
        return False
    if rec.envelope is None:
        return True
    return any(e == "no_events" or e.startswith("too_many_events") for e in (rec.error or "").split(",") if e)


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------

def _day(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000.0))


class SQLiteStore:
    """Appends raw records to events.db and replay.db."""

    name = "sqlite"

    def __init__(self, data_dir: str, journal_mode: str = "wal", synchronous: str = "normal",
                 busy_timeout_ms: int = 10000) -> None:
        self.data_dir = data_dir
        self.events_path = os.path.join(data_dir, "events.db")
        self.replay_path = os.path.join(data_dir, "replay.db")
        self.journal_mode = journal_mode
        self.synchronous = synchronous
        self.busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()
        self._last_prune = 0.0

    # Connections are per thread and per process (a forked child reconnects).
    def _conn(self, which: str) -> sqlite3.Connection:
        key = (which, os.getpid())
        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = self._local.conns = {}
        conn = conns.get(key)
        if conn is None:
            path = self.events_path if which == "events" else self.replay_path
            conn = connect(path, self.journal_mode, self.synchronous, self.busy_timeout_ms)
            migrate(conn, EVENTS_MIGRATIONS if which == "events" else REPLAY_MIGRATIONS)
            conns[key] = conn
        return conn

    def events_conn(self) -> sqlite3.Connection:
        return self._conn("events")

    def replay_conn(self) -> sqlite3.Connection:
        return self._conn("replay")

    def close(self) -> None:
        conns = getattr(self._local, "conns", None) or {}
        for conn in conns.values():
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local.conns = {}

    def _discard(self, which: str) -> None:
        conns = getattr(self._local, "conns", None) or {}
        conn = conns.pop((which, os.getpid()), None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def _over_budget(self, conn: sqlite3.Connection, limits: Limits, now_ms: int) -> Optional[str]:
        """Count this request against its budgets; the counter name of the first one exceeded, else None."""
        minute = now_ms // 60000
        refused = None
        for b in limits.budgets:
            if not b.key or b.limit <= 0:
                continue
            conn.execute("INSERT OR IGNORE INTO rate_limits(key, minute, hits) VALUES (?, ?, 0)", (b.key, minute))
            conn.execute("UPDATE rate_limits SET hits = hits + ? WHERE key = ? AND minute = ?",
                         (b.amount, b.key, minute))
            used = conn.execute("SELECT hits FROM rate_limits WHERE key = ? AND minute = ?",
                                (b.key, minute)).fetchone()[0]
            if used > b.limit:
                refused = b.counter
                break
        now = time.monotonic()
        if now - self._last_prune > 60:  # old minutes, at most once a minute per process, a bounded batch at a time
            self._last_prune = now
            conn.execute("SAVEPOINT prune")
            try:
                conn.execute("DELETE FROM rate_limits WHERE (key, minute) IN (SELECT key, minute FROM rate_limits "
                             "WHERE minute < ? LIMIT 20000)", (minute - 60,))
            except sqlite3.OperationalError:  # SQLite before 3.15 has no row values: the oldest minute instead
                conn.execute("ROLLBACK TO prune")
                conn.execute("DELETE FROM rate_limits WHERE minute = (SELECT MIN(minute) FROM rate_limits "
                             "WHERE minute < ?)", (minute - 60,))
            conn.execute("RELEASE prune")
        return refused

    @staticmethod
    def _over_quota(conn: sqlite3.Connection, limits: Limits, day: str) -> bool:
        if limits.quota <= 0 or limits.hits <= 0:
            return False
        row = conn.execute("SELECT n FROM counters WHERE day = ? AND site_id = ? AND name = 'hits'",
                           (day, limits.site)).fetchone()
        return (row[0] if row else 0) + limits.hits > limits.quota

    @staticmethod
    def count(conn: sqlite3.Connection, day: str, site_id: Optional[str], name: str, n: int = 1) -> None:
        conn.execute("INSERT OR IGNORE INTO counters(day, site_id, name, n) VALUES (?, ?, ?, 0)",
                     (day, site_id or "", name))
        conn.execute("UPDATE counters SET n = n + ? WHERE day = ? AND site_id = ? AND name = ?",
                     (n, day, site_id or "", name))

    def write_request(self, rec: RequestRecord, limits: Limits = NO_LIMITS) -> str:
        conn = self.events_conn()
        day = _day(rec.received_at)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                refused = self._over_budget(conn, limits, rec.received_at)
                if refused is None and self._over_quota(conn, limits, day):
                    refused = "over_quota"
                if refused is not None:
                    self.count(conn, day, limits.site, refused)
                    conn.execute("COMMIT")
                    return RATE_LIMITED
                request_id = self._insert_request(conn, rec)
                self._insert_hits(conn, request_id, rec)
                if limits.site and limits.hits > 0:
                    self.count(conn, day, limits.site, "hits", limits.hits)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        except sqlite3.Error:
            self._discard("events")
            raise
        return STORED

    @staticmethod
    def _insert_request(conn: sqlite3.Connection, rec: RequestRecord) -> int:
        cur = conn.execute(
            "INSERT INTO raw_requests(received_at, site_id, site_status, endpoint, method, ip, headers, "
            "cookies, query, content_type, body_bytes, body_encoding, envelope, body, error, proto_v, "
            "event_count, server) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rec.received_at, rec.site_id, rec.site_status, rec.endpoint, rec.method, rec.ip,
             _json(rec.headers) or "{}", _json(rec.cookies), rec.query, rec.content_type, rec.body_bytes,
             rec.body_encoding, _json(rec.envelope),
             sqlite3.Binary(rec.body) if rec.body is not None else None, rec.error, rec.proto_v,
             len(rec.hits), _json(rec.server)))
        return int(cur.lastrowid)

    @staticmethod
    def _insert_hits(conn: sqlite3.Connection, request_id: int, rec: RequestRecord) -> None:
        if not rec.hits:
            return
        site_ok = rec.site_ok
        conn.executemany(
            "INSERT INTO raw_hits(request_id, seq, received_at, site_id, site_ok, type, name, client_ts, "
            "event_id, visitor_id, session_id, page_id, user_id, url, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(request_id, h.seq, rec.received_at, rec.site_id, site_ok, h.type, h.name, h.client_ts,
              h.event_id, h.visitor_id, h.session_id, h.page_id, h.user_id, h.url, h.data)
             for h in rec.hits])

    def write_chunk(self, rec: ChunkRecord, limits: Limits = NO_LIMITS) -> str:
        conn = self.replay_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                refused = self._over_budget(conn, limits, rec.received_at)
                if refused is None:
                    self._insert_chunk(conn, rec)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        except sqlite3.Error:
            self._discard("replay")
            raise
        if refused is not None:  # counters live in events.db: a separate, best-effort write
            try:
                self.record_counter(_day(rec.received_at), limits.site, refused)
            except sqlite3.Error:
                ERRORS.error("could not count a refused chunk", logger=log)
            return RATE_LIMITED
        return STORED

    @staticmethod
    def _insert_chunk(conn: sqlite3.Connection, rec: ChunkRecord) -> None:
        conn.execute(
            "INSERT INTO chunks(received_at, site_id, site_ok, kind, visitor_id, session_id, page_id, seq, "
            "client_ts, url, ip, meta, encoding, payload, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rec.received_at, rec.site_id, rec.site_ok, rec.kind, rec.visitor_id, rec.session_id,
             rec.page_id, rec.seq, rec.client_ts, rec.url, rec.ip, _json(rec.meta) or "{}", rec.encoding,
             sqlite3.Binary(rec.payload), rec.error))

    def usage(self, stream: str, keys: Sequence[str], now_ms: int) -> Dict[str, int]:
        """What the current minute has used of these allowance keys, in the stream's database (events or
        chunks). Read only: no lock is taken."""
        which = "events" if stream == "events" else "replay"
        if not keys:
            return {}
        try:
            return {k: n for k, n in self._conn(which).execute(
                "SELECT key, hits FROM rate_limits WHERE minute = ? AND key IN (%s)" % ",".join("?" * len(keys)),
                [now_ms // 60000] + list(keys))}
        except sqlite3.Error:
            self._discard(which)
            raise

    def refuse(self, stream: str, limits: Limits, now_ms: int) -> Optional[str]:
        """Charge a request refused before its body was parsed (its client was already over an allowance) and
        count the refusal. The counter name, or None if the allowance had just been renewed (a new minute)."""
        which = "events" if stream == "events" else "replay"
        conn = self._conn(which)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                refused = self._over_budget(conn, limits, now_ms)
                if refused is not None and which == "events":
                    self.count(conn, _day(now_ms), limits.site, refused)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        except sqlite3.Error:
            self._discard(which)
            raise
        if refused is not None and which == "replay":  # counters live in events.db
            try:
                self.record_counter(_day(now_ms), limits.site, refused)
            except sqlite3.Error:
                ERRORS.error("could not count a refused chunk", logger=log)
        return refused

    def record_counter(self, day: str, site_id: Optional[str], name: str, n: int = 1,
                       limits: Limits = NO_LIMITS) -> None:
        """Count something; with budgets, only while they hold (the budgets are consumed either way)."""
        conn = self.events_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if self._over_budget(conn, limits, int(time.time() * 1000)) is None:
                    self.count(conn, day, site_id, name, n)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        except sqlite3.Error:
            self._discard("events")
            raise

    def healthy(self) -> bool:
        try:
            self.events_conn().execute("SELECT 1 FROM raw_requests LIMIT 1").fetchall()
            return os.access(self.data_dir, os.W_OK)
        except (sqlite3.Error, OSError):
            self._discard("events")
            return False


# ---------------------------------------------------------------------------
# Spool backend
# ---------------------------------------------------------------------------

class SpoolStore:
    """Appends one JSON line per record to a per-process file (no locking needed).

    Layout: <data_dir>/spool/events/<YYYY-MM-DD>/<host>-<pid>.jsonl and
    <data_dir>/spool/chunks/<YYYY-MM-DD>/<host>-<pid>.jsonl. Rate limiting is
    approximate (per process, in memory) in this mode.
    """

    name = "spool"

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.root = os.path.join(data_dir, "spool")
        self._host = socket.gethostname().split(".")[0] or "host"
        self._lock = threading.Lock()
        self._fds: Dict[Tuple[str, str, int], int] = {}
        self._rate: Dict[int, Dict[Tuple[str, str], int]] = {}  # minute -> (stream, budget key) -> amount used
        self._quota: Dict[Tuple[str, str], int] = {}
        self._refusals: Dict[Tuple[str, str, str], int] = {}  # (day, site, counter) -> refusals not yet written
        self._refusals_at = 0.0

    def _fd(self, stream: str, ms: int) -> int:
        day = _day(ms)
        key = (stream, day, os.getpid())
        fd = self._fds.get(key)
        if fd is None:
            directory = os.path.join(self.root, stream, day)
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, "%s-%d.jsonl" % (self._host, os.getpid()))
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            for old in [k for k in self._fds if k[0] == stream and k != key]:
                try:
                    os.close(self._fds.pop(old))
                except OSError:
                    pass
            self._fds[key] = fd
        return fd

    def _append(self, stream: str, ms: int, obj: Dict[str, Any]) -> None:
        line = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            fd = self._fd(stream, ms)
            view = memoryview(line)
            while view:
                written = os.write(fd, view)
                view = view[written:]

    def _over_budget(self, limits: Limits, now_ms: int, stream: str,
                     quota_day: Optional[str] = None) -> Optional[str]:
        """As SQLiteStore._over_budget, in memory: per process, so only approximate with several processes. The
        allowances of each stream are separate, as those of events.db and replay.db are."""
        minute = now_ms // 60000
        with self._lock:
            if len(self._rate) > 1 or minute not in self._rate:  # only the current minute is ever needed
                self._rate = {m: v for m, v in self._rate.items() if m >= minute}
            used = self._rate.setdefault(minute, {})
            for b in limits.budgets:
                if not b.key or b.limit <= 0:
                    continue
                k = (stream, b.key)
                n = used.get(k, 0) + b.amount
                used[k] = n
                if n > b.limit:
                    return b.counter
            if quota_day and limits.quota > 0 and limits.hits > 0:
                key = (limits.site, quota_day)
                if self._quota.get(key, 0) + limits.hits > limits.quota:
                    return "over_quota"
                if len(self._quota) > 10000:
                    self._quota = {k: v for k, v in self._quota.items() if k[1] == quota_day}
                self._quota[key] = self._quota.get(key, 0) + limits.hits
        return None

    def _refused(self, day: str, site: str, name: str) -> None:
        """Count a refusal. Counts are written at most once a minute per process (summed), so a refused flood
        does not grow the spool line by line."""
        with self._lock:
            key = (day, site, name)
            self._refusals[key] = self._refusals.get(key, 0) + 1
            due = time.monotonic() - self._refusals_at >= 60 or len(self._refusals) > 1000
        if due:
            self._flush_refusals()

    def _flush_refusals(self) -> None:
        with self._lock:
            pending, self._refusals = self._refusals, {}
            self._refusals_at = time.monotonic()
        for (day, site, name), n in pending.items():
            self._append("events", int(time.time() * 1000),
                         {"k": "counter", "day": day, "site_id": site, "name": name, "n": n})

    def write_request(self, rec: RequestRecord, limits: Limits = NO_LIMITS) -> str:
        day = _day(rec.received_at)
        refused = self._over_budget(limits, rec.received_at, "events", day)
        if refused is not None:
            self._refused(day, limits.site, refused)
            return RATE_LIMITED
        self._append("events", rec.received_at, {"k": "request", "rec": rec.to_dict()})
        return STORED

    def write_chunk(self, rec: ChunkRecord, limits: Limits = NO_LIMITS) -> str:
        refused = self._over_budget(limits, rec.received_at, "chunks")
        if refused is not None:
            self._refused(_day(rec.received_at), limits.site, refused)
            return RATE_LIMITED
        self._append("chunks", rec.received_at, {"k": "chunk", "rec": rec.to_dict()})
        return STORED

    def usage(self, stream: str, keys: Sequence[str], now_ms: int) -> Dict[str, int]:
        with self._lock:
            used = self._rate.get(now_ms // 60000, {})
            return {k: used[(stream, k)] for k in keys if (stream, k) in used}

    def refuse(self, stream: str, limits: Limits, now_ms: int) -> Optional[str]:
        refused = self._over_budget(limits, now_ms, stream)
        if refused is not None:
            self._refused(_day(now_ms), limits.site, refused)
        return refused

    def record_counter(self, day: str, site_id: Optional[str], name: str, n: int = 1,
                       limits: Limits = NO_LIMITS) -> None:
        if self._over_budget(limits, int(time.time() * 1000), "events") is not None:
            return
        self._append("events", int(time.time() * 1000),
                     {"k": "counter", "day": day, "site_id": site_id, "name": name, "n": n})

    def healthy(self) -> bool:
        try:
            os.makedirs(self.root, exist_ok=True)
            return os.access(self.root, os.W_OK)
        except OSError:
            return False

    def close(self) -> None:
        try:
            self._flush_refusals()
        except OSError:
            pass
        with self._lock:
            for fd in self._fds.values():
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._fds = {}


class FallbackStore:
    """SQLite first; on a database error, append to the spool instead (never lose a hit)."""

    BREAK_SECONDS = 30  # after SQLite failed, the spool alone for this long: no request waits for a lock again

    def __init__(self, primary: SQLiteStore, spool: SpoolStore) -> None:
        self.primary = primary
        self.spool = spool
        self.name = primary.name
        self._down_until = 0.0

    def _try(self, method: str, *args: Any) -> Tuple[bool, Any, Optional[BaseException]]:
        """(True, result) from SQLite, or (False, None, error) when it failed or recently failed (then the
        caller uses the spool, without waiting for a busy database on every request)."""
        if time.monotonic() < self._down_until:
            return False, None, None
        try:
            return True, getattr(self.primary, method)(*args), None
        except (sqlite3.Error, OSError) as exc:
            self._down_until = time.monotonic() + self.BREAK_SECONDS
            ERRORS.error("SQLite failed (%s); using the spool for %d s", exc, self.BREAK_SECONDS, logger=log)
            return False, None, exc

    def _write(self, method: str, *args: Any) -> str:
        ok, outcome, exc = self._try(method, *args)
        if ok:
            return outcome
        try:
            outcome = getattr(self.spool, method)(*args)
            return SPOOLED if outcome == STORED else outcome  # RATE_LIMITED: only a counter was written
        except OSError as exc2:
            raise StorageError("could not store the record: %s / %s" % (exc or "SQLite unavailable", exc2))

    def write_request(self, rec: RequestRecord, limits: Limits = NO_LIMITS) -> str:
        return self._write("write_request", rec, limits)

    def write_chunk(self, rec: ChunkRecord, limits: Limits = NO_LIMITS) -> str:
        return self._write("write_chunk", rec, limits)

    def record_counter(self, day: str, site_id: Optional[str], name: str, n: int = 1,
                       limits: Limits = NO_LIMITS) -> None:
        if not self._try("record_counter", day, site_id, name, n, limits)[0]:
            self.spool.record_counter(day, site_id, name, n, limits)

    def usage(self, stream: str, keys: Sequence[str], now_ms: int) -> Dict[str, int]:
        ok, used, _exc = self._try("usage", stream, keys, now_ms)
        return used if ok else self.spool.usage(stream, keys, now_ms)

    def refuse(self, stream: str, limits: Limits, now_ms: int) -> Optional[str]:
        ok, refused, _exc = self._try("refuse", stream, limits, now_ms)
        return refused if ok else self.spool.refuse(stream, limits, now_ms)

    def healthy(self) -> bool:
        return self.primary.healthy() or self.spool.healthy()

    def close(self) -> None:
        self.primary.close()
        self.spool.close()


def open_store(data_dir: str, storage_mode: str = "sqlite", journal_mode: str = "wal",
               synchronous: str = "normal", busy_timeout_ms: int = 10000, spool_fallback: bool = True):
    """Create the store described by the [server] config."""
    if storage_mode == "spool":
        return SpoolStore(data_dir)
    primary = SQLiteStore(data_dir, journal_mode, synchronous, busy_timeout_ms)
    if spool_fallback:
        return FallbackStore(primary, SpoolStore(data_dir))
    return primary


# ---------------------------------------------------------------------------
# Spool loader (run from cron: `jobs.py load-spool`)
# ---------------------------------------------------------------------------

def _spool_files(root: str, stream: str) -> List[str]:
    base = os.path.join(root, stream)
    out = []
    if not os.path.isdir(base):
        return out
    for day in sorted(os.listdir(base)):
        day_dir = os.path.join(base, day)
        if not os.path.isdir(day_dir):
            continue
        for name in sorted(os.listdir(day_dir)):
            if name.endswith(".jsonl"):
                out.append(os.path.join(day_dir, name))
    return out


def _read_complete_lines(path: str, offset: int, max_bytes: int) -> Tuple[List[bytes], int]:
    """Lines from offset that end with a newline (a line still being written is left for later)."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read(max_bytes)
    end = data.rfind(b"\n")
    if end < 0:
        if len(data) == max_bytes:  # a single line larger than max_bytes: read it whole
            with open(path, "rb") as fh:
                fh.seek(offset)
                line = fh.readline()
            if line.endswith(b"\n"):
                return [line[:-1]], offset + len(line)
        return [], offset
    chunk = data[:end + 1]
    return chunk.split(b"\n")[:-1], offset + len(chunk)


def load_spool(data_dir: str, journal_mode: str = "wal", synchronous: str = "normal",
               busy_timeout_ms: int = 10000, batch_bytes: int = 4 * 1024 * 1024,
               delete_after_days: int = 2, now_ms: Optional[int] = None) -> Dict[str, int]:
    """Import spool files into SQLite. Idempotent: progress is committed with the rows."""
    store = SQLiteStore(data_dir, journal_mode, synchronous, busy_timeout_ms)
    root = os.path.join(data_dir, "spool")
    stats = {"requests": 0, "chunks": 0, "counters": 0, "corrupt": 0, "files_done": 0, "files_deleted": 0}
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    yesterday = _day(now_ms - 86400000)  # files older than this can no longer grow
    cutoff = _day(now_ms - delete_after_days * 86400000)
    for stream, conn in (("events", store.events_conn()), ("chunks", store.replay_conn())):
        for path in _spool_files(root, stream):
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            day = rel.split("/")[1]
            row = conn.execute("SELECT offset, done FROM spool_progress WHERE path = ?", (rel,)).fetchone()
            offset, done = (row[0], row[1]) if row else (0, 0)
            if not done:
                while True:
                    lines, new_offset = _read_complete_lines(path, offset, batch_bytes)
                    if not lines:
                        break
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        for raw in lines:
                            _load_line(conn, store, stream, raw, stats)
                        conn.execute("INSERT OR REPLACE INTO spool_progress(path, offset, done) VALUES (?, ?, 0)",
                                     (rel, new_offset))
                        conn.execute("COMMIT")
                    except BaseException:
                        conn.execute("ROLLBACK")
                        raise
                    offset = new_offset
                if day < yesterday and offset >= os.path.getsize(path):
                    conn.execute("INSERT OR REPLACE INTO spool_progress(path, offset, done) VALUES (?, ?, 1)",
                                 (rel, offset))
                    done = 1
                    stats["files_done"] += 1
            if done and day < cutoff:
                try:
                    os.remove(path)
                    stats["files_deleted"] += 1
                    try:
                        os.rmdir(os.path.dirname(path))
                    except OSError:
                        pass
                except OSError:
                    pass
    store.close()
    return stats


def _load_line(conn: sqlite3.Connection, store: SQLiteStore, stream: str, raw: bytes,
               stats: Dict[str, int]) -> None:
    if not raw.strip():
        return
    try:
        obj = json.loads(raw.decode("utf-8"))
        kind = obj.get("k")
        if stream == "events" and kind == "request":
            rec = RequestRecord.from_dict(obj["rec"])
            rid = store._insert_request(conn, rec)
            store._insert_hits(conn, rid, rec)
            if rec.hits and rec.site_id and not is_flagged(rec):  # as the collector counts in SQLite mode
                store.count(conn, _day(rec.received_at), rec.site_id, "hits", len(rec.hits))
            stats["requests"] += 1
            return
        if stream == "events" and kind == "counter":
            store.count(conn, obj["day"], obj.get("site_id"), obj["name"], int(obj.get("n", 1)))
            stats["counters"] += 1
            return
        if stream == "chunks" and kind == "chunk":
            store._insert_chunk(conn, ChunkRecord.from_dict(obj["rec"]))
            stats["chunks"] += 1
            return
        raise ValueError("unknown spool record kind %r" % kind)
    except Exception as exc:  # keep the line, flagged, rather than losing it
        stats["corrupt"] += 1
        now = int(time.time() * 1000)
        if stream == "events":
            store._insert_request(conn, RequestRecord(
                received_at=now, endpoint="spool", method="-", site_id=None, site_status="invalid",
                ip=None, headers={}, body=raw, body_bytes=len(raw), error="spool_corrupt: %s" % exc))
        else:
            store._insert_chunk(conn, ChunkRecord(
                received_at=now, kind="corrupt", site_id=None, site_status="invalid",
                payload=zlib.compress(raw), encoding="zlib", meta={}, error="spool_corrupt: %s" % exc))
