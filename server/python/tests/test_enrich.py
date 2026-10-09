"""End-to-end enrichment: traffic posted through the collector, then derived.db checked."""

from __future__ import annotations

import gzip
import json
import os
import sqlite3

import pytest

from conftest import Harness, query_db
from mmdb_writer import write_database
from tidesight import collect
from tidesight.config import loads
from tidesight.enrich import pipeline
from tidesight.enrich.db import derived_path

CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/129.0.0.0 Safari/537.36")
HEADLESS = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "HeadlessChrome/129.0.0.0 Safari/537.36")
IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
          "Version/17.6 Mobile/15E148 Safari/604.1")
T0 = 1780300000000  # 2026-06-01T07:46:40Z (09:46 in Paris)
MIN = 60000

CITY = {
    "81.2.69.0/24": {"city": {"names": {"en": "London"}}, "continent": {"code": "EU"},
                     "country": {"iso_code": "GB", "names": {"en": "United Kingdom"}},
                     "location": {"latitude": 51.5142, "longitude": -0.0931, "time_zone": "Europe/London",
                                  "accuracy_radius": 10},
                     "subdivisions": [{"iso_code": "ENG", "names": {"en": "England"}}]},
    "89.160.20.0/24": {"city": {"names": {"en": "Linköping"}}, "continent": {"code": "EU"},
                       "country": {"iso_code": "SE", "names": {"en": "Sweden"}, "is_in_european_union": True},
                       "location": {"latitude": 58.4167, "longitude": 15.6167, "time_zone": "Europe/Stockholm",
                                    "accuracy_radius": 76}},
}
ASN = {
    "81.2.69.0/24": {"autonomous_system_number": 20712, "autonomous_system_organization": "Andrews & Arnold Ltd"},
    "52.0.0.0/11": {"autonomous_system_number": 16509, "autonomous_system_organization": "AMAZON-02"},
}


def make_config(tmp_path, extra: str = ""):
    geo = tmp_path / "geo"
    geo.mkdir(exist_ok=True)
    write_database(str(geo / "GeoLite2-City.mmdb"), list(CITY.items()), database_type="GeoLite2-City",
                   ipv4_aliases=True)
    write_database(str(geo / "GeoLite2-ASN.mmdb"), list(ASN.items()), database_type="GeoLite2-ASN")
    text = """
[server]
data_dir = "{data}"
rate_limit_per_minute = 0
secret = "test-secret"

[enrich]
geo_dir = "{geo}"
internal_ips = ["198.51.100.0/24"]

[sites.example]
origins = ["https://example.com"]
timezone = "Europe/Paris"
""".format(data=str(tmp_path / "data").replace("\\", "/"), geo=str(geo).replace("\\", "/")) + extra
    return loads(text, str(tmp_path / "tidesight.toml"))


class Traffic:
    """Posts through the real collector at chosen server times."""

    def __init__(self, cfg, monkeypatch) -> None:
        self.h = Harness(cfg)
        self.mp = monkeypatch

    def at(self, ms: int) -> "Traffic":
        self.mp.setattr(collect, "now_ms", lambda: ms)
        return self

    def batch(self, ms: int, vid: str, sid: str, events, ua: str = CHROME, ip: str = "81.2.69.142",
              sent=None, uid=None, site: str = "example", tries: int = 0, origin: str = "https://example.com",
              extra_headers=None):
        env = {"v": 1, "site": site, "vid": vid, "vt": T0 - 86400000, "sid": sid, "sent": sent if sent is not None else ms,
               "lib": "ts.js/test", "try": tries, "events": events}
        if uid:
            env["uid"] = uid
        headers = {"User-Agent": ua, "Origin": origin, "Accept-Language": "en-GB,en;q=0.9"}
        headers.update(extra_headers or {})
        r = self.at(ms).h.post(env, headers=headers, remote_addr=ip)
        assert r.code == 204, r.status
        return env

    def pixel(self, ms: int, ip: str, query: str = "site=example", ua: str = IPHONE):
        r = self.at(ms).h.call("GET", "/p.gif", query=query, headers={"User-Agent": ua,
                                                                       "Referer": "https://example.com/nojs"},
                                remote_addr=ip)
        assert r.code == 200


