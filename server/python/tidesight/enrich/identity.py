"""Identity stitching: a confidence-scored visitor key per persistent visitor id.

Evidence (recorded by the session rebuild in id_edges) links visitor ids:

* uid  — the ids identified as the same known user (confidence 1.0);
* idc  — the cookie and localStorage held different ids in one browser (0.95);
* fpck — the same fingerprint hash on the same day, IP and user agent
         (cookieless hash) (0.7). A fingerprint+cookieless node that links
         more than MAX_FPCK_FANOUT ids is ignored: it is a shared environment
         (identical managed devices behind one NAT), not one person.

Connected ids form one visitor; its key is the oldest id. Hits without a
visitor id (no-JS pixels) are keyed by their cookieless hash.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Any, Dict, List, Set, Tuple

from ..config import Config

IDENTITY_VERSION = 1
CONFIDENCE = {"uid": 1.0, "idc": 0.95, "fpck": 0.7}
MAX_FPCK_FANOUT = 5


class UnionFind:
    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def run(conn: sqlite3.Connection, cfg: Config) -> Dict[str, Any]:
    """Recompute stitching for every site with sessions; update only what changed."""
    stats = {"sites": 0, "remapped": 0, "visitors_updated": 0}
    sites = [r[0] for r in conn.execute("SELECT DISTINCT site_id FROM sessions")]
    for site_id in sites:
        conn.execute("BEGIN IMMEDIATE")
        try:
            changed_keys = _stitch_site(conn, site_id, stats)
            stats["visitors_updated"] += _refresh_visitors(conn, site_id, changed_keys)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        stats["sites"] += 1
    return stats


def _stitch_site(conn: sqlite3.Connection, site_id: str, stats: Dict[str, Any]) -> Set[str]:
    first_seen = {r["vid"]: r["first"] for r in conn.execute(
        "SELECT vid, MIN(started_at) AS first FROM sessions WHERE site_id = ? AND vid IS NOT NULL GROUP BY vid",
        (site_id,))}
    edges = conn.execute("SELECT a, b, kind FROM id_edges WHERE site_id = ?", (site_id,)).fetchall()
    fanout: Dict[str, Set[str]] = defaultdict(set)
    for e in edges:
        if e["kind"] == "fpck":
            fanout[e["b"]].add(e["a"])
    # Union in stages, strongest evidence first. An id's confidence is that of the strongest stage
    # at which it joins its visitor's oldest id (the weakest link actually needed, not every edge).
    uf = UnionFind()
    vnodes = ["v:" + vid for vid in first_seen]
    for node in vnodes:
        uf.find(node)
    stages: List[Tuple[float, Dict[str, str]]] = []
    for kind in ("uid", "idc", "fpck"):
        for e in edges:
            if e["kind"] != kind or (kind == "fpck" and len(fanout[e["b"]]) > MAX_FPCK_FANOUT):
                continue
            uf.union(e["a"], e["b"])
        stages.append((CONFIDENCE[kind], {n: uf.find(n) for n in vnodes}))

    # Group visitor ids by final component.
    members: Dict[str, List[str]] = defaultdict(list)
    for node in vnodes:
        members[uf.find(node)].append(node[2:])

    def joined_at(vid: str, rep: str) -> float:
        if vid == rep:
            return 1.0
        for conf, roots in stages:
            if roots["v:" + vid] == roots["v:" + rep]:
                return conf
        return CONFIDENCE["fpck"]

    current = {r["vid"]: (r["visitor_key"], r["confidence"]) for r in conn.execute(
        "SELECT vid, visitor_key, confidence FROM vid_map WHERE site_id = ?", (site_id,))}
    changed: Set[str] = set()
    for root, vids in members.items():
        rep = min(vids, key=lambda v: (first_seen.get(v) or 0, v))
        key = "%s/%s" % (site_id, rep)
        for vid in vids:
            confidence = joined_at(vid, rep)
            old = current.get(vid)
            if old is None or old[0] != key or abs(old[1] - confidence) > 1e-9:
                conn.execute("INSERT OR REPLACE INTO vid_map(site_id, vid, visitor_key, confidence, first_seen) "
                             "VALUES (?, ?, ?, ?, ?)", (site_id, vid, key, confidence, first_seen.get(vid)))
                conn.execute("UPDATE sessions SET visitor_key = ? WHERE site_id = ? AND vid = ?", (key, site_id, vid))
                changed.add(key)
                if old is not None:
                    changed.add(old[0])
                stats["remapped"] += 1
    # Visitors of sessions without a visitor id (pixels) or not yet summarised.
    for r in conn.execute("SELECT DISTINCT s.visitor_key FROM sessions s LEFT JOIN visitors v "
                          "ON v.visitor_key = s.visitor_key WHERE s.site_id = ? AND v.visitor_key IS NULL", (site_id,)):
        changed.add(r[0])
    return changed


def _refresh_visitors(conn: sqlite3.Connection, site_id: str, keys: Set[str]) -> int:
    """Recompute the visitors rows for the given keys (also picks up sessions changed this run)."""
    keys = set(keys)
    for r in conn.execute("SELECT DISTINCT s.visitor_key FROM sessions s JOIN visitors v ON v.visitor_key = s.visitor_key "
                          "WHERE s.site_id = ? AND (s.ended_at > v.last_seen OR s.started_at < v.first_seen)", (site_id,)):
        keys.add(r[0])
    n = 0
    for key in keys:
        agg = conn.execute(
            "SELECT MIN(started_at) AS first_seen, MAX(ended_at) AS last_seen, COUNT(*) AS sessions, "
            "SUM(pageviews) AS pageviews, SUM(engaged_ms) AS engaged_ms, MAX(is_bot) AS is_bot "
            "FROM sessions WHERE visitor_key = ?", (key,)).fetchone()
        if not agg or not agg["sessions"]:
            conn.execute("DELETE FROM visitors WHERE visitor_key = ?", (key,))
            continue
        last = conn.execute("SELECT uid, country, device_type, browser, os FROM sessions WHERE visitor_key = ? "
                            "ORDER BY started_at DESC LIMIT 1", (key,)).fetchone()
        uid = conn.execute("SELECT uid FROM sessions WHERE visitor_key = ? AND uid IS NOT NULL "
                           "ORDER BY started_at DESC LIMIT 1", (key,)).fetchone()
        vids = [r[0] for r in conn.execute("SELECT vid FROM vid_map WHERE visitor_key = ? ORDER BY first_seen", (key,))]
        conf = conn.execute("SELECT MIN(confidence) FROM vid_map WHERE visitor_key = ?", (key,)).fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO visitors(visitor_key, site_id, first_seen, last_seen, sessions, pageviews, "
            "engaged_ms, uid, vids, confidence, country, device_type, browser, os, is_bot) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, site_id, agg["first_seen"], agg["last_seen"], agg["sessions"], agg["pageviews"] or 0,
             agg["engaged_ms"] or 0, uid[0] if uid else None, json.dumps(vids), conf if conf is not None else 1.0,
             last["country"], last["device_type"], last["browser"], last["os"], agg["is_bot"] or 0))
        n += 1
    return n
