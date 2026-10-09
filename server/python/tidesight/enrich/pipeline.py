"""The enrichment pipeline: raw hits → derived.db.

Incremental: each run processes raw hits after the last processed id, in
batches, each batch committed together with the watermark. When the
combined version of the parsers, databases and algorithms changes, the
whole derived database is rebuilt into a new file and swapped in, so the
dashboard never sees a half-built state. Re-running is idempotent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Set

from ..config import Config, SiteConfig
from ..storage import connect
from . import bots, identity, rollup, sessions, sources, ua
from .db import JobLock, derived_path, get_state, open_derived, set_state, transaction
from .geo import GeoLookup
from .ip import IP_VERSION, client_ip, cookieless_hash, in_networks, networks
from .raw import RawReader, raw_paths
from .timeutil import local_day
from .util import b36, jdump, num, status_code, to_int, to_str, url_parts

log = logging.getLogger("tidesight.enrich")

PIPELINE_VERSION = 3  # bump when hit processing changes
DAY_MS = 86400000

# Hit types copied to the typed tables.
CLICK_TYPES = ("click", "rage", "dead", "errclick")
MISC_SKIP = ("pageview", "engagement")


class Enricher:
    """Runs the pipeline against one data directory."""

    def __init__(self, cfg: Config, derived_file: Optional[str] = None) -> None:
        self.cfg = cfg
        self.data_dir = cfg.data_dir
        self.events_path = os.path.join(self.data_dir, "events.db")
        self.replay_path = os.path.join(self.data_dir, "replay.db")
        self.derived_file = derived_file or derived_path(self.data_dir)
        e = cfg.enrich
        self.geo = GeoLookup(e["geo_dir"] or os.path.join(self.data_dir, "geo"))
        self.crawlers = bots.load_crawler_list(e["bot_list"]) if e["bot_list"] else []
        self.internal_nets = networks(tuple(e["internal_ips"]))
        self.trusted = tuple(e["trusted_proxies"])
        self.secret = cfg.secret
        self._ua_cache: Dict[str, int] = {}
        self._geo_done: Set[str] = set()

    # -- versioning -------------------------------------------------------

    def version(self) -> str:
        parts = [
            "p%d" % PIPELINE_VERSION, "ip%d" % IP_VERSION, "s%d" % sessions.SESSIONS_VERSION,
            "id%d" % identity.IDENTITY_VERSION, "ua%s" % ua.current_parser_version(), "bot%s" % bots.BOTS_VERSION,
            "src%s" % sources.SOURCES_VERSION, "geo:" + self.geo.version(),
            "cfg:" + self._config_fingerprint(),
        ]
        if self.crawlers:
            parts.append("crawl:%d" % len(self.crawlers))
        return "|".join(parts)

    def _config_fingerprint(self) -> str:
        """Settings that change derived values (time zones, origins, IP handling, search params)."""
        sites = {sid: [s.timezone, s.origins, s.settings.get("search_params"), s.settings.get("session_timeout_min")]
                 for sid, s in sorted(self.cfg.sites.items())}
        e = self.cfg.enrich
        blob = json.dumps([sites, e["ip_header"], e["trusted_proxies"], e["internal_ips"]], sort_keys=True)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]

    # -- entry points ---------------------------------------------------------

    def run(self, rebuild: bool = False, max_seconds: Optional[float] = None, batch_size: int = 0) -> Dict[str, Any]:
        """Process new raw hits. Rebuilds from scratch when the version changed (or when asked)."""
        batch_size = batch_size or int(self.cfg.enrich["batch_size"])
        if not raw_paths(self.data_dir):
            return {"hits": 0, "note": "no events.db yet"}
        version = self.version()
        target = self.derived_file
        conn = open_derived(target)
        state = get_state(conn, "enrich")
        rebuilding = rebuild or (state["version"] is not None and state["version"] != version)
        if rebuilding:
            conn.close()
            target = self.derived_file + ".next"
            for suffix in ("", "-wal", "-shm", "-journal"):
                try:
                    os.remove(target + suffix)
                except OSError:
                    pass
            conn = open_derived(target)
            log.info("rebuilding derived data (version %s)", version)
            max_seconds = None  # a rebuild runs to completion before it is swapped in
        stats = self._process(conn, version, max_seconds, batch_size)
        stats["identity"] = identity.run(conn, self.cfg)
        stats["rollup"] = rollup.run(conn, self.cfg)
        conn.execute("PRAGMA optimize")
        if rebuilding:
            stats["rollups_carried"] = self._carry_rollups(conn)
        conn.close()
        if rebuilding:
            self._swap(target)
            stats["rebuilt"] = True
        stats["version"] = version
        return stats

    def _carry_rollups(self, conn: sqlite3.Connection) -> int:
        """Keep the old daily aggregates of days that no longer have raw data (retention)."""
        if not os.path.exists(self.derived_file):
            return 0
        conn.execute("ATTACH DATABASE ? AS old", (self.derived_file,))
        try:
            with transaction(conn):
                cur = conn.execute(
                    "INSERT OR IGNORE INTO daily SELECT * FROM old.daily o WHERE NOT EXISTS "
                    "(SELECT 1 FROM sessions s WHERE s.site_id = o.site_id AND s.day = o.day)")
                n = cur.rowcount
                conn.execute(
                    "INSERT OR IGNORE INTO daily_dim SELECT * FROM old.daily_dim o WHERE NOT EXISTS "
                    "(SELECT 1 FROM sessions s WHERE s.site_id = o.site_id AND s.day = o.day)")
        except sqlite3.Error as exc:  # an old file without these tables
            log.warning("could not carry rollups over: %s", exc)
            n = 0
        finally:
            conn.execute("DETACH DATABASE old")
        return n

    def _swap(self, new_path: str) -> None:
        conn = sqlite3.connect(new_path)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        for suffix in ("-wal", "-shm"):
            try:
                os.remove(self.derived_file + suffix)
            except OSError:
                pass
        os.replace(new_path, self.derived_file)
        for suffix in ("-wal", "-shm"):
            try:
                os.remove(new_path + suffix)
            except OSError:
                pass

    # -- batch processing -----------------------------------------------------

    def _process(self, conn: sqlite3.Connection, version: str, max_seconds: Optional[float],
                 batch_size: int) -> Dict[str, Any]:
        reader = RawReader(self.data_dir)
        rconn = connect(self.replay_path, readonly=True) if os.path.exists(self.replay_path) else None
        started = time.monotonic()
        stats = {"hits": 0, "batches": 0, "sessions": 0, "duplicates": 0}
        try:
            while True:
                state = get_state(conn, "enrich")
                rows = reader.hits_after(state["last_id"], batch_size)
                if not rows:
                    break
                with transaction(conn):
                    affected = self._batch(conn, rows, stats)
                    stats["sessions"] += sessions.rebuild(conn, self.cfg, affected, reader, rconn, self, stats)
                    set_state(conn, "enrich", version, rows[-1]["hit_id"])
                stats["hits"] += len(rows)
                stats["batches"] += 1
                if max_seconds is not None and time.monotonic() - started > max_seconds:
                    stats["stopped_early"] = True
                    break
        finally:
            reader.close()
            if rconn is not None:
                rconn.close()
        if stats["batches"] == 0:
            with transaction(conn):
                set_state(conn, "enrich", version, get_state(conn, "enrich")["last_id"])
        return stats

    def _batch(self, conn: sqlite3.Connection, rows: List[sqlite3.Row], stats: Dict[str, Any]) -> Set[str]:
        affected: Set[str] = set()
        reqs: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            conn.execute("SAVEPOINT hit")
            try:
                rid = row["request_id"]
                if rid not in reqs:
                    reqs[rid] = self.request(conn, row)
                key = self.hit(conn, row, reqs[rid], stats)
                conn.execute("RELEASE hit")
            except Exception as exc:  # one bad hit must never stop the others, nor every later run
                conn.execute("ROLLBACK TO hit")
                conn.execute("RELEASE hit")
                self.failed(conn, "hit", exc, stats, hit_id=row["hit_id"])
                continue
            if key:
                affected.add(key)
        return affected

    def failed(self, conn: sqlite3.Connection, stage: str, exc: BaseException, stats: Dict[str, Any],
               hit_id: Optional[int] = None, session_key: Optional[str] = None) -> None:
        """Record what could not be derived (the raw data stays; a later version may handle it)."""
        stats["failures"] = stats.get("failures", 0) + 1
        if stats["failures"] <= 5:
            log.warning("enrichment skipped %s %s: %s", stage, hit_id if hit_id is not None else session_key,
                        repr(exc)[:300])
        conn.execute("INSERT INTO failures(at, stage, hit_id, session_key, error) VALUES (?, ?, ?, ?, ?)",
                     (int(time.time() * 1000), stage, hit_id, session_key, repr(exc)[:500]))

    # -- requests ---------------------------------------------------------------

    def request(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
        existing = conn.execute("SELECT * FROM requests WHERE request_id = ?", (row["request_id"],)).fetchone()
        if existing is not None:
            out = dict(existing)
            out["headers"] = json.loads(row["headers"] or "{}")
            return out
        headers = json.loads(row["headers"] or "{}")
        envelope = json.loads(row["envelope"]) if row["envelope"] else {}
        if not isinstance(envelope, dict):
            envelope = {}
        e = self.cfg.enrich
        ip, ip_source = client_ip(row["ip"], headers, e["ip_header"], self.trusted)
        if ip is None and row["ip"] and row["ip"].startswith("h:"):  # anonymised in privacy mode
            ip, ip_source = row["ip"], "hashed"
        user_agent = headers.get("user-agent")
        hints = {k: v for k, v in headers.items() if k.startswith("sec-ch-ua")}
        ua_id = self.ua_id(conn, user_agent, hints, None)
        sent = to_int(envelope.get("sent"))  # a clock more than a year off is not a clock: ignore it
        skew = row["received_at"] - sent if sent is not None and abs(row["received_at"] - sent) < 366 * DAY_MS else None
        out = {
            "request_id": row["request_id"], "received_at": row["received_at"], "site_id": row["site_id"],
            "site_ok": row["site_ok"], "endpoint": row["endpoint"], "client_ip": ip, "ip_source": ip_source,
            "ua_id": ua_id, "ck": cookieless_hash(self.secret, row["received_at"], row["site_id"], ip, user_agent),
            "skew_ms": skew, "try": to_int(envelope.get("try")),
            "internal": 1 if ip and self.internal_nets and in_networks(ip, self.internal_nets) else 0,
        }
        conn.execute("INSERT OR REPLACE INTO requests(request_id, received_at, site_id, site_ok, endpoint, client_ip, "
                     "ip_source, ua_id, ck, skew_ms, try, internal) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     tuple(out[k] for k in ("request_id", "received_at", "site_id", "site_ok", "endpoint", "client_ip",
                                            "ip_source", "ua_id", "ck", "skew_ms", "try", "internal")))
        if ip:
            note = None
            if row["geo"]:
                try:
                    note = json.loads(row["geo"])
                except ValueError:
                    note = None
            self.ensure_geo(conn, ip, note if isinstance(note, dict) else None)
        out["headers"] = headers
        return out

    def ua_id(self, conn: sqlite3.Connection, user_agent: Optional[str], header_hints: Dict[str, str],
              js_hints: Optional[Dict[str, Any]]) -> Optional[int]:
        if not user_agent and not header_hints and not js_hints:
            return None
        hints_blob = json.dumps({"h": header_hints or {}, "js": js_hints or {}}, sort_keys=True)
        key = hashlib.sha1(((user_agent or "") + "\n" + hints_blob).encode("utf-8")).hexdigest()
        cached = self._ua_cache.get(key)
        if cached is not None:
            return cached
        row = conn.execute("SELECT id FROM ua WHERE key = ?", (key,)).fetchone()
        if row is None:
            p = ua.parse(user_agent, header_hints or None, js_hints or None)
            cur = conn.execute(
                "INSERT INTO ua(key, ua, hints, browser, browser_version, browser_major, engine, os, os_version, "
                "device_type, device_brand, device_model, is_webview, in_app, is_bot_ua, bot_name, parser) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key, user_agent, hints_blob, p.get("browser"), p.get("browser_version"), p.get("browser_major"),
                 p.get("engine"), p.get("os"), p.get("os_version"), p.get("device_type"), p.get("device_brand"),
                 p.get("device_model"), 1 if p.get("is_webview") else 0, p.get("in_app"),
                 1 if p.get("is_bot_ua") else 0, p.get("bot_name"), str(p.get("parser_version") or p.get("parser"))))
            ua_id = int(cur.lastrowid)
        else:
            ua_id = int(row["id"])
        self._ua_cache[key] = ua_id
        return ua_id

    def ensure_geo(self, conn: sqlite3.Connection, ip: str, note: Optional[Dict[str, Any]] = None) -> None:
        """Geolocate an IP once (cached in ip_geo). `note` is geo saved before the IP was anonymised."""
        if ip in self._geo_done:
            return
        self._geo_done.add(ip)
        if conn.execute("SELECT 1 FROM ip_geo WHERE ip = ? LIMIT 1", (ip,)).fetchone():
            return
        if note:
            found = {p: r for p, r in note.items() if isinstance(r, dict)}
        else:
            found = self.geo.lookup(ip) if self.geo.providers else {}
        if not found:
            conn.execute("INSERT OR IGNORE INTO ip_geo(ip, provider) VALUES (?, '')", (ip,))
            return
        for provider, rec in found.items():
            conn.execute(
                "INSERT OR REPLACE INTO ip_geo(ip, provider, country, country_name, continent, is_eu, region, "
                "region_name, city, postal, lat, lon, accuracy_km, tz, asn, as_org, network) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ip, provider, rec.get("country"), rec.get("country_name"), rec.get("continent"),
                 None if rec.get("is_eu") is None else int(bool(rec.get("is_eu"))), rec.get("region"),
                 rec.get("region_name"), rec.get("city"), rec.get("postal"), rec.get("lat"), rec.get("lon"),
                 rec.get("accuracy_km"), rec.get("tz"), rec.get("asn"), rec.get("as_org"), rec.get("network")))

    # -- hits -----------------------------------------------------------------------

    def effective_ts(self, row: sqlite3.Row, req: Dict[str, Any]) -> int:
        """Client time corrected by the batch's clock skew; server time when unknown."""
        received = row["received_at"]
        cts = row["client_ts"]
        if cts is None or req.get("skew_ms") is None:
            return received
        ts = cts + req["skew_ms"]
        # Not in the future, and not more than 30 days late (a batch kept offline): otherwise server time.
        return received if ts > received + 60000 or ts < received - 30 * DAY_MS else ts

    def session_key(self, conn: sqlite3.Connection, row: sqlite3.Row, req: Dict[str, Any], ts: int) -> str:
        site = row["site_id"] or ""
        if row["session_id"]:
            return "%s/%s" % (site, row["session_id"])
        ck = req.get("ck")
        if not ck:
            return "%s/hit-%d" % (site, row["hit_id"])
        timeout = self.cfg.site_or_default(site).settings["session_timeout_min"] * 60000
        prev = conn.execute("SELECT session_key, last_ts FROM px_sessions WHERE site_id = ? AND ck = ?",
                            (site, ck)).fetchone()
        if prev is not None and abs(ts - prev["last_ts"]) <= timeout:  # same visitor, within the timeout
            key, last = prev["session_key"], max(ts, prev["last_ts"])
        else:
            key, last = "%s/px-%s-%s" % (site, ck[:12], b36(ts)), ts
        conn.execute("INSERT OR REPLACE INTO px_sessions(site_id, ck, session_key, last_ts) VALUES (?, ?, ?, ?)",
                     (site, ck, key, last))
        return key

    def hit(self, conn: sqlite3.Connection, row: sqlite3.Row, req: Dict[str, Any], stats: Dict[str, Any]) -> Optional[str]:
        try:
            data = json.loads(row["data"])
        except ValueError:
            data = None
        if not isinstance(data, dict):
            data = {}
        site_id = row["site_id"] or ""
        site = self.cfg.site_or_default(site_id)
        htype = row["type"] or "_invalid"
        dup = 0
        if row["event_id"]:
            cur = conn.execute("INSERT OR IGNORE INTO event_ids(site_id, event_id, hit_id) VALUES (?, ?, ?)",
                               (site_id, row["event_id"], row["hit_id"]))
            if cur.rowcount == 0:
                dup = 1
                stats["duplicates"] += 1
        ts = self.effective_ts(row, req)
        skey = self.session_key(conn, row, req, ts)
        if row["page_id"]:
            pkey = "%s/%s" % (site_id, row["page_id"])
        elif htype == "pageview":
            pkey = "%s/px%d" % (site_id, row["hit_id"])
        else:
            pkey = None
        ms = sd = None
        fp = None
        extra: Optional[Dict[str, Any]] = None
        if htype == "pageview":
            extra = pageview_extra(data, pixel=row["endpoint"] == "pixel")
        elif htype == "engagement":
            ms = to_int(data.get("ms"))
            sd = to_int(data.get("sd"))
        elif htype == "scroll":
            sd = to_int(data.get("max")) or to_int(data.get("d"))
        elif htype == "status" and status_code(data.get("st")) is not None:
            extra = {"st": status_code(data.get("st"))}
        elif htype == "fp":
            fp = to_str(data.get("h"), 64)
            if isinstance(data.get("bot"), dict):
                extra = {"bot": bot_signals(data["bot"])}
        conn.execute(
            "INSERT OR REPLACE INTO hits(hit_id, request_id, site_id, site_ok, session_key, page_key, type, name, ts, "
            "received_at, dup, vid, uid, url, ns, sn, pn, ms, sd, fp, extra) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row["hit_id"], row["request_id"], site_id, row["site_ok"], skey, pkey, htype, row["name"], ts,
             row["received_at"], dup, row["visitor_id"], row["user_id"], row["url"], to_int(data.get("ns")),
             to_int(data.get("sn")), to_int(data.get("pn")), ms, sd, fp, jdump(extra)))
        if not dup:
            self.typed(conn, row, data, htype, site, skey, pkey, ts)
        return skey

    def typed(self, conn: sqlite3.Connection, row: sqlite3.Row, d: Dict[str, Any], htype: str, site: SiteConfig,
              skey: str, pkey: Optional[str], ts: int) -> None:
        """Copy a hit into its typed table."""
        day = local_day(ts, site.timezone)
        _host, path, _q, _h = url_parts(row["url"])
        base = (row["hit_id"], row["site_id"], skey, pkey, ts, day, path)
        if htype == "event":
            props = d.get("p") if isinstance(d.get("p"), dict) else None
            conn.execute("INSERT OR REPLACE INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         base + (row["name"] or to_str(d.get("n"), 200), jdump(props), num(d.get("val"))))
        elif htype in CLICK_TYPES:
            conn.execute("INSERT OR REPLACE INTO clicks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                         "?, ?, ?, ?, ?, ?, ?)",
                         base + (htype, num(d.get("x")), num(d.get("y")), num(d.get("vx")), num(d.get("vy")),
                                 to_int(d.get("pw")), to_int(d.get("ph")), to_int(d.get("vw")), to_int(d.get("vh")),
                                 num(d.get("ex")), num(d.get("ey")), num(d.get("ew")), num(d.get("eh")),
                                 to_str(d.get("sel")), to_str(d.get("tag"), 32), to_str(d.get("txt"), 200),
                                 to_str(d.get("href"), 2048), to_str(d.get("lk"), 8), to_int(d.get("n")),
                                 to_str(d.get("msg"), 500)))
        elif htype == "vital":
            if pkey:  # a vital may be re-sent with a final value (upd: 1): the last one per page wins
                conn.execute("DELETE FROM vitals WHERE page_key = ? AND metric = ?", (pkey, to_str(d.get("n"), 16)))
            conn.execute("INSERT OR REPLACE INTO vitals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         base + (to_str(d.get("n"), 16), num(d.get("val")), to_str(d.get("r"), 8), to_str(d.get("el")),
                                 to_str(d.get("nt"), 24)))
        elif htype == "perf":
            fields = {k: v for k, v in d.items() if k not in ("t", "id", "ts", "pid", "url", "sid", "vid", "ns")}
            conn.execute("INSERT OR REPLACE INTO perf VALUES (?, ?, ?, ?, ?, ?, ?, ?)", base + (jdump(fields),))
        elif htype == "error":
            kind = to_str(d.get("k"), 16) or "error"
            msg = to_str(d.get("msg"), 1000)
            src = to_str(d.get("src") or d.get("res"), 1000)
            grp = hashlib.sha1("|".join([kind, msg or "", src or "", str(d.get("line") or "")]).encode("utf-8")).hexdigest()[:16]
            if d.get("upd") and pkey:  # an occurrence count for an error already reported on this page
                cur = conn.execute("UPDATE errors SET n = MAX(COALESCE(n, 1), ?) WHERE page_key = ? AND grp = ?",
                                   (to_int(d.get("n")) or 1, pkey, grp))
                if cur.rowcount:
                    return
            conn.execute("INSERT OR REPLACE INTO errors VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         base + (kind, msg, to_str(d.get("src"), 1000), to_int(d.get("line")), to_int(d.get("col")),
                                 to_str(d.get("stack"), 4096), to_str(d.get("tag"), 32), to_str(d.get("res"), 2048),
                                 to_int(d.get("n")) or 1, grp))
        elif htype == "form":
            conn.execute("INSERT OR REPLACE INTO forms VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         base + (to_str(d.get("a"), 16), to_str(d.get("f"), 200), to_str(d.get("fld"), 200),
                                 to_str(d.get("ft"), 32), to_int(d.get("ms")), to_int(d.get("filled")), to_int(d.get("n")),
                                 to_str(d.get("last"), 200), to_str(d.get("why"), 64)))
        elif htype == "search":
            conn.execute("INSERT OR REPLACE INTO searches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         base + (to_str(d.get("q"), 200), to_int(d.get("n")), to_str(d.get("cat"), 100), "api"))
        elif htype == "pageview":
            if site.features.get("site_search", True):
                q = sources.extract_search_query(row["url"], site.settings.get("search_params") or ())
                if q:
                    conn.execute("INSERT OR REPLACE INTO searches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 base + (q, None, None, "url"))
        elif htype not in MISC_SKIP:
            fields = {k: v for k, v in d.items() if k not in ("t", "id", "ts", "pid", "url", "sid", "vid")}
            conn.execute("INSERT OR REPLACE INTO misc VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         base + (htype, jdump(fields)))


# What a pageview keeps in hits.extra. Raw values are whatever a client sent (a title can be a list, a number
# 1e999, a referrer a megabyte): only bounded text and finite numbers go on to the derived tables.
_PV_TEXT = {"title": 512, "ref": 2048, "nav": 32, "canon": 2048, "lang": 35, "idsrc": 16}
_CTX_TEXT = {"lang": 35, "tz": 64, "ua": 1024}
_CTX_NUM = ("sw", "sh", "vw", "vh", "dpr", "tp")


def _number(v: Any) -> Optional[Any]:
    n = num(v)
    return None if n is None else (int(n) if n == int(n) else n)


def _clean_uad(uad: Dict[str, Any]) -> Dict[str, Any]:
    """navigator.userAgentData as the tracker sends it, keeping only well-typed, bounded fields."""
    out: Dict[str, Any] = {}
    for k in ("platform", "platformVersion", "model", "architecture", "bitness", "uaFullVersion"):
        v = to_str(uad.get(k), 128)
        if v is not None:
            out[k] = v
    for k in ("mobile", "wow64"):
        if isinstance(uad.get(k), bool):
            out[k] = uad[k]
    for k in ("brands", "fullVersionList"):
        items = uad.get(k)
        if isinstance(items, list):
            out[k] = [{"brand": to_str(i.get("brand"), 128) or "", "version": to_str(i.get("version"), 64) or ""}
                      for i in items[:20] if isinstance(i, dict)]
    if isinstance(uad.get("formFactors"), list):
        out["formFactors"] = [to_str(f, 32) for f in uad["formFactors"][:10] if to_str(f, 32)]
    return out


def pageview_extra(data: Dict[str, Any], pixel: bool = False) -> Dict[str, Any]:
    extra: Dict[str, Any] = {}
    for k, n in _PV_TEXT.items():
        v = to_str(data.get(k), n)
        if v is not None:
            extra[k] = v
    if pixel:
        for k, src, n in (("title", "title", 512), ("ref", "r", 2048)):
            if k not in extra and to_str(data.get(src), n) is not None:
                extra[k] = to_str(data.get(src), n)
    if status_code(data.get("st")) is not None:
        extra["st"] = status_code(data.get("st"))
    if to_int(data.get("new")) is not None:
        extra["new"] = to_int(data.get("new"))
    idc = data.get("idc")
    if isinstance(idc, dict):
        ids = {k: to_str(idc.get(k), 64) for k in ("cookie", "ls") if to_str(idc.get(k), 64)}
        if ids:
            extra["idc"] = ids
    ctx = data.get("ctx") if isinstance(data.get("ctx"), dict) else {}
    sub: Dict[str, Any] = {}
    for k in _CTX_NUM:
        if _number(ctx.get(k)) is not None:
            sub[k] = _number(ctx.get(k))
    for k, n in _CTX_TEXT.items():
        if to_str(ctx.get(k), n) is not None:
            sub[k] = to_str(ctx.get(k), n)
    if isinstance(ctx.get("uad"), dict):
        sub["uad"] = _clean_uad(ctx["uad"])
    if sub:
        extra["ctx"] = sub
    return extra


def bot_signals(b: Dict[str, Any]) -> Dict[str, Any]:
    """The tracker's automation signals (fp.bot), well-typed and bounded."""
    out: Dict[str, Any] = {k: _number(b.get(k)) for k in ("wd", "pl", "lg", "oz", "hl", "co") if _number(b.get(k)) is not None}
    if isinstance(b.get("au"), list):
        out["au"] = [to_str(a, 64) for a in b["au"][:20] if to_str(a, 64)]
    return out


def run(cfg: Config, rebuild: bool = False, max_seconds: Optional[float] = None) -> Dict[str, Any]:
    """`jobs.py enrich`: one locked run of the pipeline."""
    with JobLock(cfg.data_dir):
        return Enricher(cfg).run(rebuild=rebuild, max_seconds=max_seconds)
