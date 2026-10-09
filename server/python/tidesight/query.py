"""Report queries over derived.db: filters, comparisons and the breakdown engine of the read API.

A report has a scope: sessions (visits), pageviews or events. Session dimensions (source, country, device…)
work in every scope through the joined session; page dimensions (page, title) need the pageview scope and
the event dimension the event scope.

Filters are [{dim, op, values}] with op one of is, is_not, contains, not_contains, starts_with ("(none)"
stands for a missing value). In a session report, a filter on a page or an event keeps the visits that
viewed that page or sent that event (is_not and not_contains: the visits that never did); in a page or an
event report it keeps the matching rows themselves. A goal filter keeps the visits that completed (or not)
one of the site's goals. Every value is a bound parameter; every column comes from the lists below.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from .config import Config, SiteConfig

# Session dimensions: name -> sessions column (or expression over its columns).
SESSION_DIMS = {
    "source": "source", "medium": "medium", "campaign": "campaign", "term": "term", "content": "content",
    "channel": "channel", "referrer": "referrer_domain", "country": "country", "region": "region_name",
    "city": "city", "device": "device_type", "browser": "browser", "os": "os", "screen": "screen",
    "language": "language", "timezone": "timezone", "entry": "entry_path", "exit": "exit_path",
    "visitor": "visitor_key", "uid": "uid", "asn": "as_org", "brand": "device_brand", "model": "device_model",
    "browser_version": "browser || ' ' || COALESCE(browser_version, '')", "os_version": "os || ' ' || COALESCE(os_version, '')",
    "hour": "hour", "new": "is_new", "bot": "bot_name", "category": "source_category",
}
PAGE_DIMS = {"page": "path", "title": "title"}
EVENT_DIMS = {"event": "name"}
NUMERIC_DIMS = {"hour", "new"}
OPS = ("is", "is_not", "contains", "not_contains", "starts_with")
MAX_FILTERS = 20
MAX_VALUES = 200
# Bound parameters per statement stay under 999 (SQLite before 3.32): filter values, a row list, a chunk of
# comparison rows, and the few fixed parameters.
MAX_FILTER_VALUES = 300
MAX_ROW_VALUES = 300

DIM_LABELS = {
    "source": "Source", "medium": "Medium", "campaign": "Campaign", "term": "Term", "content": "Content",
    "channel": "Channel", "referrer": "Referrer", "country": "Country", "region": "Region", "city": "City",
    "device": "Device", "browser": "Browser", "os": "Operating system", "screen": "Screen size", "language": "Language",
    "timezone": "Time zone", "entry": "Entry page", "exit": "Exit page", "visitor": "Visitor", "uid": "User id",
    "asn": "Network", "brand": "Device brand", "model": "Device model", "browser_version": "Browser version",
    "os_version": "OS version", "hour": "Hour of day", "new": "New visitor", "bot": "Bot", "category": "Source category",
    "page": "Page", "title": "Page title", "event": "Event", "goal": "Goal",
}

# Metrics per scope: key -> SQL aggregate. Totals use the same expressions without GROUP BY.
METRICS = {
    "session": [
        ("visitors", "COUNT(DISTINCT s.visitor_key)"),
        ("sessions", "COUNT(*)"),
        ("pageviews", "COALESCE(SUM(s.pageviews), 0)"),
        ("views_per_session", "ROUND(1.0 * COALESCE(SUM(s.pageviews), 0) / COUNT(*), 2)"),
        ("bounce_rate", "ROUND(1.0 * SUM(s.bounce) / COUNT(*), 4)"),
        ("avg_duration", "CAST(AVG(s.duration_ms) AS INTEGER)"),
        ("avg_engaged", "CAST(AVG(s.engaged_ms) AS INTEGER)"),
        ("new_visitors", "COUNT(DISTINCT CASE WHEN s.is_new = 1 THEN s.visitor_key END)"),
    ],
    "page": [
        ("pageviews", "COUNT(*)"),
        ("visitors", "COUNT(DISTINCT s.visitor_key)"),
        ("sessions", "COUNT(DISTINCT s.session_key)"),
        ("avg_time", "CAST(AVG(p.duration_ms) AS INTEGER)"),
        ("avg_engaged", "CAST(AVG(p.engaged_ms) AS INTEGER)"),
        ("avg_scroll", "CAST(AVG(p.scroll_max) AS INTEGER)"),
        ("entries", "COALESCE(SUM(p.is_entry), 0)"),
        ("exits", "COALESCE(SUM(p.is_exit), 0)"),
        ("exit_rate", "ROUND(1.0 * SUM(p.is_exit) / COUNT(*), 4)"),
        ("bounce_rate", "ROUND(1.0 * SUM(CASE WHEN p.is_entry = 1 AND s.bounce = 1 THEN 1 ELSE 0 END) "
                        "/ NULLIF(SUM(p.is_entry), 0), 4)"),
    ],
    "event": [
        ("count", "COUNT(*)"),
        ("visitors", "COUNT(DISTINCT s.visitor_key)"),
        ("sessions", "COUNT(DISTINCT s.session_key)"),
        ("total_value", "SUM(e.value)"),
        ("avg_value", "ROUND(AVG(e.value), 2)"),
    ],
}
DEFAULT_SORT = {"session": "visitors", "page": "pageviews", "event": "count"}
METRIC_LABELS = {
    "visitors": "Visitors", "sessions": "Visits", "pageviews": "Pageviews", "views_per_session": "Views per visit",
    "bounce_rate": "Bounce rate", "avg_duration": "Visit duration (ms)", "avg_engaged": "Engaged time (ms)",
    "new_visitors": "New visitors", "avg_time": "Time on page (ms)", "avg_scroll": "Scroll depth (%)",
    "entries": "Entries", "exits": "Exits", "exit_rate": "Exit rate", "count": "Events", "total_value": "Total value",
    "avg_value": "Average value", "conversions": "Conversions", "conversion_rate": "Conversion rate",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Filter(NamedTuple):
    dim: str
    op: str
    values: Tuple[str, ...]


def _json(raw: Optional[str]) -> Any:
    try:
        return json.loads(raw) if raw else None
    except (ValueError, RecursionError):
        return None


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def glob_literal(pattern: str) -> str:
    """A pattern where only * is special, for SQLite GLOB (which also reads ? and [ as wildcards)."""
    return "".join("[" + c + "]" if c in "?[" else c for c in pattern)


def parse_filters(params: Dict[str, str], site: SiteConfig) -> List[Filter]:
    """The request's filters: `filters` (JSON) and the older f.<dim>=value parameters."""
    out: List[Filter] = []
    raw = params.get("filters")
    if raw:
        data = _json(raw)
        if not isinstance(data, list):
            raise ApiError(400, 'filters must be a JSON list like [{"dim":"country","op":"is","values":["FR"]}]')
        if len(data) > MAX_FILTERS:
            raise ApiError(400, "at most %d filters" % MAX_FILTERS)
        for item in data:
            if not isinstance(item, dict):
                raise ApiError(400, "each filter is an object {dim, op, values}")
            values = item.get("values", item.get("value"))
            if isinstance(values, (str, int)) and not isinstance(values, bool):
                values = [values]
            out.append(_check(str(item.get("dim", "")), str(item.get("op", "is")), values, site))
    for k, v in params.items():
        if k.startswith("f.") and v != "":
            name = k[2:]
            out.append(_check("page", "starts_with", [v], site) if name == "path_prefix" else _check(name, "is", [v], site))
    if len(out) > MAX_FILTERS:
        raise ApiError(400, "at most %d filters" % MAX_FILTERS)
    if sum(len(f.values) for f in out) > MAX_FILTER_VALUES:
        raise ApiError(400, "at most %d filter values in all" % MAX_FILTER_VALUES)
    return out


def _check(dim: str, op: str, values: Any, site: SiteConfig) -> Filter:
    if dim not in SESSION_DIMS and dim not in PAGE_DIMS and dim not in EVENT_DIMS and dim != "goal":
        raise ApiError(400, "unknown filter %r" % dim)
    if op not in OPS:
        raise ApiError(400, "unknown filter operator %r (is, is_not, contains, not_contains, starts_with)" % op)
    if not isinstance(values, list) or not values or len(values) > MAX_VALUES:
        raise ApiError(400, "a filter needs 1 to %d values" % MAX_VALUES)
    vals = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (str, int)):
            raise ApiError(400, "filter values are texts")
        v = str(v)
        if not v or len(v) > 500:
            raise ApiError(400, "filter values are 1 to 500 characters")
        vals.append(v)
    if dim in NUMERIC_DIMS or dim == "goal":
        if op not in ("is", "is_not"):
            raise ApiError(400, "%s filters take is or is_not" % dim)
    if dim in NUMERIC_DIMS and not all(re.match(r"^-?\d{1,6}$", v) or v == "(none)" for v in vals):
        raise ApiError(400, "%s filter values are numbers" % dim)
    if dim == "goal":
        known = {g["id"] for g in site.settings.get("goals") or []}
        unknown = [v for v in vals if v not in known]
        if unknown:
            raise ApiError(400, "unknown goal %r" % unknown[0])
    return Filter(dim, op, tuple(vals))


