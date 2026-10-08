"""Shared conformance suite: every connector must store the same rows for the same requests.

Each available connector runs as an HTTP server on a free port with the same
configuration; the tests send identical requests and inspect what lands in
events.db and replay.db. Connector authors: see docs/connectors.md.

Select connectors with TIDESIGHT_CONNECTORS=python,php,node,worker (default: every
connector whose code and runtime are present).
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

import pytest

from tidesight.config import loads
from tidesight.storage import EVENTS_MIGRATIONS, REPLAY_MIGRATIONS, connect, migrate

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SECRET = "conformance-secret"

CONFIG = """
[server]
data_dir = "{data}"
secret = "{secret}"
rate_limit_per_minute = 5
rate_limit_key = "x-test-client"
max_payload_kb = 8
max_chunk_kb = 16

[headers]
secret_patterns = ["*token*", "*key*", "*secret*", "*password*"]

[sites.conf]
origins = ["https://conf.example", "https://*.conf.example"]
server_cookie_domain = ".conf.example"

[sites.priv]
origins = ["https://priv.example"]
privacy.truncate_ip = true
privacy.honour_dnt = true

[sites.hashed]
privacy.hash_ip = true
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _php_cmd() -> Optional[List[str]]:
    php = shutil.which("php")
    if not php:
        return None
    base = [php]
    try:
        ok = subprocess.run([php, "-r", "echo class_exists('SQLite3') ? 1 : 0;"], capture_output=True, text=True,
                            timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if ok != "1":  # e.g. Windows PHP without php.ini: load the bundled extension
        ext = os.path.join(os.path.dirname(os.path.realpath(php)), "ext")
        base += ["-d", "extension_dir=" + ext, "-d", "extension=sqlite3", "-d", "extension=pdo_sqlite"]
    return base


def _launchers() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    out["python"] = lambda port, toml, js, data: (
        [sys.executable, os.path.join(REPO, "server", "python", "devserver.py"), "--config", toml, "--port", str(port)], {})
    php_router = os.path.join(REPO, "server", "php", "router.php")
    php = _php_cmd()
    if php and os.path.exists(php_router):
        out["php"] = lambda port, toml, js, data: (
            php + ["-S", "127.0.0.1:%d" % port, php_router], {"TIDESIGHT_CONFIG": js})
    node = shutil.which("node")
    node_server = os.path.join(REPO, "server", "node", "server.mjs")
    if node and os.path.exists(node_server):
        out["node"] = lambda port, toml, js, data: (
            [node, "--no-warnings", node_server, "--port", str(port), "--config", js], {})
    worker_serve = os.path.join(REPO, "server", "worker", "test", "serve.mjs")
    if node and os.path.exists(worker_serve):
        out["worker"] = lambda port, toml, js, data: (
            [node, "--no-warnings", worker_serve, "--port", str(port), "--config", js, "--data", data], {})
    wanted = os.environ.get("TIDESIGHT_CONNECTORS")
    if wanted:
        names = [n.strip() for n in wanted.split(",") if n.strip()]
        missing = [n for n in names if n not in out]
        if missing:
            raise RuntimeError("connectors not available: %s" % ", ".join(missing))
        out = {n: out[n] for n in names}
    return out


LAUNCHERS = _launchers()


class Running:
    def __init__(self, name: str, base: str, data_dir: str, proc: subprocess.Popen) -> None:
        self.name = name
        self.base = base
        self.data_dir = data_dir
        self.proc = proc

    def request(self, method: str, path: str, body: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
                client: Optional[str] = None) -> Tuple[int, Dict[str, str], bytes]:
        h = {"X-Test-Client": client or uuid.uuid4().hex}
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()

    def post(self, payload: Any, raw: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
             path: str = "/collect", **kw: Any) -> Tuple[int, Dict[str, str], bytes]:
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        h = {"Content-Type": "text/plain;charset=UTF-8", "Origin": "https://conf.example",
             "User-Agent": "Conformance/1.0"}
        h.update(headers or {})
        return self.request("POST", path, body, h, **kw)

    def rows(self, db: str, sql: str, params: Tuple = ()) -> List[Dict[str, Any]]:
        path = os.path.join(self.data_dir, db)
        for _ in range(50):  # connectors may answer just before their commit is visible
            if os.path.exists(path):
                break
            time.sleep(0.1)
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def request_for(self, vid: str) -> Dict[str, Any]:
        rows = self.rows("events.db", "SELECT r.* FROM raw_requests r WHERE r.id = (SELECT request_id FROM raw_hits "
                         "WHERE visitor_id = ? ORDER BY id DESC LIMIT 1)", (vid,))
        assert rows, "no request stored for visitor %s" % vid
        return rows[0]

    def hits_for(self, vid: str) -> List[Dict[str, Any]]:
        return self.rows("events.db", "SELECT * FROM raw_hits WHERE visitor_id = ? ORDER BY seq", (vid,))


@pytest.fixture(scope="module", params=sorted(LAUNCHERS))
def connector(request, tmp_path_factory):
    name = request.param
    root = tmp_path_factory.mktemp("conformance-" + name)
    data = str(root / "data").replace("\\", "/")
    toml_path = str(root / "tidesight.toml")
    with open(toml_path, "w", encoding="utf-8") as fh:
        fh.write(CONFIG.format(data=data, secret=SECRET))
    cfg = loads(open(toml_path, encoding="utf-8").read(), toml_path)
    json_path = str(root / "tidesight.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(cfg.resolved(), fh)
    port = _free_port()
    cmd, env = LAUNCHERS[name](port, toml_path, json_path, data)
    log = open(str(root / "server.log"), "w")
    proc = subprocess.Popen(cmd, env=dict(os.environ, **env), stdout=log, stderr=subprocess.STDOUT, cwd=REPO)
    base = "http://127.0.0.1:%d" % port
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/health", timeout=2).read()
            break
        except Exception:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("%s connector did not start (see %s)" % (name, root / "server.log"))
    if proc.poll() is not None:
        pytest.fail("%s connector exited: %s" % (name, open(str(root / "server.log")).read()[-2000:]))
    yield Running(name, base, data, proc)
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


def _vid() -> str:
    return "v" + uuid.uuid4().hex[:15]


def _env(vid: str, events: Optional[List[Any]] = None, **kw: Any) -> Dict[str, Any]:
    env = {"v": 1, "site": "conf", "vid": vid, "vt": 1728380000000, "sid": "s" + vid[1:], "sent": 1728380012345,
           "lib": "conformance", "try": 0,
           "events": events if events is not None else [
               {"t": "pageview", "id": "e1-" + vid, "ts": 1728380012001, "pid": "p" + vid[1:],
                "url": "https://conf.example/a?b=1#c", "title": "Ä title", "ctx": {"sw": 1920}},
               {"t": "event", "id": "e2-" + vid, "ts": 1728380012002, "pid": "p" + vid[1:], "n": "signup",
                "p": {"plan": "pro"}, "uid": "user-9"}]}
    env.update(kw)
    return env


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _schema(path: str) -> Dict[str, Any]:
    conn = sqlite3.connect(path)
    try:
        tables = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                                    "AND name NOT LIKE '_cf_%' AND name NOT LIKE 'd1_%'"):
            tables[name] = [(r[1], r[2].upper(), r[3], r[5]) for r in conn.execute("PRAGMA table_info(%s)" % name)]
        indexes = sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' "
                                                    "AND name NOT LIKE 'sqlite_%'"))
        return {"tables": tables, "indexes": indexes, "user_version": conn.execute("PRAGMA user_version").fetchone()[0]}
    finally:
        conn.close()


