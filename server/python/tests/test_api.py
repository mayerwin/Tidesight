"""Read API and web app: numbers checked against the known traffic of the enrichment fixture."""

from __future__ import annotations

import base64
import gzip
import json
import os
import sqlite3

import pytest

from conftest import call
from test_enrich import T0, Traffic, make_config, pv, world  # noqa: F401  (fixture)
from tidesight.api import Api, ApiError
from tidesight.auth import Throttle, hash_password, verify_password
from tidesight.enrich import pipeline
from tidesight.wsgi import create_app

RANGE = {"site": "example", "from": "2026-06-01", "to": "2026-06-02"}


@pytest.fixture
def api(world):  # noqa: F811
    pipeline.run(world)
    return Api(world)


def test_overview(api):
    res = api.call("overview", dict(RANGE))
    t = res["totals"]
    # Clean traffic: A's two visits, C's visit (same stitched visitor), two no-JS pixel visits.
    assert (t["sessions"], t["visitors"], t["pageviews"]) == (5, 2, 2 + 3 + 1 + 1)
    assert res["interval"] == "day" and [p["t"] for p in res["series"]] == ["2026-06-01", "2026-06-02"]
    assert [p["sessions"] for p in res["series"]] == [3, 2]
    # Engaged (GA4): A1 (13 s, 2 pages) and the first pixel visit (2 pages). The other three bounce.
    assert t["bounce_rate"] == pytest.approx(3 / 5)
    one_day = api.call("overview", dict(RANGE, to="2026-06-01"))
    assert one_day["interval"] == "hour" and len(one_day["series"]) == 24
    assert api.call("overview", dict(RANGE, bots="1"))["totals"]["sessions"] == 6


def test_filters_and_breakdowns(api):
    channels = {r["value"]: r for r in api.call("breakdown", dict(RANGE, dim="channel"))["rows"]}
    assert channels["Email"]["sessions"] == 1 and channels["Organic Search"]["sessions"] == 1
    assert channels["Direct"]["sessions"] == 3
    gb = api.call("overview", dict(RANGE, **{"f.country": "GB"}))["totals"]
    assert gb["sessions"] == 2
    pages = {r["value"]: r for r in api.call("breakdown", dict(RANGE, dim="page"))["rows"]}
    assert pages["/landing"]["pageviews"] == 1 and pages["/nojs"]["pageviews"] == 3
    via_page = api.call("overview", dict(RANGE, **{"f.page": "/pricing"}))["totals"]
    assert via_page["sessions"] == 1
    via_event = api.call("overview", dict(RANGE, **{"f.event": "signup"}))["totals"]
    assert via_event["sessions"] == 1
    cities = api.call("breakdown", dict(RANGE, dim="city"))["rows"]
    assert {r["value"]: r["country"] for r in cities}["London"] == "GB"
    assert api.call("breakdown", dict(RANGE, dim="event"))["rows"][0]["value"] == "signup"
    with pytest.raises(ApiError):
        api.call("breakdown", dict(RANGE, dim="nope"))
    with pytest.raises(ApiError):
        api.call("overview", dict(RANGE, **{"f.nope": "x"}))
    with pytest.raises(ApiError):
        api.call("overview", dict(RANGE, site="unknown"))


def test_pages_events_funnel_retention(api):
    pages = {r["value"]: r for r in api.call("pages", dict(RANGE))["rows"]}
    assert pages["/pricing"]["avg_scroll"] == 80 and pages["/landing"]["avg_engaged"] == 5000
    ev = api.call("events", dict(RANGE, name="signup"))
    assert ev["count"] == 1 and ev["total_value"] == 49 and ev["props"]["plan"][0] == {"value": "pro", "count": 1}
    steps = [{"type": "page", "value": "/landing"}, {"type": "event", "value": "signup"}, {"type": "page", "value": "/pri*"}]
    funnel = api.call("funnel", dict(RANGE, steps=json.dumps(steps)))
    assert [s["sessions"] for s in funnel["steps"]] == [1, 1, 1]
    assert funnel["steps"][2]["conversion"] == 1.0 and funnel["steps"][1]["median_ms"] == 2200
    wrong_order = api.call("funnel", dict(RANGE, steps=json.dumps(list(reversed(steps)))))
    assert [s["sessions"] for s in wrong_order["steps"]] == [1, 0, 0]
    ret = api.call("retention", dict(RANGE, period="day"))
    first = ret["cohorts"][0]
    assert first["cohort"] == "2026-06-01" and first["visitors"] == 2 and first["retention"][1] == 0.5


