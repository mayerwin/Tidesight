"""Maintenance jobs: backup, vacuum, privacy clean-up, archiving, export, deleting a site's data.

Every destructive job is a dry run unless told to apply, and privacy-clean
takes a backup first. See docs/privacy.md.
"""

from __future__ import annotations

import csv
import gzip
import json
import os
import shutil
import sqlite3
import sys
import time
from typing import Any, Dict, IO, Iterable, List, Optional, Tuple

from .anonymize import anonymize_headers, anonymize_ip
from .config import Config
from .enrich.raw import raw_paths
from .storage import EVENTS_MIGRATIONS, REPLAY_MIGRATIONS, connect, migrate

DB_FILES = ("events.db", "replay.db", "derived.db", "search.db")
DAY_MS = 86400000


def _db(cfg: Config, name: str, readonly: bool = False) -> Optional[sqlite3.Connection]:
    path = os.path.join(cfg.data_dir, name)
    if not os.path.exists(path):
        return None
    if readonly:
        conn = connect(path, readonly=True, busy_timeout_ms=cfg.server["busy_timeout_ms"])
    else:
        conn = connect(path, cfg.server["journal_mode"], cfg.server["synchronous"], max(30000, cfg.server["busy_timeout_ms"]))
        if name == "events.db":
            migrate(conn, EVENTS_MIGRATIONS)
        elif name == "replay.db":
            migrate(conn, REPLAY_MIGRATIONS)
        elif name == "search.db":
            from .searchengines import SEARCH_MIGRATIONS
            migrate(conn, SEARCH_MIGRATIONS)
    conn.row_factory = sqlite3.Row
    return conn


def _utc_day_start(day: str) -> int:
    import calendar
    return calendar.timegm(time.strptime(day, "%Y-%m-%d")) * 1000


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def backup(cfg: Config, dest: Optional[str] = None, keep: int = 7, with_replay: bool = False,
           with_derived: bool = False, with_archives: bool = False) -> Dict[str, Any]:
    """Consistent online copies (SQLite backup API), gzip-compressed, rotated: the raw data, the dashboard's own
    data (web.db: segments, notes), the search engines' figures (search.db: they cannot be fetched again beyond 16
    months) and the settings kept in the data directory (properties, users, search engine accounts)."""
    root = dest or os.path.join(cfg.data_dir, "backups")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    target = os.path.join(root, stamp)
    os.makedirs(target, exist_ok=True)
    names = (["events.db", "web.db", "search.db"] + (["replay.db"] if with_replay else [])
             + (["derived.db"] if with_derived else []))
    if with_archives:
        names += [os.path.join("archive", os.path.basename(p)) for p in raw_paths(cfg.data_dir)
                  if os.path.basename(os.path.dirname(p)) == "archive"]
        os.makedirs(os.path.join(target, "archive"), exist_ok=True)
    done = []
    for name in names:
        src_path = os.path.join(cfg.data_dir, name)
        if not os.path.exists(src_path):
            continue
        tmp = os.path.join(target, name)
        src = sqlite3.connect(src_path, timeout=60)
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst, pages=4096, sleep=0.01)  # in steps, so the collector keeps writing
        finally:
            dst.close()
            src.close()
        with open(tmp, "rb") as fin, gzip.open(tmp + ".gz", "wb", compresslevel=6) as fout:
            shutil.copyfileobj(fin, fout, 1024 * 1024)
        os.remove(tmp)
        done.append({"file": name, "gz_mb": round(os.path.getsize(tmp + ".gz") / 1e6, 2)})
    for name in ("properties.json", "users.json", "search-engines.json"):  # written atomically: a plain copy is consistent
        src_path = os.path.join(cfg.data_dir, name)
        if os.path.exists(src_path):
            shutil.copy2(src_path, os.path.join(target, name))
            done.append({"file": name})
    # Rotation: keep the newest `keep` backup folders.
    folders = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)) and d[:8].isdigit())
    removed = []
    for old in folders[:-keep] if keep > 0 else []:
        shutil.rmtree(os.path.join(root, old), ignore_errors=True)
        removed.append(old)
    return {"backup": target, "files": done, "removed": removed}


# ---------------------------------------------------------------------------
# Vacuum / optimise
# ---------------------------------------------------------------------------

