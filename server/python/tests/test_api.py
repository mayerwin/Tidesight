"""Read API and web app: numbers checked against the known traffic of the enrichment fixture."""

from __future__ import annotations

import base64
import json

import pytest

from conftest import call
from test_enrich import T0, Traffic, make_config, pv, world  # noqa: F401  (fixture)
from tidesight.api import Api, ApiError
from tidesight.auth import hash_password, verify_password
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
