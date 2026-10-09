"""Reports (filters, comparisons, breakdowns, goals, exports) and what the dashboard changes (sign-in, properties,
segments, annotations), checked against the known traffic of the enrichment fixture."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import zipfile
from xml.etree import ElementTree

import pytest

from conftest import call, query_db
from test_enrich import T0, Traffic, make_config, pv, world  # noqa: F401  (fixture)
from tidesight import properties
from tidesight.api import Api, ApiError
from tidesight.auth import hash_password
from tidesight.enrich import pipeline
from tidesight.maintenance import purge_queued, purge_site
from tidesight.webdb import WebDB
from tidesight.wsgi import create_app

RANGE = {"site": "example", "from": "2026-06-01", "to": "2026-06-02"}
GOALS = [{"name": "Signup", "type": "event", "match": "signup"}, {"name": "Pricing", "type": "page", "match": "/pricing*"}]


@pytest.fixture
def api(world):  # noqa: F811
    pipeline.run(world)
    world.site("example").settings["goals"] = [dict(g, id=g["name"].lower()) for g in GOALS]
    return Api(world)


def total(api, **params):
    return api.call("overview", dict(RANGE, **params))["totals"]["sessions"]


def flt(*items):
    return json.dumps([{"dim": d, "op": op, "values": list(v)} for d, op, v in items])


def test_filter_operators(api):
    # Clean visits: A1 (GB, email, /landing then /pricing, signup), A2 (GB, Google, /blog/post), C1 (SE, iPhone,
    # /account), two no-JS pixel visits (SE, /nojs).
    assert total(api) == 5
    assert total(api, filters=flt(("country", "is", ["GB", "SE"]))) == 5
    assert total(api, filters=flt(("country", "is_not", ["GB"]))) == 3
    assert total(api, filters=flt(("page", "contains", ["pric"]))) == 1
    assert total(api, filters=flt(("page", "not_contains", ["nojs"]))) == 3  # visits that never saw a /nojs page
    assert total(api, filters=flt(("page", "starts_with", ["/blog"]))) == 1
    assert total(api, filters=flt(("event", "is", ["signup"]))) == 1
    assert total(api, filters=flt(("event", "is_not", ["signup"]))) == 4
    # A missing value is "(none)", and never matches a negative filter's text: direct visits have no referrer.
    direct = {r["value"]: r["sessions"] for r in api.call("breakdown", dict(RANGE, dim="referrer"))["rows"]}["(none)"]
    assert total(api, filters=flt(("referrer", "is", ["(none)"]))) == direct
    assert total(api, filters=flt(("referrer", "not_contains", ["google"]))) == 4
    assert total(api, filters=flt(("country", "is", ["SE"]), ("device", "is", ["mobile"]))) == 3
    # LIKE wildcards in values are literal.
    assert total(api, filters=flt(("page", "contains", ["%"]))) == 0
    # The older f.<dim> parameters still work.
    assert total(api, **{"f.country": "GB"}) == 2
    for bad in ('[{"dim":"nope","op":"is","values":["x"]}]', '[{"dim":"country","op":"like","values":["x"]}]',
                '[{"dim":"country","op":"is","values":[]}]', '{"dim":"country"}', '[{"dim":"hour","op":"is","values":["x"]}]',
                '[{"dim":"hour","op":"contains","values":["1"]}]', '[{"dim":"goal","op":"is","values":["nope"]}]'):
        with pytest.raises(ApiError) as e:
            api.call("overview", dict(RANGE, filters=bad))
        assert e.value.status == 400


def test_day_of_week(api):
    rows = {r["value"]: r["sessions"] for r in api.call("breakdown", dict(RANGE, dim="weekday"))["rows"]}
    assert rows == {"1": 3, "2": 2}  # 2026-06-01 is a Monday (0 = Sunday)
    assert total(api, filters=flt(("weekday", "is", ["1"]))) == 3
    grid = api.call("breakdown", dict(RANGE, dim="weekday", dim2="hour"))["rows"]
    assert sum(r["sessions"] for r in grid) == 5
    with pytest.raises(ApiError):
        api.call("overview", dict(RANGE, filters=flt(("weekday", "contains", ["1"]))))


def test_bots_and_goal_filters(api):
    assert total(api, bots="include") == 6 and total(api, bots="1") == 6 and total(api, bots="only") == 1
    assert total(api, filters=flt(("goal", "is", ["signup"]))) == 1
    assert total(api, filters=flt(("goal", "is_not", ["signup", "pricing"]))) == 4


def test_breakdown_rows_totals_and_paging(api):
    res = api.call("breakdown", dict(RANGE, dim="country"))
    assert [(r["value"], r["sessions"]) for r in sorted(res["rows"], key=lambda r: r["value"])] == [("GB", 2), ("SE", 3)]
    assert res["totals"]["sessions"] == 5 and res["total_rows"] == 2 and res["scope"] == "session"
    assert [c["key"] for c in res["columns"]][:3] == ["value", "visitors", "sessions"]
    asc = api.call("breakdown", dict(RANGE, dim="country", sort="sessions", dir="asc"))["rows"]
    assert [r["value"] for r in asc] == ["GB", "SE"]
    page2 = api.call("breakdown", dict(RANGE, dim="country", sort="sessions", limit="1", offset="1"))
    assert [r["value"] for r in page2["rows"]] == ["GB"] and page2["total_rows"] == 2
    found = api.call("breakdown", dict(RANGE, dim="country", q="g"))
    assert [r["value"] for r in found["rows"]] == ["GB"] and found["totals"]["sessions"] == 2
    picked = api.call("breakdown", dict(RANGE, dim="country", values='["SE"]'))
    assert [r["value"] for r in picked["rows"]] == ["SE"]
    # Two dimensions.
    both = api.call("breakdown", dict(RANGE, dim="country", dim2="device"))
    pairs = {(r["value"], r["value2"]): r["sessions"] for r in both["rows"]}
    assert pairs == {("GB", "desktop"): 2, ("SE", "mobile"): 3}
    # Page scope: rows are pageviews; a page filter selects rows there, not visits.
    pages = api.call("breakdown", dict(RANGE, dim="page", filters=flt(("page", "contains", ["/n"]))))
    assert [(r["value"], r["pageviews"]) for r in pages["rows"]] == [("/nojs", 3)] and pages["scope"] == "page"
    landing = {r["value"]: r for r in api.call("breakdown", dict(RANGE, dim="page"))["rows"]}["/landing"]
    assert landing["entries"] == 1 and landing["exit_rate"] == 0 and landing["bounce_rate"] == 0
    with pytest.raises(ApiError):
        api.call("breakdown", dict(RANGE, dim="page", dim2="event"))


def test_comparisons(api):
    day2 = {"site": "example", "from": "2026-06-02", "to": "2026-06-02"}
    out = api.call("overview", dict(day2, compare="previous"))
    assert out["totals"]["sessions"] == 2 and out["compare"]["totals"]["sessions"] == 3
    assert (out["compare"]["from"], out["compare"]["to"]) == ("2026-06-01", "2026-06-01")
    assert len(out["series"]) == len(out["compare"]["series"]) == 24  # one day: hours
    assert api.call("overview", dict(day2))["previous"]["sessions"] == 3  # the change shown without a comparison
    rows = {r["value"]: r for r in api.call("breakdown", dict(day2, dim="country", compare="previous"))["rows"]}
    assert rows["GB"]["sessions"] == 1 and rows["GB"]["cmp"]["sessions"] == 1
    assert rows["SE"]["sessions"] == 1 and rows["SE"]["cmp"]["sessions"] == 2
    year = api.call("overview", dict(day2, compare="year"))["compare"]
    assert (year["from"], year["totals"]["sessions"]) == ("2025-06-02", 0)
    custom = api.call("overview", dict(day2, compare="custom", compare_from="2026-06-01", compare_to="2026-06-01"))
    assert custom["compare"]["totals"]["sessions"] == 3
    with pytest.raises(ApiError):
        api.call("overview", dict(day2, compare="custom"))
    with pytest.raises(ApiError):
        api.call("overview", dict(day2, compare="sideways"))


def test_comparisons_of_many_rows_stay_within_sqlite_limits(api):
    from tidesight.enrich.db import derived_path
    from tidesight.query import Query, compare_rows
    conn = sqlite3.connect(derived_path(api.cfg.data_dir))
    conn.row_factory = sqlite3.Row
    q = Query(api.cfg, dict(RANGE, compare="previous"), "example")
    rows = [{"value": "/landing" if i == 0 else "/p%d" % i, "value2": "desktop" if i % 2 else "mobile", "pageviews": 1}
            for i in range(1200)]
    result = {"rows": rows, "metrics": ["pageviews"]}
    compare_rows(conn, q, q.comparison(), result, "page", "device", None)  # 1,200 keys: chunked statements
    conn.close()
    assert all("cmp" in r for r in rows) and result["compare"]["from"] == "2026-05-30"
    many = json.dumps([{"dim": "country", "op": "is", "values": ["X%d" % i for i in range(200)]},
                       {"dim": "city", "op": "is", "values": ["Y%d" % i for i in range(200)]}])
    with pytest.raises(ApiError):
        api.call("overview", dict(RANGE, filters=many))  # 400 values in all: over the bound


def test_intervals_and_value_series(api):
    assert [p["t"] for p in api.call("overview", dict(RANGE, interval="week"))["series"]] == ["2026-06-01"]
    month = api.call("overview", dict(RANGE, interval="month"))["series"]
    assert [(p["t"], p["sessions"]) for p in month] == [("2026-06-01", 5)]
    trend = api.call("breakdown_series", dict(RANGE, dim="country", values='["GB", "SE"]', metric="sessions"))
    assert trend["keys"] == ["2026-06-01", "2026-06-02"]
    assert {s["value"]: s["values"] for s in trend["series"]} == {"GB": [1, 1], "SE": [2, 1]}


def test_goals(api):
    res = api.call("goals", dict(RANGE))
    goals = {g["id"]: g for g in res["goals"]}
    assert goals["signup"]["conversions"] == 1 and goals["signup"]["conversion_rate"] == 0.2
    assert goals["signup"]["value"] == 49 and goals["pricing"]["conversions"] == 1
    assert res["selected"] == "signup" and sum(p["conversions"] for p in res["series"]) == 1
    by_channel = {r["value"]: r for r in api.call("breakdown", dict(RANGE, dim="channel", goal="signup"))["rows"]}
    assert by_channel["Email"]["conversions"] == 1 and by_channel["Email"]["conversion_rate"] == 1.0
    assert by_channel["Direct"]["conversions"] == 0
    ov = api.call("overview", dict(RANGE, goal="pricing"))
    assert ov["totals"]["conversions"] == 1 and ov["goal"]["name"] == "Pricing"


def test_report_files(world):  # noqa: F811
    pipeline.run(world)
    app = create_app(config=world, auth=False)
    q = "site=example&from=2026-06-01&to=2026-06-02&dim=channel&compare=previous"
    csv = call(app, "GET", "/dashboard/api/breakdown", query=q + "&format=csv")
    assert csv.code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert 'filename="example_channel_2026-06-01_2026-06-02.csv"' in csv.headers["content-disposition"]
    text = csv.body.decode("utf-8")
    assert text.startswith("﻿Channel,Visitors,Visitors · compared period,Visitors · change,")
    assert "Visit duration (s)" in text and "Direct," in text
    tsv = call(app, "GET", "/dashboard/api/breakdown", query=q + "&format=tsv")
    assert tsv.body.decode("utf-8").splitlines()[0].split("\t")[0] == "﻿Channel"
    doc = json.loads(call(app, "GET", "/dashboard/api/breakdown", query=q + "&format=json").body)
    assert doc["meta"]["Compared with"] == "2026-05-30 to 2026-05-31" and doc["meta"]["Traffic"].startswith("humans")
    assert {r["value"] for r in doc["rows"]} == {"Direct", "Email", "Organic Search"}
    xlsx = call(app, "GET", "/dashboard/api/breakdown", query=q + "&format=xlsx")
    with zipfile.ZipFile(io.BytesIO(xlsx.body)) as z:
        sheet = ElementTree.fromstring(z.read("xl/worksheets/sheet1.xml"))
        about = z.read("xl/worksheets/sheet2.xml").decode("utf-8")
        ElementTree.fromstring(z.read("xl/workbook.xml"))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    first = sheet.find("m:sheetData/m:row/m:c/m:is/m:t", ns)
    assert first is not None and first.text == "Channel" and "Example" not in about and "example" in about
    trend = call(app, "GET", "/dashboard/api/overview", query="site=example&from=2026-06-01&to=2026-06-02&format=csv")
    assert trend.body.decode("utf-8").splitlines()[0].startswith("﻿Day,Visitors,Visits,Pageviews")
    # Formulas from visitors' data are neutralised; public pages never get files.
    from tidesight.tabular import Column, write_delimited
    assert write_delimited([Column("v", "V")], [["=HYPERLINK(1)"], [-3]], ",").decode("utf-8").splitlines()[1:] == ["'=HYPERLINK(1)", "-3"]
    world.site("example").settings["public"] = True
    pub = call(app, "GET", "/share/example/api/breakdown", query="dim=channel&format=csv")
    assert pub.headers["content-type"].startswith("application/json")


def test_live_counts_recent_visitors(tmp_path, monkeypatch):
    import time as _time
    cfg = make_config(tmp_path)
    tr = Traffic(cfg, monkeypatch)
    now = int(_time.time() * 1000)
    tr.batch(now - 60000, "visitorLIVE01", "sessL1", [pv("l1", "pL1", "https://example.com/", now - 60000)])
    tr.batch(now - 20 * 60000, "visitorOLD001", "sessO1", [pv("o1", "pO1", "https://example.com/", now - 20 * 60000)])
    tr.h.close()
    assert Api(cfg).call("live", {"site": "example"}) == {"active": 1}


def test_export_catalog_counts_rows(world):  # noqa: F811
    pipeline.run(world)
    cat = {d["id"]: d for d in Api(world).call("export_catalog", dict(RANGE))["datasets"]}
    assert cat["sessions"]["rows"] == 7 and "visitor_key" in cat["sessions"]["columns"]
    assert cat["raw"]["rows"] > 0 and cat["raw"]["utc_days"] and "ip" in cat["raw"]["columns"]
    assert cat["pageviews"]["description"].startswith("One row per page viewed")


# -- signing in ------------------------------------------------------------------------------------

def _app(world, **dash):  # noqa: F811
    pipeline.run(world)
    users = {"admin": hash_password("pw", iterations=1000), "viewer": hash_password("v", iterations=1000)}
    for table in (world.dashboard, world.raw.setdefault("dashboard", {})):  # reloads rebuild from the raw tables
        table["users"] = dict(users)
        table.update(dash)
    return create_app(config=world)


WRITE = {"X-Tidesight": "1", "Origin": "https://127.0.0.1", "Content-Type": "application/json"}


def _login(app, user="admin", password="pw", remember=True, **kw):
    return call(app, "POST", "/dashboard/api/login", body=json.dumps({"user": user, "password": password,
                                                                      "remember": remember}).encode(),
                headers=WRITE, **kw)


def _cookie(res):
    value = res.headers["set-cookie"].split(";")[0]
    assert value.startswith("ts_login=")
    return value


def test_sign_in_with_a_session_cookie(world):  # noqa: F811
    app = _app(world)
    assert call(app, "POST", "/dashboard/api/login", body=b'{"user":"admin","password":"pw"}',
                headers={"Content-Type": "application/json"}).code == 403  # no X-Tidesight: refused
    assert _login(app, password="nope").code == 401
    ok = _login(app)
    assert ok.code == 200 and ok.json() == {"user": "admin", "admin": True}
    attrs = ok.headers["set-cookie"]
    for part in ("HttpOnly", "SameSite=Strict", "Path=/dashboard/", "Secure", "Max-Age=2592000"):
        assert part in attrs
    cookie = _cookie(ok)
    me = call(app, "GET", "/dashboard/api/me", headers={"Cookie": cookie})
    assert me.code == 200 and me.json()["user"] == "admin" and me.json()["auth"] == "session"
    assert call(app, "GET", "/dashboard/api/sites", headers={"Cookie": cookie}).code == 200
    # Signed out: the dashboard's own requests get no browser prompt, scripts do.
    plain = call(app, "GET", "/dashboard/api/sites", headers={"X-Tidesight": "1"})
    assert plain.code == 401 and "www-authenticate" not in plain.headers
    assert "www-authenticate" in call(app, "GET", "/dashboard/api/sites").headers
    # Only a hash of the token is stored.
    rows = query_db(os.path.join(world.data_dir, "web.db"), "SELECT token_hash FROM sessions")
    assert rows and cookie.split("=", 1)[1] not in rows[0]["token_hash"]
    # The page itself loads signed out, without the CSP that names the sites.
    page = call(app, "GET", "/dashboard/")
    assert page.code == 200 and "example.com" not in page.headers["content-security-policy"]
    assert "https://example.com" in call(app, "GET", "/dashboard/", headers={"Cookie": cookie}).headers["content-security-policy"]
    # A session ends with sign-out, and when the password changes.
    out = call(app, "POST", "/dashboard/api/logout", headers=dict(WRITE, Cookie=cookie))
    assert out.code == 200 and "Max-Age=0" in out.headers["set-cookie"]
    assert call(app, "GET", "/dashboard/api/me", headers={"Cookie": cookie}).code == 401
    cookie = _cookie(_login(app, remember=False))
    assert call(app, "GET", "/dashboard/api/me", headers={"Cookie": cookie}).code == 200
    world.dashboard["users"]["admin"] = hash_password("new", iterations=1000)
    assert call(app, "GET", "/dashboard/api/me", headers={"Cookie": cookie}).code == 401


def test_the_cookie_path_follows_a_folder_install():
    from tidesight.wsgi import _dashboard_path
    # CGI in a folder, with a rewrite that hides collect.cgi: the browser's path counts.
    assert _dashboard_path({"REQUEST_URI": "/tidesight/dashboard/api/login", "SCRIPT_NAME": "/tidesight/collect.cgi",
                            "PATH_INFO": "/dashboard/api/login"}) == "/tidesight/dashboard/"
    assert _dashboard_path({"SCRIPT_NAME": "", "PATH_INFO": "/dashboard/api/login"}) == "/dashboard/"
    assert _dashboard_path({"REQUEST_URI": "/x;y/dashboard/api/login"}) == "/"  # nothing odd in a Set-Cookie


def test_sign_in_guessing_is_throttled_across_processes(world):  # noqa: F811
    app = _app(world)
    codes = [_login(app, password="guess").code for _ in range(11)]
    assert codes == [401] * 10 + [429]
    fresh = create_app(config=world)  # another process (CGI starts one per request): web.db remembers
    assert _login(fresh).code == 429
    assert _login(fresh, remote_addr="198.51.100.20").code == 200


def test_changes_need_the_header_and_this_origin(world):  # noqa: F811
    app = _app(world)
    cookie = _cookie(_login(app))
    body = json.dumps({"site": "example", "day": "2026-06-01", "text": "launch"}).encode()
    for headers in ({"Origin": "https://evil.example", "X-Tidesight": "1"}, {"Origin": "null", "X-Tidesight": "1"},
                    {"Sec-Fetch-Site": "same-site", "X-Tidesight": "1", "Origin": "https://127.0.0.1"},
                    {"Origin": "https://127.0.0.1"}):
        r = call(app, "POST", "/dashboard/api/annotations", body=body,
                 headers=dict(headers, Cookie=cookie, **{"Content-Type": "application/json"}))
        assert r.code == 403, headers
    ok = call(app, "POST", "/dashboard/api/annotations", body=body, headers=dict(WRITE, Cookie=cookie))
    assert ok.code == 201 and ok.json()["text"] == "launch"
    form = call(app, "POST", "/dashboard/api/annotations", body=b"site=example",
                headers=dict(WRITE, Cookie=cookie, **{"Content-Type": "application/x-www-form-urlencoded"}))
    assert form.code == 415


# -- properties ------------------------------------------------------------------------------------

def test_properties_are_added_changed_and_deleted(world, monkeypatch):  # noqa: F811
    from tidesight import admin as admin_mod
    monkeypatch.setattr(admin_mod, "_now_ms", lambda: T0 + 3600000)  # the fixture's traffic is "recent"
    app = _app(world, admins=["admin"])
    admin = dict(WRITE, Cookie=_cookie(_login(app)))
    viewer = dict(WRITE, Cookie=_cookie(_login(app, "viewer", "v")))

    def send(method, path, body=None, headers=admin, query=""):
        return call(app, method, "/dashboard/api/" + path, body=json.dumps(body).encode() if body is not None else b"",
                    headers=headers, query=query)

    listing = send("GET", "properties").json()
    assert [p["id"] for p in listing["properties"]] == ["example"] and listing["can_edit"]
    assert listing["unknown"][0]["id"] == "nope" and listing["unknown"][0]["valid_id"]
    assert send("POST", "properties", {"domains": ["blog.example.net"]}, headers=viewer).code == 403
    assert send("POST", "properties", {"domains": []}).code == 400
    assert send("POST", "properties", {"domains": ["not a domain"]}).code == 400
    assert send("POST", "properties", {"domains": ["intranet"]}).code == 400  # one label: only localhost
    assert send("POST", "properties", {"domains": ["*.com"]}).code == 400  # never every site of a top-level domain
    assert send("POST", "properties", {"domains": ["blog.example.net"], "timezone": "Mars/Olympus"}).code == 400
    assert send("POST", "properties", {"domains": ["x.example.net"], "id": "example"}).code == 409
    created = send("POST", "properties", {"domains": ["https://Blog.Example.net/", "www.blog.example.net"],
                                          "timezone": "Europe/Paris", "preset": "privacy"})
    assert created.code == 201, created.body
    prop = created.json()
    assert prop["id"] == "blog-example-net" and prop["label"] == "blog.example.net" and prop["managed"]
    assert prop["origins"] == ["https://blog.example.net", "https://www.blog.example.net"]
    assert prop["privacy"]["truncate_ip"] and prop["features"]["fingerprint"] is False and prop["can_edit"]
    saved = json.load(open(properties.path(world.data_dir), encoding="utf-8"))
    assert saved["sites"]["blog-example-net"]["timezone"] == "Europe/Paris"
    # The collector accepts the new property at once.
    hit = {"v": 1, "site": "blog-example-net", "vid": "visitorNEW00001", "sid": "sessNEW1", "sent": T0,
           "events": [pv("n1", "pN1", "https://blog.example.net/", T0)]}
    r = call(app, "POST", "/collect", body=json.dumps(hit).encode(),
             headers={"Origin": "https://blog.example.net", "Content-Type": "text/plain"})
    assert r.code == 204
    stored = query_db(os.path.join(world.data_dir, "events.db"),
                      "SELECT site_status FROM raw_requests WHERE site_id = 'blog-example-net'")
    assert stored == [{"site_status": "ok"}]
    # Changes: checked like the configuration file.
    changed = send("PUT", "property", {"name": "The blog", "features": {"replay": True}, "replay": {"sample_rate": 0.5},
                                       "goals": [{"name": "Newsletter", "type": "event", "match": "subscribe"}],
                                       "share_token": "generate", "public": True}, query="site=blog-example-net")
    assert changed.code == 200, changed.body
    p = changed.json()
    assert p["label"] == "The blog" and p["features"]["replay"] and p["options"]["replay"]["sample_rate"] == 0.5
    assert p["goals"][0]["id"] == "newsletter" and len(p["share_token"]) >= 16
    for bad in ({"features": {"nope": True}}, {"replay": {"sample_rate": 2}}, {"unknown": 1}, {"origins": []},
                {"public_metrics": ["secrets"]}, {"max_hits_per_day": -1}, {"goals": [{"name": "x", "type": "visit", "match": "y"}]}):
        assert send("PUT", "property", bad, query="site=blog-example-net").code == 400, bad
    assert send("PUT", "property", {"name": "x"}, query="site=example").code == 409  # the configuration file's
    assert send("GET", "property", query="site=blog-example-net").json()["name"] == "The blog"
    # Deleting: confirmed with the id; the data goes at the next cron run.
    assert send("DELETE", "property", {"confirm": "nope"}, query="site=blog-example-net").code == 400
    gone = send("DELETE", "property", {"confirm": "blog-example-net", "delete_data": True}, query="site=blog-example-net")
    assert gone.code == 200 and gone.json()["data"] == "scheduled for deletion"
    assert send("POST", "properties", {"domains": ["blog.example.net"], "id": "blog-example-net"}).code == 409
    assert [x["site_id"] for x in WebDB(world.data_dir).purges()] == ["blog-example-net"]
    cfg = app.holder.get()
    assert cfg.site("blog-example-net") is None
    done = purge_queued(cfg)
    assert done["blog-example-net"]["rows"]["events.db:raw_hits"] == 1 and WebDB(world.data_dir).purges() == []
    assert query_db(os.path.join(world.data_dir, "events.db"),
                    "SELECT COUNT(*) AS n FROM raw_requests WHERE site_id = 'blog-example-net'") == [{"n": 0}]


def test_purge_site_deletes_everything_of_one_site(world):  # noqa: F811
    pipeline.run(world)
    dry = purge_site(world, "example")
    assert not dry["applied"] and dry["rows"]["derived.db:sessions"] > 0
    WebDB(world.data_dir).add_annotation("example", "2026-06-01", "x", "admin")
    out = purge_site(world, "example", apply=True)
    assert out["rows"]["events.db:raw_hits"] > 0
    conn = sqlite3.connect(os.path.join(world.data_dir, "derived.db"))
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE site_id = 'example'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM requests WHERE site_id = 'nope'").fetchone()[0] > 0  # others stay
    conn.close()
    assert WebDB(world.data_dir).annotations("example", "2026-01-01", "2027-01-01") == []


def test_cron_keeps_the_resolved_configuration_current(world, tmp_path):  # noqa: F811
    import argparse
    from tidesight import jobs
    out = str(tmp_path / "resolved.json")
    world.server["resolved_config_file"] = out
    args = argparse.Namespace(max_seconds=30.0)
    assert jobs.cmd_cron(world, args)["resolved_config"] == {"written": out}
    assert jobs.cmd_cron(world, args)["resolved_config"] == {"unchanged": out}
    assert "example" in json.load(open(out, encoding="utf-8"))["sites"]


def test_segments_and_annotations(world):  # noqa: F811
    app = _app(world)
    h = dict(WRITE, Cookie=_cookie(_login(app)))
    seg = call(app, "POST", "/dashboard/api/segments", headers=h, body=json.dumps(
        {"name": "UK mobile", "site": "example", "filters": [{"dim": "country", "op": "is", "values": ["GB"]}]}).encode())
    assert seg.code == 201
    sid = seg.json()["id"]
    bad = call(app, "POST", "/dashboard/api/segments", headers=h, body=json.dumps(
        {"name": "x", "filters": [{"dim": "nope", "op": "is", "values": ["x"]}]}).encode())
    assert bad.code == 400
    listed = call(app, "GET", "/dashboard/api/segments", query="site=example", headers={"Cookie": h["Cookie"]}).json()
    assert [s["name"] for s in listed["segments"]] == ["UK mobile"]
    assert call(app, "PUT", "/dashboard/api/segment", query="id=%d" % sid, headers=h,
                body=json.dumps({"name": "UK"}).encode()).json()["name"] == "UK"
    assert call(app, "DELETE", "/dashboard/api/segment", query="id=%d" % sid, headers=h).code == 200
    call(app, "POST", "/dashboard/api/annotations", headers=h,
         body=json.dumps({"site": "example", "day": "2026-06-02", "text": "Newsletter sent"}).encode())
    ov = call(app, "GET", "/dashboard/api/overview", query="site=example&from=2026-06-01&to=2026-06-02",
              headers={"Cookie": h["Cookie"]}).json()
    assert [(a["day"], a["text"]) for a in ov["annotations"]] == [("2026-06-02", "Newsletter sent")]


def test_config_file_sites_win_and_broken_properties_are_ignored(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "properties.json").write_text(json.dumps({"sites": {
        "example": {"origins": ["https://shadow.example"]},
        "broken": {"timezone": 5},
        "fine": {"origins": ["https://fine.example"], "name": "Fine"}}}), encoding="utf-8")
    from tidesight.config import loads
    cfg = loads('[server]\ndata_dir = "%s"\n[sites.example]\norigins = ["https://example.com"]\n'
                % str(data).replace("\\", "/"), str(tmp_path / "t.toml"))
    assert cfg.site("example").origins == ["https://example.com"] and not cfg.site("example").managed
    assert cfg.site("fine").managed and cfg.site("fine").display_name == "Fine" and cfg.site("broken") is None
    assert any("broken" in w for w in cfg.warnings)
    (data / "properties.json").write_text("{not json", encoding="utf-8")
    assert set(loads('[server]\ndata_dir = "%s"\n' % str(data).replace("\\", "/"), str(tmp_path / "t.toml")).sites) == set()


def test_display_names():
    assert properties.display_name("x", "", ["https://example.com", "https://www.example.com"]) == "example.com"
    assert properties.display_name("x", "", ["https://blog.example.com", "https://*.example.com"]) == "blog.example.com"
    assert properties.display_name("x", "", ["https://*.example.com"]) == "example.com"
    assert properties.display_name("x", "Shop", ["https://example.com"]) == "Shop"
    assert properties.display_name("x", "", []) == "x"
    assert properties.origin_from_domain("Bücher.de") == "https://xn--bcher-kva.de"
    assert not properties.ORIGIN_RE.match(properties.origin_from_domain("javascript:alert(1)"))