def vacuum(cfg: Config) -> Dict[str, Any]:
    out = {}
    for name in DB_FILES:
        conn = _db(cfg, name)
        if conn is None:
            continue
        before = os.path.getsize(os.path.join(cfg.data_dir, name))
        conn.execute("PRAGMA optimize")
        conn.execute("VACUUM")
        if cfg.server["journal_mode"] == "wal":
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        out[name] = {"mb_before": round(before / 1e6, 2),
                     "mb_after": round(os.path.getsize(os.path.join(cfg.data_dir, name)) / 1e6, 2)}
    return out


# ---------------------------------------------------------------------------
# Privacy clean-up
# ---------------------------------------------------------------------------

def _mark_derived_stale(cfg: Config) -> None:
    """Make the next `enrich` run rebuild derived.db from the cleaned raw data."""
    conn = _db(cfg, "derived.db")
    if conn is None:
        return
    try:
        conn.execute("UPDATE job_state SET version = 'stale' WHERE job = 'enrich'")
    except sqlite3.Error:
        pass
    conn.close()


BATCH = 2000  # rows per write transaction: the collector waits for the database at most that long


def _delete_in_batches(conn: sqlite3.Connection, table: str, where: str, args: Tuple = ()) -> int:
    """DELETE in short transactions, so the collector is never locked out for long (it writes between them).
    A table WITHOUT ROWID (small counters) is cleared in one statement."""
    total = 0
    while True:
        conn.execute("BEGIN IMMEDIATE")
        try:
            try:
                cur = conn.execute("DELETE FROM %s WHERE rowid IN (SELECT rowid FROM %s WHERE %s LIMIT %d)"
                                   % (table, table, where, BATCH), args)
            except sqlite3.OperationalError as exc:
                if "rowid" not in str(exc):
                    raise
                cur = conn.execute("DELETE FROM %s WHERE %s" % (table, where), args)
                conn.execute("COMMIT")
                return total + cur.rowcount
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        total += cur.rowcount
        if cur.rowcount < BATCH:
            return total


def _annotate_geo(cfg: Config, econn: sqlite3.Connection) -> int:
    """Before anonymising IPs, keep each request's derived geolocation in raw_requests.geo."""
    dconn = _db(cfg, "derived.db", readonly=True)
    if dconn is None:
        return 0
    n = 0
    try:
        rows = econn.execute("SELECT id FROM raw_requests WHERE geo IS NULL AND ip IS NOT NULL AND ip NOT LIKE 'h:%'").fetchall()
        for start in range(0, len(rows), 1000):
            ids = [r["id"] for r in rows[start:start + 1000]]
            marks = ",".join("?" * len(ids))
            found = dconn.execute(
                "SELECT r.request_id, g.* FROM requests r JOIN ip_geo g ON g.ip = r.client_ip "
                "WHERE r.request_id IN (%s) AND g.provider != ''" % marks, ids).fetchall()
            notes: Dict[int, Dict[str, Any]] = {}
            for g in found:
                rec = {k: g[k] for k in g.keys() if k not in ("request_id", "ip", "provider") and g[k] is not None}
                notes.setdefault(g["request_id"], {})[g["provider"]] = rec
            econn.execute("BEGIN IMMEDIATE")
            for rid, note in notes.items():
                econn.execute("UPDATE raw_requests SET geo = ? WHERE id = ?", (json.dumps(note), rid))
                n += 1
            econn.execute("COMMIT")
    finally:
        dconn.close()
    return n


