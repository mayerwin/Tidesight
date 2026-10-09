"""Read-only JSON API for the dashboard and public share pages (standard library only).

Endpoints live under /dashboard/api/<name> (HTTP Basic auth, see [dashboard] users)
and, for sites with `public = true`, under /share/<site>/api/<name> with only the
metrics listed in the site's `public_metrics`.

Common parameters: site, from and to (YYYY-MM-DD, site-local days, inclusive),
filters as f.<dimension>=<value>, and bots=1 to include bot traffic.
"""

from __future__ import annotations

import fnmatch
import gzip
import io
import json
import math
import os
import re
import sqlite3
import time
import zlib
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .config import Config, SiteConfig
from .enrich.timeutil import local_day, zone

# Session dimensions that can be broken down or filtered on: name -> sessions column.
SESSION_DIMS = {
    "source": "source", "medium": "medium", "campaign": "campaign", "term": "term", "content": "content",
    "channel": "channel", "referrer": "referrer_domain", "country": "country", "region": "region_name",
    "city": "city", "device": "device_type", "browser": "browser", "os": "os", "screen": "screen",
    "language": "language", "timezone": "timezone", "entry": "entry_path", "exit": "exit_path",
    "visitor": "visitor_key", "uid": "uid", "asn": "as_org", "brand": "device_brand", "model": "device_model",
    "browser_version": "browser || ' ' || COALESCE(browser_version, '')", "os_version": "os || ' ' || COALESCE(os_version, '')",
    "hour": "hour", "new": "is_new", "bot": "bot_name", "category": "source_category",
}
PUBLIC_ENDPOINTS = {"overview", "breakdown", "sites", "realtime"}
PUBLIC_DIMS = {"pages": ("page", "entry", "exit"), "sources": ("source", "referrer", "channel", "medium", "campaign"),
               "countries": ("country", "region", "city"), "devices": ("device", "browser", "os", "screen"),
               "events": ("event",)}
VITAL_THRESHOLDS = {"LCP": (2500, 4000), "INP": (200, 500), "CLS": (0.1, 0.25), "FCP": (1800, 3000), "TTFB": (800, 1800)}


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _percentile(values: Sequence[float], p: float) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * p
    lo = int(math.floor(k))
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _days(start: str, end: str) -> List[str]:
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    out = []
    while d0 <= d1 and len(out) < 3700:
        out.append(d0.isoformat())
        d0 += timedelta(days=1)
    return out


