"""The dashboard's own small database, web.db in the data directory (standard library only).

Login sessions, the login throttle, saved segments, chart annotations and the queue of sites whose data is to be
deleted. It is kept apart from the analytics databases: deleting it only logs everyone out and forgets segments
and notes. Every call opens its own short connection, so it works the same under CGI (one process per request)
and Passenger.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from typing import Any, Dict, List, Optional

SESSION_DAYS = 30
MIGRATIONS = [
    (1, [
        """CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user       TEXT NOT NULL,
            pw_tag     TEXT NOT NULL,
            created    INTEGER NOT NULL,
            expires    INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS throttle (
            client   TEXT PRIMARY KEY,
            failures INTEGER NOT NULL,
            since    INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS segments (
            id         INTEGER PRIMARY KEY,
            site_id    TEXT,
            name       TEXT NOT NULL,
            filters    TEXT NOT NULL,
            created_by TEXT,
            created_at INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS annotations (
            id         INTEGER PRIMARY KEY,
            site_id    TEXT NOT NULL,
            day        TEXT NOT NULL,
            text       TEXT NOT NULL,
            created_by TEXT,
            created_at INTEGER NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS annotations_site_day ON annotations(site_id, day)",
    ]),
    (2, [
        """CREATE TABLE IF NOT EXISTS purges (
            site_id      TEXT PRIMARY KEY,
            requested_by TEXT,
            requested_at INTEGER NOT NULL
        )""",
    ]),
    (3, [
        """CREATE TABLE IF NOT EXISTS calls (
            client TEXT PRIMARY KEY,
            minute INTEGER NOT NULL,
            n      INTEGER NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS calls_minute ON calls(minute)",
    ]),
]


def _now() -> int:
    return int(time.time() * 1000)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class WebDB:
    def __init__(self, data_dir: str) -> None:
        self.path = os.path.join(data_dir, "web.db")

    def _conn(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for number, statements in MIGRATIONS:
            if number > version:
                conn.execute("BEGIN IMMEDIATE")
                for sql in statements:
                    conn.execute(sql)
                conn.execute("PRAGMA user_version = %d" % number)
                conn.execute("COMMIT")
        return conn

    # -- login sessions ----------------------------------------------------------

    def create_session(self, user: str, pw_tag: str, days: float = SESSION_DAYS) -> str:
        """A new login session; only a hash of its token is stored (a copy of web.db signs nobody in)."""
        token = secrets.token_urlsafe(32)
        now = _now()
        conn = self._conn()
        try:
            conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            if conn.execute("SELECT COUNT(*) FROM sessions WHERE user = ?", (user,)).fetchone()[0] >= 100:
                conn.execute("DELETE FROM sessions WHERE token_hash IN (SELECT token_hash FROM sessions WHERE user = ? "
                             "ORDER BY created LIMIT 10)", (user,))
            conn.execute("INSERT INTO sessions(token_hash, user, pw_tag, created, expires) VALUES (?, ?, ?, ?, ?)",
                         (_hash(token), user, pw_tag, now, now + int(days * 86400000)))
        finally:
            conn.close()
        return token

    def session_user(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        """{user, pw_tag} of a valid session token, else None."""
        if not token or len(token) > 200:
            return None
        conn = self._conn()
        try:
            row = conn.execute("SELECT user, pw_tag FROM sessions WHERE token_hash = ? AND expires > ?",
                               (_hash(token), _now())).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def delete_session(self, token: Optional[str]) -> None:
        if not token:
            return
        conn = self._conn()
        try:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))
        finally:
            conn.close()

    def delete_user_sessions(self, user: str) -> None:
        conn = self._conn()
        try:
            conn.execute("DELETE FROM sessions WHERE user = ?", (user,))
        finally:
            conn.close()

    # -- login throttle (shared by every process) -------------------------------------------

    def throttle_blocked(self, client: str, max_failures: int, window_s: float) -> bool:
        conn = self._conn()
        try:
            row = conn.execute("SELECT failures, since FROM throttle WHERE client = ?", (client,)).fetchone()
        finally:
            conn.close()
        return bool(row) and _now() - row["since"] <= window_s * 1000 and row["failures"] >= max_failures

    def throttle_failed(self, client: str, window_s: float) -> None:
        now = _now()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Bounded: old entries go, and a flood of identities cannot grow the table without end.
            conn.execute("DELETE FROM throttle WHERE since < ?", (now - int(window_s * 1000),))
            if conn.execute("SELECT COUNT(*) FROM throttle").fetchone()[0] > 20000:
                conn.execute("DELETE FROM throttle WHERE client IN (SELECT client FROM throttle ORDER BY since LIMIT 1000)")
            row = conn.execute("SELECT failures, since FROM throttle WHERE client = ?", (client,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO throttle(client, failures, since) VALUES (?, 1, ?)", (client, now))
            else:
                conn.execute("UPDATE throttle SET failures = failures + 1 WHERE client = ?", (client,))
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def throttle_reserve(self, client: str, max_failures: int, window_s: float) -> bool:
        """Count an attempt unless the client is at its limit, in one write transaction (atomic across processes)."""
        now = _now()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM throttle WHERE since < ?", (now - int(window_s * 1000),))
            if conn.execute("SELECT COUNT(*) FROM throttle").fetchone()[0] > 20000:
                conn.execute("DELETE FROM throttle WHERE client IN (SELECT client FROM throttle ORDER BY since LIMIT 1000)")
            row = conn.execute("SELECT failures FROM throttle WHERE client = ?", (client,)).fetchone()
            if row is not None and row["failures"] >= max_failures:
                conn.execute("COMMIT")
                return False
            if row is None:
                conn.execute("INSERT INTO throttle(client, failures, since) VALUES (?, 1, ?)", (client, now))
            else:
                conn.execute("UPDATE throttle SET failures = failures + 1 WHERE client = ?", (client,))
            conn.execute("COMMIT")
            return True
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def throttle_forgive(self, client: str) -> None:
        conn = self._conn()
        try:
            conn.execute("UPDATE throttle SET failures = failures - 1 WHERE client = ? AND failures > 0", (client,))
        finally:
            conn.close()

    def throttle_reset(self, client: str) -> None:
        conn = self._conn()
        try:
            conn.execute("DELETE FROM throttle WHERE client = ?", (client,))
        finally:
            conn.close()

    # -- public API calls per client and minute (CGI: no memory between requests) ----------------------

    def count_call(self, client: str, minute: int, limit: int) -> bool:
        """Count one call; False beyond `limit` this minute. Old minutes are deleted as they pass."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM calls WHERE minute < ?", (minute,))
            row = conn.execute("SELECT n FROM calls WHERE client = ?", (client,)).fetchone()
            n = (row["n"] if row else 0) + 1
            if row is None:
                if conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] >= 50000:
                    conn.execute("COMMIT")
                    return True  # bounded: a flood of identities is not tracked one by one
                conn.execute("INSERT INTO calls(client, minute, n) VALUES (?, ?, 1)", (client, minute))
            else:
                conn.execute("UPDATE calls SET n = ? WHERE client = ?", (n, client))
            conn.execute("COMMIT")
            return n <= limit
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # -- segments ------------------------------------------------------------------------

    def segments(self, site_id: Optional[str]) -> List[Dict[str, Any]]:
        conn = self._conn()
        try:
            rows = conn.execute("SELECT id, site_id, name, filters, created_by, created_at FROM segments "
                                "WHERE site_id IS NULL OR site_id = ? ORDER BY name COLLATE NOCASE", (site_id,)).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["filters"] = json.loads(item["filters"])
            except ValueError:
                item["filters"] = []
            out.append(item)
        return out

    def segment(self, seg_id: int) -> Optional[Dict[str, Any]]:
        conn = self._conn()
        try:
            row = conn.execute("SELECT id, site_id, name, created_by FROM segments WHERE id = ?", (seg_id,)).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def count_segments(self, site_id: Optional[str]) -> int:
        conn = self._conn()
        try:
            return conn.execute("SELECT COUNT(*) FROM segments WHERE site_id IS ?", (site_id,)).fetchone()[0]
        finally:
            conn.close()

    def add_segment(self, site_id: Optional[str], name: str, filters: List[Dict[str, Any]], user: Optional[str]) -> int:
        conn = self._conn()
        try:
            cur = conn.execute("INSERT INTO segments(site_id, name, filters, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                               (site_id, name, json.dumps(filters), user, _now()))
            return int(cur.lastrowid)
        finally:
            conn.close()

    def update_segment(self, seg_id: int, name: Optional[str], filters: Optional[List[Dict[str, Any]]]) -> bool:
        conn = self._conn()
        try:
            cur = conn.execute("UPDATE segments SET name = COALESCE(?, name), filters = COALESCE(?, filters) WHERE id = ?",
                               (name, json.dumps(filters) if filters is not None else None, seg_id))
            return cur.rowcount > 0
        finally:
            conn.close()

    def delete_segment(self, seg_id: int) -> bool:
        conn = self._conn()
        try:
            return conn.execute("DELETE FROM segments WHERE id = ?", (seg_id,)).rowcount > 0
        finally:
            conn.close()

    # -- annotations -----------------------------------------------------------------------

    def annotations(self, site_id: str, start: str, end: str) -> List[Dict[str, Any]]:
        conn = self._conn()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT id, day, text, created_by, created_at FROM annotations WHERE site_id = ? AND day BETWEEN ? AND ? "
                "ORDER BY day, id", (site_id, start, end))]
        finally:
            conn.close()

    def annotation(self, ann_id: int, site_id: str) -> Optional[Dict[str, Any]]:
        conn = self._conn()
        try:
            row = conn.execute("SELECT id, site_id, day, text, created_by FROM annotations WHERE id = ? AND site_id = ?",
                               (ann_id, site_id)).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def count_annotations(self, site_id: str) -> int:
        conn = self._conn()
        try:
            return conn.execute("SELECT COUNT(*) FROM annotations WHERE site_id = ?", (site_id,)).fetchone()[0]
        finally:
            conn.close()

    def add_annotation(self, site_id: str, day: str, text: str, user: Optional[str]) -> int:
        conn = self._conn()
        try:
            cur = conn.execute("INSERT INTO annotations(site_id, day, text, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
                               (site_id, day, text, user, _now()))
            return int(cur.lastrowid)
        finally:
            conn.close()

    def delete_annotation(self, ann_id: int, site_id: str) -> bool:
        conn = self._conn()
        try:
            return conn.execute("DELETE FROM annotations WHERE id = ? AND site_id = ?", (ann_id, site_id)).rowcount > 0
        finally:
            conn.close()

    # -- sites whose data is to be deleted (by the next `jobs.py cron`) ---------------------------

    def add_purge(self, site_id: str, user: Optional[str]) -> None:
        conn = self._conn()
        try:
            conn.execute("INSERT OR REPLACE INTO purges(site_id, requested_by, requested_at) VALUES (?, ?, ?)",
                         (site_id, user, _now()))
        finally:
            conn.close()

    def purges(self) -> List[Dict[str, Any]]:
        conn = self._conn()
        try:
            return [dict(r) for r in conn.execute("SELECT site_id, requested_by, requested_at FROM purges ORDER BY requested_at")]
        finally:
            conn.close()

    def purge_done(self, site_id: str) -> None:
        conn = self._conn()
        try:
            conn.execute("DELETE FROM purges WHERE site_id = ?", (site_id,))
        finally:
            conn.close()

    def delete_site(self, site_id: str) -> None:
        conn = self._conn()
        try:
            conn.execute("DELETE FROM annotations WHERE site_id = ?", (site_id,))
            conn.execute("DELETE FROM segments WHERE site_id = ?", (site_id,))
        finally:
            conn.close()