def privacy_clean(cfg: Config, hash_ip: bool = False, truncate_ip: bool = False, drop_fingerprint: bool = False,
                  drop_replay: bool = False, drop_mouse: bool = False, apply_retention: bool = False,
                  apply: bool = False, backup_first: bool = True, now_ms: Optional[int] = None) -> Dict[str, Any]:
    """Each step is opt-in; nothing changes without apply=True."""
    if not any((hash_ip, truncate_ip, drop_fingerprint, drop_replay, drop_mouse, apply_retention)):
        return {"note": "nothing to do: pass --hash-ip, --truncate-ip, --drop-fingerprint, --drop-replay, "
                        "--drop-mouse and/or --retention"}
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    report: Dict[str, Any] = {"dry_run": not apply, "steps": {}}
    # Raw data lives in events.db and in the yearly archives (which enrichment reads too): clean them all.
    econns = [_raw_db(cfg, path) for path in raw_paths(cfg.data_dir)]
    rconn = _db(cfg, "replay.db")
    try:
        if apply and backup_first:
            report["backup"] = backup(cfg, with_replay=rconn is not None and (drop_replay or drop_mouse or apply_retention),
                                      with_archives=len(econns) > 1)
        if hash_ip or truncate_ip:
            ip_steps = [_clean_ips(cfg, econn, None, hash_ip, truncate_ip, apply) for econn in econns]
            chunks = _clean_ips(cfg, None, rconn, hash_ip, truncate_ip, apply)
            report["steps"]["ip"] = {"requests": sum(x["requests"] for x in ip_steps), "chunks": chunks["chunks"],
                                     "files": len(econns)}
        if drop_fingerprint:
            n = 0
            for econn in econns:
                found = econn.execute("SELECT COUNT(*) FROM raw_hits WHERE type = 'fp'").fetchone()[0]
                if apply and found:
                    _delete_in_batches(econn, "raw_hits", "type = 'fp'")
                n += found
            report["steps"]["fingerprint"] = {"fp_hits": n}
        for flag, kinds in ((drop_replay, ("replay", "snapshot")), (drop_mouse, ("mouse",))):
            if flag and rconn is not None:
                marks = ",".join("?" * len(kinds))
                n = rconn.execute("SELECT COUNT(*) FROM chunks WHERE kind IN (%s)" % marks, kinds).fetchone()[0]
                if apply and n:
                    _delete_in_batches(rconn, "chunks", "kind IN (%s)" % marks, kinds)
                report["steps"]["chunks_" + kinds[0]] = {"chunks": n}
        if apply_retention:
            steps = [_retention(cfg, econn, None, apply, now_ms) for econn in econns] or [{}]
            out = _retention(cfg, None, rconn, apply, now_ms)
            if any("raw_requests" in st for st in steps):
                out["raw_requests"] = sum(st.get("raw_requests", 0) for st in steps)
                out.pop("note", None)
            report["steps"]["retention"] = out
        if apply:
            for conn in econns + [rconn]:
                if conn is not None:
                    conn.execute("VACUUM")
            _mark_derived_stale(cfg)
            report["note"] = "derived.db will be rebuilt by the next enrich run"
        else:
            report["note"] = "dry run: re-run with --apply to change data"
    finally:
        for conn in econns + [rconn]:
            if conn is not None:
                conn.close()
    return report


def _raw_db(cfg: Config, path: str) -> sqlite3.Connection:
    conn = connect(path, cfg.server["journal_mode"], cfg.server["synchronous"], max(30000, cfg.server["busy_timeout_ms"]))
    migrate(conn, EVENTS_MIGRATIONS)
    conn.row_factory = sqlite3.Row
    return conn


def _clean_ips(cfg: Config, econn: Optional[sqlite3.Connection], rconn: Optional[sqlite3.Connection],
               do_hash: bool, do_truncate: bool, apply: bool) -> Dict[str, Any]:
    secret = cfg.secret
    out: Dict[str, Any] = {"requests": 0, "chunks": 0}
    if econn is not None:
        rows = econn.execute("SELECT id, ip, headers FROM raw_requests WHERE ip IS NOT NULL AND ip NOT LIKE 'h:%'").fetchall()
        out["requests"] = len(rows)
        if apply and rows:
            out["geo_annotated"] = _annotate_geo(cfg, econn)
            for start in range(0, len(rows), 1000):
                econn.execute("BEGIN IMMEDIATE")
                for r in rows[start:start + 1000]:
                    headers = json.loads(r["headers"] or "{}")
                    headers = anonymize_headers(headers, do_hash, do_truncate, secret)
                    econn.execute("UPDATE raw_requests SET ip = ?, headers = ? WHERE id = ?",
                                  (anonymize_ip(r["ip"], do_hash, do_truncate, secret),
                                   json.dumps(headers, ensure_ascii=False, separators=(",", ":")), r["id"]))
                econn.execute("COMMIT")
    if rconn is not None:
        rows = rconn.execute("SELECT id, ip FROM chunks WHERE ip IS NOT NULL AND ip NOT LIKE 'h:%'").fetchall()
        out["chunks"] = len(rows)
        if apply and rows:
            for start in range(0, len(rows), BATCH):
                rconn.execute("BEGIN IMMEDIATE")
                for r in rows[start:start + BATCH]:
                    rconn.execute("UPDATE chunks SET ip = ? WHERE id = ?",
                                  (anonymize_ip(r["ip"], do_hash, do_truncate, secret), r["id"]))
                rconn.execute("COMMIT")
    return out