def _json_or(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


class Query:
    """Parsed request parameters plus SQL helpers bound to one site and date range."""

    def __init__(self, cfg: Config, params: Dict[str, str], site_id: str, public: bool = False) -> None:
        self.cfg = cfg
        self.params = params
        self.public = public
        site = cfg.site(site_id)
        if site is None:
            raise ApiError(404, "unknown site %r" % site_id)
        self.site: SiteConfig = site
        self.site_id = site_id
        self.tz = site.timezone
        today = local_day(int(time.time() * 1000), self.tz)
        end = params.get("to") or today
        start = params.get("from") or (datetime.strptime(end, "%Y-%m-%d").date() - timedelta(days=29)).isoformat()
        for d in (start, end):
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                raise ApiError(400, "dates must be YYYY-MM-DD")
        if start > end:
            start, end = end, start
        self.start, self.end = start, end
        self.bots = params.get("bots") == "1" and not public
        self.filters: List[Tuple[str, str]] = []
        for k, v in params.items():
            if k.startswith("f.") and v != "":
                name = k[2:]
                if name not in SESSION_DIMS and name not in ("page", "event", "path_prefix"):
                    raise ApiError(400, "unknown filter %r" % name)
                self.filters.append((name, v))

    def limit(self, default: int = 50, maximum: int = 1000) -> int:
        try:
            return max(1, min(maximum, int(self.params.get("limit", default))))
        except ValueError:
            return default

    def session_where(self, alias: str = "s") -> Tuple[str, List[Any]]:
        if not alias:
            raise ValueError("session_where needs a table alias")  # EXISTS subqueries reference it
        a = alias + "."
        where = ["%ssite_id = ?" % a, "%sday BETWEEN ? AND ?" % a, "%ssite_ok = 1" % a, "%sinternal = 0" % a]
        args: List[Any] = [self.site_id, self.start, self.end]
        if not self.bots:
            where.append("%sis_bot = 0" % a)
        for name, value in self.filters:
            if name == "page":
                where.append("EXISTS (SELECT 1 FROM pageviews fp WHERE fp.session_key = %ssession_key AND fp.path = ?)" % a)
                args.append(value)
            elif name == "path_prefix":
                where.append("EXISTS (SELECT 1 FROM pageviews fp WHERE fp.session_key = %ssession_key AND fp.path LIKE ?)" % a)
                args.append(value.replace("%", "") + "%")
            elif name == "event":
                where.append("EXISTS (SELECT 1 FROM events fe WHERE fe.session_key = %ssession_key AND fe.name = ?)" % a)
                args.append(value)
            elif value == "(none)":
                where.append("(%s) IS NULL" % self._col(name, a))
            else:
                where.append("(%s) = ?" % self._col(name, a))
                args.append(int(value) if name in ("hour", "new") and value.lstrip("-").isdigit() else value)
        return " AND ".join(where), args

    @staticmethod
    def _col(name: str, prefix: str) -> str:
        expr = SESSION_DIMS[name]
        if re.match(r"^\w+$", expr):
            return prefix + expr
        return re.sub(r"\b(browser|browser_version|os|os_version)\b", lambda m: prefix + m.group(1), expr)

    def previous(self) -> "Query":
        n = len(_days(self.start, self.end))
        d0 = datetime.strptime(self.start, "%Y-%m-%d").date()
        q = Query(self.cfg, dict(self.params), self.site_id, self.public)
        q.start = (d0 - timedelta(days=n)).isoformat()
        q.end = (d0 - timedelta(days=1)).isoformat()
        return q


class Api:
    """Executes API calls against the databases of one deployment."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    # -- connections ----------------------------------------------------------

    def _conn(self, name: str) -> Optional[sqlite3.Connection]:
        path = os.path.join(self.cfg.data_dir, name)
        if not os.path.exists(path):
            return None
        uri = "file:%s?mode=ro" % path.replace("\\", "/").replace("?", "%3f").replace("#", "%23")
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def derived(self) -> sqlite3.Connection:
        conn = self._conn("derived.db")
        if conn is None:
            raise ApiError(503, "no derived data yet: run `jobs.py enrich`")
        return conn

    # -- dispatch -------------------------------------------------------------

    def call(self, endpoint: str, params: Dict[str, str], public_site: Optional[str] = None) -> Any:
        fn: Optional[Callable[..., Any]] = getattr(self, "ep_" + endpoint.replace("-", "_"), None)
        if fn is None or not re.match(r"^[a-z_-]+$", endpoint):
            raise ApiError(404, "unknown endpoint")
        if public_site is not None:
            if endpoint not in PUBLIC_ENDPOINTS:
                raise ApiError(403, "not available on public pages")
            params = dict(params, site=public_site)
            if endpoint == "breakdown":
                allowed = {d for m in self.cfg.site(public_site).settings["public_metrics"] for d in PUBLIC_DIMS.get(m, ())}
                if params.get("dim") not in allowed:
                    raise ApiError(403, "this breakdown is not public")
            if endpoint == "realtime" and "realtime" not in self.cfg.site(public_site).settings["public_metrics"]:
                raise ApiError(403, "realtime is not public")
        if endpoint not in ("sites", "status") and not params.get("site"):
            raise ApiError(400, "missing site")
        return fn(params, public_site is not None)

    def q(self, params: Dict[str, str], public: bool) -> Query:
        return Query(self.cfg, params, params.get("site", ""), public)

    # -- endpoints ------------------------------------------------------------

    def ep_sites(self, params: Dict[str, str], public: bool) -> Any:
        sites = [{"id": s.id, "name": s.name, "timezone": s.timezone, "public": bool(s.settings["public"]),
                  "public_metrics": s.settings["public_metrics"] if s.settings["public"] else [],
                  "origins": [o for o in s.origins if "*" not in o]}
                 for s in self.cfg.sites.values()]
        if public:
            sites = [dict(s, origins=[]) for s in sites if s["id"] == params.get("site")]
        return {"sites": sites, "version": __version__}

    def ep_status(self, params: Dict[str, str], public: bool) -> Any:
        out: Dict[str, Any] = {"version": __version__, "storage_mode": self.cfg.server["storage_mode"], "files": {}}
        for name in ("events.db", "replay.db", "derived.db"):
            path = os.path.join(self.cfg.data_dir, name)
            if os.path.exists(path):
                out["files"][name] = round(os.path.getsize(path) / 1e6, 2)
        conn = self._conn("derived.db")
        if conn is not None:
            try:
                st = conn.execute("SELECT version, last_id, updated_at FROM job_state WHERE job = 'enrich'").fetchone()
                out["enrich"] = dict(st) if st else None
            finally:
                conn.close()
        ev = self._conn("events.db")
        if ev is not None:
            try:
                out["last_hit"] = ev.execute("SELECT MAX(id) AS id, MAX(received_at) AS at FROM raw_hits").fetchone()[1]
                out["flagged_today"] = ev.execute(
                    "SELECT site_status, COUNT(*) FROM raw_requests WHERE received_at > ? AND site_status != 'ok' "
                    "GROUP BY site_status", (int(time.time() * 1000) - 86400000,)).fetchall()
                out["flagged_today"] = {r[0]: r[1] for r in out["flagged_today"]}
            finally:
                ev.close()
        return out

    def ep_overview(self, params: Dict[str, str], public: bool) -> Any:
        q = self.q(params, public)
        conn = self.derived()
        try:
            totals = self._totals(conn, q)
            prev = self._totals(conn, q.previous())
            interval = params.get("interval") or ("hour" if q.start == q.end else "day" if len(_days(q.start, q.end)) <= 92 else "week")
            series = self._series(conn, q, interval)
        finally:
            conn.close()
        if public:
            allowed = set(q.site.settings["public_metrics"])
            keep = {"visitors", "pageviews"} & allowed
            totals = {k: v for k, v in totals.items() if k in keep}
            prev = {k: v for k, v in prev.items() if k in keep}
            series = [{k: v for k, v in p.items() if k in keep or k == "t"} for p in series]
        return {"site": q.site_id, "from": q.start, "to": q.end, "interval": interval, "totals": totals,
                "previous": prev, "series": series}

    def _totals(self, conn: sqlite3.Connection, q: Query) -> Dict[str, Any]:
        where, args = q.session_where("s")
        r = conn.execute(
            "SELECT COUNT(DISTINCT s.visitor_key) AS visitors, COUNT(*) AS sessions, COALESCE(SUM(s.pageviews), 0) AS pageviews, "
            "COALESCE(SUM(s.bounce), 0) AS bounces, AVG(s.duration_ms) AS avg_duration, AVG(s.engaged_ms) AS avg_engaged, "
            "COALESCE(SUM(s.events), 0) AS events, COUNT(DISTINCT CASE WHEN s.is_new = 1 THEN s.visitor_key END) AS new_visitors "
            "FROM sessions s WHERE " + where, args).fetchone()
        out = dict(r)
        out["bounce_rate"] = round(out["bounces"] / out["sessions"], 4) if out["sessions"] else None
        out["views_per_session"] = round(out["pageviews"] / out["sessions"], 2) if out["sessions"] else None
        for k in ("avg_duration", "avg_engaged"):
            out[k] = int(out[k]) if out[k] is not None else None
        return out

    def _series(self, conn: sqlite3.Connection, q: Query, interval: str) -> List[Dict[str, Any]]:
        bucket = {"hour": "s.hour", "day": "s.day", "week": "strftime('%Y-W%W', s.day)",
                  "month": "substr(s.day, 1, 7)"}.get(interval)
        if bucket is None:
            raise ApiError(400, "interval must be hour, day, week or month")
        where, args = q.session_where("s")
        rows = {r["t"]: dict(r) for r in conn.execute(
            "SELECT " + bucket + " AS t, COUNT(DISTINCT s.visitor_key) AS visitors, COUNT(*) AS sessions, "
            "COALESCE(SUM(s.pageviews), 0) AS pageviews, COALESCE(SUM(s.bounce), 0) AS bounces, "
            "AVG(s.duration_ms) AS avg_duration FROM sessions s WHERE " + where + " GROUP BY t ORDER BY t", args)}
        if interval == "hour":
            keys: List[Any] = list(range(24))
        elif interval == "day":
            keys = _days(q.start, q.end)
        else:
            keys = sorted(rows)
        out = []
        for k in keys:
            r = rows.get(k) or {"t": k, "visitors": 0, "sessions": 0, "pageviews": 0, "bounces": 0, "avg_duration": None}
            r["avg_duration"] = int(r["avg_duration"]) if r.get("avg_duration") is not None else None
            out.append(r)
        return out

    def ep_breakdown(self, params: Dict[str, str], public: bool) -> Any:
        q = self.q(params, public)
        dim = params.get("dim", "source")
        limit = q.limit()
        conn = self.derived()
        try:
            if dim in ("page", "title"):
                rows = self._pages(conn, q, limit, by=dim)
            elif dim == "event":
                rows = self._events(conn, q, limit)
            elif dim in SESSION_DIMS:
                where, args = q.session_where("s")
                col = q._col(dim, "s.")
                rows = [dict(r) for r in conn.execute(
                    "SELECT COALESCE(CAST(" + col + " AS TEXT), '(none)') AS value, COUNT(DISTINCT s.visitor_key) AS visitors, "
                    "COUNT(*) AS sessions, COALESCE(SUM(s.pageviews), 0) AS pageviews, "
                    "ROUND(1.0 * SUM(s.bounce) / COUNT(*), 4) AS bounce_rate, CAST(AVG(s.duration_ms) AS INTEGER) AS avg_duration, "
                    "CAST(AVG(s.engaged_ms) AS INTEGER) AS avg_engaged "
                    "FROM sessions s WHERE " + where + " GROUP BY 1 ORDER BY visitors DESC, sessions DESC LIMIT ?",
                    args + [limit])]
                if dim == "city":
                    extra = {(r["city"] or "(none)"): r["country"] for r in conn.execute(
                        "SELECT city, country FROM sessions s WHERE " + where + " GROUP BY city", args)}
                    for r in rows:
                        r["country"] = extra.get(r["value"])
            else:
                raise ApiError(400, "unknown dimension %r" % dim)
        finally:
            conn.close()
        if public:
            rows = [{k: v for k, v in r.items() if k in ("value", "visitors", "pageviews", "country")} for r in rows]
        return {"dim": dim, "rows": rows, "from": q.start, "to": q.end}

    def _pages(self, conn: sqlite3.Connection, q: Query, limit: int, by: str = "page") -> List[Dict[str, Any]]:
        where, args = q.session_where("s")
        col = "p.path" if by == "page" else "p.title"
        return [dict(r) for r in conn.execute(
            "SELECT COALESCE(" + col + ", '(none)') AS value, COUNT(DISTINCT s.visitor_key) AS visitors, COUNT(*) AS pageviews, "
            "CAST(AVG(p.engaged_ms) AS INTEGER) AS avg_engaged, CAST(AVG(p.scroll_max) AS INTEGER) AS avg_scroll, "
            "SUM(p.is_entry) AS entries, SUM(p.is_exit) AS exits, "
            "SUM(CASE WHEN p.is_entry = 1 AND s.bounce = 1 THEN 1 ELSE 0 END) AS bounces, "
            "CAST(AVG(p.duration_ms) AS INTEGER) AS avg_time FROM pageviews p CROSS JOIN sessions s ON s.session_key = p.session_key "
            "WHERE p.site_id = ? AND p.day BETWEEN ? AND ? AND " + where + " GROUP BY 1 ORDER BY pageviews DESC LIMIT ?",
            [q.site_id, q.start, q.end] + args + [limit])]

    def _events(self, conn: sqlite3.Connection, q: Query, limit: int) -> List[Dict[str, Any]]:
        where, args = q.session_where("s")
        return [dict(r) for r in conn.execute(
            "SELECT COALESCE(e.name, '(none)') AS value, COUNT(*) AS count, COUNT(DISTINCT s.visitor_key) AS visitors, "
            "COUNT(DISTINCT s.session_key) AS sessions, SUM(e.value) AS total_value FROM events e "
            "JOIN sessions s ON s.session_key = e.session_key WHERE e.site_id = ? AND e.day BETWEEN ? AND ? AND " + where +
            " GROUP BY 1 ORDER BY count DESC LIMIT ?", [q.site_id, q.start, q.end] + args + [limit])]

    def ep_pages(self, params: Dict[str, str], public: bool) -> Any:
        q = self.q(params, public)
        conn = self.derived()
        try:
            rows = self._pages(conn, q, q.limit(100))
        finally:
            conn.close()
        for r in rows:
            r["exit_rate"] = round(r["exits"] / r["pageviews"], 4) if r["pageviews"] else None
            r["bounce_rate"] = round(r["bounces"] / r["entries"], 4) if r["entries"] else None
        return {"rows": rows}

    def ep_not_found(self, params: Dict[str, str], public: bool) -> Any:
        """Pages flagged with an error status (404 and other 4xx/5xx), with the referrers that lead to them."""
        q = self.q(params, public)
        conn = self.derived()
        try:
            where, args = q.session_where("s")
            base = ("FROM pageviews p CROSS JOIN sessions s ON s.session_key = p.session_key "
                    "WHERE p.site_id = ? AND p.day BETWEEN ? AND ? AND p.status >= 400 AND " + where)
            rows = [dict(r) for r in conn.execute(
                "SELECT p.path AS value, p.status, COUNT(*) AS pageviews, COUNT(DISTINCT s.visitor_key) AS visitors, "
                "MAX(p.ts) AS last_seen " + base + " GROUP BY p.path, p.status ORDER BY pageviews DESC LIMIT ?",
                [q.site_id, q.start, q.end] + args + [q.limit(100)])]
            refs: Dict[Tuple[str, int], Counter] = defaultdict(Counter)
            for r in conn.execute("SELECT p.path, p.status, p.referrer " + base + " AND p.referrer > ''",
                                  [q.site_id, q.start, q.end] + args):
                refs[(r["path"], r["status"])][r["referrer"]] += 1
        finally:
            conn.close()
        for r in rows:
            r["referrers"] = [{"value": v, "count": n} for v, n in refs[(r["value"], r["status"])].most_common(3)]
        return {"rows": rows}

    def ep_events(self, params: Dict[str, str], public: bool) -> Any:
        q = self.q(params, public)
        conn = self.derived()
        try:
            name = params.get("name")
            if not name:
                return {"rows": self._events(conn, q, q.limit(100))}
            where, args = q.session_where("s")
            rows = conn.execute(
                "SELECT e.props, e.value, e.day FROM events e CROSS JOIN sessions s ON s.session_key = e.session_key "
                "WHERE e.site_id = ? AND e.name = ? AND e.day BETWEEN ? AND ? AND " + where + " LIMIT 100000",
                [q.site_id, name, q.start, q.end] + args).fetchall()
        finally:
            conn.close()
        props: Dict[str, Counter] = defaultdict(Counter)
        per_day: Counter = Counter()
        total_value = 0.0
        for r in rows:
            per_day[r["day"]] += 1
            total_value += r["value"] or 0
            for k, v in (_json_or(r["props"], {}) or {}).items():
                if isinstance(v, (str, int, float, bool)) or v is None:
                    props[k][str(v)] += 1
        return {"name": name, "count": len(rows), "total_value": total_value,
                "series": [{"t": d, "count": per_day.get(d, 0)} for d in _days(q.start, q.end)],
                "props": {k: [{"value": v, "count": n} for v, n in c.most_common(20)] for k, c in props.items()}}

    def ep_realtime(self, params: Dict[str, str], public: bool) -> Any:
        site_id = params.get("site", "")
        if self.cfg.site(site_id) is None:
            raise ApiError(404, "unknown site")
        minutes = int(self.cfg.dashboard["realtime_minutes"])
        now = int(time.time() * 1000)
        since = now - minutes * 60000
        ev = self._conn("events.db")
        if ev is None:
            return {"active": 0, "minutes": minutes, "per_minute": [], "pages": [], "referrers": [], "recent": []}
        try:
            rows = ev.execute(
                "SELECT h.type, h.name, h.received_at, h.visitor_id, h.url, h.data, h.request_id FROM raw_hits h "
                "WHERE h.site_id = ? AND h.received_at > ? AND h.site_ok = 1 ORDER BY h.id DESC LIMIT 20000",
                (site_id, since)).fetchall()
        finally:
            ev.close()
        active = {r["visitor_id"] for r in rows if r["visitor_id"] and r["received_at"] > now - 5 * 60000}
        per_min: Dict[int, Dict[str, Any]] = {}
        pages: Counter = Counter()
        refs: Counter = Counter()
        for r in rows:
            m = (now - r["received_at"]) // 60000
            b = per_min.setdefault(m, {"pageviews": 0, "visitors": set()})
            if r["visitor_id"]:
                b["visitors"].add(r["visitor_id"])
            if r["type"] == "pageview":
                b["pageviews"] += 1
                path = urlsplit(r["url"] or "").path or "/"
                pages[path] += 1
                ref = (_json_or(r["data"], {}) or {}).get("ref") or ""
                host = urlsplit(ref).hostname if ref else None
                page_host = urlsplit(r["url"] or "").hostname
                if host and host != page_host:
                    refs[host] += 1
        series = [{"m": -m, "pageviews": per_min.get(m, {}).get("pageviews", 0),
                   "visitors": len(per_min.get(m, {}).get("visitors", ()))} for m in range(minutes - 1, -1, -1)]
        recent = [] if public else [
            {"type": r["type"], "name": r["name"], "at": r["received_at"], "path": urlsplit(r["url"] or "").path}
            for r in rows[:25] if r["type"] not in ("engagement",)]
        return {"active": len(active), "minutes": minutes, "per_minute": series,
                "pages": [{"value": k, "pageviews": v} for k, v in pages.most_common(10)],
                "referrers": [{"value": k, "pageviews": v} for k, v in refs.most_common(10)], "recent": recent}

    def ep_funnel(self, params: Dict[str, str], public: bool) -> Any:
        q = self.q(params, public)
        steps = _json_or(params.get("steps"), None)
        if not isinstance(steps, list) or not 1 <= len(steps) <= 10:
            raise ApiError(400, 'steps must be a JSON list like [{"type":"page","value":"/pricing"},{"type":"event","value":"signup"}]')
        conn = self.derived()
        try:
            where, args = q.session_where("s")
            pvs = conn.execute("SELECT p.session_key, p.ts, p.path FROM pageviews p JOIN sessions s ON s.session_key = p.session_key "
                               "WHERE " + where + " ORDER BY p.ts", args).fetchall()
            evs = conn.execute("SELECT e.session_key, e.ts, e.name FROM events e CROSS JOIN sessions s ON s.session_key = e.session_key "
                               "WHERE " + where + " ORDER BY e.ts", args).fetchall()
        finally:
            conn.close()
        timeline: Dict[str, List[Tuple[int, str, str]]] = defaultdict(list)
        for r in pvs:
            timeline[r["session_key"]].append((r["ts"], "page", r["path"] or ""))
        for r in evs:
            timeline[r["session_key"]].append((r["ts"], "event", r["name"] or ""))

        def matches(step: Dict[str, Any], kind: str, value: str) -> bool:
            if step.get("type", "page") != kind:
                return False
            pattern = str(step.get("value", ""))
            return fnmatch.fnmatchcase(value, pattern) if "*" in pattern else value == pattern

        reached = [0] * len(steps)
        times: List[List[int]] = [[] for _ in steps]
        for items in timeline.values():
            items.sort()
            i, last_ts = 0, None
            for ts, kind, value in items:
                if i < len(steps) and matches(steps[i], kind, value):
                    reached[i] += 1
                    if last_ts is not None:
                        times[i].append(ts - last_ts)
                    last_ts = ts
                    i += 1
        out = []
        for i, step in enumerate(steps):
            prev = reached[i - 1] if i else None
            out.append({"step": step, "sessions": reached[i],
                        "conversion": round(reached[i] / reached[0], 4) if reached[0] else None,
                        "from_previous": round(reached[i] / prev, 4) if prev else None,
                        "median_ms": _percentile(times[i], 0.5)})
        return {"steps": out, "sessions_considered": len(timeline)}

    def ep_retention(self, params: Dict[str, str], public: bool) -> Any:
        q = self.q(params, public)
        period = params.get("period", "week")
        size = 7 if period == "week" else 1 if period == "day" else 30
        conn = self.derived()
        try:
            where, args = q.session_where("s")
            first = {r[0]: r[1] for r in conn.execute(
                "SELECT s.visitor_key, MIN(s.day) FROM sessions s WHERE " + where + " GROUP BY s.visitor_key", args)}
            visits = conn.execute(
                "SELECT DISTINCT s.visitor_key, s.day FROM sessions s WHERE s.site_id = ? AND s.day >= ? AND s.site_ok = 1 "
                "AND s.is_bot = 0", (q.site_id, q.start)).fetchall()
        finally:
            conn.close()
        d_start = datetime.strptime(q.start, "%Y-%m-%d").date()

        def idx(day: str) -> int:
            return (datetime.strptime(day, "%Y-%m-%d").date() - d_start).days // size

        cohorts: Dict[int, set] = defaultdict(set)
        for v, d in first.items():
            cohorts[idx(d)].add(v)
        back: Dict[Tuple[int, int], set] = defaultdict(set)
        for r in visits:
            v = r[0]
            if v in first:
                c = idx(first[v])
                offset = idx(r[1]) - c
                if offset >= 0:
                    back[(c, offset)].add(v)
        rows = []
        for c in sorted(cohorts)[:24]:
            size_c = len(cohorts[c])
            start_day = (d_start + timedelta(days=c * size)).isoformat()
            rows.append({"cohort": start_day, "visitors": size_c,
                         "retention": [round(len(back.get((c, o), ())) / size_c, 4) for o in range(0, 12)]})
        return {"period": period, "cohorts": rows}

    def ep_geo(self, params: Dict[str, str], public: bool) -> Any:
        q = self.q(params, public)
        conn = self.derived()
        try:
            where, args = q.session_where("s")
            countries = [dict(r) for r in conn.execute(
                "SELECT COALESCE(country, '(none)') AS value, COUNT(DISTINCT visitor_key) AS visitors, COUNT(*) AS sessions "
                "FROM sessions s WHERE " + where + " GROUP BY 1 ORDER BY visitors DESC", args)]
            points = [dict(r) for r in conn.execute(
                "SELECT country, city, ROUND(AVG(lat), 3) AS lat, ROUND(AVG(lon), 3) AS lon, COUNT(DISTINCT visitor_key) AS visitors "
                "FROM sessions s WHERE " + where + " AND lat IS NOT NULL GROUP BY country, city ORDER BY visitors DESC LIMIT 1000",
                args)]
        finally:
            conn.close()
        return {"countries": countries, "points": points}

    def ep_performance(self, params: Dict[str, str], public: bool) -> Any:
        q = self.q(params, public)
        conn = self.derived()
        try:
            where, args = q.session_where("s")
            vit = conn.execute("SELECT v.metric, v.value, v.rating, v.path, v.day FROM vitals v CROSS JOIN sessions s "
                               "ON s.session_key = v.session_key WHERE v.site_id = ? AND v.day BETWEEN ? AND ? AND " + where,
                               [q.site_id, q.start, q.end] + args).fetchall()
            perf = conn.execute("SELECT p.data FROM perf p CROSS JOIN sessions s ON s.session_key = p.session_key "
                                "WHERE p.site_id = ? AND p.day BETWEEN ? AND ? AND " + where + " LIMIT 50000",
                                [q.site_id, q.start, q.end] + args).fetchall()
        finally:
            conn.close()
        by_metric: Dict[str, List[float]] = defaultdict(list)
        ratings: Dict[str, Counter] = defaultdict(Counter)
        by_page: Dict[Tuple[str, str], List[float]] = defaultdict(list)
        by_day: Dict[Tuple[str, str], List[float]] = defaultdict(list)
        for r in vit:
            if r["value"] is None or r["metric"] is None:
                continue
            by_metric[r["metric"]].append(r["value"])
            ratings[r["metric"]][r["rating"] or "?"] += 1
            by_page[(r["metric"], r["path"] or "/")].append(r["value"])
            by_day[(r["metric"], r["day"])].append(r["value"])
        metrics = {}
        for m, values in by_metric.items():
            p75 = _percentile(values, 0.75)
            th = VITAL_THRESHOLDS.get(m)
            metrics[m] = {"p75": p75, "count": len(values), "ratings": dict(ratings[m]),
                          "rating": None if th is None or p75 is None else "good" if p75 <= th[0] else "ni" if p75 <= th[1] else "poor",
                          "series": [{"t": d, "p75": _percentile(by_day.get((m, d), []), 0.75)} for d in _days(q.start, q.end)]}
        pages: Dict[str, Dict[str, Any]] = defaultdict(dict)
        for (m, path), values in by_page.items():
            pages[path][m] = _percentile(values, 0.75)
            pages[path]["count"] = max(pages[path].get("count", 0), len(values))
        timing: Dict[str, List[float]] = defaultdict(list)
        for r in perf:
            for k, v in (_json_or(r["data"], {}) or {}).items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    timing[k].append(v)
        return {"vitals": metrics,
                "pages": sorted(({"path": p, **v} for p, v in pages.items()), key=lambda x: -x.get("count", 0))[:50],
                "timing": {k: {"median": _percentile(v, 0.5), "p75": _percentile(v, 0.75), "count": len(v)}
                           for k, v in timing.items()}}

    def ep_errors(self, params: Dict[str, str], public: bool) -> Any:
        q = self.q(params, public)
        conn = self.derived()
        try:
            where, args = q.session_where("s")
            grp = params.get("grp")
            if grp:
                rows = [dict(r) for r in conn.execute(
                    "SELECT e.*, s.browser, s.os, s.device_type, s.visitor_key FROM errors e CROSS JOIN sessions s "
                    "ON s.session_key = e.session_key WHERE e.site_id = ? AND e.grp = ? AND e.day BETWEEN ? AND ? AND "
                    + where + " ORDER BY e.ts DESC LIMIT 100", [q.site_id, grp, q.start, q.end] + args)]
                return {"occurrences": rows}
            rows = [dict(r) for r in conn.execute(
                "SELECT e.grp, e.kind, e.msg, e.src, e.line, e.res, COUNT(*) AS events, SUM(COALESCE(e.n, 1)) AS occurrences, "
                "COUNT(DISTINCT e.session_key) AS sessions, COUNT(DISTINCT s.visitor_key) AS visitors, MIN(e.ts) AS first_seen, "
                "MAX(e.ts) AS last_seen FROM errors e CROSS JOIN sessions s ON s.session_key = e.session_key "
                "WHERE e.site_id = ? AND e.day BETWEEN ? AND ? AND " + where + " GROUP BY e.grp ORDER BY occurrences DESC LIMIT ?",
                [q.site_id, q.start, q.end] + args + [q.limit(100)])]
        finally:
            conn.close()
        return {"rows": rows}

    def ep_heatmap(self, params: Dict[str, str], public: bool) -> Any:
        q = self.q(params, public)
        page = params.get("page")
        if not page:
            raise ApiError(400, "missing page (a path, e.g. /pricing)")
        device = params.get("device")
        conn = self.derived()
        try:
            where, args = q.session_where("s")
            dev_sql, dev_args = ("", [])
            if device:
                dev_sql, dev_args = " AND s.device_type = ?", [device]
            clicks = [dict(r) for r in conn.execute(
                "SELECT c.kind, c.x, c.y, c.ex, c.ey, c.vw, c.ph, c.sel "
                "FROM clicks c CROSS JOIN sessions s ON s.session_key = c.session_key WHERE c.site_id = ? AND c.path = ? "
                "AND c.day BETWEEN ? AND ? AND " + where + dev_sql + " ORDER BY c.ts DESC LIMIT 10000",
                [q.site_id, page, q.start, q.end] + args + dev_args)]
            depth = [r[0] for r in conn.execute(
                "SELECT p.scroll_max FROM pageviews p CROSS JOIN sessions s ON s.session_key = p.session_key WHERE p.site_id = ? "
                "AND p.path = ? AND p.day BETWEEN ? AND ? AND " + where + dev_sql,
                [q.site_id, page, q.start, q.end] + args + dev_args)]
        finally:
            conn.close()
        by_sel: Dict[str, Counter] = defaultdict(Counter)
        for c in clicks:
            by_sel[c["sel"] or "?"][c["kind"]] += 1
        selectors = sorted(({"sel": s, "clicks": c.get("click", 0), "rage": c.get("rage", 0), "dead": c.get("dead", 0),
                             "errors": c.get("errclick", 0)} for s, c in by_sel.items()),
                           key=lambda x: -(x["clicks"] + x["rage"] + x["dead"]))[:100]
        reached = [sum(1 for d in depth if d is not None and d >= p) for p in range(0, 101, 5)]
        return {"page": page, "clicks": clicks, "selectors": selectors, "pageviews": len(depth),
                "scroll": [{"depth": p * 5, "reached": n} for p, n in enumerate(reached)],
                "mouse": self._mouse(q, page, device)}

    def _chunks(self, sql: str, args: Sequence[Any]) -> Iterable[sqlite3.Row]:
        conn = self._conn("replay.db")
        if conn is None:
            return []
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    @staticmethod
    def _decode(row: sqlite3.Row) -> Any:
        data = row["payload"]
        try:
            if row["encoding"] == "gzip":
                data = gzip.decompress(data)
            elif row["encoding"] == "zlib":
                data = zlib.decompress(data)
            return json.loads(data.decode("utf-8"))
        except (OSError, ValueError, zlib.error):
            return None

    def _mouse(self, q: Query, page: str, device: Optional[str]) -> Dict[str, Any]:
        start_ms = int(datetime.strptime(q.start, "%Y-%m-%d").replace(tzinfo=zone(q.tz)).timestamp() * 1000)
        end_ms = int((datetime.strptime(q.end, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=zone(q.tz)).timestamp() * 1000)
        rows = self._chunks("SELECT payload, encoding, url FROM chunks WHERE site_id = ? AND kind = 'mouse' AND site_ok = 1 "
                            "AND received_at BETWEEN ? AND ? ORDER BY id DESC LIMIT 2000", (q.site_id, start_ms, end_ms))
        cols, rows_n = 50, 100
        grid = [[0] * cols for _ in range(rows_n)]
        n = 0
        for r in rows:
            if (urlsplit(r["url"] or "").path or "/") != page:
                continue
            d = self._decode(r)
            if not isinstance(d, dict) or not d.get("pw") or not d.get("ph"):
                continue
            pw, ph = float(d["pw"]), float(d["ph"])
            for pt in d.get("pts") or []:
                try:
                    x, y = float(pt[0]), float(pt[1])
                except (TypeError, ValueError, IndexError):
                    continue
                cx, cy = int(min(cols - 1, max(0, x / pw * cols))), int(min(rows_n - 1, max(0, y / ph * rows_n)))
                grid[cy][cx] += 1
                n += 1
        return {"points": n, "cols": cols, "rows": rows_n, "grid": grid if n else []}

    def ep_snapshot(self, params: Dict[str, str], public: bool) -> Any:
        q = self.q(params, public)
        page = params.get("page") or "/"
        rows = self._chunks("SELECT payload, encoding, url, received_at FROM chunks WHERE site_id = ? AND kind = 'snapshot' "
                            "ORDER BY id DESC LIMIT 200", (q.site_id,))
        for r in rows:
            if (urlsplit(r["url"] or "").path or "/") == page:
                return {"page": page, "at": r["received_at"], "url": r["url"], "snapshot": self._decode(r)}
        return {"page": page, "snapshot": None}

    def ep_replays(self, params: Dict[str, str], public: bool) -> Any:
        q = self.q(params, public)
        conn = self.derived()
        try:
            where, args = q.session_where("s")
            rows = [dict(r) for r in conn.execute(
                "SELECT session_key, visitor_key, started_at, duration_ms, engaged_ms, pageviews, entry_path, country, city, "
                "device_type, browser, os, errors, rage_clicks FROM sessions s WHERE has_replay = 1 AND " + where +
                " ORDER BY started_at DESC LIMIT ?", args + [q.limit(100, 500)])]
        finally:
            conn.close()
        return {"rows": rows}

    def ep_replay(self, params: Dict[str, str], public: bool) -> Any:
        key = params.get("session", "")
        if "/" not in key:
            raise ApiError(400, "missing session")
        site_id, sid = key.split("/", 1)
        if site_id != params.get("site"):
            raise ApiError(400, "session does not belong to this site")
        rows = self._chunks("SELECT payload, encoding, meta, page_id, seq, client_ts FROM chunks WHERE site_id = ? AND "
                            "session_id = ? AND kind = 'replay' ORDER BY client_ts, id", (site_id, sid))
        events: List[Any] = []
        recs: Dict[Tuple[str, int], Any] = {}
        for r in rows:
            meta = _json_or(r["meta"], {})
            k = (str(meta.get("rec") or r["page_id"] or ""), int(r["seq"] or 0))
            if k in recs:
                continue  # a retried chunk
            data = self._decode(r)
            recs[k] = True
            if isinstance(data, list):
                events.extend(e for e in data if isinstance(e, dict))
        events.sort(key=lambda e: e.get("timestamp", 0))
        return {"session": key, "events": events, "chunks": len(recs)}

    def ep_sessions(self, params: Dict[str, str], public: bool) -> Any:
        q = self.q(params, public)
        conn = self.derived()
        try:
            where, args = q.session_where("s")
            try:
                offset = max(0, int(params.get("offset", "0")))
            except ValueError:
                offset = 0
            rows = [dict(r) for r in conn.execute(
                "SELECT session_key, visitor_key, uid, started_at, duration_ms, engaged_ms, pageviews, events, entry_path, "
                "exit_path, source, channel, country, city, device_type, browser, os, is_new, is_bot, has_replay, errors "
                "FROM sessions s WHERE " + where + " ORDER BY started_at DESC LIMIT ? OFFSET ?",
                args + [q.limit(50, 500), offset])]
        finally:
            conn.close()
        return {"rows": rows}

    def ep_session(self, params: Dict[str, str], public: bool) -> Any:
        key = params.get("key", "")
        conn = self.derived()
        try:
            s = conn.execute("SELECT * FROM sessions WHERE session_key = ? AND site_id = ?", (key, params.get("site"))).fetchone()
            if s is None:
                raise ApiError(404, "unknown session")
            pvs = [dict(r) for r in conn.execute("SELECT * FROM pageviews WHERE session_key = ? ORDER BY ts", (key,))]
            hits = [dict(r) for r in conn.execute(
                "SELECT hit_id, type, name, ts, page_key, url, dup FROM hits WHERE session_key = ? ORDER BY ts, hit_id LIMIT 3000",
                (key,))]
        finally:
            conn.close()
        raw = {}
        ev = self._conn("events.db")
        if ev is not None and hits:
            try:
                ids = [h["hit_id"] for h in hits]
                for start in range(0, len(ids), 500):
                    chunk = ids[start:start + 500]
                    for r in ev.execute("SELECT id, data FROM raw_hits WHERE id IN (%s)" % ",".join("?" * len(chunk)), chunk):
                        raw[r["id"]] = _json_or(r["data"], None)
            finally:
                ev.close()
        for h in hits:
            h["data"] = raw.get(h["hit_id"])
        return {"session": dict(s), "pageviews": pvs, "timeline": hits}

    def ep_visitor(self, params: Dict[str, str], public: bool) -> Any:
        key = params.get("key", "")
        conn = self.derived()
        try:
            v = conn.execute("SELECT * FROM visitors WHERE visitor_key = ? AND site_id = ?", (key, params.get("site"))).fetchone()
            if v is None:
                raise ApiError(404, "unknown visitor")
            sessions = [dict(r) for r in conn.execute(
                "SELECT session_key, started_at, duration_ms, pageviews, events, entry_path, source, channel, country, city, "
                "device_type, browser, os, vid, uid, has_replay FROM sessions WHERE visitor_key = ? ORDER BY started_at DESC "
                "LIMIT 500", (key,))]
            ids = [dict(r) for r in conn.execute("SELECT vid, confidence, first_seen FROM vid_map WHERE visitor_key = ?", (key,))]
        finally:
            conn.close()
        out = dict(v)
        out["vids"] = _json_or(out.get("vids"), [])
        return {"visitor": out, "sessions": sessions, "ids": ids}

    def ep_forms(self, params: Dict[str, str], public: bool) -> Any:
        q = self.q(params, public)
        conn = self.derived()
        try:
            where, args = q.session_where("s")
            rows = conn.execute(
                "SELECT f.form, f.action, f.field, f.ms, f.filled, f.session_key, f.last FROM forms f CROSS JOIN sessions s "
                "ON s.session_key = f.session_key WHERE f.site_id = ? AND f.day BETWEEN ? AND ? AND " + where,
                [q.site_id, q.start, q.end] + args).fetchall()
        finally:
            conn.close()
        forms: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            f = forms.setdefault(r["form"] or "?", {"form": r["form"] or "?", "start": set(), "submit": set(),
                                                    "abandon": set(), "fields": defaultdict(lambda: {"blur": 0, "ms": [],
                                                                                                     "filled": 0, "last": 0})})
            a = r["action"]
            if a in ("start", "submit", "abandon"):
                f[a].add(r["session_key"])
            if a == "blur" and r["field"]:
                fd = f["fields"][r["field"]]
                fd["blur"] += 1
                if r["ms"] is not None:
                    fd["ms"].append(r["ms"])
                fd["filled"] += 1 if r["filled"] else 0
            if a == "abandon" and r["last"]:
                f["fields"][r["last"]]["last"] += 1
        out = []
        for f in forms.values():
            starts = len(f["start"]) or len(f["submit"] | f["abandon"])
            out.append({"form": f["form"], "starts": starts, "submits": len(f["submit"]), "abandons": len(f["abandon"]),
                        "conversion": round(len(f["submit"]) / starts, 4) if starts else None,
                        "fields": sorted(({"field": k, "interactions": v["blur"], "median_ms": _percentile(v["ms"], 0.5),
                                           "filled_rate": round(v["filled"] / v["blur"], 4) if v["blur"] else None,
                                           "abandoned_here": v["last"]} for k, v in f["fields"].items()),
                                         key=lambda x: -x["interactions"])})
        return {"rows": sorted(out, key=lambda x: -x["starts"])}

    def ep_search(self, params: Dict[str, str], public: bool) -> Any:
        q = self.q(params, public)
        conn = self.derived()
        try:
            where, args = q.session_where("s")
            rows = [dict(r) for r in conn.execute(
                "SELECT LOWER(TRIM(x.query)) AS value, COUNT(*) AS searches, COUNT(DISTINCT s.visitor_key) AS visitors, "
                "AVG(x.results) AS avg_results FROM searches x CROSS JOIN sessions s ON s.session_key = x.session_key "
                "WHERE x.site_id = ? AND x.day BETWEEN ? AND ? AND " + where + " GROUP BY 1 ORDER BY searches DESC LIMIT ?",
                [q.site_id, q.start, q.end] + args + [q.limit(100)])]
        finally:
            conn.close()
        return {"rows": rows}
