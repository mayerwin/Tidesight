"""History imported from Plausible, Google Analytics 4 and Matomo (fake APIs), and the reports that add it to the
days before Tidesight's own data (the enrichment fixture starts on 2026-06-01)."""

from __future__ import annotations

import base64
import io
import json
import os
import zipfile
from urllib.parse import parse_qs

import pytest

from conftest import call, query_db
from test_enrich import world  # noqa: F401  (fixture)
from test_searchengines import FakeGoogle, key_file
from tidesight import history
from tidesight import searchengines as se
from tidesight.api import Api
from tidesight.auth import hash_password
from tidesight.enrich import pipeline
from tidesight.maintenance import backup, purge_site
from tidesight.wsgi import create_app

D1, D2, D3 = "2026-05-20", "2026-05-21", "2026-06-01"  # D3: Tidesight's first day, never taken from an import


def plausible_zip(visits: int = 10) -> bytes:
    files = {
        "imported_visitors_20260520_20260601.csv": "date,visitors,pageviews,bounces,visits,visit_duration\n"
                                                   "%s,8,30,4,%d,600\n%s,5,12,2,6,300\n%s,99,99,9,99,99\n" % (D1, visits, D2, D3),
        "imported_sources_20260520_20260601.csv": "date,source,referrer,utm_source,utm_medium,utm_campaign,utm_content,"
                                                  "utm_term,pageviews,visitors,visits,visit_duration,bounces\n"
                                                  "%s,Google,google.com,,,,,,20,6,7,400,3\n"
                                                  "%s,Direct / None,,,,,,,10,2,3,200,1\n"
                                                  "%s,Newsletter,,newsletter,email,may,,,5,3,4,100,1\n" % (D1, D1, D2),
        "imported_pages_20260520_20260601.csv": "date,hostname,page,visits,visitors,pageviews,total_scroll_depth,"
                                                "total_scroll_depth_visits,total_time_on_page,total_time_on_page_visits\n"
                                                "%s,example.com,/pricing,4,4,9,0,0,0,0\n%s,example.com,/,6,5,21,0,0,0,0\n" % (D1, D1),
        "imported_locations_20260520_20260601.csv": "date,country,region,city,visitors,visits,visit_duration,bounces,pageviews\n"
                                                    "%s,GB,GB-ENG,2643743,6,8,500,3,25\n%s,,,0,2,2,100,1,5\n" % (D1, D1),
        "imported_devices_20260520_20260601.csv": "date,device,visitors,visits,visit_duration,bounces,pageviews\n"
                                                  "%s,Laptop,3,4,100,1,9\n%s,Desktop,5,6,500,3,21\n" % (D1, D1),
        "imported_operating_systems_20260520_20260601.csv": "date,operating_system,operating_system_version,visitors,visits,"
                                                            "visit_duration,bounces,pageviews\n%s,Mac,14,8,10,600,4,30\n" % D1,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


@pytest.fixture
def ready(world):  # noqa: F811
    pipeline.run(world)
    return world


def import_plausible(cfg, data=None, **span):
    folder = history.uploads_dir(cfg.data_dir)
    os.makedirs(folder, exist_ok=True)
    name = os.urandom(16).hex() + ".zip"
    with open(os.path.join(folder, name), "wb") as fh:
        fh.write(data or plausible_zip())
    job = history.create_job(cfg, "example", "plausible", dict(span, file=name), "admin")
    return job, history.run(cfg, max_seconds=30)


def test_plausible_history_fills_the_days_before_tidesight(ready):
    cfg = ready
    job, out = import_plausible(cfg)
    assert out[str(job)]["done"] is True
    status = history.jobs(cfg, "example")[0]
    assert (status["status"], status["first_day"], status["last_day"]) == ("done", D1, D2)  # D3 is Tidesight's
    assert "file" not in status["params"] and not os.listdir(history.uploads_dir(cfg.data_dir))  # the upload is gone
    db = history.db_path(cfg.data_dir)
    names = {(r["dim"], r["value"]) for r in query_db(db, "SELECT dim, value FROM imported")}
    assert {("source", "google"), ("channel", "Organic Search"), ("referrer", "google.com"), ("source", "(direct)"),
            ("channel", "Email"), ("campaign", "may"), ("device", "desktop"), ("os", "macOS"), ("country", "GB"),
            ("country", "(none)"), ("page", "/pricing")} <= names
    assert query_db(db, "SELECT visits FROM imported WHERE dim = 'device' AND value = 'desktop'")[0]["visits"] == 10

    api = Api(cfg)
    rng = {"site": "example", "from": D1, "to": "2026-06-02"}
    plain = Api(cfg).call("overview", dict(rng, filters=json.dumps([{"dim": "country", "op": "is", "values": ["GB"]}])))
    assert plain["imported"] == {"applied": False, "reason": "filters"}
    ov = api.call("overview", rng)
    tidesight = ov["totals"]["sessions"] - 16
    assert ov["imported"]["applied"] and ov["imported"]["sources"] == ["Plausible"]
    assert tidesight == plain_sessions(api, rng)  # Tidesight's own visits, and the 10 + 6 imported ones
    by_day = {p["t"]: p for p in ov["series"]}
    assert (by_day[D1]["sessions"], by_day[D1]["pageviews"], by_day[D2]["sessions"]) == (10, 30, 6)
    assert by_day[D1]["avg_duration"] == 60000 and by_day[D1]["bounce_rate"] == 0.4
    src = {r["value"]: r for r in api.call("breakdown", dict(rng, dim="source"))["rows"]}
    assert src["google"]["sessions"] == 7 + 1  # 7 imported, A's organic visit on 2026-06-02
    assert src["newsletter"]["sessions"] == 4 + 1
    sorted_rows = api.call("breakdown", dict(rng, dim="source", sort="sessions", limit="1"))
    assert sorted_rows["rows"][0]["value"] == "google" and sorted_rows["total_rows"] == len(src)
    assert api.call("breakdown", dict(rng, dim="title"))["imported"] == {"applied": False, "reason": "dimension"}
    pages = {r["value"]: r for r in api.call("breakdown", dict(rng, dim="page"))["rows"]}
    assert pages["/pricing"]["pageviews"] == 9 + 1
    cmp = api.call("breakdown", dict(rng, dim="source", compare="custom", compare_from=D1, compare_to=D1))
    assert next(r for r in cmp["rows"] if r["value"] == "google")["cmp"]["sessions"] == 7
    series = api.call("breakdown_series", dict(rng, dim="source", values=json.dumps(["google"]), metric="sessions",
                                               interval="day"))
    assert series["series"][0]["values"][:2] == [7, 0]
    assert next(s for s in api.call("sites", {})["sites"] if s["id"] == "example")["first_day"] == D1

    # Another import of the same days replaces this one.
    job2, _ = import_plausible(cfg, plausible_zip(visits=20))
    assert query_db(db, "SELECT COUNT(DISTINCT job_id) AS n FROM imported")[0]["n"] == 1
    assert api.call("overview", dict(rng, to=D1))["totals"]["sessions"] == 20
    assert history.delete_job(cfg, "example", job2)
    assert "imported" not in api.call("overview", rng)


def plain_sessions(api, rng):
    """Tidesight's own visits: a filter every visit passes keeps the imported days out."""
    everyone = json.dumps([{"dim": "visitor", "op": "is_not", "values": ["nobody"]}])
    return api.call("overview", dict(rng, filters=everyone))["totals"]["sessions"]


class FakeGA4(FakeGoogle):
    """Google's token, and the Data API's runReport."""

    def __call__(self, method, url, data=None, headers=None, timeout=None):
        if url in (se.GOOGLE_TOKEN_URL, se.GOOGLE_API + "sites"):
            return super().__call__(method, url, data, headers, timeout)
        self.calls.append((method, url))
        assert url == history.GA4_API + "properties/123456:runReport" and headers["Authorization"] == "Bearer tok"
        if self.deny:
            return 403, json.dumps({"error": {"code": 403, "message": self.deny}}).encode()
        body = json.loads(data)
        dims = [d["name"] for d in body["dimensions"]][1:]
        start, end = body["dateRanges"][0]["startDate"], body["dateRanges"][0]["endDate"]
        values = {(): [], ("pagePath",): ["/pricing"], ("landingPage",): ["/landing"],
                  ("sessionSource", "sessionMedium", "sessionCampaignName"): ["google", "organic", "(organic)"],
                  ("sessionDefaultChannelGroup",): ["Organic Search"], ("countryId",): ["FR"], ("region",): ["Brittany"],
                  ("city",): ["Rennes"], ("deviceCategory",): ["mobile"], ("browser",): ["Safari"],
                  ("operatingSystem",): ["Macintosh"]}[tuple(dims)]
        rows = []
        for day in ("20260520", "20260521"):
            iso = "%s-%s-%s" % (day[:4], day[4:6], day[6:])
            if start <= iso <= end:
                metrics = ["9", "4", "3"] if dims == ["pagePath"] else ["5", "6", "12", "4", "30.5"]
                rows.append({"dimensionValues": [{"value": day}] + [{"value": v} for v in values],
                             "metricValues": [{"value": m} for m in metrics[:len(body["metrics"])]]})
        return 200, json.dumps({"rows": rows, "rowCount": len(rows)}).encode()


def test_ga4_history(ready, monkeypatch):
    cfg = ready
    fake = FakeGA4()
    monkeypatch.setattr(se, "http_request", fake)
    se.connect_engine(cfg, "google", key_file(), "admin")
    job = history.create_job(cfg, "example", "ga4", {"property": "123456"}, "admin")
    out = history.run(cfg, max_seconds=60)
    assert out[str(job)]["done"] is True, out
    db = history.db_path(cfg.data_dir)
    rows = {(r["day"], r["dim"], r["value"]): r for r in query_db(db, "SELECT * FROM imported")}
    total = rows[(D1, "", "")]
    assert (total["visitors"], total["visits"], total["pageviews"], total["bounces"], total["duration_s"]) == (5, 6, 12, 2, 183)
    assert rows[(D1, "source", "google")]["visits"] == 6 and rows[(D1, "campaign", "(organic)")]["visits"] == 6
    assert rows[(D1, "page", "/pricing")]["pageviews"] == 9 and rows[(D1, "os", "macOS")]
    assert rows[(D1, "country", "FR")] and rows[(D2, "city", "Rennes")] and rows[(D1, "device", "mobile")]
    # No access: the job fails with what to do.
    fake.deny = "User does not have sufficient permissions for this property."
    job = history.create_job(cfg, "example", "ga4", {"property": "123456"}, "admin")
    history.run(cfg, max_seconds=60)
    failed = next(j for j in history.jobs(cfg, "example") if j["id"] == job)
    assert failed["status"] == "failed" and "Property access management" in failed["error"]


def test_matomo_history(ready, monkeypatch):
    cfg = ready
    calls = []

    def fake(method, url, data=None, headers=None, timeout=None):
        q = {k: v[0] for k, v in parse_qs(data.decode()).items()}
        calls.append(q["method"])
        assert url == "https://matomo.example.org/index.php" and q["token_auth"] == "a" * 32
        if q["method"] == "SitesManager.getSiteFromId":
            return 200, json.dumps({"idsite": "3", "name": "Example"}).encode()
        days = {D1: True, D2: False}
        if q["method"] == "VisitsSummary.get":
            doc = {d: ({"nb_uniq_visitors": 4, "nb_visits": 5, "nb_actions": 11, "bounce_count": 2, "sum_visit_length": 300}
                       if has else []) for d, has in days.items()}
        elif q["method"] == "Actions.get":
            doc = {D1: {"nb_pageviews": 10}, D2: []}
        else:
            rows = {"Actions.getPageUrls": [{"label": "pricing", "url": "https://example.com/pricing?x=1", "nb_visits": 3,
                                             "nb_hits": 4, "entry_nb_visits": 2, "entry_bounce_count": 1,
                                             "exit_nb_visits": 1}],
                    "Referrers.getAll": [{"label": "Google", "referer_type": 2, "nb_visits": 3},
                                         {"label": "news.ycombinator.com", "referer_type": 3, "nb_visits": 1},
                                         {"label": "spring-sale", "referer_type": 6, "nb_visits": 1}],
                    "UserCountry.getCountry": [{"label": "France", "code": "fr", "nb_visits": 5}],
                    "UserCountry.getCity": [{"label": "Rennes, Brittany, France", "city_name": "Rennes",
                                             "region_name": "Brittany", "nb_visits": 2}],
                    "DevicesDetection.getType": [{"label": "Smartphone", "nb_visits": 3}],
                    "DevicesDetection.getBrowsers": [{"label": "Microsoft Edge", "nb_visits": 1}],
                    "DevicesDetection.getOsFamilies": [{"label": "GNU/Linux", "nb_visits": 1}]}[q["method"]]
            doc = {D1: rows, D2: []}
        return 200, json.dumps(doc).encode()

    monkeypatch.setattr(se, "http_request", fake)
    assert history.check_matomo("https://matomo.example.org/", "3", "a" * 32)["name"] == "Example"
    with pytest.raises(history.HistoryError):
        history.check_matomo("https://matomo.example.org/", "x", "a" * 32)
    job = history.create_job(cfg, "example", "matomo", {"url": "https://matomo.example.org", "id_site": "3",
                                                         "token": "a" * 32, "from": "2026-05-01"}, "admin")
    out = history.run(cfg, max_seconds=60)
    assert out[str(job)]["done"] is True, out
    j = history.jobs(cfg, "example")[0]
    assert (j["first_day"], j["last_day"]) == (D1, D1) and "token" not in json.dumps(
        query_db(history.db_path(cfg.data_dir), "SELECT params FROM history_jobs"))
    rows = {(r["dim"], r["value"]): r for r in query_db(history.db_path(cfg.data_dir), "SELECT * FROM imported")}
    assert rows[("", "")]["pageviews"] == 10 and rows[("page", "/pricing")]["pageviews"] == 4
    assert rows[("entry", "/pricing")]["visits"] == 2 and rows[("exit", "/pricing")]["visits"] == 1
    assert rows[("source", "google")]["visits"] == 3 and rows[("channel", "Organic Search")]
    assert rows[("source", "news.ycombinator.com")] and rows[("campaign", "spring-sale")]
    assert rows[("country", "FR")] and rows[("city", "Rennes")] and rows[("region", "Brittany")]
    assert rows[("device", "mobile")] and rows[("browser", "Edge")] and rows[("os", "Linux")]


def test_dashboard_upload_and_backup(ready):
    cfg = ready
    users = {"admin": hash_password("pw", iterations=1000)}
    for table in (cfg.dashboard, cfg.raw.setdefault("dashboard", {})):
        table["users"] = dict(users)
    app = create_app(config=cfg)
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode(), "X-Tidesight": "1",
            "Origin": "https://127.0.0.1"}
    up = lambda body: call(app, "POST", "/dashboard/api/history_upload", body,  # noqa: E731
                           dict(auth, **{"Content-Type": "application/zip"}), query="site=example")
    assert up(b"not a zip").code == 400
    res = up(plausible_zip())
    assert res.code == 201 and res.json()["jobs"][0]["status"] == "queued"
    got = call(app, "GET", "/dashboard/api/history", headers=auth, query="site=example").json()
    assert got["jobs"][0]["label"] == "Plausible" and got["first_tidesight_day"] == D3 and got["default_to"] == "2026-05-31"
    bad = call(app, "POST", "/dashboard/api/history", json.dumps({"source": "ga4", "property": "G-ABC"}).encode(),
               dict(auth, **{"Content-Type": "application/json"}), query="site=example")
    assert bad.code == 400 and "measurement id" in bad.json()["error"]
    history.run(cfg)
    done = backup(cfg, dest=os.path.join(cfg.data_dir, "b"))
    assert "history.db" in {f["file"] for f in done["files"]}
    gone = call(app, "DELETE", "/dashboard/api/history", headers=auth, query="site=example&id=%d" % res.json()["job"])
    assert gone.json()["jobs"] == []
    import_plausible(cfg)
    cfg.sites.pop("example")
    assert purge_site(cfg, "example", apply=True)["rows"]["history.db:imported"] > 0