def _retention(cfg: Config, econn: Optional[sqlite3.Connection], rconn: Optional[sqlite3.Connection],
               apply: bool, now_ms: int) -> Dict[str, Any]:
    r = cfg.retention
    out: Dict[str, Any] = {}
    if r["raw_days"] and econn is not None:
        cutoff = now_ms - r["raw_days"] * DAY_MS
        n = econn.execute("SELECT COUNT(*) FROM raw_requests WHERE received_at < ?", (cutoff,)).fetchone()[0]
        if apply and n:
            while True:  # a few requests (and their hits) at a time: short write transactions
                ids = [r[0] for r in econn.execute("SELECT id FROM raw_requests WHERE received_at < ? LIMIT ?",
                                                   (cutoff, BATCH // 4))]
                if not ids:
                    break
                marks = ",".join("?" * len(ids))
                econn.execute("BEGIN IMMEDIATE")
                try:
                    econn.execute("DELETE FROM raw_hits WHERE request_id IN (%s)" % marks, ids)
                    econn.execute("DELETE FROM raw_requests WHERE id IN (%s)" % marks, ids)
                    econn.execute("COMMIT")
                except BaseException:
                    econn.execute("ROLLBACK")
                    raise
        out["raw_requests"] = n
    if rconn is not None:
        for key, kinds in (("replay_days", ("replay", "snapshot")), ("mouse_days", ("mouse",))):
            if r[key]:
                cutoff = now_ms - r[key] * DAY_MS
                marks = ",".join("?" * len(kinds))
                n = rconn.execute("SELECT COUNT(*) FROM chunks WHERE received_at < ? AND kind IN (%s)" % marks,
                                  (cutoff,) + kinds).fetchone()[0]
                if apply and n:
                    _delete_in_batches(rconn, "chunks", "received_at < ? AND kind IN (%s)" % marks, (cutoff,) + kinds)
                out[key] = n
    if not out:
        out["note"] = "no [retention] window is set"
    return out


# ---------------------------------------------------------------------------
# Deleting a site's data (a property deleted in the dashboard, or `jobs.py purge-site`)
# ---------------------------------------------------------------------------

def purge_site(cfg: Config, site_id: str, apply: bool = False) -> Dict[str, Any]:
    """Delete everything stored for one site: raw requests and hits (archives included), recordings, derived
    data and the dashboard's segments and notes. Short transactions: the collector keeps writing meanwhile.
    Backups made before keep the data until they are rotated out."""
    from .enrich.db import JobLock
    from .webdb import WebDB
    if not site_id:
        raise ValueError("purge_site needs a site id")
    out: Dict[str, Any] = {"site": site_id, "applied": apply, "rows": {}}
    rows = out["rows"]

    def count(conn: sqlite3.Connection, table: str) -> int:
        return conn.execute("SELECT COUNT(*) FROM %s WHERE site_id = ?" % table, (site_id,)).fetchone()[0]

    for path in raw_paths(cfg.data_dir):
        conn = _raw_db(cfg, path)
        try:
            name = os.path.relpath(path, cfg.data_dir)
            for table in ("raw_hits", "raw_requests", "counters"):
                n = count(conn, table)
                if apply and n:
                    _delete_in_batches(conn, table, "site_id = ?", (site_id,))
                rows["%s:%s" % (name, table)] = n
        finally:
            conn.close()
    sconn = _db(cfg, "search.db")
    if sconn is not None:
        try:
            for table in ("search_rows", "search_totals", "search_days", "search_state"):
                n = count(sconn, table)
                if apply and n:
                    _delete_in_batches(sconn, table, "site_id = ?", (site_id,))
                if n:
                    rows["search.db:" + table] = n
        finally:
            sconn.close()
    rconn = _db(cfg, "replay.db")
    if rconn is not None:
        try:
            n = count(rconn, "chunks")
            if apply and n:
                _delete_in_batches(rconn, "chunks", "site_id = ?", (site_id,))
            rows["replay.db:chunks"] = n
        finally:
            rconn.close()
    dconn = _db(cfg, "derived.db")
    if dconn is not None:
        lock = JobLock(cfg.data_dir)
        if apply and not lock.acquire():  # enrichment is running: try again at the next run
            dconn.close()
            raise RuntimeError("enrichment is running")
        try:
            tables = [r[0] for r in dconn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
            for table in tables:
                cols = [r[1] for r in dconn.execute("PRAGMA table_info(%s)" % table)]
                if "site_id" not in cols:
                    continue
                n = count(dconn, table)
                if apply and n:
                    _delete_in_batches(dconn, table, "site_id = ?", (site_id,))
                if n:
                    rows["derived.db:" + table] = n
        finally:
            if apply:
                lock.release()
            dconn.close()
    if apply:
        WebDB(cfg.data_dir).delete_site(site_id)
    return out


def purge_queued(cfg: Config) -> Dict[str, Any]:
    """Carry out the deletions the dashboard asked for (sites no longer configured only)."""
    from .webdb import WebDB
    web = WebDB(cfg.data_dir)
    done: Dict[str, Any] = {}
    for item in web.purges():
        sid = item["site_id"]
        if cfg.site(sid) is not None:  # configured again since: keep its data
            web.purge_done(sid)
            done[sid] = "skipped: the site is configured"
            continue
        try:
            done[sid] = purge_site(cfg, sid, apply=True)
            web.purge_done(sid)
        except (RuntimeError, sqlite3.Error) as exc:
            done[sid] = "postponed: %s" % exc
    return done


# ---------------------------------------------------------------------------
# Archiving: old raw data into yearly databases
# ---------------------------------------------------------------------------

def archive(cfg: Config, before: str, apply: bool = False, batch: int = 5000) -> Dict[str, Any]:
    """Move raw requests (and their hits) received before `before` (UTC date) to archive/events-YYYY.db.

    Enrichment reads the archives too, so derived data and rebuilds are unaffected.
    """
    cutoff = _utc_day_start(before)
    econn = _db(cfg, "events.db")
    if econn is None:
        return {"note": "no events.db"}
    years = econn.execute(
        "SELECT strftime('%Y', received_at / 1000, 'unixepoch') AS y, COUNT(*) AS n FROM raw_requests "
        "WHERE received_at < ? GROUP BY y ORDER BY y", (cutoff,)).fetchall()
    report: Dict[str, Any] = {"dry_run": not apply, "years": {r["y"]: r["n"] for r in years}}
    if not apply:
        econn.close()
        return report
    arch_dir = os.path.join(cfg.data_dir, "archive")
    os.makedirs(arch_dir, exist_ok=True)
    for r in years:
        year = r["y"]
        path = os.path.join(arch_dir, "events-%s.db" % year)
        aconn = connect(path, "delete", "full")
        migrate(aconn, EVENTS_MIGRATIONS)
        aconn.close()
        start = _utc_day_start("%s-01-01" % year)
        end = min(cutoff, _utc_day_start("%d-01-01" % (int(year) + 1)))
        econn.execute("ATTACH DATABASE ? AS arch", (path,))
        try:
            cols_r = [c[1] for c in econn.execute("PRAGMA main.table_info(raw_requests)")]
            cols_h = [c[1] for c in econn.execute("PRAGMA main.table_info(raw_hits)")]
            while True:
                ids = [x[0] for x in econn.execute(
                    "SELECT id FROM raw_requests WHERE received_at >= ? AND received_at < ? ORDER BY id LIMIT ?",
                    (start, end, batch))]
                if not ids:
                    break
                marks = ",".join("?" * len(ids))
                econn.execute("BEGIN IMMEDIATE")
                econn.execute("INSERT OR REPLACE INTO arch.raw_requests(%s) SELECT %s FROM main.raw_requests WHERE id IN (%s)"
                              % (",".join(cols_r), ",".join(cols_r), marks), ids)
                econn.execute("INSERT OR REPLACE INTO arch.raw_hits(%s) SELECT %s FROM main.raw_hits WHERE request_id IN (%s)"
                              % (",".join(cols_h), ",".join(cols_h), marks), ids)
                econn.execute("DELETE FROM main.raw_hits WHERE request_id IN (%s)" % marks, ids)
                econn.execute("DELETE FROM main.raw_requests WHERE id IN (%s)" % marks, ids)
                econn.execute("COMMIT")
        finally:
            econn.execute("DETACH DATABASE arch")
    econn.execute("VACUUM")
    econn.close()
    report["archive_dir"] = arch_dir
    return report


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

# Datasets: (database, query). Derived rows are selected by the site's local day; raw rows and visitors by
# server time, between the UTC midnights that start the first day and end the last (index-friendly bounds).
EXPORTS = {
    "sessions": ("derived.db", "SELECT * FROM sessions WHERE site_id = ? AND day BETWEEN ? AND ? ORDER BY started_at"),
    "pageviews": ("derived.db", "SELECT * FROM pageviews WHERE site_id = ? AND day BETWEEN ? AND ? ORDER BY ts"),
    "events": ("derived.db", "SELECT * FROM events WHERE site_id = ? AND day BETWEEN ? AND ? ORDER BY ts"),
    "errors": ("derived.db", "SELECT * FROM errors WHERE site_id = ? AND day BETWEEN ? AND ? ORDER BY ts"),
    "vitals": ("derived.db", "SELECT * FROM vitals WHERE site_id = ? AND day BETWEEN ? AND ? ORDER BY ts"),
    "clicks": ("derived.db", "SELECT * FROM clicks WHERE site_id = ? AND day BETWEEN ? AND ? ORDER BY ts"),
    "forms": ("derived.db", "SELECT * FROM forms WHERE site_id = ? AND day BETWEEN ? AND ? ORDER BY ts"),
    "daily": ("derived.db", "SELECT * FROM daily WHERE site_id = ? AND day BETWEEN ? AND ? ORDER BY day"),
    "visitors": ("derived.db", "SELECT * FROM visitors WHERE site_id = ? AND last_seen >= ? AND last_seen < ? "
                               "ORDER BY last_seen"),
    "search": ("search.db", "SELECT engine, day, query, page, clicks, impressions, position FROM search_rows "
                            "WHERE site_id = ? AND day BETWEEN ? AND ? ORDER BY engine, day, clicks DESC"),
    "raw": ("events.db", "SELECT h.*, r.ip, r.headers, r.envelope, r.site_status, r.endpoint FROM raw_hits h "
                         "JOIN raw_requests r ON r.id = h.request_id WHERE h.site_id = ? AND h.received_at >= ? "
                         "AND h.received_at < ? ORDER BY h.id"),
}
_MS_BOUNDS = {"visitors", "raw"}

# What the dashboard's export dialog says about each dataset, and how it counts its rows.
EXPORT_INFO: Dict[str, Dict[str, str]] = {
    "sessions": {"label": "Visits", "db": "derived.db",
                 "description": "One row per visit: start and end, source, medium, campaign, channel, landing and exit "
                                "page, location, device, browser, pageviews, duration, engaged time, and flags (bot, "
                                "internal, new visitor).",
                 "count": "SELECT COUNT(*) FROM sessions WHERE site_id = ? AND day BETWEEN ? AND ?"},
    "pageviews": {"label": "Pageviews", "db": "derived.db",
                  "description": "One row per page viewed: URL, path, title, referrer, time on page, engaged time, "
                                 "scroll depth, and whether it started or ended the visit.",
                  "count": "SELECT COUNT(*) FROM pageviews WHERE site_id = ? AND day BETWEEN ? AND ?"},
    "events": {"label": "Custom events", "db": "derived.db",
               "description": "One row per custom event: name, properties (JSON), value and page.",
               "count": "SELECT COUNT(*) FROM events WHERE site_id = ? AND day BETWEEN ? AND ?"},
    "errors": {"label": "JavaScript errors", "db": "derived.db",
               "description": "One row per error: message, source file, line, stack, page.",
               "count": "SELECT COUNT(*) FROM errors WHERE site_id = ? AND day BETWEEN ? AND ?"},
    "vitals": {"label": "Web Vitals", "db": "derived.db",
               "description": "One row per measurement: LCP, INP, CLS, FCP or TTFB, its value, rating and page.",
               "count": "SELECT COUNT(*) FROM vitals WHERE site_id = ? AND day BETWEEN ? AND ?"},
    "clicks": {"label": "Clicks", "db": "derived.db",
               "description": "One row per recorded click: element, text, link, position, and rage, dead or error "
                              "click flags.",
               "count": "SELECT COUNT(*) FROM clicks WHERE site_id = ? AND day BETWEEN ? AND ?"},
    "forms": {"label": "Form interactions", "db": "derived.db",
              "description": "One row per form or field interaction: start, field focus and time, submit, abandon "
                             "(never what visitors typed).",
              "count": "SELECT COUNT(*) FROM forms WHERE site_id = ? AND day BETWEEN ? AND ?"},
    "daily": {"label": "Daily totals", "db": "derived.db",
              "description": "One row per day: visitors, new visitors, visits, pageviews, events, bounces, engaged "
                             "visits and time.",
              "count": "SELECT COUNT(*) FROM daily WHERE site_id = ? AND day BETWEEN ? AND ?"},
    "visitors": {"label": "Visitors", "db": "derived.db",
                 "description": "One row per visitor seen in the period (last visit, UTC days): first and last seen, "
                                "visits, pageviews, engaged time, user id and linked browser ids.",
                 "count": "SELECT COUNT(*) FROM visitors WHERE site_id = ? AND last_seen >= ? AND last_seen < ?"},
    "search": {"label": "Search engine queries", "db": "search.db",
               "description": "One row per engine, day, query and page from Google Search Console and Bing Webmaster "
                              "Tools: clicks, impressions, average position (Google's days are Pacific Time days).",
               "count": "SELECT COUNT(*) FROM search_rows WHERE site_id = ? AND day BETWEEN ? AND ?"},
    "raw": {"label": "Raw hits", "db": "events.db",
            "description": "Every hit exactly as received (UTC days), with its request's IP address, headers and "
                           "envelope. Unfiltered and large: for audits, re-processing or moving to another tool.",
            "count": "SELECT COUNT(*) FROM raw_hits WHERE site_id = ? AND received_at >= ? AND received_at < ?"},
}


def export_args(what: str, site: str, start: Optional[str], end: Optional[str]) -> Tuple[Any, ...]:
    """The query arguments of a dataset for a site and a range of days (both bounds included)."""
    start, end = start or "0000-01-01", end or "9999-12-31"
    if what in _MS_BOUNDS:
        lo = _utc_day_start(start) if start > "0001" else 0
        hi = _utc_day_start(end) + DAY_MS if end < "9999" else 2 ** 62
        return site, lo, hi
    return site, start, end


def iter_export(cfg: Config, site: str, what: str, start: Optional[str], end: Optional[str]) -> Tuple[List[str], Iterable[Tuple]]:
    db, sql = EXPORTS[what]
    conn = _db(cfg, db, readonly=True)
    if conn is None:
        return [], iter(())
    cur = conn.execute(sql, export_args(what, site, start, end))
    cols = [d[0] for d in cur.description]

    def rows() -> Iterable[Tuple]:
        try:
            for row in cur:
                yield tuple(row)
        finally:
            conn.close()
    return cols, rows()


# Text a spreadsheet would run as a formula (CSV injection): stored values come from visitors' browsers.
_FORMULA_START = ("=", "+", "-", "@", chr(9), chr(13))


def _csv_cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, str) and v.startswith(_FORMULA_START):
        return "'" + v
    return v


def write_export(cols: List[str], rows: Iterable[Tuple], fmt: str, fh: IO[str]) -> int:
    n = 0
    if fmt == "csv":
        w = csv.writer(fh)
        w.writerow(cols)
        for row in rows:
            w.writerow([_csv_cell(v) for v in row])
            n += 1
    elif fmt == "jsonl":
        for row in rows:
            fh.write(json.dumps(dict(zip(cols, row)), ensure_ascii=False, default=lambda b: b.hex()) + "\n")
            n += 1
    else:
        fh.write("[")
        for row in rows:
            fh.write(("," if n else "") + "\n" + json.dumps(dict(zip(cols, row)), ensure_ascii=False,
                                                              default=lambda b: b.hex()))
            n += 1
        fh.write("\n]\n")
    return n


def export(cfg: Config, site: str, what: str, start: Optional[str], end: Optional[str], fmt: str = "csv",
           out: str = "-") -> Dict[str, Any]:
    cols, rows = iter_export(cfg, site, what, start, end)
    if out == "-":
        n = write_export(cols, rows, fmt, sys.stdout)
        sys.stdout.flush()
        return {}
    with open(out, "w", encoding="utf-8", newline="") as fh:
        n = write_export(cols, rows, fmt, fh)
    return {"rows": n, "file": out}