def _condition(expr: str, f: Filter, args: List[Any], numeric: bool = False) -> str:
    """The SQL condition of one filter on one column expression (a missing value counts as not matching)."""
    positive = f.op in ("is", "contains", "starts_with")
    if f.op in ("is", "is_not"):
        vals: List[Any] = [v for v in f.values if v != "(none)"]
        if numeric:
            vals = [int(v) for v in vals]
        parts = []
        if vals:
            parts.append("%s IN (%s)" % (expr, ",".join("?" * len(vals))))
            args.extend(vals)
        if "(none)" in f.values:
            parts.append("%s IS NULL" % expr)
        cond = "(" + " OR ".join(parts) + ")"
    else:
        tmpl = "%s" if f.op == "starts_with" else "%%%s"
        likes = []
        for v in f.values:
            likes.append("CAST(%s AS TEXT) LIKE ? ESCAPE '\\'" % expr)
            args.append((tmpl % _like(v)) + "%")
        cond = "(" + " OR ".join(likes) + ")"
    return cond if positive else "NOT COALESCE(%s, 0)" % cond


def goal_condition(goal: Dict[str, Any], args: List[Any], alias: str = "s") -> str:
    """EXISTS: the visit completed the goal (an event with that name, or a view of a matching page)."""
    if goal["type"] == "event":
        args.append(glob_literal(goal["match"]))
        return "EXISTS (SELECT 1 FROM events ge WHERE ge.session_key = %s.session_key AND ge.name GLOB ?)" % alias
    args.append(glob_literal(goal["match"]))
    return "EXISTS (SELECT 1 FROM pageviews gp WHERE gp.session_key = %s.session_key AND gp.path GLOB ?)" % alias