def test_schema_matches_python(connector, tmp_path):
    connector.post(_env(_vid()))
    connector.request("POST", "/chunk?site=conf&k=replay&seq=0", b"[]", {"Origin": "https://conf.example"})
    for name, migrations in (("events.db", EVENTS_MIGRATIONS), ("replay.db", REPLAY_MIGRATIONS)):
        ref = str(tmp_path / ("ref-" + name))
        conn = connect(ref)
        migrate(conn, migrations)
        conn.close()
        assert _schema(os.path.join(connector.data_dir, name)) == _schema(ref), name


# ---------------------------------------------------------------------------
# /collect
# ---------------------------------------------------------------------------

def test_collect_stores_request_and_hits(connector):
    vid = _vid()
    env = _env(vid)
    status, headers, _ = connector.post(env, headers={
        "Cookie": "ts_vid=%s.1; session=abc; other=1" % vid, "Authorization": "Bearer t", "X-Api-Key": "k",
        "Accept-Language": "fr-FR", "X-Forwarded-For": "198.51.100.1", "Referer": "https://conf.example/a"})
    assert status == 204
    assert headers.get("access-control-allow-origin") == "https://conf.example"
    assert headers.get("access-control-allow-credentials") == "true"
    assert "no-store" in headers.get("cache-control", "")
    req = connector.request_for(vid)
    assert (req["site_id"], req["site_status"], req["endpoint"], req["method"]) == ("conf", "ok", "collect", "POST")
    assert req["ip"] == "127.0.0.1" and req["error"] is None and req["body"] is None
    assert (req["proto_v"], req["event_count"], req["body_encoding"]) == (1, 2, "identity")
    assert req["content_type"].startswith("text/plain") and req["body_bytes"] == len(json.dumps(env).encode())
    h = json.loads(req["headers"])
    assert h["accept-language"] == "fr-FR" and h["x-forwarded-for"] == "198.51.100.1"
    assert h["authorization"] == "[redacted]" and h["x-api-key"] == "[redacted]" and "cookie" not in h
    assert all(k == k.lower() for k in h)
    assert json.loads(req["cookies"]) == {"kept": {"ts_vid": vid + ".1"}, "others": 2}
    envelope = json.loads(req["envelope"])
    assert "events" not in envelope and envelope["vid"] == vid and envelope["lib"] == "conformance"
    server = json.loads(req["server"])
    assert server["connector"] == connector.name and server.get("version")
    hits = connector.hits_for(vid)
    assert [x["type"] for x in hits] == ["pageview", "event"]
    pv, ev = hits
    assert (pv["seq"], pv["site_id"], pv["site_ok"], pv["client_ts"], pv["event_id"]) == (0, "conf", 1, 1728380012001, "e1-" + vid)
    assert (pv["session_id"], pv["page_id"], pv["url"], pv["user_id"]) == ("s" + vid[1:], "p" + vid[1:], "https://conf.example/a?b=1#c", None)
    assert json.loads(pv["data"]) == env["events"][0] and pv["received_at"] == req["received_at"]
    assert ev["name"] == "signup" and ev["user_id"] == "user-9"
    assert abs(req["received_at"] - time.time() * 1000) < 120000


