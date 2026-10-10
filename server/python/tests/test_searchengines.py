"""Search engines (Google Search Console, Bing Webmaster Tools) against fake APIs, RS256 against OpenSSL, and the
paths report, on the known traffic of the enrichment fixture."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from conftest import call, query_db
from test_enrich import T0, world  # noqa: F401  (fixture)
from tidesight import rs256
from tidesight import searchengines as se
from tidesight.api import Api, ApiError
from tidesight.auth import hash_password
from tidesight.enrich import pipeline
from tidesight.maintenance import backup, purge_site
from tidesight.wsgi import create_app

# A 1024-bit test key (made for this test, used nowhere else), and the signature `openssl dgst -sha256 -sign` gave
# for MESSAGE: PKCS#1 v1.5 signatures are deterministic.
KEY = {
    "n": "00da7b4ce9d28ed848f7b9c563a3e012fde904e4e7f8baaa7db6a9c4aaa2c1916b943f7ad234ece6a6f1dea6f381d1b7302d31a54628faf"
         "2acb7db19a925c2c863af18679b1bdab0a56495e2f11cb9bf99343e758d4bd628b5a95e9c7dfde9cede191da05a0532331009bfc10c6b"
         "9c38712c4e397d5a239899783aefc34d93976d",
    "e": "010001",
    "d": "229c51cce0cc3c3b22f34cefc1f027520dedf68d40fbe132551383d23c4249d52f00f76601785df9a86b4b2b5c2f6ac3b9051a5788fa682"
         "330650725a293bec6b0d3f392b15d19d5999c55a793961b15536c7d768f14200b8052c395bcfd0e4dcc6e336e375cf3b23a292ea2d805"
         "3b87585a89c77c31567596be88b97cc4ea41",
    "p": "00fa1ee8747aca3c4657c4db12c2f9515a8d9e13677e82870d99dd2e7c7a8b4905f08bc698605a194823b14af2de3e08817ba001a3ed85"
         "97ef09382079721ce735",
    "q": "00df9e0160ff10de6ed84be533966b18e47d0ebb232cc92c645727c6777693a75bb73dc521ca718bde89ccf32a3472f5e080f206493981"
         "62177120e7d8ef931e59",
    "dp": "00b3f274af0880632900904244973da40810070052df33ad525db6417191009082d372dc685839ba22aa7b2d3eda21ab20ecfe4aefd33c"
          "dff83b9bc3d15c05eb59",
    "dq": "00c4c101f59678013f0691c0d9e33fd86732a20386afa152adf701c85ed4b6964adcb03d03be5ca9f4e4cab02a327bf45bd5191eeeceea"
          "48a4d7a55c5a4471ee01",
    "qinv": "00d0504f4d6c4ddd93fb9df5c9390a3263ff2e37ed0793cf9371a46f341ee8deb819649b3ef4eec90f623ca888e59ec7e12012f1ce86"
            "ad6f6110e72018bd5a1a4f",
}
MESSAGE = b"tidesight test message"
OPENSSL_SIGNATURE = (
    "ad4ded4f67781eefd99083539a12a5340fa79a7f4166b2ad2151dc2f9fe67addb621b9e500f463c4280d4bbcb76af1ec6ac9011543e5f34b"
    "221f95c43c9fbdf424e09605abf2832414c1c77a5602a7471bb7b352e62a92bf79617368cec77e85b5e6e80886393421cfe044c77084a659"
    "b43c9870f61cf9055676d1da61eb93fe")


def _der(tag: int, body: bytes) -> bytes:
    n = len(body)
    length = bytes([n]) if n < 0x80 else bytes([0x80 | ((n.bit_length() + 7) // 8)]) + n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag]) + length + body


def _int(hexa: str) -> bytes:
    return _der(0x02, bytes.fromhex(hexa if len(hexa) % 2 == 0 else "0" + hexa))


def _pem(kind: str = "pkcs8") -> str:
    rsa = _der(0x30, _int("00") + b"".join(_int(KEY[k]) for k in ("n", "e", "d", "p", "q", "dp", "dq", "qinv")))
    if kind == "pkcs1":
        der, label = rsa, "RSA PRIVATE KEY"
    else:
        algorithm = _der(0x30, _der(0x06, bytes.fromhex("2a864886f70d010101")) + _der(0x05, b""))
        der, label = _der(0x30, _int("00") + algorithm + _der(0x04, rsa)), "PRIVATE KEY"
    b64 = base64.b64encode(der).decode("ascii")
    return "-----BEGIN %s-----\n%s\n-----END %s-----\n" % (label, "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64)), label)


def key_file(email: str = "tidesight@demo-project.iam.gserviceaccount.com") -> str:
    return json.dumps({"type": "service_account", "project_id": "demo-project", "private_key_id": "abc123",
                       "private_key": _pem(), "client_email": email, "client_id": "1",
                       "token_uri": "https://oauth2.googleapis.com/token"})


def test_rs256_matches_openssl():
    for kind in ("pkcs8", "pkcs1"):
        key = rs256.load_private_key(_pem(kind))
        assert rs256.sign(key, MESSAGE).hex() == OPENSSL_SIGNATURE
    # Key files keep the PEM in a JSON string; one pasted with literal \n still loads.
    assert rs256.load_private_key(_pem().replace("\n", "\\n")).n == int(KEY["n"], 16)
    token = rs256.jwt(rs256.load_private_key(_pem()), {"iss": "x"}, {"kid": "k"})
    head, claims, sig = token.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    assert json.loads(base64.urlsafe_b64decode(pad(head))) == {"kid": "k", "alg": "RS256", "typ": "JWT"}
    assert _verify((head + "." + claims).encode(), base64.urlsafe_b64decode(pad(sig)))


def test_rs256_refuses_what_is_not_a_key():
    for bad in ("", "hello", "-----BEGIN PRIVATE KEY-----\n!!!\n-----END PRIVATE KEY-----",
                "-----BEGIN PRIVATE KEY-----\nMAA=\n-----END PRIVATE KEY-----", None):
        with pytest.raises(rs256.KeyFormatError):
            rs256.load_private_key(bad)
    broken = dict(KEY, p="00fa1ee8747aca3c4657c4db12c2f9515a8d9e13677e82870d99dd2e7c7a8b4905f08bc698605a194823b14af2de3e08"
                          "817ba001a3ed8597ef09382079721ce737")
    KEY_SAVE = dict(KEY)
    KEY.update(broken)
    try:
        with pytest.raises(rs256.KeyFormatError):  # p × q is not n
            rs256.load_private_key(_pem())
    finally:
        KEY.update(KEY_SAVE)


def _verify(message: bytes, signature: bytes) -> bool:
    n, e = int(KEY["n"], 16), int(KEY["e"], 16)
    em = pow(int.from_bytes(signature, "big"), e, n).to_bytes(128, "big")
    return em.endswith(hashlib.sha256(message).digest()) and em.startswith(b"\x00\x01\xff")


# ---- fake engines ------------------------------------------------------------------------------------------

DAY1, DAY2 = "2026-06-01", "2026-06-02"
NOW = T0 + 5 * 86400000  # what the imports take for "now": Search Console's 16 months end a few days later


class FakeGoogle:
    """Search Console: a token for a validly signed JWT, the sites, and searchAnalytics.query."""

    def __init__(self) -> None:
        self.calls = []
        self.sites = [{"siteUrl": "sc-domain:example.com", "permissionLevel": "siteFullUser"},
                      {"siteUrl": "https://other.example/", "permissionLevel": "siteUnverifiedUser"}]
        self.deny = None

    def rows(self, body):
        dims = body["dimensions"]
        if body.get("startRow"):
            return []
        if dims == ["date"]:
            return [{"keys": [d], "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 5}
                    for d in (DAY1, DAY2) if body["startDate"] <= d <= body["endDate"]]
        if dims == ["query", "page"]:
            if body["startDate"] == DAY2:
                return [{"keys": ["tide tables", "https://example.com/blog/post"], "clicks": 3, "impressions": 40,
                         "ctr": 0.075, "position": 4.0},
                        {"keys": ["tides", "https://example.com/blog/post"], "clicks": 1, "impressions": 60,
                         "ctr": 0.0167, "position": 9.0},
                        {"keys": ["example", "https://example.com/"], "clicks": 2, "impressions": 5, "ctr": 0.4,
                         "position": 1.2}]
            return [{"keys": ["tides", "https://example.com/blog/post"], "clicks": 1, "impressions": 20, "ctr": 0.05,
                     "position": 8.0}]
        if dims == ["country", "device"]:
            if body["startDate"] == DAY2:
                return [{"keys": ["gbr", "DESKTOP"], "clicks": 5, "impressions": 90, "ctr": 0.05, "position": 6.0},
                        {"keys": ["swe", "MOBILE"], "clicks": 3, "impressions": 30, "ctr": 0.1, "position": 3.0}]
            return [{"keys": ["usa", "MOBILE"], "clicks": 1, "impressions": 25, "ctr": 0.04, "position": 7.0}]
        raise AssertionError(dims)

    def __call__(self, method, url, data=None, headers=None, timeout=None):
        self.calls.append((method, url))
        if url == se.GOOGLE_TOKEN_URL:
            form = parse_qs(data.decode())
            head, claims, sig = form["assertion"][0].split(".")
            pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
            assert _verify((head + "." + claims).encode(), base64.urlsafe_b64decode(pad(sig)))
            c = json.loads(base64.urlsafe_b64decode(pad(claims)))
            assert c["scope"] == se.GOOGLE_SCOPE and c["aud"] == se.GOOGLE_TOKEN_URL and c["exp"] > c["iat"]
            return 200, json.dumps({"access_token": "tok", "expires_in": 3599}).encode()
        assert headers["Authorization"] == "Bearer tok"
        if self.deny:
            return 403, json.dumps({"error": {"code": 403, "message": self.deny}}).encode()
        if url == se.GOOGLE_API + "sites":
            return 200, json.dumps({"siteEntry": self.sites}).encode()
        assert url == se.GOOGLE_API + "sites/sc-domain%3Aexample.com/searchAnalytics/query"
        return 200, json.dumps({"rows": self.rows(json.loads(data))}).encode()


def bing_date(day: str) -> str:
    import calendar
    import time
    ms = calendar.timegm(time.strptime(day, "%Y-%m-%d")) * 1000 + 7 * 3600000  # midnight in California
    return "/Date(%d-0700)/" % ms


class FakeBing:
    def __init__(self) -> None:
        self.calls = []
        self.clicks = 4

    def __call__(self, method, url, data=None, headers=None, timeout=None):
        parts = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.calls.append(parts.path.rsplit("/", 1)[1])
        if q.get("apikey") != "0123456789abcdef0123456789abcdef":
            return 400, json.dumps({"ErrorCode": 3, "Message": "ERROR!!! InvalidApiKey"}).encode()
        name = self.calls[-1]
        if name == "GetUserSites":
            return 200, json.dumps({"d": [{"Url": "https://example.com/", "IsVerified": True}]}).encode()
        assert q["siteUrl"] == "https://example.com/"
        if name == "GetQueryStats":
            d = [{"Query": "tide tables", "Date": bing_date(DAY1), "Clicks": self.clicks, "Impressions": 50,
                  "AvgImpressionPosition": 3, "AvgClickPosition": 2}]
        elif name == "GetPageStats":
            d = [{"Query": "https://example.com/blog/post", "Date": bing_date(DAY1), "Clicks": self.clicks,
                  "Impressions": 70, "AvgImpressionPosition": 4, "AvgClickPosition": 3}]
        else:
            d = [{"Date": bing_date(DAY1), "Clicks": self.clicks, "Impressions": 80},
                 {"Date": bing_date(DAY2), "Clicks": 1, "Impressions": 30}]
        return 200, json.dumps({"d": d}).encode()


@pytest.fixture
def linked(world, monkeypatch):  # noqa: F811
    pipeline.run(world)
    monkeypatch.setattr(se, "_now_ms", lambda: NOW)
    google, bing = FakeGoogle(), FakeBing()

    def fake(method, url, data=None, headers=None, timeout=None):
        return (google if "google" in url else bing)(method, url, data, headers, timeout)

    monkeypatch.setattr(se, "http_request", fake)
    site = world.site("example")
    site.settings["search_console"] = "sc-domain:example.com"
    site.settings["bing_site"] = "https://example.com/"
    site.settings["goals"] = [{"id": "signup", "name": "Signup", "type": "event", "match": "signup"}]
    return world, google, bing


def test_google_import(linked):
    cfg, google, bing = linked
    assert se.connect_engine(cfg, "google", key_file(), "admin")["sites"] == [
        {"url": "sc-domain:example.com", "permission": "siteFullUser"}]  # an unverified one is of no use
    stored = json.load(open(se.path(cfg.data_dir), encoding="utf-8"))["engines"]["google"]
    assert stored["client_email"].startswith("tidesight@") and stored["added_by"] == "admin"
    assert "private_key" not in json.dumps(se.describe(se.credentials(cfg.data_dir)))
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(se.path(cfg.data_dir)).st_mode) == 0o600
    google.calls.clear()
    out = se.run(cfg, max_seconds=60)
    assert out["example:google"] == {"days": 2, "rows": 4}
    queries = [c for c in google.calls if c[0] == "POST" and "searchAnalytics" in c[1]]
    assert len(queries) == 1 + 2 * 2  # which days have data (one call), then two calls for each of them
    db = se.db_path(cfg.data_dir)
    assert query_db(db, "SELECT COUNT(*) AS n FROM search_days")[0]["n"] == se.GOOGLE_HISTORY_DAYS
    assert {(r["country"], r["device"]) for r in query_db(db, "SELECT * FROM search_totals")} == {
        ("GB", "desktop"), ("SE", "mobile"), ("US", "mobile")}
    # Nothing is due again until Google's recent days are worth asking for again.
    google.calls.clear()
    assert se.run(cfg) == {}
    assert google.calls == []

    api = Api(cfg)
    rng = {"site": "example", "from": DAY1, "to": DAY2}
    r = api.call("seo", dict(rng))
    assert r["engine"] == "google" and [e["id"] for e in r["engines"]] == ["google", "bing"]
    assert r["totals"]["clicks"] == 9 and r["totals"]["impressions"] == 145
    assert r["totals"]["position"] == round((6 * 90 + 3 * 30 + 7 * 25) / 145, 1)
    assert r["totals"]["visits"] == 1  # Tidesight saw one visit from Google's organic results (A's second)
    assert [x["value"] for x in r["rows"]] == ["tide tables", "tides", "example"]  # ties: more impressions first
    tides = r["rows"][1]
    assert (tides["clicks"], tides["impressions"], tides["position"]) == (2, 80, round((9 * 60 + 8 * 20) / 80, 1))
    assert r["coverage"] == round(7 / 9, 4)  # Google keeps the other two clicks' queries to itself
    assert [p["clicks"] for p in r["series"]] == [1, 8]
    pages = api.call("seo", dict(rng, tab="pages", goal="signup"))
    post = next(x for x in pages["rows"] if x["path"] == "/blog/post")
    assert (post["clicks"], post["visits"], post["conversions"]) == (5, 1, 0)
    one = api.call("seo", dict(rng, tab="pages", query="tides"))
    assert one["totals"]["clicks"] == 2 and [x["value"] for x in one["rows"]] == ["https://example.com/blog/post"]
    assert api.call("seo", dict(rng, tab="countries"))["rows"][0]["value"] == "GB"
    assert api.call("seo", dict(rng, tab="countries", query="tides"))["rows"] == []
    cmp = api.call("seo", dict(rng, compare="custom", compare_from=DAY1, compare_to=DAY1))
    assert cmp["compare"]["totals"]["clicks"] == 1 and cmp["rows"][1]["cmp"]["clicks"] == 1
    with pytest.raises(ApiError):
        api.call("seo", dict(rng, tab="nope"))

    # Another Search Console property: the figures of the previous one go.
    cfg.site("example").settings["search_console"] = "https://example.com/"
    google.deny = "User does not have sufficient permission for site 'https://example.com/'."
    out = se.run(cfg)
    assert "sufficient permission" in out["example:google"]["error"] and "add tidesight@" in out["example:google"]["error"]
    assert query_db(db, "SELECT COUNT(*) AS n FROM search_rows WHERE engine = 'google'")[0]["n"] == 0
    google.calls.clear()
    assert se.run(cfg) == {} and google.calls == []  # it waits before asking again…
    se.clear_retries(cfg, site_id="example")  # …unless the settings change
    assert "error" in se.run(cfg)["example:google"]
    status = se.status(cfg, "example")["google"]
    assert status["target"] == "https://example.com/" and status["error"] and status["retry_at"]


def test_bing_import(linked):
    cfg, google, bing = linked
    with pytest.raises(se.SearchEngineError) as err:
        se.connect_engine(cfg, "bing", "0123456789abcdef0123456789abcdeX", "admin")
    assert "InvalidApiKey" in str(err.value) and "0123456789abcdef" not in str(err.value)
    assert not os.path.exists(se.path(cfg.data_dir))  # a refused key is not kept
    with pytest.raises(se.SearchEngineError):
        se.connect_engine(cfg, "bing", "not a key", "admin")
    assert se.connect_engine(cfg, "bing", " 0123456789abcdef0123456789abcdef ", "admin")["sites"][0]["url"] == "https://example.com/"
    assert se.describe(se.credentials(cfg.data_dir))["bing"]["key_hint"] == "…cdef"
    assert se.run(cfg)["example:bing"] == {"queries": 1, "pages": 1, "days": 2}
    bing.clicks = 6
    se.run(cfg, force=True)  # the same days again: replaced, not added
    db = se.db_path(cfg.data_dir)
    rows = query_db(db, "SELECT * FROM search_rows WHERE engine = 'bing' ORDER BY query")
    assert [(r["day"], r["query"], r["page"], r["clicks"]) for r in rows] == [
        (DAY1, "", "https://example.com/blog/post", 6), (DAY1, "tide tables", "", 6)]
    api = Api(cfg)
    r = api.call("seo", {"site": "example", "from": DAY1, "to": DAY2, "engine": "bing"})
    assert r["engine"] == "bing" and r["totals"]["clicks"] == 7 and r["totals"]["position"] == 3.0
    assert r["rows"][0]["value"] == "tide tables"
    assert api.call("seo", {"site": "example", "from": DAY1, "to": DAY2, "engine": "bing", "tab": "pages",
                            "query": "tide tables"})["rows"] == []  # Bing does not pair queries with pages


def test_search_data_is_backed_up_and_purged(linked):
    cfg, google, bing = linked
    se.connect_engine(cfg, "google", key_file(), "admin")
    se.run(cfg, max_seconds=60)
    done = backup(cfg, dest=os.path.join(cfg.data_dir, "b"))
    assert {"search.db", "search-engines.json"} <= {f["file"] for f in done["files"]}
    cfg.sites.pop("example")
    out = purge_site(cfg, "example", apply=True)
    assert out["rows"]["search.db:search_rows"] == 4
    assert query_db(se.db_path(cfg.data_dir), "SELECT COUNT(*) AS n FROM search_days")[0]["n"] == 0


def test_dashboard_connects_engines_and_links_properties(linked):
    cfg, google, bing = linked
    users = {"admin": hash_password("pw", iterations=1000), "viewer": hash_password("pw2", iterations=1000)}
    for table in (cfg.dashboard, cfg.raw.setdefault("dashboard", {})):  # reloads rebuild from the raw tables
        table["users"] = dict(users)
        table["admins"] = ["admin"]
    app = create_app(config=cfg)
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode(), "X-Tidesight": "1",
            "Origin": "https://127.0.0.1"}
    viewer = dict(auth, Authorization="Basic " + base64.b64encode(b"viewer:pw2").decode())
    put = lambda body, h=auth: call(app, "PUT", "/dashboard/api/search_engines", json.dumps(body).encode(),  # noqa: E731
                                    dict(h, **{"Content-Type": "application/json"}))
    assert put({"engine": "google", "key": key_file()}, viewer).code == 403
    bad = put({"engine": "google", "key": json.dumps({"installed": {"client_id": "x"}})})
    assert bad.code == 400 and "OAuth client" in bad.json()["error"]
    res = put({"engine": "google", "key": json.loads(key_file())})
    assert res.code == 200 and res.json()["google"]["email"].startswith("tidesight@")
    assert res.json()["sites"][0]["url"] == "sc-domain:example.com" and "BEGIN" not in res.body.decode()
    got = call(app, "GET", "/dashboard/api/search_engines", headers=auth, query="site=example").json()
    assert got["google"]["project_id"] == "demo-project" and got["bing"] is None
    assert call(app, "GET", "/dashboard/api/search_engine_sites", headers=auth, query="engine=google").json()["sites"]
    google.deny = "Google Search Console API has not been used in project 1 before or it is disabled."
    res = call(app, "GET", "/dashboard/api/search_engine_sites", headers=auth, query="engine=google")
    assert res.code == 502 and "Enable the Google Search Console API" in res.json()["error"]
    gone = call(app, "DELETE", "/dashboard/api/search_engines", headers=auth, query="engine=google")
    assert gone.json()["google"] is None
    # A property names its Search Console property and Bing site: checked, and kept in properties.json.
    from tidesight import properties
    made = call(app, "POST", "/dashboard/api/properties", json.dumps({"id": "shop", "domains": ["shop.example"]}).encode(),
                dict(auth, **{"Content-Type": "application/json"}))
    assert made.code == 201
    change = lambda body: call(app, "PUT", "/dashboard/api/property", json.dumps(body).encode(),  # noqa: E731
                               dict(auth, **{"Content-Type": "application/json"}), query="site=shop")
    assert change({"search_console": "shop.example"}).code == 400
    res = change({"search_console": "sc-domain:shop.example", "bing_site": "https://shop.example/"})
    assert res.code == 200 and res.json()["search_console"] == "sc-domain:shop.example"
    assert properties.read_tables(cfg.data_dir)["shop"]["bing_site"] == "https://shop.example/"
    sites = call(app, "GET", "/dashboard/api/sites", headers=auth).json()["sites"]
    assert next(s for s in sites if s["id"] == "shop")["search"] == {"google": True, "bing": True}


def test_property_links_are_checked():
    for good in ("sc-domain:example.com", "https://www.example.com/", "http://example.com/blog/"):
        assert se.check_target("google", good) == good
    assert se.check_target("google", " SC-DOMAIN:Example.com ".strip().lower()) == "sc-domain:example.com"
    for bad in ("example.com", "https://example.com", "sc-domain:", "javascript:alert(1)//x/", "https://a b/"):
        with pytest.raises(se.SearchEngineError):
            se.check_target("google", bad)
    assert se.check_target("bing", "https://example.com") == "https://example.com"
    assert se.check_target("bing", "") == ""
    with pytest.raises(se.SearchEngineError):
        se.check_target("bing", "ftp://example.com/")


def test_bing_dates_and_country_codes():
    assert se.bing_day("/Date(1399100400000-0700)/") == "2014-05-03"
    assert se.bing_day("/Date(1399100400000)/") == "2014-05-03"
    assert se.bing_day("2014-05-03") is None and se.bing_day(None) is None
    assert se._ALPHA3["fra"] == "FR" and se._ALPHA3["usa"] == "US" and se._ALPHA3["pyf"] == "PF"


# ---- paths ------------------------------------------------------------------------------------------------

def test_paths(world):  # noqa: F811
    pipeline.run(world)
    api = Api(world)
    rng = {"site": "example", "from": "2026-06-01", "to": "2026-06-02"}
    flow = api.call("paths", dict(rng, steps="3"))
    first = {n["value"]: n for n in flow["steps"][0]["nodes"]}
    # A1 /landing → /pricing; A2 /blog/post; C1 /account; the pixel visits /nojs (a page seen twice in a row is
    # one step) and /nojs.
    assert {k: v["visits"] for k, v in first.items()} == {"/landing": 1, "/blog/post": 1, "/account": 1, "/nojs": 2}
    assert first["/nojs"]["ends"] == 2 and first["/landing"]["ends"] == 0
    assert flow["links"] == [{"source": "0|/landing", "target": "1|/pricing", "visits": 1}]
    assert flow["sessions"] == 5 and flow["direction"] == "forward"
    narrow = api.call("paths", dict(rng, width="2"))
    assert {n["value"] for n in narrow["steps"][0]["nodes"]} >= {"(other pages)"}
    back = api.call("paths", dict(rng, end="/pricing"))
    assert back["direction"] == "backward" and back["sessions"] == 1
    assert [n["value"] for s in back["steps"] for n in s["nodes"]] == ["/pricing", "/landing"]
    assert api.call("paths", dict(rng, start="/blog/*"))["sessions"] == 1
    tr = api.call("paths", dict(rng, mode="transitions", page="/pricing"))
    assert tr["views"] == 1 and tr["previous"] == [{"value": "/landing", "visits": 1}] and tr["exits"] == 1
    tr = api.call("paths", dict(rng, mode="transitions", page="/landing"))
    assert tr["entrances"] == [{"value": "Email", "visits": 1}] and tr["next"][0]["value"] == "/pricing"
    for bad in ({"mode": "nope"}, {"mode": "transitions"}, {"start": "/a", "end": "/b"}, {"steps": "x"}):
        with pytest.raises(ApiError):
            api.call("paths", dict(rng, **bad))