def pv(eid, pid, url, ts, ref="", title="T", **kw):
    ev = {"t": "pageview", "id": eid, "pid": pid, "url": url, "ts": ts, "ref": ref, "title": title, "nav": "navigate"}
    ev.update(kw)
    return ev


CTX = {"sw": 1920, "sh": 1080, "vw": 1536, "vh": 730, "dpr": 1.25, "lang": "en-GB", "tz": "Europe/London",
       "ua": CHROME, "uad": {"brands": [{"brand": "Google Chrome", "version": "129"}], "mobile": False,
                             "platform": "Windows", "platformVersion": "15.0.0",
                             "fullVersionList": [{"brand": "Google Chrome", "version": "129.0.6668.90"}]}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A small but complete traffic history."""
    cfg = make_config(tmp_path)
    tr = Traffic(cfg, monkeypatch)
    # Visitor A, first session: email campaign landing, 2 pages, a signup, a click, a vital, an error, a form.
    landing = "https://example.com/landing?utm_source=newsletter&utm_medium=email&utm_campaign=oct"
    tr.batch(T0, "visitorAAAA01", "sessA1", [
        pv("a1", "pA1", landing, T0 - 100, ns=1, sn=1, pn=1, new=1, idsrc="new", ctx=CTX),
        {"t": "fp", "id": "a2", "pid": "pA1", "ts": T0 - 50, "h": "fphash1", "fv": 1,
         "bot": {"wd": 0, "pl": 5, "lg": 2, "oz": 0, "hl": 0, "co": 1}},
    ])
    batch2 = [
        {"t": "click", "id": "a3", "pid": "pA1", "ts": T0 + 2000, "url": landing, "x": 100, "y": 200, "sel": "#cta",
         "tag": "a", "txt": "Sign up", "ex": 0.5, "ey": 0.4, "pw": 1536, "ph": 3000, "vw": 1536, "vh": 730},
        {"t": "event", "id": "a4", "pid": "pA1", "ts": T0 + 2100, "url": landing, "n": "signup", "p": {"plan": "pro"},
         "val": 49},
        {"t": "vital", "id": "a5", "pid": "pA1", "ts": T0 + 2200, "url": landing, "n": "LCP", "val": 1800, "r": "good",
         "el": "img.hero"},
        {"t": "error", "id": "a6", "pid": "pA1", "ts": T0 + 2300, "url": landing, "k": "error",
         "msg": "TypeError: x is undefined", "src": "https://example.com/app.js", "line": 10, "col": 5},
        {"t": "form", "id": "a7", "pid": "pA1", "ts": T0 + 2400, "url": landing, "a": "submit", "f": "signup", "n": 3},
        {"t": "engagement", "id": "a8", "pid": "pA1", "ts": T0 + 5000, "url": landing, "ms": 5000, "tot": 5000,
         "why": "route"},
        pv("a9", "pA2", "https://example.com/pricing?q=plans", T0 + 5100, ref=landing, pn=2, sn=1, ctx=CTX),
        {"t": "scroll", "id": "a10", "pid": "pA2", "ts": T0 + 9000, "url": "https://example.com/pricing", "d": 75,
         "max": 75},
        {"t": "engagement", "id": "a11", "pid": "pA2", "ts": T0 + 13100, "url": "https://example.com/pricing",
         "ms": 8000, "tot": 8000, "why": "hidden", "sd": 80},
    ]
    tr.batch(T0 + 13200, "visitorAAAA01", "sessA1", batch2)
    # The same batch retried (the response was lost): every event is a duplicate.
    tr.batch(T0 + 20000, "visitorAAAA01", "sessA1", batch2, tries=1)
    # Visitor B: a headless browser from AWS.
    tr.batch(T0 + 1000, "visitorBBBB01", "sessB1", [
        pv("b1", "pB1", "https://example.com/", T0 + 900, ns=1, sn=1, pn=1, new=1),
        {"t": "fp", "id": "b2", "pid": "pB1", "ts": T0 + 950, "h": "fphashB",
         "bot": {"wd": 1, "pl": 0, "lg": 1, "oz": 1, "hl": 1, "co": 0, "au": ["__pwInitScripts"]}},
    ], ua=HEADLESS, ip="52.1.2.3")
    # Visitor A again the next day: organic search, identifies as user-1. Its clock is one hour fast.
    day2 = T0 + 86400000
    tr.batch(day2, "visitorAAAA01", "sessA2", [
        pv("c1", "pA3", "https://example.com/blog/post", day2 + 3600000 - 500, ref="https://www.google.com/",
           ns=1, sn=2, pn=1, ctx=CTX),
        {"t": "identify", "id": "c2", "pid": "pA3", "ts": day2 + 3600000 - 400, "uid": "user-1"},
    ], sent=day2 + 3600000, uid="user-1")
    # Visitor C (another browser) identifies as the same user.
    tr.batch(day2 + 5000, "visitorCCCC01", "sessC1", [
        pv("d1", "pC1", "https://example.com/account", day2 + 4900, ns=1, sn=1, pn=1, new=1),
    ], ua=IPHONE, ip="89.160.20.112", uid="user-1")
    # No-JS pixels from one IP: two within 10 minutes (one session), one 40 minutes later (another).
    tr.pixel(T0, "89.160.20.112")
    tr.pixel(T0 + 10 * MIN, "89.160.20.112")
    tr.pixel(T0 + 50 * MIN, "89.160.20.112")
    # An unknown site, and an internal visit.
    tr.batch(T0 + 3000, "visitorDDDD01", "sessD1", [pv("e1", "pD1", "https://other.org/", T0 + 2900)], site="nope",
             origin="https://other.org")
    tr.batch(T0 + 4000, "visitorEEEE01", "sessE1", [pv("f1", "pE1", "https://example.com/", T0 + 3900, sn=1)],
             ip="198.51.100.7")
    tr.h.close()
    return cfg


def sessions(cfg):
    rows = query_db(derived_path(cfg.data_dir), "SELECT * FROM sessions ORDER BY started_at")
    return {r["session_key"]: r for r in rows}


def test_full_pipeline(world):
    stats = pipeline.run(world)
    assert stats["hits"] == 30 and stats["duplicates"] == 9
    s = sessions(world)
    a1 = s["example/sessA1"]
    assert (a1["pageviews"], a1["events"], a1["engaged_ms"], a1["engaged"], a1["bounce"]) == (2, 1, 13000, 1, 0)
    assert (a1["entry_path"], a1["exit_path"], a1["is_new"], a1["session_number"]) == ("/landing", "/pricing", 1, 1)
    assert (a1["source"], a1["medium"], a1["campaign"], a1["channel"]) == ("newsletter", "email", "oct", "Email")
    assert (a1["country"], a1["city"], a1["region"], a1["asn"]) == ("GB", "London", "ENG", 20712)
    assert (a1["browser"], a1["os"], a1["device_type"]) == ("Chrome", "Windows", "desktop")
    assert a1["os_version"] == "11"  # from the high-entropy Client Hints
    assert (a1["screen"], a1["viewport"], a1["language"], a1["timezone"]) == ("1920x1080", "1536x730", "en-GB",
                                                                               "Europe/London")
    assert (a1["is_bot"], a1["internal"], a1["has_js"], a1["fp"], a1["errors"]) == (0, 0, 1, "fphash1", 1)
    assert a1["day"] == "2026-06-01" and a1["site_ok"] == 1
    assert a1["duration_ms"] == 13100 + 100  # first pageview at T0-100, last activity T0+13100

    b1 = s["example/sessB1"]
    assert b1["is_bot"] == 1 and b1["is_hosting"] == 1 and b1["bot_kind"] in ("headless", "automation")

    a2 = s["example/sessA2"]
    assert (a2["source"], a2["medium"], a2["channel"]) == ("google", "organic", "Organic Search")
    assert a2["uid"] == "user-1" and a2["is_new"] == 0
    # Clock skew: the client was an hour fast; the effective time is back on server time.
    assert abs(a2["started_at"] - (T0 + 86400000 - 500)) < 1000

    c1 = s["example/sessC1"]
    assert c1["visitor_key"] == a1["visitor_key"] == a2["visitor_key"] == "example/visitorAAAA01"
    assert c1["device_type"] == "mobile" and c1["country"] == "SE"

    px = [k for k in s if "/px-" in k]
    assert len(px) == 2
    first_px = s[sorted(px, key=lambda k: s[k]["started_at"])[0]]
    assert first_px["pageviews"] == 2 and first_px["has_js"] == 0 and first_px["entry_path"] == "/nojs"

    assert s["nope/sessD1"]["site_ok"] == 0
    assert s["example/sessE1"]["internal"] == 1

    db = derived_path(world.data_dir)
    pvs = query_db(db, "SELECT * FROM pageviews WHERE session_key = 'example/sessA1' ORDER BY seq")
    assert [(p["path"], p["engaged_ms"], p["scroll_max"], p["is_entry"], p["is_exit"]) for p in pvs] == [
        ("/landing", 5000, None, 1, 0), ("/pricing", 8000, 80, 0, 1)]
    assert pvs[0]["duration_ms"] == 5200
    assert query_db(db, "SELECT name, props, value FROM events") == [{"name": "signup", "props": '{"plan":"pro"}',
                                                                      "value": 49.0}]
    assert query_db(db, "SELECT COUNT(*) AS n FROM clicks")[0]["n"] == 1
    assert query_db(db, "SELECT metric, value, rating FROM vitals") == [{"metric": "LCP", "value": 1800.0,
                                                                        "rating": "good"}]
    err = query_db(db, "SELECT kind, msg, line, grp FROM errors")[0]
    assert err["kind"] == "error" and err["line"] == 10 and len(err["grp"]) == 16
    assert query_db(db, "SELECT query, origin FROM searches") == [{"query": "plans", "origin": "url"}]

    visitors = {v["visitor_key"]: v for v in query_db(db, "SELECT * FROM visitors")}
    a = visitors["example/visitorAAAA01"]
    assert a["sessions"] == 3 and a["uid"] == "user-1" and a["confidence"] == 1.0
    assert json.loads(a["vids"]) == ["visitorAAAA01", "visitorCCCC01"]

    daily = {r["day"]: r for r in query_db(db, "SELECT * FROM daily WHERE site_id = 'example'")}
    d1 = daily["2026-06-01"]
    # Clean traffic only: A1 and the two pixel sessions (B is a bot, E internal, D another site).
    assert (d1["sessions"], d1["bots"], d1["pageviews"]) == (3, 1, 2 + 3)
    dims = {(r["dim"], r["value"]): r for r in query_db(db, "SELECT * FROM daily_dim WHERE day = '2026-06-01'")}
    assert dims[("channel", "Email")]["sessions"] == 1
    assert dims[("page", "/pricing")]["pageviews"] == 1
    assert dims[("event", "signup")]["pageviews"] == 1


def _dump(db):
    conn = sqlite3.connect(db)
    out = {}
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('job_state')"):
        out[t] = sorted(map(repr, conn.execute("SELECT * FROM %s" % t).fetchall()))
    conn.close()
    return out


def test_idempotent_and_rebuild_identical(world):
    pipeline.run(world)
    db = derived_path(world.data_dir)
    first = _dump(db)
    again = pipeline.run(world)
    assert again["hits"] == 0
    assert _dump(db) == first
    rebuilt = pipeline.run(world, rebuild=True)
    assert rebuilt["rebuilt"] and rebuilt["hits"] == 30
    second = _dump(db)
    # ua ids may be assigned in another order; everything else is identical.
    for table in first:
        if table not in ("ua", "requests", "sessions"):
            assert second[table] == first[table], table
    assert len(second["sessions"]) == len(first["sessions"])


def test_version_change_triggers_rebuild(world, monkeypatch):
    pipeline.run(world)
    monkeypatch.setattr(pipeline, "PIPELINE_VERSION", 999)
    stats = pipeline.run(world)
    assert stats.get("rebuilt") and stats["hits"] == 30


def test_incremental_batches(world):
    e = pipeline.Enricher(world)
    stats = e.run(batch_size=3)
    assert stats["batches"] >= 9 and stats["hits"] == 30
    s = sessions(world)
    assert s["example/sessA1"]["pageviews"] == 2 and s["example/sessA1"]["engaged_ms"] == 13000


def test_no_geo_databases_degrades_gracefully(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    cfg.enrich["geo_dir"] = str(tmp_path / "missing")
    tr = Traffic(cfg, monkeypatch)
    tr.batch(T0, "visitorAAAA01", "sessA1", [pv("a1", "pA1", "https://example.com/", T0)])
    tr.h.close()
    pipeline.run(cfg)
    s = sessions(cfg)["example/sessA1"]
    assert s["country"] is None and s["pageviews"] == 1


def test_privacy_clean_keeps_geo(world):
    from tidesight.maintenance import privacy_clean
    pipeline.run(world)
    dry = privacy_clean(world, hash_ip=True)
    assert dry["dry_run"] and dry["steps"]["ip"]["requests"] > 0
    events = os.path.join(world.data_dir, "events.db")
    assert query_db(events, "SELECT COUNT(*) AS n FROM raw_requests WHERE ip LIKE 'h:%'")[0]["n"] == 0
    done = privacy_clean(world, hash_ip=True, drop_fingerprint=True, apply=True)
    assert done["backup"]["files"]
    assert query_db(events, "SELECT COUNT(*) AS n FROM raw_requests WHERE ip NOT LIKE 'h:%'")[0]["n"] == 0
    assert query_db(events, "SELECT COUNT(*) AS n FROM raw_hits WHERE type = 'fp'")[0]["n"] == 0
    stats = pipeline.run(world)  # stale version → rebuild from the cleaned raw data
    assert stats.get("rebuilt")
    a1 = sessions(world)["example/sessA1"]
    assert a1["country"] == "GB" and a1["city"] == "London"  # kept through the geo annotation
    assert a1["client_ip"].startswith("h:") and a1["fp"] is None


def test_retention_and_rollups_survive_rebuild(world):
    from tidesight.maintenance import privacy_clean
    pipeline.run(world)
    world.retention["raw_days"] = 1
    now = T0 + 86400000 + 12 * 3600000  # everything from the first day is older than a day
    privacy_clean(world, apply_retention=True, apply=True, backup_first=False, now_ms=now)
    stats = pipeline.run(world)
    assert stats.get("rebuilt")
    db = derived_path(world.data_dir)
    assert "example/sessA1" not in sessions(world)
    days = [r["day"] for r in query_db(db, "SELECT day FROM daily WHERE site_id = 'example' ORDER BY day")]
    assert "2026-06-01" in days  # aggregates of deleted days are kept


def test_archive_keeps_enrichment_complete(world):
    from tidesight.maintenance import archive
    pipeline.run(world)
    before = len(sessions(world))
    assert archive(world, before="2026-06-02")["dry_run"]
    report = archive(world, before="2026-06-02", apply=True)
    assert report["years"] == {"2026": 9}
    assert os.path.exists(os.path.join(world.data_dir, "archive", "events-2026.db"))
    stats = pipeline.run(world, rebuild=True)
    assert stats["hits"] == 30 and len(sessions(world)) == before


def test_export_and_backup(world, tmp_path):
    from tidesight.maintenance import backup, export
    pipeline.run(world)
    out = tmp_path / "sessions.csv"
    res = export(world, site="example", what="sessions", start="2026-06-01", end="2026-06-02", fmt="csv",
                 out=str(out))
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert res["rows"] == len(lines) - 1 >= 5 and lines[0].startswith("session_key,")
    b = backup(world, keep=1, with_replay=True, with_derived=True)
    names = sorted(f["file"] for f in b["files"])
    assert names == ["derived.db", "events.db"]
    with gzip.open(os.path.join(b["backup"], "events.db.gz"), "rb") as fh:
        assert fh.read(16) == b"SQLite format 3\x00"
    # The dashboard's data and the settings of the data directory are kept too, once they exist.
    from tidesight import properties, users
    from tidesight.webdb import WebDB
    WebDB(world.data_dir).add_annotation("example", "2026-06-01", "launch", "admin")
    properties.update(world.data_dir, lambda tables: tables.setdefault("shop", {"origins": ["https://shop.example"]}))
    users.update(world.data_dir, lambda table: table.setdefault("admin", "pbkdf2_sha256$1000$c2FsdA$aGFzaA"))
    b = backup(world, keep=1)
    assert sorted(f["file"] for f in b["files"]) == ["events.db", "properties.json", "users.json", "web.db"]
    assert json.load(open(os.path.join(b["backup"], "users.json"), encoding="utf-8"))["users"] == {
        "admin": "pbkdf2_sha256$1000$c2FsdA$aGFzaA"}


def test_page_status_from_meta_and_status_event(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    tr = Traffic(cfg, monkeypatch)
    tr.batch(T0, "visitorSTAT01", "sessS1", [
        pv("s1", "pS1", "https://example.com/missing", T0 - 100, ns=1, sn=1, pn=1, st=404),
        pv("s2", "pS2", "https://example.com/gone", T0 + 1000, sn=1, pn=2),
        {"t": "status", "id": "s3", "pid": "pS2", "ts": T0 + 1100, "url": "https://example.com/gone", "st": "410"},
        pv("s4", "pS3", "https://example.com/ok", T0 + 2000, sn=1, pn=3),
        {"t": "status", "id": "s5", "pid": "pS3", "ts": T0 + 2100, "url": "https://example.com/ok", "st": "oops"},
    ])
    tr.h.close()
    pipeline.run(cfg)
    rows = query_db(derived_path(cfg.data_dir), "SELECT path, status FROM pageviews ORDER BY seq")
    assert [(r["path"], r["status"]) for r in rows] == [("/missing", 404), ("/gone", 410), ("/ok", None)]


def test_per_hit_lookups_have_indexes(world):
    # Without them every hit scans a table, and enrichment time grows with the square of the data.
    pipeline.run(world)
    conn = sqlite3.connect(derived_path(world.data_dir))

    def indexed(table, cols):
        for idx in conn.execute("PRAGMA index_list(%s)" % table):
            if [r[2] for r in conn.execute("PRAGMA index_info(%s)" % idx[1])][:len(cols)] == cols:
                return True
        return False

    for table, cols in (("vitals", ["page_key", "metric"]), ("errors", ["page_key", "grp"]),
                        ("hits", ["session_key"]), ("errors", ["session_key"]), ("pageviews", ["session_key"])):
        assert indexed(table, cols), (table, cols)
    conn.close()


POISON = [  # values a client can send that once crashed enrichment (and every later run) on the same hit
    {"sent": 1e300}, {"sent": 10 ** 30},
    {"events": [{"t": "pageview", "id": "p1", "pid": "pp1", "url": "https://example.com/", "st": "\u00b2"}]},
    {"events": [{"t": "engagement", "id": "p2", "pid": "pp1", "ms": 10 ** 400}]},
    {"events": [{"t": "pageview", "id": "p3", "pid": "pp2", "url": "https://example.com/", "title": ["x"]}]},
    {"events": [{"t": "pageview", "id": "p4", "pid": "pp3", "url": "https://example.com/", "ctx": {"ua": 1}}]},
    {"events": [{"t": "event", "id": "p5", "pid": "pp1", "n": "x", "ts": -9000000000000000000}]},
    {"events": [{"t": "fp", "id": "p6", "pid": "pp1", "h": "x", "bot": {"lg": "__INF__"}}]}, {"sent": "__INF__"},
    {"events": [{"t": "pageview", "id": "p7", "pid": "pp4", "url": "https://example.com/", "ctx": {"tp": "__INF__"}}]},
    {"events": [{"t": "pageview", "id": "p8", "pid": "pp5", "url": "https://example.com/",
                 "ref": "http://" + "a." * 30000 + "com/"}]},
]


def test_malformed_values_never_stop_enrichment(tmp_path, monkeypatch):
    import time as _time
    cfg = make_config(tmp_path)
    tr = Traffic(cfg, monkeypatch)
    # The collector now refuses numbers too large for a float (1e999); data stored by older versions may still
    # hold them, so enrichment must cope: post as an older collector would have stored them.
    monkeypatch.setattr(collect, "_finite_float", float)
    for i, poison in enumerate(POISON):
        env = {"events": [pv("ok%d" % i, "pok%d" % i, "https://example.com/p%d" % i, T0 + i * 1000, ns=1, sn=1, pn=1)]}
        env.update(poison)
        events = env.pop("events")
        body = json.dumps(dict({"v": 1, "site": "example", "vid": "visitorPOIS%02d" % i, "sid": "sessP%02d" % i,
                                "sent": T0 + i * 1000, "events": events}, **env)).replace('"__INF__"', "1e999")
        r = tr.at(T0 + i * 1000).h.post(None, raw=body.encode(), headers={"Origin": "https://example.com", "User-Agent": CHROME})
        assert r.code == 204, (i, r.status)
    tr.batch(T0 + 60000, "visitorGOOD01", "sessGOOD1", [pv("g1", "pg1", "https://example.com/good", T0 + 59000, sn=1)])
    tr.h.close()
    started = _time.monotonic()
    stats = pipeline.run(cfg)
    assert _time.monotonic() - started < 20
    db = derived_path(cfg.data_dir)
    assert query_db(db, "SELECT COUNT(*) AS n FROM sessions WHERE session_key = 'example/sessGOOD1'")[0]["n"] == 1
    raw_max = query_db(os.path.join(cfg.data_dir, "events.db"), "SELECT MAX(id) AS m FROM raw_hits")[0]["m"]
    assert query_db(db, "SELECT last_id FROM job_state WHERE job = 'enrich'")[0]["last_id"] == raw_max
    assert pipeline.run(cfg)["hits"] == 0  # nothing left to retry forever
    assert stats.get("failures", 0) == len(query_db(db, "SELECT id FROM failures"))


def test_privacy_clean_covers_the_archives(world):
    from tidesight.maintenance import archive, privacy_clean
    pipeline.run(world)
    archive(world, before="2026-06-02", apply=True)
    arch = os.path.join(world.data_dir, "archive", "events-2026.db")
    assert query_db(arch, "SELECT COUNT(*) AS n FROM raw_requests WHERE ip NOT LIKE 'h:%'")[0]["n"] > 0
    dry = privacy_clean(world, hash_ip=True, drop_fingerprint=True)
    assert dry["steps"]["ip"]["files"] == 2 and dry["steps"]["fingerprint"]["fp_hits"] == 2
    done = privacy_clean(world, hash_ip=True, drop_fingerprint=True, apply=True)
    assert any(f["file"].startswith("archive") for f in done["backup"]["files"])
    for db in (arch, os.path.join(world.data_dir, "events.db")):
        assert query_db(db, "SELECT COUNT(*) AS n FROM raw_requests WHERE ip NOT LIKE 'h:%'")[0]["n"] == 0
        assert query_db(db, "SELECT COUNT(*) AS n FROM raw_hits WHERE type = 'fp'")[0]["n"] == 0
        headers = [json.loads(r["headers"]) for r in query_db(db, "SELECT headers FROM raw_requests")]
        assert not any("198.51.100" in h.get("x-forwarded-for", "") for h in headers)
    pipeline.run(world)  # the rebuild finds no raw address anywhere
    assert all((s["client_ip"] or "h:").startswith("h:") for s in sessions(world).values())


def test_session_rebuilds_stay_linear(tmp_path, monkeypatch):
    """Anyone can send hits: 8,000 pageviews sharing one page id once took half a minute to rebuild (and every
    later hit of that session as long again), stalling enrichment for every site."""
    import time as _time
    cfg = make_config(tmp_path)
    tr = Traffic(cfg, monkeypatch)
    for b in range(40):
        tr.batch(T0 + b * MIN, "visitorFLOOD1", "sessFLOOD1", [
            {"t": "pageview", "id": "f%d-%d" % (b, i), "pid": "SAME", "url": "https://example.com/x",
             "ts": T0 + b * MIN + i} for i in range(200)])
    tr.h.close()
    started = _time.monotonic()
    pipeline.run(cfg)
    assert _time.monotonic() - started < 10
    row = sessions(cfg)["example/sessFLOOD1"]
    assert row["pageviews"] == 8000 and row["hits"] == 8000


def test_flooded_session_is_a_bot_built_once(tmp_path, monkeypatch):
    from tidesight.enrich import sessions as sessions_mod
    monkeypatch.setattr(sessions_mod, "MAX_SESSION_HITS", 100)
    cfg = make_config(tmp_path)
    tr = Traffic(cfg, monkeypatch)
    tr.batch(T0, "visitorFLOOD2", "sessFLOOD2", [
        pv("g%d" % i, "pg%d" % i, "https://example.com/p%d" % i, T0 + i, sn=1, pn=i + 1) for i in range(150)])
    pipeline.run(cfg)
    row = sessions(cfg)["example/sessFLOOD2"]
    assert (row["is_bot"], row["bot_kind"], row["hits"], row["pageviews"]) == (1, "automation", 150, 100)
    assert "too_many_hits" in json.loads(row["bot_reasons"])
    tr.batch(T0 + MIN, "visitorFLOOD2", "sessFLOOD2", [pv("late", "plate", "https://example.com/late", T0 + MIN)])
    tr.h.close()
    pipeline.run(cfg)
    assert sessions(cfg)["example/sessFLOOD2"]["hits"] == 150  # not rebuilt for each new hit
