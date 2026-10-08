"""Read access to the raw data: events.db plus yearly archives (archive/events-YYYY.db)."""

from __future__ import annotations

import glob
import heapq
import json
import os
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Set

from ..storage import EVENTS_MIGRATIONS, connect

HITS_SQL = (
    "SELECT h.id AS hit_id, h.request_id, h.seq, h.received_at, h.site_id, h.site_ok, h.type, h.name, h.client_ts, "
    "h.event_id, h.visitor_id, h.session_id, h.page_id, h.user_id, h.url, h.data, r.ip, r.headers, r.envelope, "
    "r.endpoint, r.geo FROM raw_hits h JOIN raw_requests r ON r.id = h.request_id "
    "WHERE h.id > ? ORDER BY h.id LIMIT ?"
)


def raw_paths(data_dir: str) -> List[str]:
    """Archives (oldest first) then the live events.db."""
    paths = sorted(glob.glob(os.path.join(data_dir, "archive", "events-*.db")))
    live = os.path.join(data_dir, "events.db")
    if os.path.exists(live):
        paths.append(live)
    return paths


class RawReader:
    def __init__(self, data_dir: str) -> None:
        self.conns: List[sqlite3.Connection] = []
        for path in raw_paths(data_dir):
            conn = connect(path, readonly=True)
            conn.row_factory = sqlite3.Row
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < EVENTS_MIGRATIONS[-1][0]:
                # An events.db created before the latest migration: migrate it (adds columns only).
                conn.close()
                rw = connect(path)
                from ..storage import migrate
                migrate(rw, EVENTS_MIGRATIONS)
                rw.close()
                conn = connect(path, readonly=True)
                conn.row_factory = sqlite3.Row
            self.conns.append(conn)

    def hits_after(self, last_id: int, limit: int) -> List[sqlite3.Row]:
        """The next `limit` hits after last_id, across all files, in id order."""
        batches = [conn.execute(HITS_SQL, (last_id, limit)).fetchall() for conn in self.conns]
        return list(heapq.merge(*batches, key=lambda r: r["hit_id"]))[:limit]

    def request(self, request_id: int) -> Optional[sqlite3.Row]:
        for conn in reversed(self.conns):
            row = conn.execute("SELECT * FROM raw_requests WHERE id = ?", (request_id,)).fetchone()
            if row is not None:
                return row
        return None

    def headers(self, request_id: int) -> Dict[str, str]:
        row = self.request(request_id)
        if row is None or not row["headers"]:
            return {}
        try:
            return json.loads(row["headers"])
        except ValueError:
            return {}

    def endpoints(self, request_ids: Iterable[int]) -> Set[str]:
        ids = sorted({int(i) for i in request_ids})
        out: Set[str] = set()
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            for conn in self.conns:
                out.update(r[0] for r in conn.execute(
                    "SELECT DISTINCT endpoint FROM raw_requests WHERE id IN (%s)" % marks, chunk))
        return out

    def close(self) -> None:
        for conn in self.conns:
            conn.close()
        self.conns = []
