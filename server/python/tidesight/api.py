"""JSON API for the dashboard and public share pages (standard library only). docs/api.md describes it.

Endpoints live under /dashboard/api/<name> (a login session or HTTP Basic auth, see [dashboard] users)
and, for sites with `public = true`, under /share/<site>/api/<name> with only the metrics listed in the
site's `public_metrics`. Reads are here; the writes (properties, segments, annotations) are in admin.py.

Common parameters: site, from and to (YYYY-MM-DD, site-local days, inclusive), filters (JSON, see
query.py; f.<dimension>=<value> still works), bots (exclude, include or only), compare (previous, year,
custom with compare_from and compare_to).
"""

from __future__ import annotations

import fnmatch
import json
import math
import os
import re
import shutil
import sqlite3
import time
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from . import __repository__, __version__, webcheck
from .config import Config, SiteConfig
from .enrich.timeutil import local_day, zone

from .properties import hosts as site_hosts
from .query import (DIM_LABELS, MAX_ROW_VALUES, METRIC_LABELS, SESSION_DIMS, ApiError, Query, breakdown, compare_rows, glob_literal,
                    goal_condition, interval_for, series as time_series, totals as period_totals, value_series)
from .webdb import WebDB

PUBLIC_ENDPOINTS = {"overview", "breakdown", "sites", "realtime"}
PUBLIC_DIMS = {"pages": ("page", "entry", "exit"), "sources": ("source", "referrer", "channel", "medium", "campaign"),
               "countries": ("country", "region", "city"), "devices": ("device", "browser", "os", "screen"),
               "events": ("event",)}
# Bounds on decompressing stored chunks in one API call (a chunk is attacker-controlled data).
MAX_CHUNK_DECODED = 32 * 1024 * 1024
MAX_MOUSE_DECODED = 2 * 1024 * 1024  # a page's mouse chunk holds at most a few thousand points
MAX_DECODED_PER_CALL = 128 * 1024 * 1024
# Decoded JSON objects and arrays per API call: memory follows their number, not the bytes (a megabyte of
# `[{},{},…]` is a third of a million dicts). A long, busy replay holds a few hundred thousand.
MAX_OBJECTS_PER_CALL = 600000
MAX_RAW_HIT = 65536  # raw hit payloads larger than this are not decoded for the session timeline
PUBLIC_MIN_VISITORS = 3  # public rows and buckets with fewer visitors are hidden (no singling out)
MAX_PATH_SESSIONS = 300000  # visits one paths report reads (beyond, it says it is truncated)


def public_dims(site: "SiteConfig") -> set:
    """The dimensions a site's public page may break down and filter by (its public_metrics)."""
    return {d for m in site.settings["public_metrics"] for d in PUBLIC_DIMS.get(m, ())}


VITAL_THRESHOLDS = {"LCP": (2500, 4000), "INP": (200, 500), "CLS": (0.1, 0.25), "FCP": (1800, 3000), "TTFB": (800, 1800)}


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
    except (ValueError, RecursionError):
        return default


class Budget:
    """What one API call may decode of stored, client-sent JSON: bytes and objects (memory follows the number of
    objects: a megabyte of `[{},{},…]` is a third of a million dicts)."""

    def __init__(self, nbytes: Optional[int] = None, objects: Optional[int] = None) -> None:
        self.bytes = MAX_DECODED_PER_CALL if nbytes is None else nbytes
        self.objects = MAX_OBJECTS_PER_CALL if objects is None else objects
        self.exhausted = False

    def take(self, data: Any) -> bool:
        """Charge a JSON text (str or bytes) about to be decoded; False when it does not fit."""
        if isinstance(data, str):
            n, objs = len(data), data.count("{") + data.count("[")
        else:
            n, objs = len(data), data.count(b"{") + data.count(b"[")
        if n > self.bytes or objs > self.objects:
            self.exhausted = True
            return False
        self.bytes -= n
        self.objects -= objs
        return True

    def loads(self, raw: Optional[str], default: Any = None) -> Any:
        if not raw or not self.take(raw):
            return default
        return _json_or(raw, default)


def _path(url: Any) -> str:
    """The path of a stored URL ("/" when it has none or does not parse: stored URLs are anyone's data)."""
    try:
        return urlsplit(url).path or "/" if isinstance(url, str) else "/"
    except ValueError:
        return "/"


def _host(url: Any) -> Optional[str]:
    try:
        return urlsplit(url).hostname if isinstance(url, str) and url else None
    except ValueError:
        return None