def _parse_day(value: Optional[str], what: str) -> Optional[date]:
    if not value:
        return None
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        raise ApiError(400, "%s must be YYYY-MM-DD" % what)
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ApiError(400, "%s is not a date" % what)


def _minus_year(d: date) -> date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:  # 29 February
        return d.replace(year=d.year - 1, day=28)


class Query:
    """Parsed request parameters plus SQL helpers bound to one site and date range."""

    def __init__(self, cfg: Config, params: Dict[str, str], site_id: str, public: bool = False,
                 today: Optional[str] = None) -> None:
        from .enrich.timeutil import local_day
        import time as _time

        self.cfg = cfg
        self.params = params
        self.public = public
        site = cfg.site(site_id)
        if site is None:
            raise ApiError(404, "unknown site %r" % site_id)
        self.site: SiteConfig = site
        self.site_id = site_id
        self.tz = site.timezone
        today = today or local_day(int(_time.time() * 1000), self.tz)
        end = _parse_day(params.get("to"), "to") or datetime.strptime(today, "%Y-%m-%d").date()
        start = _parse_day(params.get("from"), "from") or end - timedelta(days=29)
        if start > end:
            start, end = end, start
        self.start, self.end = start.isoformat(), end.isoformat()
        bots = params.get("bots", "")
        self.bots = "exclude" if public else ("include" if bots in ("1", "include") else "only" if bots == "only" else "exclude")
        self.filters = parse_filters(params, site)
        self.goals = {g["id"]: g for g in site.settings.get("goals") or []}

    # -- ranges ------------------------------------------------------------------

    def with_range(self, start: str, end: str) -> "Query":
        q = Query.__new__(Query)
        q.__dict__.update(self.__dict__)
        q.start, q.end = start, end
        return q

    def days(self) -> int:
        return (datetime.strptime(self.end, "%Y-%m-%d") - datetime.strptime(self.start, "%Y-%m-%d")).days + 1

    def previous(self) -> "Query":
        n = self.days()
        d0 = datetime.strptime(self.start, "%Y-%m-%d").date()
        return self.with_range((d0 - timedelta(days=n)).isoformat(), (d0 - timedelta(days=1)).isoformat())

    def comparison(self) -> Optional["Query"]:
        """The period to compare with: compare=previous (the same number of days before), year (a year
        before), custom (compare_from, compare_to); None without compare."""
        mode = self.params.get("compare", "")
        if mode in ("", "off", "none"):
            return None
        if mode == "previous":
            return self.previous()
        if mode == "year":
            d0, d1 = (datetime.strptime(d, "%Y-%m-%d").date() for d in (self.start, self.end))
            return self.with_range(_minus_year(d0).isoformat(), _minus_year(d1).isoformat())
        if mode == "custom":
            c0 = _parse_day(self.params.get("compare_from"), "compare_from")
            c1 = _parse_day(self.params.get("compare_to"), "compare_to")
            if not c0 or not c1:
                raise ApiError(400, "compare=custom needs compare_from and compare_to")
            if c0 > c1:
                c0, c1 = c1, c0
            return self.with_range(c0.isoformat(), c1.isoformat())
        raise ApiError(400, "compare is previous, year or custom")

    def limit(self, default: int = 50, maximum: int = 1000) -> int:
        try:
            return max(1, min(maximum, int(self.params.get("limit", default))))
        except ValueError:
            return default

    def offset(self) -> int:
        try:
            return max(0, min(10 ** 7, int(self.params.get("offset", 0))))
        except ValueError:
            return 0

    def goal(self, name: str = "goal") -> Optional[Dict[str, Any]]:
        gid = self.params.get(name)
        if not gid:
            return None
        if gid not in self.goals:
            raise ApiError(400, "unknown goal %r" % gid)
        return self.goals[gid]

    # -- SQL ---------------------------------------------------------------------

    @staticmethod
    def _col(name: str, prefix: str) -> str:
        expr = SESSION_DIMS[name]
        if re.match(r"^\w+$", expr):
            return prefix + expr
        return re.sub(r"\b(browser|browser_version|os|os_version)\b", lambda m: prefix + m.group(1), expr)

    def session_where(self, alias: str = "s", scope: str = "session") -> Tuple[str, List[Any]]:
        """Conditions on the joined session. In a page or an event scope, the filters on that scope's own
        dimensions are left to row_where (they select rows, not visits)."""
        if not alias:
            raise ValueError("session_where needs a table alias")  # EXISTS subqueries reference it
        a = alias + "."
        where = ["%ssite_id = ?" % a, "%sday BETWEEN ? AND ?" % a, "%ssite_ok = 1" % a, "%sinternal = 0" % a]
        args: List[Any] = [self.site_id, self.start, self.end]
        if self.bots == "exclude":
            where.append("%sis_bot = 0" % a)
        elif self.bots == "only":
            where.append("%sis_bot = 1" % a)
        for f in self.filters:
            if f.dim in PAGE_DIMS:
                if scope == "page":
                    continue
                sub_args: List[Any] = []
                cond = _condition("fp." + PAGE_DIMS[f.dim], Filter(f.dim, _positive(f.op), f.values), sub_args)
                where.append("%sEXISTS (SELECT 1 FROM pageviews fp WHERE fp.session_key = %ssession_key AND %s)"
                             % ("" if f.op in ("is", "contains", "starts_with") else "NOT ", a, cond))
                args.extend(sub_args)
            elif f.dim in EVENT_DIMS:
                if scope == "event":
                    continue
                sub_args = []
                cond = _condition("fe.name", Filter(f.dim, _positive(f.op), f.values), sub_args)
                where.append("%sEXISTS (SELECT 1 FROM events fe WHERE fe.session_key = %ssession_key AND %s)"
                             % ("" if f.op in ("is", "contains", "starts_with") else "NOT ", a, cond))
                args.extend(sub_args)
            elif f.dim == "goal":
                parts = []
                for gid in f.values:
                    parts.append(goal_condition(self.goals[gid], args, alias))
                cond = "(" + " OR ".join(parts) + ")"
                where.append(cond if f.op == "is" else "NOT " + cond)
            else:
                where.append(_condition("(%s)" % self._col(f.dim, a), f, args, numeric=f.dim in NUMERIC_DIMS))
        return " AND ".join(where), args

    def row_where(self, scope: str, alias: str) -> Tuple[str, List[Any]]:
        """Conditions on the rows of a page (alias p) or event (alias e) report: its own filters."""
        dims = PAGE_DIMS if scope == "page" else EVENT_DIMS if scope == "event" else {}
        conds: List[str] = []
        args: List[Any] = []
        for f in self.filters:
            if f.dim in dims:
                conds.append(_condition("%s.%s" % (alias, dims[f.dim]), f, args))
        return (" AND ".join(conds) if conds else "1"), args