def test_gzip_and_content_types(connector):
    vid = _vid()
    raw = gzip.compress(json.dumps(_env(vid)).encode())
    assert connector.post(None, raw=raw)[0] == 204
    assert connector.request_for(vid)["body_encoding"] == "gzip"
    vid2 = _vid()
    assert connector.post(_env(vid2), headers={"Content-Type": "application/json"})[0] == 204
    vid3 = _vid()
    form = ("d=" + urllib.request.quote(json.dumps(_env(vid3)))).encode()
    assert connector.post(None, raw=form, headers={"Content-Type": "application/x-www-form-urlencoded"})[0] == 204
    assert len(connector.hits_for(vid2)) == 2 and len(connector.hits_for(vid3)) == 2


@pytest.mark.parametrize("raw,error", [(b"{oops", "bad_json"), (b"[1]", "not_an_object"), (b"\x1f\x8bjunk", "bad_gzip"),
                                       (b'{"v":1,"events":[NaN]}', "bad_json")])
def test_malformed_bodies_flagged(connector, raw, error):
    marker = uuid.uuid4().hex
    status, _, _ = connector.post(None, raw=raw, path="/collect", headers={"X-Marker": marker})
    assert status == 400
    rows = connector.rows("events.db", "SELECT * FROM raw_requests WHERE headers LIKE ?", ("%" + marker + "%",))
    assert len(rows) == 1 and rows[0]["error"].startswith(error) and rows[0]["body"] == raw
    assert rows[0]["site_status"] == "invalid"