def _rrweb_events(data: Any) -> List[Dict[str, Any]]:
    """The items of a stored replay or snapshot chunk that have rrweb's shape (type, timestamp)."""
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict) and type(e.get("type")) is int
            and isinstance(e.get("timestamp"), (int, float)) and not isinstance(e.get("timestamp"), bool)]


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
                if params.get("dim") not in public_dims(self.cfg.site(public_site)):
                    raise ApiError(403, "this breakdown is not public")
            if endpoint == "realtime" and "realtime" not in self.cfg.site(public_site).settings["public_metrics"]:
                raise ApiError(403, "realtime is not public")
        if endpoint not in ("sites", "status") and not params.get("site"):
            raise ApiError(400, "missing site")
        return fn(params, public_site is not None)

    def q(self, params: Dict[str, str], public: bool) -> Query:
        if public:  # refused before their values are even read
            site = self.cfg.site(params.get("site", ""))
            allowed = public_dims(site) if site is not None else set()
            asked = [k[2:] for k in params if k.startswith("f.") and params[k] != ""]
            raw = _json_or(params.get("filters"), [])
            asked += [str(f.get("dim")) for f in raw if isinstance(f, dict)] if isinstance(raw, list) else ["?"]
            if any(("page" if d == "path_prefix" else d) not in allowed for d in asked):
                raise ApiError(403, "this filter is not public")
        q = Query(self.cfg, params, params.get("site", ""), public)
        if public:
            allowed = public_dims(q.site)
            # A public page must not single anyone out: only its own dimensions, exact values, one at a time.
            # One exact value: a group with and without a small value would reveal the small one's count.
            if any(f.dim not in allowed or f.op != "is" or len(f.values) != 1 for f in q.filters):
                raise ApiError(403, "this filter is not public")
            if len(q.filters) > 1:  # combining filters narrows a public view down to one person
                raise ApiError(400, "public pages take one filter at a time")
        return q

    # -- endpoints ------------------------------------------------------------

    def ep_sites(self, params: Dict[str, str], public: bool) -> Any:
        sites = [{"id": s.id, "name": s.name, "label": s.display_name, "domains": site_hosts(s.origins),
                  "timezone": s.timezone, "public": bool(s.settings["public"]), "managed": s.managed,
                  "public_metrics": s.settings["public_metrics"] if s.settings["public"] else [],
                  "origins": [o for o in s.origins if "*" not in o], "goals": s.settings.get("goals") or [],
                  "search": {"google": bool(s.settings["search_console"]), "bing": bool(s.settings["bing_site"])}}
                 for s in sorted(self.cfg.sites.values(), key=lambda x: x.display_name.lower())]
        first: Dict[str, str] = {}
        conn = self._conn("derived.db")
        if conn is not None:
            try:  # the first day with data: where "All time" starts
                first = {r[0]: r[1] for r in conn.execute("SELECT site_id, MIN(day) FROM daily GROUP BY site_id")}
            except sqlite3.Error:
                pass
            finally:
                conn.close()
        for s in sites:
            s["first_day"] = first.get(s["id"])
        if public:
            # A public page names its site by its name or id, never by a host name the origins would reveal.
            sites = [dict(s, origins=[], domains=[], goals=[], search={}, label=s["name"] or s["id"]) for s in sites
                     if s["id"] == params.get("site")]
            return {"sites": sites, "version": __version__, "repository": __repository__}
        return {"sites": sites, "version": __version__, "repository": __repository__, "freshness": self._freshness()}

    def _freshness(self) -> Dict[str, Any]:
        """When enrichment last ran and when the last hit arrived: the dashboard says how fresh its data is."""
        out: Dict[str, Any] = {"enriched_at": None, "last_hit": None}
        conn = self._conn("derived.db")
        if conn is not None:
            try:
                row = conn.execute("SELECT updated_at FROM job_state WHERE job = 'enrich'").fetchone()
                out["enriched_at"] = row[0] if row else None
            except sqlite3.Error:
                pass
            finally:
                conn.close()
        ev = self._conn("events.db")
        if ev is not None:
            try:
                out["last_hit"] = ev.execute("SELECT MAX(received_at) FROM raw_hits WHERE id = (SELECT MAX(id) FROM raw_hits)").fetchone()[0]
            except sqlite3.Error:
                pass
            finally:
                ev.close()
        return out

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
                out["counters_today"] = [
                    {"site": r[0], "name": r[1], "n": r[2]} for r in ev.execute(
                        "SELECT site_id, name, n FROM counters WHERE day = ? ORDER BY site_id, name",
                        (time.strftime("%Y-%m-%d", time.gmtime()),))]
            finally:
                ev.close()
        try:
            out["disk_free_mb"] = int(shutil.disk_usage(self.cfg.data_dir).free // (1024 * 1024))
        except OSError:
            out["disk_free_mb"] = None
        out["min_free_disk_mb"] = self.cfg.server["min_free_disk_mb"]
        check = webcheck.state(self.cfg)
        if check and not public:  # files inside the website's folder: can the web download them? (no server paths)
            out["web_check"] = {k: check.get(k) for k in ("checked_at", "verified_at", "exposed", "unverified", "url",
                                                           "skipped", "warnings") if check.get(k) is not None}
            if check.get("folders"):
                out["web_check"]["checked"] = [{"what": f.get("what"), "url": f.get("url"), "status": f.get("status")}
                                               for f in check["folders"]]
        return out

    def ep_overview(self, params: Dict[str, str], public: bool) -> Any:
        q = self.q(params, public)
        goal = None if public else q.goal()
        cq = None if public else q.comparison()
        interval = interval_for(q, params.get("interval"))
        conn = self.derived()
        try:
            totals = period_totals(conn, q, goal)
            series = time_series(conn, q, interval, goal)
            # Without a comparison, the change shown is against the period of the same length just before.
            prev = period_totals(conn, cq or q.previous(), goal)
            cmp_series = time_series(conn, cq, interval, goal) if cq else None
        finally:
            conn.close()
        out: Dict[str, Any] = {"site": q.site_id, "from": q.start, "to": q.end, "interval": interval, "totals": totals,
                               "previous": prev, "series": series}
        if cq:
            out["compare"] = {"from": cq.start, "to": cq.end, "totals": prev, "series": cmp_series}
        if goal:
            out["goal"] = goal
        if public:
            allowed = set(q.site.settings["public_metrics"])
            keep = {"visitors", "pageviews"} & allowed

            def shown(d: Dict[str, Any]) -> Dict[str, Any]:
                few = q.filters and (d.get("visitors") or 0) < PUBLIC_MIN_VISITORS  # filtered: no singling out
                return {k: (v if k == "t" or not few else 0) for k, v in d.items() if k in keep or k == "t"}

            out.update(totals=shown(totals), previous=shown(prev), series=[shown(p) for p in series])
        else:
            out["annotations"] = WebDB(self.cfg.data_dir).annotations(q.site_id, q.start, q.end)
        return out

    def ep_breakdown(self, params: Dict[str, str], public: bool) -> Any:
        """Rows of a dimension (dim, optionally dim2), sorted (sort, dir), paged (limit, offset), searched (q, or
        values: a JSON list), with totals, the number of rows, a goal's conversions (goal) and a comparison."""
        q = self.q(params, public)
        dim = params.get("dim", "source")
        if public:
            dim2, search, values_in, goal, sort, desc, offset = None, None, None, None, None, True, 0
        else:
            dim2 = params.get("dim2") or None
            search = (params.get("q") or "").strip()[:200] or None
            values_in = _json_or(params.get("values"), None)
            if values_in is not None and (not isinstance(values_in, list) or len(values_in) > MAX_ROW_VALUES
                                          or not all(isinstance(v, str) for v in values_in)):
                raise ApiError(400, "values must be a JSON list of texts")
            goal = q.goal()
            sort, desc, offset = params.get("sort"), params.get("dir", "desc") != "asc", q.offset()
        if public:
            limit = q.limit(10, 100)
        else:
            limit = q.limit(10, 100000 if params.get("format") in ("csv", "tsv", "json", "xlsx") else 1000)
        conn = self.derived()
        try:
            result = breakdown(conn, q, dim, dim2, sort=sort, desc=desc, limit=limit, offset=offset, search=search,
                               values_in=values_in, goal=goal)
            cq = None if public else q.comparison()
            if cq is not None:
                compare_rows(conn, q, cq, result, dim, dim2, goal, search=search, values_in=values_in)
        finally:
            conn.close()
        if public:
            # Rows of one or two visitors are not shown publicly: a page only one person saw may be a reset link
            # with its token in the path, or tell what that person did.
            result["rows"] = [{k: v for k, v in r.items() if k in ("value", "visitors", "pageviews", "country")}
                              for r in result["rows"] if (r.get("visitors") or 0) >= PUBLIC_MIN_VISITORS]
            for key in ("totals", "total_rows", "metrics"):
                result.pop(key, None)
        else:
            result["columns"] = ([{"key": "value", "label": DIM_LABELS.get(dim, dim)}]
                                 + ([{"key": "value2", "label": DIM_LABELS.get(dim2, dim2)}] if dim2 else [])
                                 + [{"key": m, "label": METRIC_LABELS.get(m, m)} for m in result["metrics"]])
        result.update({"from": q.start, "to": q.end})
        return result

    def ep_breakdown_series(self, params: Dict[str, str], public: bool) -> Any:
        """One metric over time for a few values of a dimension (values: a JSON list of up to 10)."""
        q = self.q(params, public)
        values = _json_or(params.get("values"), None)
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ApiError(400, "values must be a JSON list of texts")
        interval = interval_for(q, params.get("interval"))
        conn = self.derived()
        try:
            out = value_series(conn, q, params.get("dim", "source"), values[:10], params.get("metric", ""), interval)
        finally:
            conn.close()
        out["interval"] = interval
        return out

    def ep_goals(self, params: Dict[str, str], public: bool) -> Any:
        """Each goal's conversions (visits that completed it), converted visitors and rate, with a comparison."""
        q = self.q(params, public)
        cq = q.comparison()
        goals = list(q.goals.values())
        conn = self.derived()
        try:
            base = period_totals(conn, q)
            base_cmp = period_totals(conn, cq) if cq else None
            rows = []
            for g in goals:
                row = dict(g, **self._goal_stats(conn, q, g, base))
                if cq:
                    row["cmp"] = self._goal_stats(conn, cq, g, base_cmp)
                rows.append(row)
            selected = q.goal() or (goals[0] if goals else None)
            interval = interval_for(q, params.get("interval"))
            trend = time_series(conn, q, interval, selected) if selected else []
            trend_cmp = time_series(conn, cq, interval, selected) if selected and cq else None
        finally:
            conn.close()
        out: Dict[str, Any] = {"goals": rows, "sessions": base["sessions"], "interval": interval,
                               "selected": selected["id"] if selected else None, "series": trend}
        if cq:
            out["compare"] = {"from": cq.start, "to": cq.end, "sessions": base_cmp["sessions"], "series": trend_cmp}
        return out

    @staticmethod
    def _goal_stats(conn: sqlite3.Connection, q: Query, goal: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
        """Visits that completed the goal, their visitors and rate, and the summed value of matching events."""
        where, args = q.session_where("s")
        gargs: List[Any] = []
        cond = goal_condition(goal, gargs)
        r = conn.execute("SELECT COUNT(*) AS conversions, COUNT(DISTINCT s.visitor_key) AS visitors FROM sessions s "
                         "WHERE " + where + " AND " + cond, args + gargs).fetchone()
        value = None
        if goal["type"] == "event":
            value = conn.execute("SELECT SUM(e.value) FROM events e CROSS JOIN sessions s ON s.session_key = e.session_key "
                                 "WHERE e.site_id = ? AND e.day BETWEEN ? AND ? AND e.name GLOB ? AND " + where,
                                 [q.site_id, q.start, q.end, glob_literal(goal["match"])] + args).fetchone()[0]
        sessions = base["sessions"] or 0
        return {"conversions": r["conversions"], "visitors": r["visitors"], "value": value,
                "conversion_rate": round(r["conversions"] / sessions, 4) if sessions else None}

    def ep_annotations(self, params: Dict[str, str], public: bool) -> Any:
        q = self.q(params, public)
        return {"annotations": WebDB(self.cfg.data_dir).annotations(q.site_id, q.start, q.end)}

    def ep_segments(self, params: Dict[str, str], public: bool) -> Any:
        site = params.get("site")
        return {"segments": WebDB(self.cfg.data_dir).segments(site)}

    def ep_export_catalog(self, params: Dict[str, str], public: bool) -> Any:
        """What each raw dataset holds (description, columns) and how many rows it has for this site and period."""
        from .maintenance import EXPORT_INFO, EXPORTS, export_args
        q = self.q(params, public)
        out = []
        conns: Dict[str, Optional[sqlite3.Connection]] = {}
        try:
            for key, info in EXPORT_INFO.items():
                db = info["db"]
                if db not in conns:
                    conns[db] = self._conn(db)
                conn = conns[db]
                rows, columns = None, []
                if conn is not None:
                    try:
                        args = export_args(key, q.site_id, q.start, q.end)
                        rows = conn.execute(info["count"], args).fetchone()[0]
                        columns = [d[0] for d in conn.execute("SELECT * FROM (%s) LIMIT 0" % EXPORTS[key][1], args).description]
                    except sqlite3.Error:
                        rows = None
                out.append({"id": key, "label": info["label"], "description": info["description"],
                            "columns": columns, "rows": rows, "utc_days": key in ("raw", "visitors")})
        finally:
            for c in conns.values():
                if c is not None:
                    c.close()
        return {"datasets": out, "from": q.start, "to": q.end}

    def _pages(self, conn: sqlite3.Connection, q: Query, limit: int, by: str = "page") -> List[Dict[str, Any]]:
        return breakdown(conn, q, by, limit=limit, with_totals=False)["rows"]

    def _events(self, conn: sqlite3.Connection, q: Query, limit: int) -> List[Dict[str, Any]]:
        return breakdown(conn, q, "event", limit=limit, with_totals=False)["rows"]

    def ep_pages(self, params: Dict[str, str], public: bool) -> Any:
        """Pages with their engagement (the breakdown of `page`, kept for older dashboards and scripts)."""
        q = self.q(params, public)
        conn = self.derived()
        try:
            rows = self._pages(conn, q, q.limit(100))
        finally:
            conn.close()
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

    def ep_live(self, params: Dict[str, str], public: bool) -> Any:
        """Visitors seen in the last 5 minutes (the dashboard's "now" indicator): one indexed count."""
        site_id = params.get("site", "")
        if self.cfg.site(site_id) is None:
            raise ApiError(404, "unknown site")
        ev = self._conn("events.db")
        if ev is None:
            return {"active": 0}
        try:
            n = ev.execute("SELECT COUNT(DISTINCT visitor_id) FROM raw_hits WHERE site_id = ? AND received_at > ? "
                           "AND site_ok = 1", (site_id, int(time.time() * 1000) - 5 * 60000)).fetchone()[0]
        finally:
            ev.close()
        return {"active": n}

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
        try:  # raw data is client-controlled: only pageviews' payloads are read, and only reasonable ones
            rows = ev.execute(
                "SELECT h.type, h.name, h.received_at, h.visitor_id, h.url, h.request_id, "
                "CASE WHEN h.type = 'pageview' AND length(h.data) <= 65536 THEN h.data END AS data FROM raw_hits h "
                "WHERE h.site_id = ? AND h.received_at > ? AND h.site_ok = 1 ORDER BY h.id DESC LIMIT ?",
                (site_id, since, 5000 if public else 20000)).fetchall()
        finally:
            ev.close()
        active = {r["visitor_id"] for r in rows if r["visitor_id"] and r["received_at"] > now - 5 * 60000}
        per_min: Dict[int, Dict[str, Any]] = {}
        pages: Counter = Counter()
        refs: Counter = Counter()
        page_vis: Dict[str, set] = defaultdict(set)
        ref_vis: Dict[str, set] = defaultdict(set)
        for r in rows:
            m = (now - r["received_at"]) // 60000
            b = per_min.setdefault(m, {"pageviews": 0, "visitors": set()})
            if r["visitor_id"]:
                b["visitors"].add(r["visitor_id"])
            if r["type"] == "pageview":
                b["pageviews"] += 1
                path = _path(r["url"])
                pages[path] += 1
                page_vis[path].add(r["visitor_id"])
                data = _json_or(r["data"], {})
                ref = data.get("ref") if isinstance(data, dict) else None
                host, page_host = _host(ref), _host(r["url"])
                if host and host != page_host:
                    refs[host[:253]] += 1
                    ref_vis[host[:253]].add(r["visitor_id"])
        series = [{"m": -m, "pageviews": per_min.get(m, {}).get("pageviews", 0),
                   "visitors": len(per_min.get(m, {}).get("visitors", ()))} for m in range(minutes - 1, -1, -1)]
        if public:  # only what the site's public metrics show, and only what several visitors did
            shown = set(self.cfg.site(site_id).settings["public_metrics"])
            pages = Counter({k: v for k, v in pages.items() if "pages" in shown
                             and len(page_vis[k]) >= PUBLIC_MIN_VISITORS})
            refs = Counter({k: v for k, v in refs.items() if "sources" in shown
                            and len(ref_vis[k]) >= PUBLIC_MIN_VISITORS})
        recent = [] if public else [
            {"type": r["type"], "name": r["name"], "at": r["received_at"], "path": _path(r["url"])}
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

    # -- paths ----------------------------------------------------------------------------

    def _sequences(self, conn: sqlite3.Connection, q: Query) -> Iterable[Tuple[List[str], str]]:
        """Each visit's pages in order (a page seen again at once, a reload, is one step), with its channel."""
        where, args = q.session_where("s")
        cur = conn.execute(
            "SELECT p.session_key, p.path, s.channel FROM pageviews p CROSS JOIN sessions s ON s.session_key = p.session_key "
            "WHERE p.site_id = ? AND p.day BETWEEN ? AND ? AND " + where + " ORDER BY p.session_key, p.ts, p.seq",
            [q.site_id, q.start, q.end] + args)
        key, seq, channel, n = None, [], "", 0
        for sk, path, ch in cur:
            if sk != key:
                if seq:
                    yield seq, channel
                n += 1
                if n > MAX_PATH_SESSIONS:
                    self._paths_truncated = True
                    return
                key, seq, channel = sk, [], ch or "(none)"
            p = path or "/"
            if not seq or seq[-1] != p:
                seq.append(p)
        if seq:
            yield seq, channel

    def ep_paths(self, params: Dict[str, str], public: bool) -> Any:
        """Paths through the site. mode=flow: the first `steps` pages of visits, `width` pages per step (the rest
        grouped), from their entry, from a page (start), or back from a page (end). mode=transitions: what comes
        before and after a page (page). Paths ending in * match every path that starts with the rest."""
        q = self.q(params, public)
        mode = params.get("mode", "flow")
        if mode not in ("flow", "transitions"):
            raise ApiError(400, "mode is flow or transitions")
        try:
            steps = max(2, min(8, int(params.get("steps", 4))))
            width = max(2, min(15, int(params.get("width", 6))))
        except ValueError:
            raise ApiError(400, "steps and width are whole numbers")

        def matcher(pattern: Optional[str]) -> Optional[Callable[[str], bool]]:
            if not pattern:
                return None
            pattern = pattern[:2000]
            if pattern.endswith("*") and "*" not in pattern[:-1]:
                prefix = pattern[:-1]
                return lambda p: p.startswith(prefix)
            return lambda p: p == pattern

        self._paths_truncated = False
        conn = self.derived()
        try:
            if mode == "transitions":
                page = params.get("page") or ""
                if not page:
                    raise ApiError(400, "missing page (a path, e.g. /pricing)")
                return dict(self._transitions(conn, q, matcher(page), page), truncated=self._paths_truncated)
            start, end = matcher(params.get("start")), matcher(params.get("end"))
            if start and end:
                raise ApiError(400, "a flow starts at a page or ends at one, not both")
            return dict(self._flow(conn, q, steps, width, start, end), start=params.get("start") or None,
                        end=params.get("end") or None, truncated=self._paths_truncated)
        finally:
            conn.close()

    def _flow(self, conn: sqlite3.Connection, q: Query, steps: int, width: int,
              start: Optional[Callable[[str], bool]], end: Optional[Callable[[str], bool]]) -> Dict[str, Any]:
        counts: List[Counter] = [Counter() for _ in range(steps)]
        ends: List[Counter] = [Counter() for _ in range(steps)]  # visits whose path stops at this step
        links: Counter = Counter()
        considered = 0
        for seq, _channel in self._sequences(conn, q):
            if start or end:
                hit = next((i for i, p in enumerate(seq) if (start or end)(p)), None)
                if hit is None:
                    continue
                seq = seq[hit:] if start else list(reversed(seq[:hit + 1]))
            considered += 1
            shown = seq[:steps]
            for i, p in enumerate(shown):
                counts[i][p] += 1
            for i in range(len(shown) - 1):
                links[(i, shown[i], shown[i + 1])] += 1
            if len(seq) <= steps:
                ends[len(seq) - 1][seq[-1]] += 1
        keep = [{p for p, _ in c.most_common(width)} for c in counts]
        other = "(other pages)"

        def name(i: int, p: str) -> str:
            return p if p in keep[i] else other

        out_steps = []
        for i in range(steps):
            nodes: Dict[str, Dict[str, Any]] = {}
            for p, n in counts[i].items():
                node = nodes.setdefault(name(i, p), {"id": "%d|%s" % (i, name(i, p)), "value": name(i, p),
                                                     "visits": 0, "ends": 0, "other": name(i, p) == other})
                node["visits"] += n
                node["ends"] += ends[i].get(p, 0)
            if other in nodes:
                nodes[other]["pages"] = sum(1 for p in counts[i] if p not in keep[i])
            out_steps.append({"nodes": sorted(nodes.values(), key=lambda x: (x["other"], -x["visits"], x["value"]))})
        merged: Counter = Counter()
        for (i, a, b), n in links.items():
            merged[("%d|%s" % (i, name(i, a)), "%d|%s" % (i + 1, name(i + 1, b)))] += n
        return {"mode": "flow", "direction": "backward" if end else "forward", "sessions": considered,
                "steps": [s for s in out_steps if s["nodes"]],
                "links": [{"source": a, "target": b, "visits": n} for (a, b), n in merged.most_common()]}

    def _transitions(self, conn: sqlite3.Connection, q: Query, match: Callable[[str], bool], page: str) -> Dict[str, Any]:
        before: Counter = Counter()
        after: Counter = Counter()
        entrances: Counter = Counter()
        views = exits = 0
        for seq, channel in self._sequences(conn, q):
            for i, p in enumerate(seq):
                if not match(p):
                    continue
                views += 1
                if i:
                    before[seq[i - 1]] += 1
                else:
                    entrances[channel] += 1
                if i < len(seq) - 1:
                    after[seq[i + 1]] += 1
                else:
                    exits += 1

        def top(c: Counter, n: int = 15) -> List[Dict[str, Any]]:
            rows = [{"value": k, "visits": v} for k, v in c.most_common(n)]
            rest = sum(c.values()) - sum(r["visits"] for r in rows)
            if rest:
                rows.append({"value": "(other pages)", "visits": rest, "other": True, "pages": len(c) - len(rows)})
            return rows

        return {"mode": "transitions", "page": page, "views": views, "entrances": top(entrances),
                "entrance_total": sum(entrances.values()), "previous": top(before), "next": top(after), "exits": exits}

    # -- search engines --------------------------------------------------------------------

    def ep_seo(self, params: Dict[str, str], public: bool) -> Any:
        """Google's and Bing's figures for a property (searchengines.py): clicks, impressions, CTR and average
        position per day, and per query, page, country or device (tab), for one query or page if asked; with the
        organic visits Tidesight saw from the engine."""
        from . import searchengines as se
        q = self.q(params, public)
        tab = params.get("tab") or "queries"
        if tab not in se.TABS:
            raise ApiError(400, "tab is queries, pages, countries or devices")
        query = (params.get("query") or "")[:se.MAX_TEXT] or None
        page = (params.get("page") or "")[:se.MAX_TEXT] or None
        creds = se.credentials(self.cfg.data_dir)
        conn = se.open_readonly(self.cfg.data_dir)
        out: Dict[str, Any] = {"from": q.start, "to": q.end, "tab": tab, "query": query, "page": page, "rows": [],
                               "series": [], "totals": None, "engine": None}
        try:
            engines = []
            for e in se.ENGINES:
                first, last = se.span(conn, q.site_id, e) if conn is not None else (None, None)
                engines.append({"id": e, "label": se.LABELS[e], "connected": e in creds,
                                "linked": bool(q.site.settings.get(se.SETTINGS[e])), "first_day": first, "last_day": last})
            out["engines"] = engines
            usable = [x["id"] for x in engines if x["last_day"]]
            engine = params.get("engine") if params.get("engine") in usable else (usable[0] if usable else None)
            if engine is None or conn is None:
                return out
            out["engine"] = engine
            args = (conn, q.site_id, engine)
            totals = se.totals(*args, q.start, q.end, query, page)
            days = se.series(*args, q.start, q.end, query, page)
            blank = {"clicks": 0, "impressions": 0, "ctr": None, "position": None}
            latest = next(x["last_day"] for x in engines if x["id"] == engine)
            gone = {"clicks": None, "impressions": None, "ctr": None, "position": None}  # not reported yet
            out["series"] = [dict(days.get(d) or (blank if d <= latest else gone), t=d) for d in _days(q.start, q.end)]
            cq = q.comparison()
            prev_q = cq or q.previous()
            prev = se.totals(*args, prev_q.start, prev_q.end, query, page)
            if cq is not None:
                cdays = se.series(*args, cq.start, cq.end, query, page)
                out["compare"] = {"from": cq.start, "to": cq.end, "totals": prev,
                                  "series": [dict(cdays.get(d) or blank, t=d) for d in _days(cq.start, cq.end)]}
            if (tab in ("countries", "devices") and (query or page)) or (engine == "bing" and tab == "pages" and query) \
                    or (engine == "bing" and tab == "queries" and page):
                rows, total_rows = [], 0  # the engine does not break these down this way
            else:
                rows, total_rows = se.rows(*args, q.start, q.end, tab, query, page, limit=q.limit(1000, 5000))
            if cq is not None and rows:
                found = {r["value"]: r for r in se.rows(*args, cq.start, cq.end, tab, query, page,
                                                         values=[r["value"] for r in rows])[0]}
                for r in rows:
                    r["cmp"] = {k: v for k, v in (found.get(r["value"]) or blank).items() if k != "value"}
            out.update(totals=totals, previous=prev, rows=rows, total_rows=total_rows,
                       coverage=se.coverage(*args, q.start, q.end, totals["clicks"]) if not (query or page) else None,
                       days_late=max(0, (datetime.strptime(q.end, "%Y-%m-%d") - datetime.strptime(
                           next(x["last_day"] for x in engines if x["id"] == engine), "%Y-%m-%d")).days))
        finally:
            if conn is not None:
                conn.close()
        self._seo_visits(q, out, engine, page, prev_q, params)
        return out

    def _seo_visits(self, q: Query, out: Dict[str, Any], engine: str, page: Optional[str], prev_q: Query,
                    params: Dict[str, str]) -> None:
        """Tidesight's side of the engine's figures: the organic visits from it (in total, and per landing page)."""
        from . import searchengines as se
        dconn = self._conn("derived.db")
        if dconn is None:
            return
        goal = q.goal()
        source = se.SOURCES[engine]
        try:
            def visits(qq: Query, path: Optional[str]) -> int:
                where, args = qq.session_where("s")
                sql = "SELECT COUNT(*) FROM sessions s WHERE " + where + " AND s.medium = 'organic' AND s.source = ?"
                args = args + [source]
                if path is not None:
                    sql, args = sql + " AND s.entry_path = ?", args + [path]
                return dconn.execute(sql, args).fetchone()[0]

            path = se.page_path(page) if page else None
            out["totals"]["visits"] = visits(q, path)
            out["previous"]["visits"] = visits(prev_q, path)
            if out["tab"] != "pages" or not out["rows"]:
                return
            paths = sorted({se.page_path(r["value"]) for r in out["rows"]})
            where, args = q.session_where("s")
            gargs: List[Any] = []
            conv = ", SUM(CASE WHEN %s THEN 1 ELSE 0 END) AS conversions" % goal_condition(goal, gargs) if goal else ""
            found: Dict[str, Dict[str, Any]] = {}
            for i in range(0, len(paths), 500):
                part = paths[i:i + 500]
                for r in dconn.execute(
                        "SELECT s.entry_path AS path, COUNT(*) AS visits, SUM(s.bounce) AS bounces, "
                        "SUM(s.engaged_ms) AS engaged_ms" + conv + " FROM sessions s WHERE " + where +
                        " AND s.medium = 'organic' AND s.source = ? AND s.entry_path IN (%s) GROUP BY s.entry_path"
                        % ",".join("?" * len(part)), gargs + args + [source] + part):
                    found[r["path"]] = dict(r)
            for r in out["rows"]:
                f = found.get(se.page_path(r["value"]))
                r["path"] = se.page_path(r["value"])
                r["visits"] = f["visits"] if f else 0
                r["bounce_rate"] = round(f["bounces"] / f["visits"], 4) if f and f["visits"] else None
                r["avg_engaged"] = round(f["engaged_ms"] / f["visits"]) if f and f["visits"] else None
                if goal:
                    r["conversions"] = f["conversions"] if f else 0
        finally:
            dconn.close()

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
                "SELECT DISTINCT s.visitor_key, s.day FROM sessions s WHERE s.site_id = ? AND s.day >= ? AND s.day <= ? "
                "AND s.site_ok = 1 AND s.is_bot = 0", (q.site_id, q.start, q.end)).fetchall()
        finally:
            conn.close()
        d_start = datetime.strptime(q.start, "%Y-%m-%d").date()

        def idx(day: str) -> int:
            return (datetime.strptime(day, "%Y-%m-%d").date() - d_start).days // size

        last = idx(q.end)  # a cohort has no figure for the weeks (or days) after the end of the period

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
                         "retention": [round(len(back.get((c, o), ())) / size_c, 4) for o in range(0, min(12, last - c + 1))]})
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
    def _decode(row: sqlite3.Row, budget: Optional[Budget] = None, limit: int = MAX_CHUNK_DECODED,
                rrweb: bool = True) -> Any:
        """A chunk's JSON. A stored chunk is anyone's data, so decoding is bounded: decompression (a gzip bomb)
        to `limit` bytes per chunk, and the bytes and objects of one API call (budget); rrweb data must also have
        rrweb's density (megabytes of tiny values, `[{},{},…]`, are no recording)."""
        budget = budget or Budget()
        data = row["payload"]
        limit = min(limit, budget.bytes)
        try:
            if row["encoding"] in ("gzip", "zlib"):
                d = zlib.decompressobj(16 + zlib.MAX_WBITS if row["encoding"] == "gzip" else zlib.MAX_WBITS)
                data = d.decompress(data, limit + 1)
                if len(data) > limit or d.unconsumed_tail:
                    budget.exhausted = True
                    return None
            elif len(data) > limit:
                budget.exhausted = True
                return None
            if rrweb and (data.count(b"{") + data.count(b"[") > len(data) // 8 + 8
                          or data.count(b",") > len(data) // 4 + 8):
                return None  # real rrweb events are far sparser in brackets and commas
            if not budget.take(data):
                return None
            return json.loads(data.decode("utf-8"))
        except (OSError, ValueError, zlib.error, RecursionError):
            return None

    def _mouse(self, q: Query, page: str, device: Optional[str]) -> Dict[str, Any]:
        start_ms = int(datetime.strptime(q.start, "%Y-%m-%d").replace(tzinfo=zone(q.tz)).timestamp() * 1000)
        end_ms = int((datetime.strptime(q.end, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=zone(q.tz)).timestamp() * 1000)
        rows = self._chunks("SELECT payload, encoding, url FROM chunks WHERE site_id = ? AND kind = 'mouse' AND site_ok = 1 "
                            "AND received_at BETWEEN ? AND ? ORDER BY id DESC LIMIT 2000", (q.site_id, start_ms, end_ms))
        cols, rows_n = 50, 100
        grid = [[0] * cols for _ in range(rows_n)]
        n = 0
        budget = Budget()
        for r in rows:
            if _path(r["url"]) != page:
                continue
            d = self._decode(r, budget, limit=MAX_MOUSE_DECODED, rrweb=False)
            if not isinstance(d, dict):
                continue
            try:
                pw, ph = float(d.get("pw") or 0), float(d.get("ph") or 0)
            except (TypeError, ValueError):
                continue
            if not (0 < pw < 1e6 and 0 < ph < 1e7):
                continue
            pts = d.get("pts")
            for pt in pts if isinstance(pts, list) else []:
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
                            "AND site_ok = 1 ORDER BY id DESC LIMIT 200", (q.site_id,))
        for r in rows:
            if _path(r["url"]) == page:  # a snapshot is [Meta, FullSnapshot]: nothing more is passed on
                return {"page": page, "at": r["received_at"], "url": r["url"],
                        "snapshot": _rrweb_events(self._decode(r))[:2] or None}
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
                            "session_id = ? AND kind = 'replay' AND site_ok = 1 ORDER BY client_ts, id LIMIT 5000",
                            (site_id, sid))
        events: List[Any] = []
        recs: Dict[Tuple[str, int], Any] = {}
        budget = Budget()
        for r in rows:
            meta = _json_or(r["meta"], {})
            if not isinstance(meta, dict):
                meta = {}
            k = (str(meta.get("rec") or r["page_id"] or ""), int(r["seq"] or 0))
            if k in recs:
                continue  # a retried chunk
            data = self._decode(r, budget)
            recs[k] = True
            events.extend(_rrweb_events(data))
        events.sort(key=lambda e: e["timestamp"])
        return {"session": key, "events": events, "chunks": len(recs), "truncated": budget.exhausted}

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
        budget = Budget(16 * 1024 * 1024, 200000)  # payloads are anyone's data: a bounded share of them is decoded
        if ev is not None and hits:
            try:
                ids = [h["hit_id"] for h in hits]
                for start in range(0, len(ids), 500):
                    chunk = ids[start:start + 500]
                    for r in ev.execute("SELECT id, CASE WHEN length(data) <= %d THEN data END AS data FROM raw_hits "
                                        "WHERE id IN (%s)" % (MAX_RAW_HIT, ",".join("?" * len(chunk))), chunk):
                        raw[r["id"]] = budget.loads(r["data"])
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