def _positive(op: str) -> str:
    return {"is_not": "is", "not_contains": "contains"}.get(op, op)


# ---------------------------------------------------------------------------
# The breakdown engine
# ---------------------------------------------------------------------------

def scope_of(dims: Sequence[Optional[str]]) -> str:
    present = [d for d in dims if d]
    for d in present:
        if d not in SESSION_DIMS and d not in PAGE_DIMS and d not in EVENT_DIMS:
            raise ApiError(400, "unknown dimension %r" % d)
    page = any(d in PAGE_DIMS for d in present)
    event = any(d in EVENT_DIMS for d in present)
    if page and event:
        raise ApiError(400, "a page dimension and the event dimension cannot be combined")
    return "page" if page else "event" if event else "session"


def dim_expr(dim: str, scope: str) -> str:
    if dim in PAGE_DIMS:
        return "p." + PAGE_DIMS[dim]
    if dim in EVENT_DIMS:
        return "e." + EVENT_DIMS[dim]
    return "(%s)" % Query._col(dim, "s.")


def _from(q: Query, scope: str) -> Tuple[str, List[Any]]:
    """FROM … WHERE … of a scope, with every filter applied."""
    where, args = q.session_where("s", scope)
    if scope == "session":
        return "FROM sessions s WHERE " + where, args
    alias, table = ("p", "pageviews") if scope == "page" else ("e", "events")
    rw, rargs = q.row_where(scope, alias)
    return ("FROM %s %s CROSS JOIN sessions s ON s.session_key = %s.session_key WHERE %s.site_id = ? AND %s.day BETWEEN ? "
            "AND ? AND %s AND %s" % (table, alias, alias, alias, alias, rw, where),
            [q.site_id, q.start, q.end] + rargs + args)