def test_quality_endpoints(api):
    perf = api.call("performance", dict(RANGE))
    assert perf["vitals"]["LCP"]["p75"] == 1800 and perf["vitals"]["LCP"]["rating"] == "good"
    errors = api.call("errors", dict(RANGE))["rows"]
    assert errors[0]["msg"] == "TypeError: x is undefined" and errors[0]["sessions"] == 1
    occ = api.call("errors", dict(RANGE, grp=errors[0]["grp"]))["occurrences"]
    assert occ[0]["browser"] == "Chrome"
    heat = api.call("heatmap", dict(RANGE, page="/landing"))
    assert len(heat["clicks"]) == 1 and heat["selectors"][0] == {"sel": "#cta", "clicks": 1, "rage": 0, "dead": 0, "errors": 0}
    assert heat["scroll"][0] == {"depth": 0, "reached": 0}  # /landing has no scroll data
    forms = api.call("forms", dict(RANGE))["rows"]
    assert forms[0]["form"] == "signup" and forms[0]["submits"] == 1
    assert api.call("search", dict(RANGE))["rows"][0]["value"] == "plans"
    geo = api.call("geo", dict(RANGE))
    assert {r["value"] for r in geo["countries"]} == {"GB", "SE"}
    assert any(p["city"] == "London" for p in geo["points"])


def test_people_endpoints(api):
    sessions = api.call("sessions", dict(RANGE))["rows"]
    assert len(sessions) == 5 and sessions[0]["started_at"] >= sessions[-1]["started_at"]
    detail = api.call("session", {"site": "example", "key": "example/sessA1"})
    assert detail["session"]["pageviews"] == 2 and len(detail["pageviews"]) == 2
    types = [h["type"] for h in detail["timeline"] if not h["dup"]]
    assert types[0] == "pageview" and "click" in types and detail["timeline"][0]["data"]["title"] == "T"
    v = api.call("visitor", {"site": "example", "key": "example/visitorAAAA01"})
    assert v["visitor"]["sessions"] == 3 and v["visitor"]["vids"] == ["visitorAAAA01", "visitorCCCC01"]
    assert {i["vid"]: i["confidence"] for i in v["ids"]} == {"visitorAAAA01": 1.0, "visitorCCCC01": 1.0}
    with pytest.raises(ApiError):
        api.call("session", {"site": "example", "key": "nope"})
    assert api.call("replays", dict(RANGE))["rows"] == []
    status = api.call("status", {})
    assert status["files"]["events.db"] > 0 and status["enrich"]["last_id"] > 0


