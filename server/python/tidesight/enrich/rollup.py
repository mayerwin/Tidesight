"""Daily aggregates per site, for fast long-range dashboards.

Only the days touched since the last run (dirty_days) are recomputed.
Rollups count "clean" traffic: hits for a configured site and allowed
origin, not bots, not internal.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Tuple

from ..config import Config

CLEAN = "site_ok = 1 AND is_bot = 0 AND internal = 0"

# Session-level dimensions: (name, SQL expression over sessions).
SESSION_DIMS: List[Tuple[str, str]] = [
    ("entry", "entry_path"), ("exit", "exit_path"), ("source", "source"), ("medium", "medium"),
    ("campaign", "campaign"), ("channel", "channel"), ("referrer", "referrer_domain"), ("country", "country"),
    ("region", "COALESCE(country, '') || '-' || COALESCE(region_name, region, '')"),
    ("city", "COALESCE(country, '') || '|' || COALESCE(city, '')"), ("device", "device_type"),
    ("browser", "browser"), ("os", "os"), ("screen", "screen"), ("language", "language"),
]


def run(conn: sqlite3.Connection, cfg: Config) -> Dict[str, Any]:
    days = conn.execute("SELECT site_id, day FROM dirty_days ORDER BY site_id, day").fetchall()
    for r in days:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rollup_day(conn, r["site_id"], r["day"])
            conn.execute("DELETE FROM dirty_days WHERE site_id = ? AND day = ?", (r["site_id"], r["day"]))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return {"days": len(days)}


def rollup_day(conn: sqlite3.Connection, site_id: str, day: str) -> None:
    conn.execute("DELETE FROM daily WHERE site_id = ? AND day = ?", (site_id, day))
    conn.execute("DELETE FROM daily_dim WHERE site_id = ? AND day = ?", (site_id, day))
    conn.execute(
        "INSERT INTO daily(site_id, day, visitors, new_visitors, sessions, pageviews, events, bounces, "
        "engaged_sessions, duration_ms, engaged_ms, bots) "
        "SELECT ?, ?, COUNT(DISTINCT visitor_key), COUNT(DISTINCT CASE WHEN is_new = 1 THEN visitor_key END), "
        "COUNT(*), COALESCE(SUM(pageviews), 0), COALESCE(SUM(events), 0), COALESCE(SUM(bounce), 0), "
        "COALESCE(SUM(engaged), 0), COALESCE(SUM(duration_ms), 0), COALESCE(SUM(engaged_ms), 0), "
        "(SELECT COUNT(*) FROM sessions WHERE site_id = ? AND day = ? AND is_bot = 1) "
        "FROM sessions WHERE site_id = ? AND day = ? AND " + CLEAN,
        (site_id, day, site_id, day, site_id, day))
    for dim, expr in SESSION_DIMS:
        conn.execute(
            "INSERT INTO daily_dim(site_id, day, dim, value, visitors, sessions, pageviews, bounces, engaged_ms) "
            "SELECT ?, ?, ?, COALESCE(" + expr + ", '(none)'), COUNT(DISTINCT visitor_key), COUNT(*), "
            "COALESCE(SUM(pageviews), 0), COALESCE(SUM(bounce), 0), COALESCE(SUM(engaged_ms), 0) "
            "FROM sessions WHERE site_id = ? AND day = ? AND " + CLEAN + " GROUP BY 4",
            (site_id, day, dim, site_id, day))
    # Pages: pageviews by path, visitors and sessions that viewed them.
    conn.execute(
        "INSERT INTO daily_dim(site_id, day, dim, value, visitors, sessions, pageviews, bounces, engaged_ms) "
        "SELECT ?, ?, 'page', COALESCE(p.path, '(none)'), COUNT(DISTINCT s.visitor_key), COUNT(DISTINCT s.session_key), "
        "COUNT(*), SUM(CASE WHEN p.is_entry = 1 AND s.bounce = 1 THEN 1 ELSE 0 END), COALESCE(SUM(p.engaged_ms), 0) "
        "FROM pageviews p JOIN sessions s ON s.session_key = p.session_key "
        "WHERE p.site_id = ? AND p.day = ? AND s.site_ok = 1 AND s.is_bot = 0 AND s.internal = 0 GROUP BY 4",
        (site_id, day, site_id, day))
    # Custom events: count in "pageviews", visitors and sessions that triggered them.
    conn.execute(
        "INSERT INTO daily_dim(site_id, day, dim, value, visitors, sessions, pageviews, bounces, engaged_ms) "
        "SELECT ?, ?, 'event', COALESCE(e.name, '(none)'), COUNT(DISTINCT s.visitor_key), COUNT(DISTINCT s.session_key), "
        "COUNT(*), 0, 0 FROM events e JOIN sessions s ON s.session_key = e.session_key "
        "WHERE e.site_id = ? AND e.day = ? AND s.site_ok = 1 AND s.is_bot = 0 AND s.internal = 0 GROUP BY 4",
        (site_id, day, site_id, day))
