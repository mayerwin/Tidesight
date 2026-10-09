"""Sessions and pageviews, rebuilt from the enriched hits of each affected session.

A session is keyed by the tracker's session id (or, for no-JS pixel hits,
by the cookieless hash with the idle timeout). Every rebuild recomputes the
whole session from its hits, so late or retried events simply update it.

Anyone can send hits, so a rebuild must stay cheap whatever a session holds: its
cost is linear in the session's hits, and a session with more than
MAX_SESSION_HITS hits (no person's visit: a script) is built once from its first
hits, marked as a bot, and not rebuilt again.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional

from ..config import Config
from . import bots, sources
from .timeutil import local_day, local_hour
from .util import url_parts

SESSIONS_VERSION = 2

MAX_SESSION_HITS = 20000

ENGAGED_MS = 10000  # GA4: an engaged session lasts 10 s or more, or has 2+ pageviews

GEO_PREFERENCE = ("maxmind", "dbip", "ipinfo")

SESSION_COLUMNS = (
    "session_key", "site_id", "site_ok", "visitor_key", "vid", "uid", "started_at", "ended_at", "day", "hour", "duration_ms",
    "engaged_ms", "pageviews", "events", "hits", "engaged", "bounce", "is_new", "session_number", "entry_path",
    "entry_url", "entry_title", "exit_path", "source", "medium", "campaign", "term", "content", "channel", "referrer",
    "referrer_domain", "source_category", "click_id_type", "country", "region", "region_name", "city", "lat", "lon",
    "geo_tz", "asn", "as_org", "is_hosting", "browser", "browser_version", "os", "os_version", "device_type",
    "device_brand", "device_model", "screen", "viewport", "language", "timezone", "is_bot", "bot_kind", "bot_name",
    "bot_score", "bot_reasons", "internal", "client_ip", "ck", "fp", "has_js", "has_replay", "errors", "rage_clicks",
    "dead_clicks",
)
_INSERT_SESSION = "INSERT OR REPLACE INTO sessions(%s) VALUES (%s)" % (
    ", ".join(SESSION_COLUMNS), ", ".join("?" * len(SESSION_COLUMNS)))


def _extra(row: sqlite3.Row) -> Dict[str, Any]:
    try:
        return json.loads(row["extra"]) if row["extra"] else {}
    except ValueError:
        return {}


def best_geo(conn: sqlite3.Connection, ip: Optional[str]) -> Dict[str, Any]:
    """Merge the providers' answers for an IP: the preferred provider first, gaps filled by others."""
    if not ip:
        return {}
    rows = [dict(r) for r in conn.execute("SELECT * FROM ip_geo WHERE ip = ? AND provider != ''", (ip,))]
    if not rows:
        return {}
    order = {p: i for i, p in enumerate(GEO_PREFERENCE)}
    rows.sort(key=lambda r: order.get(r["provider"], 99))
    out: Dict[str, Any] = {}
    for r in rows:
        for k, v in r.items():
            if v is not None and out.get(k) is None:
                out[k] = v
    # City-level fields must come from one provider, or the city may not match the region.
    city_src = next((r for r in rows if r.get("city")), None)
    if city_src:
        for k in ("country", "region", "region_name", "city", "lat", "lon", "tz", "postal"):
            out[k] = city_src.get(k)
    return out