def _metric_sql(scope: str, goal: Optional[Dict[str, Any]], args: List[Any]) -> Tuple[List[str], List[str]]:
    keys, parts = [], []
    for key, sql in METRICS[scope]:
        keys.append(key)
        parts.append("%s AS %s" % (sql, key))
    if goal is not None:
        cond = goal_condition(goal, args)
        if scope == "session":
            parts.append("SUM(CASE WHEN %s THEN 1 ELSE 0 END) AS conversions" % cond)
        else:
            parts.append("COUNT(DISTINCT CASE WHEN %s THEN s.session_key END) AS conversions" % cond)
        keys.append("conversions")
    return keys, parts


def _with_rates(row: Dict[str, Any], scope: str) -> Dict[str, Any]:
    if "conversions" in row:
        base = row.get("sessions") or 0
        row["conversion_rate"] = round(row["conversions"] / base, 4) if base else None
    return row


def breakdown(conn: Any, q: Query, dim: str, dim2: Optional[str] = None, sort: Optional[str] = None,
              desc: bool = True, limit: int = 10, offset: int = 0, search: Optional[str] = None,
              values_in: Optional[Sequence[str]] = None, goal: Optional[Dict[str, Any]] = None,
              restrict: Optional[Sequence[Tuple[str, Optional[str]]]] = None,
              with_totals: bool = True) -> Dict[str, Any]:
    """Rows of one dimension (or two), sorted, paged and searched, with totals over every matching row."""
    if dim2 == dim:
        dim2 = None
    scope = scope_of([dim, dim2])
    v1 = dim_expr(dim, scope)
    v2 = dim_expr(dim2, scope) if dim2 else None
    from_sql, from_args = _from(q, scope)
    metric_args: List[Any] = []
    keys, metric_parts = _metric_sql(scope, goal, metric_args)
    extra, extra_args = [], []
    if search:
        extra.append("CAST(%s AS TEXT) LIKE ? ESCAPE '\\'" % v1)
        extra_args.append("%" + _like(search) + "%")
    if values_in is not None:
        vals = [v for v in values_in if v != "(none)"]
        parts = []
        if vals:
            parts.append("%s IN (%s)" % (v1, ",".join("?" * len(vals))))
            extra_args.extend(vals)
        if "(none)" in values_in:
            parts.append("%s IS NULL" % v1)
        extra.append("(" + (" OR ".join(parts) or "0") + ")")
    if restrict:  # the rows of another period: only these values
        for expr, wanted in ((v1, sorted({r[0] for r in restrict})), (v2, sorted({r[1] for r in restrict if r[1] is not None}))):
            if expr is None or not wanted:
                continue
            parts = []
            vals = [v for v in wanted if v != "(none)"]
            if vals:
                parts.append("CAST(%s AS TEXT) IN (%s)" % (expr, ",".join("?" * len(vals))))
                extra_args.extend(vals)
            if "(none)" in wanted:
                parts.append("%s IS NULL" % expr)
            extra.append("(" + (" OR ".join(parts) or "0") + ")")
    cond = (" AND " + " AND ".join(extra)) if extra else ""
    group = "1, 2" if dim2 else "1"
    select = "COALESCE(CAST(%s AS TEXT), '(none)') AS value" % v1
    if v2:
        select += ", COALESCE(CAST(%s AS TEXT), '(none)') AS value2" % v2
    if "city" in (dim, dim2):
        metric_parts = metric_parts + ["MIN(s.country) AS country"]
    if sort == "conversion_rate" and goal is not None:
        order = "1.0 * conversions / NULLIF(sessions, 0)"
    else:
        order = sort if sort in keys + ["value"] + (["value2"] if dim2 else []) else DEFAULT_SORT[scope]
    direction = "DESC" if desc else "ASC"
    sql = ("SELECT %s, %s %s%s GROUP BY %s ORDER BY %s %s, value ASC LIMIT ? OFFSET ?"
           % (select, ", ".join(metric_parts), from_sql, cond, group, order, direction))
    rows = [_with_rates(dict(r), scope) for r in conn.execute(sql, metric_args + from_args + extra_args + [limit, offset])]
    out: Dict[str, Any] = {"dim": dim, "dim2": dim2, "scope": scope, "metrics": keys + (["conversion_rate"] if goal else []),
                           "rows": rows}
    if with_totals:
        total = conn.execute("SELECT %s %s%s" % (", ".join(p for p in metric_parts if not p.startswith("MIN(s.country)")),
                                                 from_sql, cond),
                             metric_args + from_args + extra_args).fetchone()
        out["totals"] = _with_rates(dict(total), scope) if total is not None else {}
        out["total_rows"] = conn.execute("SELECT COUNT(*) FROM (SELECT 1 %s%s GROUP BY %s)" % (
            from_sql, cond, ", ".join(x for x in ([v1] + ([v2] if v2 else [])))), from_args + extra_args).fetchone()[0]
    return out