def test_not_found_pages(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    tr = Traffic(cfg, monkeypatch)
    tr.batch(T0, "visitorNF0001", "sessN1", [
        pv("n1", "pN1", "https://example.com/old", T0 - 100, ref="https://blog.example.org/post", ns=1, sn=1, pn=1, st=404),
        pv("n2", "pN2", "https://example.com/", T0 + 1000, ref="https://example.com/old", sn=1, pn=2),
    ])
    tr.batch(T0 + 60000, "visitorNF0002", "sessN2", [
        pv("n3", "pN3", "https://example.com/old", T0 + 59000, ns=1, sn=1, pn=1, st=404),
        pv("n4", "pN4", "https://example.com/gone", T0 + 61000, ref="https://example.com/old", sn=1, pn=2),
        {"t": "status", "id": "n5", "pid": "pN4", "ts": T0 + 61500, "url": "https://example.com/gone", "st": 410},
    ])
    tr.h.close()
    pipeline.run(cfg)
    rows = Api(cfg).call("not_found", dict(RANGE))["rows"]
    assert [(r["value"], r["status"], r["pageviews"], r["visitors"]) for r in rows] == [("/old", 404, 2, 2), ("/gone", 410, 1, 1)]
    assert rows[0]["referrers"] == [{"value": "https://blog.example.org/post", "count": 1}]
    assert rows[1]["referrers"] == [{"value": "https://example.com/old", "count": 1}]
    assert (rows[0]["last_seen"], rows[1]["last_seen"]) == (T0 + 59000, T0 + 61000)


def test_realtime(tmp_path, monkeypatch):
    import time as _time
    cfg = make_config(tmp_path)
    tr = Traffic(cfg, monkeypatch)
    now = int(_time.time() * 1000)
    tr.batch(now - 60000, "visitorRRRR01", "sessR1", [pv("r1", "pR1", "https://example.com/live", now - 60000,
                                                         ref="https://news.ycombinator.com/")])
    tr.h.close()
    res = Api(cfg).call("realtime", {"site": "example"})
    assert res["active"] == 1 and res["pages"] == [{"value": "/live", "pageviews": 1}]
    assert res["referrers"] == [{"value": "news.ycombinator.com", "pageviews": 1}]
    assert sum(p["pageviews"] for p in res["per_minute"]) == 1 and res["recent"][0]["path"] == "/live"


def test_passwords():
    h = hash_password("s3cret", iterations=1000)
    assert h.startswith("pbkdf2_sha256$1000$") and verify_password("s3cret", h) and not verify_password("nope", h)
    assert not verify_password("s3cret", "garbage")


def _basic(user, pw):
    return "Basic " + base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()


def test_web_app_auth_and_share(world, tmp_path):  # noqa: F811
    pipeline.run(world)
    app = create_app(config=world)
    r = call(app, "GET", "/dashboard/api/overview", query="site=example")
    assert r.code == 403  # no users configured: the dashboard is off
    world.dashboard["users"] = {"admin": hash_password("pw", iterations=1000)}
    assert call(app, "GET", "/dashboard/api/sites").code == 401
    assert call(app, "GET", "/dashboard/api/sites", headers={"Authorization": _basic("admin", "bad")}).code == 401
    ok = call(app, "GET", "/dashboard/api/sites", headers={"Authorization": _basic("admin", "pw")})
    assert ok.code == 200 and ok.json()["sites"][0]["id"] == "example"
    page = call(app, "GET", "/dashboard/", headers={"Authorization": _basic("admin", "pw")})
    assert page.code == 200 and b"<title>Tidesight</title>" in page.body and "content-security-policy" in page.headers
    assert call(app, "GET", "/dashboard/js/main.js", headers={"Authorization": _basic("admin", "pw")}).code == 200
    assert call(app, "GET", "/dashboard/../config.py", headers={"Authorization": _basic("admin", "pw")}).code == 404
    csv = call(app, "GET", "/dashboard/api/export", query="site=example&what=sessions&from=2026-06-01&to=2026-06-02",
               headers={"Authorization": _basic("admin", "pw")})
    # Exports are complete: all 7 sessions of the site, flagged ones (bot, internal) included, plus the header.
    assert csv.code == 200 and csv.headers["content-type"].startswith("text/csv") and csv.body.count(b"\n") == 8
    # Public share page: only for public sites, only allowed metrics.
    assert call(app, "GET", "/share/example/api/overview").code == 404
    site = world.site("example")
    site.settings["public"] = True
    site.settings["share_token"] = "tok"
    assert call(app, "GET", "/share/example/api/overview").code == 403
    pub = call(app, "GET", "/share/example/api/overview", query="t=tok&from=2026-06-01&to=2026-06-02")
    assert pub.code == 200 and set(pub.json()["totals"]) == {"visitors", "pageviews"}
    assert call(app, "GET", "/share/example/api/sessions", query="t=tok").code == 403
    assert call(app, "GET", "/share/example/api/breakdown", query="t=tok&dim=browser").code == 200
    assert call(app, "GET", "/share/example/api/breakdown", query="t=tok&dim=uid").code == 403
    assert call(app, "GET", "/share/example/", query="t=tok").code == 200
    # The collector still answers on the same app.
    assert call(app, "GET", "/health").code == 200


def test_password_guessing_is_throttled(world, tmp_path):  # noqa: F811
    world.dashboard["users"] = {"admin": hash_password("pw", iterations=1000)}
    app = create_app(config=world)
    bad = {"Authorization": _basic("admin", "guess")}
    codes = [call(app, "GET", "/dashboard/api/sites", headers=bad).code for _ in range(11)]
    assert codes == [401] * 10 + [429]
    # Even the right password waits for the window to pass, from that client; others are not affected.
    assert call(app, "GET", "/dashboard/api/sites", headers={"Authorization": _basic("admin", "pw")}).code == 429
    ok = call(app, "GET", "/dashboard/api/sites", headers={"Authorization": _basic("admin", "pw")},
              remote_addr="198.51.100.5")
    assert ok.code == 200
    # Unknown users fail like wrong passwords (and take as long).
    assert call(app, "GET", "/dashboard/api/sites", headers={"Authorization": _basic("nobody", "x")},
                remote_addr="198.51.100.6").code == 401


def test_concurrent_requests_with_new_credentials_check_the_password_once(monkeypatch):
    import threading
    from tidesight import auth
    calls = []
    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda p, e: calls.append(1) or real(p, e))
    a = auth.Authenticator({"admin": hash_password("pw", iterations=20000)}, Throttle())
    results = []
    threads = [threading.Thread(target=lambda: results.append(a.verify(_basic("admin", "pw"))[1])) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["ok"] * 8 and len(calls) == 1


def test_share_pages_are_rate_limited(world):  # noqa: F811
    pipeline.run(world)
    site = world.site("example")
    site.settings["public"] = True
    site.settings["share_token"] = "tok"
    app = create_app(config=world)
    app.SHARE_API_PER_MINUTE = 3
    codes = [call(app, "GET", "/share/example/api/sites", query="t=tok").code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    # The page and its assets hold no data: they load without the token (a share page with a token works).
    assert call(app, "GET", "/share/example/", remote_addr="198.51.100.7").code == 200
    assert call(app, "GET", "/share/example/app.css", remote_addr="198.51.100.7").code == 200
    # Data needs it; a missing token is not a guess, a wrong one is (and guessing is throttled).
    assert all(call(app, "GET", "/share/example/api/sites", remote_addr="198.51.100.8").code == 403 for _ in range(12))
    assert [call(app, "GET", "/share/example/api/sites", headers={"X-Share-Token": "bad"}, remote_addr="198.51.100.7").code
            for _ in range(11)] == [403] * 10 + [429]
    ok = call(app, "GET", "/share/example/api/sites", headers={"X-Share-Token": "tok"}, remote_addr="198.51.100.9")
    assert ok.code == 200


def test_stored_chunks_are_decoded_with_bounds():
    from tidesight.api import MAX_CHUNK_DECODED, Api as A, Budget
    bomb = gzip.compress(b"[" + b" " * (MAX_CHUNK_DECODED + 10) + b"]")
    assert A._decode({"payload": bomb, "encoding": "gzip"}) is None
    small = gzip.compress(b'[{"type": 2}]')
    assert A._decode({"payload": small, "encoding": "gzip"}, Budget(10)) is None  # over this call's bytes
    assert A._decode({"payload": small, "encoding": "gzip"}, Budget(objects=1)) is None  # over its objects
    assert A._decode({"payload": small, "encoding": "gzip"}) == [{"type": 2}]


def test_a_replay_call_decodes_a_bounded_number_of_objects(world):  # noqa: F811
    """At exactly the density real events have, 4 chunks once made one call hold 15 million events (4 GB)."""
    from tidesight import api as api_mod
    pipeline.run(world)
    from tidesight.storage import REPLAY_MIGRATIONS, connect, migrate
    rconn = connect(os.path.join(world.data_dir, "replay.db"))
    migrate(rconn, REPLAY_MIGRATIONS)
    dense = gzip.compress(("[" + ",".join(['{"type":3,"timestamp":1,"data":{"a":12}}'] * 60000) + "]").encode())
    junk = gzip.compress(("[" + ",".join(["{}"] * 30000) + "]").encode())
    for seq, payload in enumerate([dense, dense, dense, junk]):
        rconn.execute("INSERT INTO chunks(received_at, site_id, site_ok, kind, session_id, seq, client_ts, meta, "
                      "encoding, payload) VALUES (1, 'example', 1, 'replay', 'big', ?, 1, ?, 'gzip', ?)",
                      (seq, json.dumps({"rec": "r1"}), payload))
    rconn.close()
    api_mod.MAX_OBJECTS_PER_CALL, saved = 150000, api_mod.MAX_OBJECTS_PER_CALL
    try:
        out = Api(world).call("replay", {"site": "example", "session": "example/big"})
    finally:
        api_mod.MAX_OBJECTS_PER_CALL = saved
    assert out["truncated"] is True and 60000 <= len(out["events"]) <= 120000  # two chunks fit, then it stops
    assert all(e["type"] == 3 for e in out["events"])  # and the junk chunk was never decoded


def test_dashboard_csp_allows_only_the_sites_origins(world):  # noqa: F811
    from tidesight.wsgi import content_security_policy
    world.dashboard["asset_origins"] = ["https://cdn.example.net", "javascript:alert(1)", "https://x; script-src *"]
    csp = content_security_policy(world)
    assert "img-src 'self' data: blob: https://example.com https://cdn.example.net;" in csp
    assert "https:;" not in csp and " http:" not in csp and "javascript" not in csp and "script-src *" not in csp
    assert "frame-src 'self' blob: https://example.com https://cdn.example.net" in csp


def test_cross_site_requests_to_the_api_are_refused(world):  # noqa: F811
    pipeline.run(world)
    app = create_app(config=world, auth=False)
    assert call(app, "GET", "/dashboard/api/sites", headers={"Sec-Fetch-Site": "cross-site"}).code == 403
    assert call(app, "GET", "/dashboard/api/sites", headers={"Sec-Fetch-Site": "same-origin"}).code == 200


def test_api_json_never_carries_nan_or_infinity():
    from tidesight.wsgi import _json
    out = {}
    body = b"".join(_json(lambda status, headers: out.update(status=status), "200 OK",
                          {"a": float("inf"), "b": [float("nan"), 1.5]}))
    assert json.loads(body) == {"a": None, "b": [None, 1.5]}


def test_exports_stream_and_neutralise_formulas(world):  # noqa: F811
    pipeline.run(world)
    app = create_app(config=world, auth=False)
    r = call(app, "GET", "/dashboard/api/export", query="site=example&what=sessions&format=json&from=2026-06-01&to=2026-06-02")
    assert r.code == 200 and len(json.loads(r.body)) == 7
    r = call(app, "GET", "/dashboard/api/export", query="site=example&what=sessions&format=jsonl&from=2026-06-01&to=2026-06-02")
    assert len([json.loads(line) for line in r.body.decode().splitlines()]) == 7


def test_public_pages_cannot_single_anyone_out(world):  # noqa: F811
    pipeline.run(world)
    site = world.site("example")
    site.settings["public"] = True
    api = Api(world)
    for f in ("f.uid", "f.visitor", "f.asn", "f.model", "f.timezone", "f.hour", "f.event", "f.path_prefix"):
        with pytest.raises(ApiError) as e:
            api.call("overview", dict(RANGE, **{f: "user-1"}), public_site="example")
        assert e.value.status == 403
    # Allowed filters still hide tiny groups.
    small = api.call("overview", dict(RANGE, **{"f.country": "SE"}), public_site="example")
    assert small["totals"] == {"visitors": 0, "pageviews": 0}
    rows = api.call("breakdown", dict(RANGE, dim="page", **{"f.country": "GB"}), public_site="example")["rows"]
    assert rows == []  # one visitor from GB: not shown
    # Unfiltered too: a page seen by one or two visitors is not published (a reset link with its token...).
    assert api.call("breakdown", dict(RANGE, dim="page"), public_site="example")["rows"] == []
    assert api.call("breakdown", dict(RANGE, dim="page"))["rows"]  # the private dashboard shows them
    with pytest.raises(ApiError) as e:  # combining filters narrows a public view down to one person
        api.call("overview", dict(RANGE, **{"f.country": "GB", "f.device": "desktop"}), public_site="example")
    assert e.value.status == 400
    prev = api.call("overview", {"site": "example", "from": "2026-06-03", "to": "2026-06-03", "f.country": "GB"},
                    public_site="example")["previous"]
    assert prev == {"visitors": 0, "pageviews": 0}  # the previous period is thresholded as well
    site.settings["public_metrics"] = site.settings["public_metrics"] + ["realtime"]
    rt = api.call("realtime", {"site": "example"}, public_site="example")
    assert rt["pages"] == [] and rt["referrers"] == []


def test_dense_tiny_json_is_not_parsed():
    from tidesight.api import Api as A
    bomb = gzip.compress(b"[" + b"{}," * 200000 + b"{}]")  # 600 KB of tiny objects: millions of dicts
    assert A._decode({"payload": bomb, "encoding": "gzip"}) is None
    mouse = gzip.compress(json.dumps({"pw": 1, "ph": 1, "pts": [[1, 2, 3]] * 30000}).encode())
    assert A._decode({"payload": mouse, "encoding": "gzip"}, rrweb=False)["pw"] == 1


def test_throttle_stays_cheap_under_a_flood_of_identities():
    import time as _time
    t = Throttle(max_failures=3, window=600)
    started = _time.perf_counter()
    for i in range(60000):  # every failure from a new client: the old clean-up made this quadratic
        t.failed("c%d" % i)
    assert _time.perf_counter() - started < 3
    assert len(t._failures) <= Throttle.MAX_CLIENTS
    for _ in range(3):
        t.failed("attacker")
    assert t.blocked("attacker") and not t.blocked("c0")