def _client_signals(fp_extra: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    b = fp_extra.get("bot") if isinstance(fp_extra.get("bot"), dict) else None
    if not b:
        return None
    return {"webdriver": bool(b.get("wd")), "plugins": b.get("pl"), "languages": b.get("lg"),
            "outer_zero": bool(b.get("oz")), "automation": b.get("au") or [], "headless_ua": bool(b.get("hl")),
            "chrome_obj": None if b.get("co") is None else bool(b.get("co"))}


def rebuild(conn: sqlite3.Connection, cfg: Config, keys: Iterable[str], reader: Any,
            rconn: Optional[sqlite3.Connection], enricher: Any, stats: Optional[Dict[str, Any]] = None) -> int:
    n = 0
    for key in sorted(keys):
        conn.execute("SAVEPOINT session")
        try:
            rebuild_one(conn, cfg, key, reader, rconn, enricher)
            conn.execute("RELEASE session")
            n += 1
        except Exception as exc:  # one session's bad data must not stop the others
            conn.execute("ROLLBACK TO session")
            conn.execute("RELEASE session")
            enricher.failed(conn, "session", exc, stats if stats is not None else {}, session_key=key)
    return n


def rebuild_one(conn: sqlite3.Connection, cfg: Config, key: str, reader: Any,
                rconn: Optional[sqlite3.Connection], enricher: Any) -> None:
    old = conn.execute("SELECT site_id, day, hits FROM sessions WHERE session_key = ?", (key,)).fetchone()
    if old is not None and (old["hits"] or 0) > MAX_SESSION_HITS:
        return  # a flood, built once (as a bot) when it went over the limit: not rebuilt for every hit it gets
    rows = conn.execute("SELECT * FROM hits WHERE session_key = ? AND dup = 0 ORDER BY ts, hit_id LIMIT ?",
                        (key, MAX_SESSION_HITS + 1)).fetchall()
    total = len(rows)
    if total > MAX_SESSION_HITS:
        rows = rows[:MAX_SESSION_HITS]
        total = conn.execute("SELECT COUNT(*) FROM hits WHERE session_key = ? AND dup = 0", (key,)).fetchone()[0]
    if old is not None:
        conn.execute("INSERT OR IGNORE INTO dirty_days(site_id, day) VALUES (?, ?)", (old["site_id"], old["day"]))
    conn.execute("DELETE FROM pageviews WHERE session_key = ?", (key,))
    if not rows:
        conn.execute("DELETE FROM sessions WHERE session_key = ?", (key,))
        return
    site_id = rows[0]["site_id"]
    site = cfg.site_or_default(site_id)
    tz = site.timezone
    pvs = [r for r in rows if r["type"] == "pageview"]
    first = pvs[0] if pvs else rows[0]
    first_extra = _extra(first)
    ctx = first_extra.get("ctx") or {}
    req = conn.execute("SELECT * FROM requests WHERE request_id = ?", (first["request_id"],)).fetchone()
    req = dict(req) if req is not None else {}
    headers = reader.headers(first["request_id"])

    started, ended = rows[0]["ts"], max(r["ts"] for r in rows)
    engaged_ms = sum(r["ms"] or 0 for r in rows if r["type"] == "engagement")
    n_pv = len(pvs)
    n_events = sum(1 for r in rows if r["type"] == "event")
    engaged = 1 if engaged_ms >= ENGAGED_MS or n_pv >= 2 else 0
    vids = Counter(r["vid"] for r in rows if r["vid"])
    vid = vids.most_common(1)[0][0] if vids else None
    uid = next((r["uid"] for r in reversed(rows) if r["uid"]), None)

    # Traffic source of the landing page.
    entry_url = first["url"]
    ref = first_extra.get("ref") or None
    hosts = list(site.hosts())
    src = sources.classify(entry_url, ref, hosts)
    if src.get("source") is None:  # internal referrer, no campaign tags: a session resumed mid-visit is direct
        src.update({"source": "(direct)", "medium": "(none)", "campaign": "(direct)", "channel": "Direct"})

    geo = best_geo(conn, req.get("client_ip"))
    hints = {k: v for k, v in headers.items() if k.startswith("sec-ch-ua")}
    ua_string = ctx.get("ua") or headers.get("user-agent")
    js_hints = dict(ctx["uad"]) if isinstance(ctx.get("uad"), dict) else {}
    if ctx.get("tp") is not None:  # also without userAgentData: iPadOS in desktop mode is only visible this way
        js_hints["max_touch_points"] = ctx.get("tp")
    js_hints = js_hints or None
    ua_id = enricher.ua_id(conn, ua_string, hints, js_hints) if (ua_string or hints or js_hints) else req.get("ua_id")
    device = conn.execute("SELECT * FROM ua WHERE id = ?", (ua_id,)).fetchone() if ua_id else None
    device = dict(device) if device is not None else {}

    fp_row = next((r for r in rows if r["type"] == "fp"), None)
    fp_extra = _extra(fp_row) if fp_row is not None else {}
    endpoints = reader.endpoints(r["request_id"] for r in rows)
    has_js = 1 if "collect" in endpoints else 0
    verdict = bots.classify(ua_string, headers, _client_signals(fp_extra), geo.get("asn"), geo.get("as_org"),
                            bool(has_js), enricher.crawlers or None)
    if total > MAX_SESSION_HITS:
        verdict = dict(verdict, is_bot=True, bot_kind=verdict.get("bot_kind") or bots.AUTOMATION, score=1.0,
                       reasons=list(verdict.get("reasons") or []) + ["too_many_hits"])

    has_replay = 0
    if rconn is not None and "/" in key and not key.split("/", 1)[1].startswith(("px-", "hit-")):
        try:
            has_replay = 1 if rconn.execute(
                "SELECT 1 FROM chunks WHERE site_id = ? AND session_id = ? AND kind = 'replay' LIMIT 1",
                (site_id, key.split("/", 1)[1])).fetchone() else 0
        except sqlite3.Error:
            has_replay = 0

    sn = first["sn"]
    is_new = 1 if first_extra.get("new") == 1 or sn == 1 else (0 if sn else None)
    vmap = conn.execute("SELECT visitor_key FROM vid_map WHERE site_id = ? AND vid = ?", (site_id, vid)).fetchone() if vid else None
    if vmap is not None:
        visitor_key = vmap["visitor_key"]
    elif vid:
        visitor_key = "%s/%s" % (site_id, vid)
    else:
        visitor_key = "%s/ck-%s" % (site_id, req.get("ck") or key)
    day = local_day(started, tz)
    screen = "%sx%s" % (ctx["sw"], ctx["sh"]) if ctx.get("sw") and ctx.get("sh") else None
    viewport = "%sx%s" % (ctx["vw"], ctx["vh"]) if ctx.get("vw") and ctx.get("vh") else None
    _h, entry_path, _q, _f = url_parts(entry_url)
    exit_path = url_parts(pvs[-1]["url"])[1] if pvs else entry_path
    values = {
        "session_key": key, "site_id": site_id, "site_ok": min(r["site_ok"] for r in rows), "visitor_key": visitor_key,
        "vid": vid, "uid": uid, "started_at": started, "ended_at": ended, "day": day,
        "hour": local_hour(started, tz), "duration_ms": ended - started,
        "engaged_ms": engaged_ms, "pageviews": n_pv, "events": n_events, "hits": total, "engaged": engaged,
        "bounce": 1 - engaged, "is_new": is_new, "session_number": sn, "entry_path": entry_path,
        "entry_url": entry_url, "entry_title": first_extra.get("title"), "exit_path": exit_path,
        "source": src.get("source"), "medium": src.get("medium"), "campaign": src.get("campaign"),
        "term": src.get("term"), "content": src.get("content"), "channel": src.get("channel"), "referrer": ref,
        "referrer_domain": src.get("referrer_domain"), "source_category": src.get("source_category"),
        "click_id_type": src.get("click_id_type"), "country": geo.get("country"), "region": geo.get("region"),
        "region_name": geo.get("region_name"), "city": geo.get("city"), "lat": geo.get("lat"), "lon": geo.get("lon"),
        "geo_tz": geo.get("tz"), "asn": geo.get("asn"), "as_org": geo.get("as_org"),
        "is_hosting": 1 if verdict.get("is_hosting") else 0, "browser": device.get("browser"),
        "browser_version": device.get("browser_version"), "os": device.get("os"), "os_version": device.get("os_version"),
        "device_type": device.get("device_type"), "device_brand": device.get("device_brand"),
        "device_model": device.get("device_model"), "screen": screen, "viewport": viewport,
        "language": ctx.get("lang") or (headers.get("accept-language") or "").split(",")[0].split(";")[0] or None,
        "timezone": ctx.get("tz"), "is_bot": 1 if verdict.get("is_bot") else 0, "bot_kind": verdict.get("bot_kind"),
        "bot_name": verdict.get("bot_name"), "bot_score": verdict.get("score"),
        "bot_reasons": json.dumps(verdict.get("reasons") or []), "internal": req.get("internal") or 0,
        "client_ip": req.get("client_ip"), "ck": req.get("ck"), "fp": fp_row["fp"] if fp_row is not None else None,
        "has_js": has_js, "has_replay": has_replay,
        "errors": conn.execute("SELECT COUNT(*) FROM errors WHERE session_key = ?", (key,)).fetchone()[0],
        "rage_clicks": sum(1 for r in rows if r["type"] == "rage"),
        "dead_clicks": sum(1 for r in rows if r["type"] == "dead"),
    }
    conn.execute(_INSERT_SESSION, tuple(values[c] for c in SESSION_COLUMNS))
    conn.execute("INSERT OR IGNORE INTO dirty_days(site_id, day) VALUES (?, ?)", (site_id, day))
    _rebuild_pageviews(conn, key, site_id, tz, rows, pvs)
    _record_identity_evidence(conn, site_id, vid, uid, values["fp"], values["ck"], pvs)


class _Page:
    """What the hits of one page (page_key) add up to, computed in one pass over the session."""
    __slots__ = ("engaged", "depth", "last_ts", "status")

    def __init__(self) -> None:
        self.engaged = 0
        self.depth: Optional[int] = None
        self.last_ts: Optional[int] = None
        self.status: Any = None  # the last tidesight('status', …) reported for the page, as a (value,) tuple


def _rebuild_pageviews(conn: sqlite3.Connection, key: str, site_id: str, tz: str, rows: List[sqlite3.Row],
                       pvs: List[sqlite3.Row]) -> None:
    pages: Dict[str, _Page] = {}
    for r in rows:
        if not r["page_key"]:
            continue
        page = pages.get(r["page_key"])
        if page is None:
            page = pages[r["page_key"]] = _Page()
        if r["type"] == "engagement":
            page.engaged += r["ms"] or 0
        if r["sd"] is not None and (page.depth is None or r["sd"] > page.depth):
            page.depth = r["sd"]
        if page.last_ts is None or r["ts"] > page.last_ts:
            page.last_ts = r["ts"]
        if r["type"] == "status":  # reported after the pageview: tidesight('status', 404)
            st = _extra(r).get("st")
            if st is not None:
                page.status = (st,)
    for i, pv in enumerate(pvs):
        page = pages.get(pv["page_key"]) or _Page()
        engaged, depth = page.engaged, page.depth
        if i + 1 < len(pvs):
            duration = pvs[i + 1]["ts"] - pv["ts"]
        else:
            duration = max(page.last_ts if page.last_ts is not None else pv["ts"], pv["ts"]) - pv["ts"]
        extra = _extra(pv)
        host, path, query, frag = url_parts(pv["url"])
        st = page.status[0] if page.status is not None else extra.get("st")
        conn.execute(
            "INSERT OR REPLACE INTO pageviews(page_key, hit_id, site_id, session_key, ts, day, hour, url, host, path, "
            "query, hash, title, referrer, nav, status, seq, engaged_ms, scroll_max, duration_ms, is_entry, is_exit) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pv["page_key"], pv["hit_id"], site_id, key, pv["ts"], local_day(pv["ts"], tz), local_hour(pv["ts"], tz),
             pv["url"], host, path,
             query, frag, extra.get("title"), extra.get("ref"), extra.get("nav"),
             st if isinstance(st, int) else None, i + 1, engaged, depth,
             max(0, duration), 1 if i == 0 else 0, 1 if i == len(pvs) - 1 else 0))
        conn.execute("INSERT OR IGNORE INTO dirty_days(site_id, day) VALUES (?, ?)", (site_id, local_day(pv["ts"], tz)))


def _record_identity_evidence(conn: sqlite3.Connection, site_id: str, vid: Optional[str], uid: Optional[str],
                              fp: Optional[str], ck: Optional[str], pvs: List[sqlite3.Row]) -> None:
    """Edges between identifiers, used by identity stitching."""
    if not vid:
        return
    edges = []
    if uid:
        edges.append(("v:" + vid, "u:" + uid, "uid"))
    if fp and ck:
        edges.append(("v:" + vid, "f:%s|%s" % (fp, ck), "fpck"))
    for pv in pvs:
        idc = _extra(pv).get("idc")
        if isinstance(idc, dict) and idc.get("cookie") and idc.get("ls"):
            edges.append(("v:" + str(idc["cookie"]), "v:" + str(idc["ls"]), "idc"))
    for a, b, kind in edges:
        conn.execute("INSERT OR IGNORE INTO id_edges(site_id, a, b, kind) VALUES (?, ?, ?, ?)", (site_id, a, b, kind))