def test_oversize_body(connector):
    marker = uuid.uuid4().hex
    status, _, _ = connector.post(None, raw=b"x" * 9000, headers={"X-Marker": marker}, path="/collect?site=conf")
    assert status == 413
    row = connector.rows("events.db", "SELECT * FROM raw_requests WHERE headers LIKE ?", ("%" + marker + "%",))[0]
    assert (row["site_status"], row["error"], row["site_id"], row["body"]) == ("too_large", "too_large", "conf", None)
    assert row["body_bytes"] == 9000


@pytest.mark.parametrize("site,origin,status", [("nope", "https://conf.example", "unknown_site"),
                                                ("conf", "https://evil.example", "origin_mismatch"),
                                                ("conf", "https://shop.conf.example", "ok")])
def test_site_and_origin_flags(connector, site, origin, status):
    vid = _vid()
    assert connector.post(_env(vid, site=site), headers={"Origin": origin})[0] == 204
    req = connector.request_for(vid)
    assert req["site_status"] == status and req["site_id"] == site
    assert {h["site_ok"] for h in connector.hits_for(vid)} == {1 if status == "ok" else 0}


def test_invalid_events_and_truncation(connector):
    vid = _vid()
    env = _env(vid, events=[{"t": "x" * 40, "id": 123, "ts": "77", "url": "u" * 3000, "n": "n" * 300}, "junk", {"no": 1}])
    assert connector.post(env)[0] == 204
    hits = connector.hits_for(vid)
    assert [h["type"] for h in hits] == ["x" * 32, "_invalid", "_invalid"]
    assert (hits[0]["event_id"], hits[0]["client_ts"], len(hits[0]["url"]), len(hits[0]["name"])) == ("123", 77, 2048, 200)
    assert json.loads(hits[1]["data"]) == "junk"
    assert connector.request_for(vid)["error"] == "bad_events:2"


def test_preflight(connector):
    status, headers, _ = connector.request("OPTIONS", "/collect", headers={"Origin": "https://conf.example",
                                                                          "Access-Control-Request-Method": "POST"})
    assert status == 204
    assert headers["access-control-allow-origin"] == "https://conf.example"
    assert "POST" in headers["access-control-allow-methods"]


def test_rate_limit(connector):
    client = uuid.uuid4().hex
    codes = [connector.post(_env(_vid()), client=client)[0] for _ in range(7)]
    assert codes == [204] * 5 + [429] * 2


def test_server_cookie(connector):
    vid = _vid()
    _, headers, _ = connector.post(_env(vid))
    cookie = headers.get("set-cookie", "")
    assert cookie.startswith("ts_svid=%s.1728380000000; Domain=.conf.example; Path=/; Max-Age=34560000; SameSite=Lax" % vid)


def test_privacy_truncate_and_dnt(connector):
    vid = _vid()
    assert connector.post(_env(vid, site="priv"), headers={"Origin": "https://priv.example",
                                                           "X-Forwarded-For": "203.0.113.77, 10.1.2.3"})[0] == 204
    req = connector.request_for(vid)
    assert req["ip"] == "127.0.0.0"
    assert json.loads(req["headers"])["x-forwarded-for"] == "203.0.113.0, 10.1.2.0"
    vid2 = _vid()
    assert connector.post(_env(vid2, site="priv"), headers={"Origin": "https://priv.example", "DNT": "1"})[0] == 204
    assert connector.hits_for(vid2) == []