def compare_rows(conn: Any, q: Query, cq: Query, result: Dict[str, Any], dim: str, dim2: Optional[str],
                 goal: Optional[Dict[str, Any]], search: Optional[str] = None,
                 values_in: Optional[Sequence[str]] = None) -> None:
    """Add each row's metrics in the comparison period (`cmp`), and the comparison totals."""
    rows = result["rows"]
    if rows:
        keys = [(r["value"], r.get("value2")) for r in rows]
        found: Dict[Tuple[str, Optional[str]], Dict[str, Any]] = {}
        for i in range(0, len(keys), MAX_ROW_VALUES // 2):  # in chunks: bounded parameters per statement
            chunk = keys[i:i + MAX_ROW_VALUES // 2]
            other = breakdown(conn, cq, dim, dim2, limit=len(chunk) * (len(chunk) if dim2 else 1), restrict=chunk,
                              goal=goal, with_totals=False)
            wanted = set(chunk)
            for r in other["rows"]:
                key = (r["value"], r.get("value2"))
                if key in wanted:
                    found[key] = r
        metrics = result["metrics"]
        for r in rows:
            o = found.get((r["value"], r.get("value2")))
            r["cmp"] = {m: (o or {}).get(m) for m in metrics} if o else {m: None for m in metrics}
    totals = breakdown(conn, cq, dim, dim2, limit=1, goal=goal, search=search, values_in=values_in)
    result["cmp_totals"] = totals.get("totals", {})
    result["compare"] = {"from": cq.start, "to": cq.end}


# ---------------------------------------------------------------------------
# Time series
# ---------------------------------------------------------------------------

def interval_for(q: Query, wanted: Optional[str]) -> str:
    if wanted in ("hour", "day", "week", "month"):
        return wanted
    n = q.days()
    return "hour" if n == 1 else "day" if n <= 92 else "week" if n <= 731 else "month"


def bucket_sql(interval: str, day_col: str, hour_col: str) -> str:
    return {"hour": hour_col, "day": day_col,
            "week": "date(%s, 'weekday 0', '-6 days')" % day_col,  # the Monday of the week
            "month": "substr(%s, 1, 7) || '-01'" % day_col}[interval]


def bucket_keys(q: Query, interval: str) -> List[Any]:
    if interval == "hour":
        return list(range(24))
    d0 = datetime.strptime(q.start, "%Y-%m-%d").date()
    d1 = datetime.strptime(q.end, "%Y-%m-%d").date()
    if interval == "day":
        return [(d0 + timedelta(days=i)).isoformat() for i in range(min(3700, (d1 - d0).days + 1))]
    if interval == "week":
        k = d0 - timedelta(days=d0.weekday())
        out = []
        while k <= d1 and len(out) < 600:
            out.append(k.isoformat())
            k += timedelta(days=7)
        return out
    k = d0.replace(day=1)
    out = []
    while k <= d1 and len(out) < 240:
        out.append(k.isoformat())
        k = (k.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def series(conn: Any, q: Query, interval: str, goal: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Visitors, visits, pageviews, bounces and durations per hour of day, day, week (from its Monday) or month."""
    bucket = bucket_sql(interval, "s.day", "s.hour")
    where, args = q.session_where("s")
    goal_args: List[Any] = []
    conv = ""
    if goal is not None:
        conv = ", SUM(CASE WHEN %s THEN 1 ELSE 0 END) AS conversions" % goal_condition(goal, goal_args)
    rows = {r["t"]: dict(r) for r in conn.execute(
        "SELECT " + bucket + " AS t, COUNT(DISTINCT s.visitor_key) AS visitors, COUNT(*) AS sessions, "
        "COALESCE(SUM(s.pageviews), 0) AS pageviews, COALESCE(SUM(s.bounce), 0) AS bounces, "
        "AVG(s.duration_ms) AS avg_duration, AVG(s.engaged_ms) AS avg_engaged" + conv +
        " FROM sessions s WHERE " + where + " GROUP BY t ORDER BY t", goal_args + args)}
    out = []
    for k in bucket_keys(q, interval):
        r = rows.get(k) or {"t": k, "visitors": 0, "sessions": 0, "pageviews": 0, "bounces": 0, "avg_duration": None,
                            "avg_engaged": None, **({"conversions": 0} if goal else {})}
        for key in ("avg_duration", "avg_engaged"):
            r[key] = int(r[key]) if r.get(key) is not None else None
        r["bounce_rate"] = round(r["bounces"] / r["sessions"], 4) if r["sessions"] else None
        r["views_per_session"] = round(r["pageviews"] / r["sessions"], 2) if r["sessions"] else None
        if goal:
            r["conversion_rate"] = round(r["conversions"] / r["sessions"], 4) if r["sessions"] else None
        out.append(r)
    return out


def totals(conn: Any, q: Query, goal: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    where, args = q.session_where("s")
    goal_args: List[Any] = []
    conv = ""
    if goal is not None:
        conv = ", SUM(CASE WHEN %s THEN 1 ELSE 0 END) AS conversions" % goal_condition(goal, goal_args)
    r = conn.execute(
        "SELECT COUNT(DISTINCT s.visitor_key) AS visitors, COUNT(*) AS sessions, COALESCE(SUM(s.pageviews), 0) AS pageviews, "
        "COALESCE(SUM(s.bounce), 0) AS bounces, AVG(s.duration_ms) AS avg_duration, AVG(s.engaged_ms) AS avg_engaged, "
        "COALESCE(SUM(s.events), 0) AS events, COUNT(DISTINCT CASE WHEN s.is_new = 1 THEN s.visitor_key END) AS new_visitors"
        + conv + " FROM sessions s WHERE " + where, goal_args + args).fetchone()
    out = dict(r)
    out["bounce_rate"] = round(out["bounces"] / out["sessions"], 4) if out["sessions"] else None
    out["views_per_session"] = round(out["pageviews"] / out["sessions"], 2) if out["sessions"] else None
    for k in ("avg_duration", "avg_engaged"):
        out[k] = int(out[k]) if out[k] is not None else None
    if goal is not None:
        out["conversions"] = out["conversions"] or 0
        out["conversion_rate"] = round(out["conversions"] / out["sessions"], 4) if out["sessions"] else None
    return out


def value_series(conn: Any, q: Query, dim: str, values: Sequence[str], metric: str, interval: str) -> Dict[str, Any]:
    """One metric over time for a few values of a dimension (the report's trend chart)."""
    scope = scope_of([dim])
    allowed = [k for k, _ in METRICS[scope]]
    if metric not in allowed:
        metric = DEFAULT_SORT[scope]
    expr = dim_expr(dim, scope)
    from_sql, from_args = _from(q, scope)
    day_col, hour_col = {"session": ("s.day", "s.hour"), "page": ("p.day", "p.hour"), "event": ("e.day", "s.hour")}[scope]
    bucket = bucket_sql(interval, day_col, hour_col)
    metric_sql = dict(METRICS[scope])[metric]
    vals = [v for v in values if v != "(none)"][:10]
    parts, args = [], []
    if vals:
        parts.append("CAST(%s AS TEXT) IN (%s)" % (expr, ",".join("?" * len(vals))))
        args.extend(vals)
    if "(none)" in values:
        parts.append("%s IS NULL" % expr)
    if not parts:
        return {"keys": bucket_keys(q, interval), "series": []}
    rows = conn.execute("SELECT COALESCE(CAST(%s AS TEXT), '(none)') AS value, %s AS t, %s AS m %s AND (%s) GROUP BY 1, 2"
                        % (expr, bucket, metric_sql, from_sql, " OR ".join(parts)), from_args + args).fetchall()
    keys = bucket_keys(q, interval)
    data: Dict[str, Dict[Any, Any]] = {}
    for r in rows:
        data.setdefault(r["value"], {})[r["t"]] = r["m"]
    return {"keys": keys, "metric": metric,
            "series": [{"value": v, "values": [data.get(v, {}).get(k, 0) for k in keys]} for v in values[:10]]}