def test_privacy_hash_matches_python(connector):
    vid = _vid()
    assert connector.post(_env(vid, site="hashed"))[0] == 204
    expected = "h:" + hmac.new(SECRET.encode(), b"127.0.0.1", hashlib.sha256).hexdigest()[:16]
    assert connector.request_for(vid)["ip"] == expected


# ---------------------------------------------------------------------------
# /p.gif, /chunk, /config.js, /health
# ---------------------------------------------------------------------------

def test_pixel(connector):
    vid = _vid()
    status, headers, body = connector.request("GET", "/p.gif?site=conf&t=email_open&n=news&vid=%s&x=1&x=2" % vid,
                                              headers={"Referer": "https://conf.example/landing"})
    assert status == 200 and headers["content-type"] == "image/gif" and body.startswith(b"GIF89a")
    req = connector.request_for(vid)
    assert (req["endpoint"], req["method"], req["site_status"]) == ("pixel", "GET", "ok")
    hit = connector.hits_for(vid)[0]
    assert (hit["type"], hit["name"], hit["url"]) == ("email_open", "news", "https://conf.example/landing")
    assert json.loads(hit["data"]) == {"site": "conf", "t": "email_open", "n": "news", "vid": vid, "x": ["1", "2"]}


def test_chunk(connector):
    sid = "s" + uuid.uuid4().hex[:15]
    payload = json.dumps([{"type": 2, "timestamp": 1}]).encode()
    status, _, _ = connector.request("POST", "/chunk?site=conf&k=replay&vid=v1&sid=%s&pid=p1&seq=3&ts=99&rec=r1" % sid,
                                     payload, {"Origin": "https://conf.example", "Content-Type": "text/plain"})
    assert status == 204
    row = connector.rows("replay.db", "SELECT * FROM chunks WHERE session_id = ?", (sid,))[0]
    assert (row["kind"], row["site_ok"], row["seq"], row["client_ts"], row["page_id"]) == ("replay", 1, 3, 99, "p1")
    assert row["encoding"] == "gzip" and gzip.decompress(row["payload"]) == payload
    meta = json.loads(row["meta"])
    assert meta["rec"] == "r1" and meta["sent_encoding"] == "identity" and meta["bytes"] == len(payload)
    gz = gzip.compress(payload)
    sid2 = sid + "x"
    assert connector.request("POST", "/chunk?site=conf&k=mouse&sid=%s&seq=0" % sid2, gz,
                             {"Origin": "https://conf.example"})[0] == 204
    assert connector.rows("replay.db", "SELECT payload FROM chunks WHERE session_id = ?", (sid2,))[0]["payload"] == gz
    assert connector.request("POST", "/chunk?site=conf&k=replay", b"x" * 17000, {"Origin": "https://conf.example"})[0] == 413


def test_config_js_matches_python(connector):
    from tidesight.config import loads as load_cfg
    expected = load_cfg(CONFIG.format(data=connector.data_dir, secret=SECRET)).public_site_config("conf")
    status, headers, body = connector.request("GET", "/config.js?site=conf")
    assert status == 200 and headers["content-type"].startswith("application/javascript")
    text = body.decode()
    assert text.startswith("(window.tidesight=window.tidesight||function(){(tidesight.q=tidesight.q||[]).push(arguments)})(\"_config\",")
    assert json.loads(text[text.index('"_config",') + 10:text.rindex(");")]) == expected
    status, headers, body = connector.request("GET", "/config.json?site=conf")
    assert json.loads(body) == expected and headers["etag"] == '"%s"' % expected["cv"]
    assert connector.request("GET", "/config.json?site=conf", headers={"If-None-Match": headers["etag"]})[0] == 304
    assert json.loads(connector.request("GET", "/config.json?site=unknown")[2])["known"] is False


def test_health(connector):
    status, headers, body = connector.request("GET", "/health")
    data = json.loads(body)
    assert status == 200 and data["status"] == "ok" and isinstance(data["time"], int)
    assert connector.data_dir.split("/")[-1] not in body.decode()
