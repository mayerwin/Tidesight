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
rate_limit_kb_per_minute = 16
rate_limit_key = "x-test-client"
rate_limit_proxies = ["127.0.0.1/32", "::1/128"]
max_payload_kb = 8
max_chunk_kb = 16
max_events_per_request = 20
chunk_rate_limit_per_minute = 4
chunk_kb_per_minute = 24
flagged_per_minute = 3
flagged_global_per_minute = 1000

[headers]
secret_patterns = ["*token*", "*key*", "*secret*", "*password*"]

[sites.conf]
origins = ["https://conf.example", "https://*.conf.example"]
server_cookie_domain = ".conf.example"
features.replay = true
features.dom_snapshots = true

[sites.quota]
max_hits_per_day = 3

[sites.priv]
origins = ["https://priv.example"]
privacy.truncate_ip = true
privacy.honour_dnt = true

[sites.hashed]
privacy.hash_ip = true
"""

# URL redaction test vectors (redact.py), shared with the tracker's tests; the "redact" site uses their patterns.
VECTORS = json.load(open(os.path.join(os.path.dirname(__file__), "redact_vectors.json"), encoding="utf-8"))
CONFIG += """
[sites.redact]
redact_params = %s
redact_paths = %s
""" % (json.dumps(VECTORS["redact_params"]), json.dumps(VECTORS["redact_paths"]))


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
        h = {"X-Test-Client": client or _client_ip()}
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


def _launch(name: str, root: Any, config: str = CONFIG, ready: str = "/health") -> Running:
    """Start a connector with a configuration; `ready` is a path that answers 200 once it serves."""
    data = str(root / "data").replace("\\", "/")
    toml_path = str(root / "tidesight.toml")
    with open(toml_path, "w", encoding="utf-8") as fh:
        fh.write(config.format(data=data, secret=SECRET))
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
            urllib.request.urlopen(base + ready, timeout=2).read()
            break
        except Exception:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
    else:
        proc.kill()
        log.close()
        pytest.fail("%s connector did not start (see %s)" % (name, root / "server.log"))
    if proc.poll() is not None:
        log.close()
        pytest.fail("%s connector exited: %s" % (name, open(str(root / "server.log")).read()[-2000:]))
    running = Running(name, base, data, proc)
    running.log = log
    return running


def _stop(running: Running) -> None:
    running.proc.terminate()
    try:
        running.proc.wait(10)
    except subprocess.TimeoutExpired:
        running.proc.kill()
    running.log.close()


@pytest.fixture(scope="module", params=sorted(LAUNCHERS))
def connector(request, tmp_path_factory):
    running = _launch(request.param, tmp_path_factory.mktemp("conformance-" + request.param))
    yield running
    _stop(running)


def _client_ip() -> str:
    """A random client address (the collectors believe X-Test-Client only when it is an IP address)."""
    n = uuid.uuid4().int
    return "10.%d.%d.%d" % (n % 256, (n >> 8) % 256, (n >> 16) % 254 + 1)


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


def test_name_column_only_from_string_n(connector):
    vid = _vid()
    env = _env(vid, events=[{"t": "rage", "n": 3}, {"t": "event", "n": "signup"}, {"t": "event", "n": ""}, {"t": "form", "n": {"x": 1}}])
    assert connector.post(env)[0] == 204
    assert [h["name"] for h in connector.hits_for(vid)] == [None, "signup", None, None]


def test_preflight(connector):
    status, headers, _ = connector.request("OPTIONS", "/collect", headers={"Origin": "https://conf.example",
                                                                          "Access-Control-Request-Method": "POST"})
    assert status == 204
    assert headers["access-control-allow-origin"] == "https://conf.example"
    assert "POST" in headers["access-control-allow-methods"]


def test_lone_surrogates_become_replacement_characters(connector):
    # Text cut in the middle of an emoji reaches JSON as a lone surrogate escape, which UTF-8 cannot hold.
    vid = _vid()
    env = _env(vid, events=[{"t": "event", "id": "e1-" + vid, "ts": 1728380012001, "pid": "p" + vid[1:],
                             "n": "party \ud83c", "p": {"label": "\udf89 done", "ok": "\U0001f389"}}], title="\ud83c")
    assert connector.post(env)[0] == 204
    hit = connector.hits_for(vid)[0]
    assert hit["name"] == "party �"
    assert json.loads(hit["data"])["p"] == {"label": "� done", "ok": "\U0001f389"}
    assert "�" in hit["data"] and "\\u" not in hit["data"]
    req = connector.request_for(vid)
    assert json.loads(req["envelope"])["title"] == "�" and req["error"] is None


def test_storage_failure_without_spool_fallback_answers_503(connector, tmp_path):
    if connector.name == "worker":
        pytest.skip("D1 has no spool fallback; a failed batch always answers 503")
    os.makedirs(str(tmp_path / "data" / "events.db"))  # a directory: the database cannot be opened
    running = _launch(connector.name, tmp_path, CONFIG.replace("[server]\n", "[server]\nspool_fallback = false\n"),
                      ready="/")
    try:
        status, headers, _ = running.post(_env(_vid()))
        assert status == 503 and headers.get("retry-after") == "60"
    finally:
        _stop(running)


def test_rate_limit(connector):
    if time.time() % 60 > 50:  # the limit counts per clock minute: do not straddle two
        time.sleep(61 - time.time() % 60)
    client = _client_ip()
    codes = [connector.post(_env(_vid()), client=client)[0] for _ in range(7)]
    assert codes == [204] * 5 + [429] * 2


# ---------------------------------------------------------------------------
# Limits and abuse protection
# ---------------------------------------------------------------------------

def _same_minute(seconds: int = 50) -> None:
    """Allowances count per clock minute: wait for a fresh one when a test could straddle two."""
    if time.time() % 60 > seconds:
        time.sleep(61 - time.time() % 60)


def _counter(connector: Running, site: str, name: str) -> int:
    rows = connector.rows("events.db", "SELECT n FROM counters WHERE day = ? AND site_id = ? AND name = ?",
                          (time.strftime("%Y-%m-%d", time.gmtime()), site, name))
    return rows[0]["n"] if rows else 0


def test_too_many_events_keep_the_raw_body(connector):
    vid = _vid()
    env = _env(vid, events=[{"t": "event", "n": "e%d" % i} for i in range(25)])
    raw = json.dumps(env).encode()
    assert connector.post(None, raw=raw)[0] == 400
    assert [h["name"] for h in connector.hits_for(vid)] == ["e%d" % i for i in range(20)]
    req = connector.request_for(vid)
    assert req["error"] == "too_many_events:25" and req["body"] == raw


def test_ipv6_clients_share_their_network_allowance(connector):
    _same_minute()
    a, b = uuid.uuid4().int % 65536, uuid.uuid4().int % 65535
    codes = [connector.post(_env(_vid()), client="2001:db8:%x:%x::%x" % (a, b, i + 1))[0] for i in range(6)]
    assert codes == [204] * 5 + [429]
    assert connector.post(_env(_vid()), client="2001:db8:%x:%x::1" % (a, b + 1))[0] == 204  # another /64


def test_kilobyte_allowance(connector):
    _same_minute()
    client = _client_ip()
    big = lambda: _env(_vid(), events=[{"t": "event", "n": "x", "p": {"pad": "y" * 6500}}])  # noqa: E731  ~7 KB
    assert [connector.post(big(), client=client)[0] for _ in range(3)] == [204, 204, 429]


def test_flagged_requests_have_their_own_allowance(connector):
    _same_minute()
    client = _client_ip()
    before = _counter(connector, "", "flagged_limited")
    codes = [connector.post(_env(_vid(), site="nope"), client=client)[0] for _ in range(4)]
    assert codes == [204, 204, 204, 429]
    # A malformed body still answers 400 (retrying cannot help), stored or not.
    assert connector.post(None, raw=b"{oops", client=client)[0] == 400
    assert _counter(connector, "", "flagged_limited") == before + 2


def test_each_hit_and_the_stored_headers_are_charged(connector):
    _same_minute()
    client = _client_ip()
    tiny = lambda: _env(_vid(), events=[{"t": "e"}] * 20)  # noqa: E731  20 hits cost 20 × 256 bytes at least
    assert [connector.post(tiny(), client=client)[0] for _ in range(3)] == [204, 204, 429]
    client = _client_ip()
    pad = {"X-Pad": "p" * 7000}
    assert [connector.post(_env(_vid()), headers=pad, client=client)[0] for _ in range(3)] == [204, 204, 429]


def test_the_key_header_uses_its_last_value_without_port(connector):
    _same_minute()
    ip = "198.51.100.%d" % (uuid.uuid4().int % 250 + 1)
    codes = [connector.post(_env(_vid()), client="10.9.8.%d, %s:%d" % (i, ip, 40000 + i))[0] for i in range(6)]
    assert codes == [204] * 5 + [429]


def test_the_key_header_is_believed_only_when_it_is_an_address(connector):
    _same_minute()
    tag = uuid.uuid4().hex[:8]
    # Not an address: the request counts as coming from the proxy itself (127.0.0.1), whatever the header says.
    codes = [connector.post(_env(_vid()), client="nobody-%s-%d" % (tag, i))[0] for i in range(6)]
    assert codes == [204] * 5 + [429]


def test_the_key_header_is_believed_only_from_listed_proxies(connector, tmp_path):
    running = _launch(connector.name, tmp_path, CONFIG.replace('rate_limit_proxies = ["127.0.0.1/32", "::1/128"]',
                                                               "rate_limit_proxies = []"))
    try:
        _same_minute()
        codes = [running.post(_env(_vid()))[0] for _ in range(6)]  # a different X-Test-Client each time
        assert codes == [204] * 5 + [429]
    finally:
        _stop(running)


def test_nesting_deeper_than_64_levels_is_unreadable(connector):
    marker = uuid.uuid4().hex
    deep = {"v": 1, "site": "conf", "events": [{"t": "event", "p": {}}]}
    node = deep["events"][0]["p"]
    for _ in range(70):
        node["x"] = {}
        node = node["x"]
    raw = json.dumps(deep).encode()
    assert connector.post(None, raw=raw, headers={"X-Marker": marker})[0] == 400
    row = connector.rows("events.db", "SELECT error, body FROM raw_requests WHERE headers LIKE ?", ("%" + marker + "%",))[0]
    assert row["error"] == "bad_json: nested deeper than 64 levels" and row["body"] == raw


def test_counter_only_requests_stay_within_the_allowance(connector):
    _same_minute()
    client = _client_ip()
    before = _counter(connector, "priv", "dropped_dnt")
    headers = {"Origin": "https://priv.example", "DNT": "1"}
    codes = [connector.post(_env(_vid(), site="priv"), headers=headers, client=client)[0] for _ in range(7)]
    assert codes == [204] * 5 + [429] * 2  # over the allowance: refused before the body is read
    assert _counter(connector, "priv", "dropped_dnt") == before + 5


def test_a_client_over_its_allowance_is_refused_before_parsing(connector):
    _same_minute()
    client = _client_ip()
    before = _counter(connector, "conf", "rate_limited")
    assert [connector.post(_env(_vid()), client=client, path="/collect?site=conf")[0] for _ in range(5)] == [204] * 5
    marker = "early-" + uuid.uuid4().hex
    status, headers, _ = connector.post(None, raw=('{"broken": "%s"' % marker).encode(), client=client,
                                        path="/collect?site=conf")
    assert status == 429 and headers.get("retry-after") == "60"  # 429, not 400: the body is never parsed
    assert not connector.rows("events.db", "SELECT id FROM raw_requests WHERE body LIKE ?", ("%" + marker + "%",))
    assert _counter(connector, "conf", "rate_limited") == before + 1
    sid = "s" + uuid.uuid4().hex[:15]
    for _ in range(4):  # chunk_rate_limit_per_minute is 4
        assert connector.request("POST", "/chunk?site=conf&k=replay&seq=0&sid=" + sid, b"[]",
                                 {"Origin": "https://conf.example"}, client=client)[0] == 204
    before = _counter(connector, "conf", "chunk_rate_limited")
    assert connector.request("POST", "/chunk?site=conf&k=nonsense&sid=" + sid, b"x" * 100,
                             {"Origin": "https://conf.example"}, client=client)[0] == 429
    assert _counter(connector, "conf", "chunk_rate_limited") == before + 1


def test_hits_are_charged_as_stored(connector):
    # 9e15 is stored as 9000000000000000.0 (Python's float repr): the KB allowance (16 here) counts what is
    # stored, 26 KB for this 7 KB body.
    body = ('{"v":1,"site":"conf","vid":"v1","events":[{"t":"event","n":"x","p":{"a":[%s]}}]}'
            % ",".join(["9e15"] * 1400))
    assert connector.post(None, raw=body.encode(), client=_client_ip())[0] == 429


def test_allowance_keys_never_hold_an_address(connector):
    client = _client_ip()
    connector.post(_env(_vid()), client=client)
    keys = [r["key"] for r in connector.rows("events.db", "SELECT key FROM rate_limits")]
    assert keys and not any(client in k for k in keys)
    expected = "r:h:" + hmac.new(SECRET.encode(), client.encode(), hashlib.sha256).hexdigest()[:16]
    assert expected in keys


def test_a_few_invalid_events_do_not_flag_a_batch(connector):
    _same_minute()
    client = _client_ip()
    env = lambda: _env(_vid(), events=[{"t": "event", "n": "ok"}, "junk"])  # noqa: E731
    assert [connector.post(env(), client=client)[0] for _ in range(4)] == [204] * 4  # flagged_per_minute is 3


def test_flagged_global_allowance(connector, tmp_path):
    running = _launch(connector.name, tmp_path, CONFIG.replace("flagged_global_per_minute = 1000",
                                                               "flagged_global_per_minute = 3"))
    try:
        _same_minute()
        codes = [running.post(_env(_vid(), site="nope"))[0] for _ in range(4)]  # a new client each time
        assert codes == [204, 204, 204, 429]
        assert running.post(_env(_vid()))[0] == 204
    finally:
        _stop(running)


def test_daily_quota_per_site(connector):
    vid = _vid()
    two = _env(vid, site="quota", events=[{"t": "event", "n": "a"}, {"t": "event", "n": "b"}])
    first = connector.post(two, headers={"Origin": "https://quota.example"})[0]
    second = connector.post(two, headers={"Origin": "https://quota.example"})[0]
    assert (first, second) == (204, 429)  # 2 + 2 > 3
    assert len(connector.hits_for(vid)) == 2
    assert (_counter(connector, "quota", "hits"), _counter(connector, "quota", "over_quota")) == (2, 1)


def test_stored_hits_are_counted_per_site_and_day(connector):
    before = _counter(connector, "conf", "hits")
    assert connector.post(_env(_vid()))[0] == 204
    assert _counter(connector, "conf", "hits") == before + 2


def test_chunk_allowances(connector):
    _same_minute()
    client = _client_ip()
    send = lambda body: connector.request("POST", "/chunk?site=conf&k=replay&seq=0", body,  # noqa: E731
                                          {"Origin": "https://conf.example"}, client=client)[0]
    assert [send(b"[]") for _ in range(5)] == [204] * 4 + [429]
    client = _client_ip()
    assert [send(b"x" * 10000) for _ in range(3)] == [204, 204, 429]  # 10 + 10 + 10 > 24 KB
    assert _counter(connector, "conf", "chunk_rate_limited") >= 2


def test_chunks_of_a_feature_the_site_does_not_record_are_flagged(connector):
    sid = "s" + uuid.uuid4().hex[:15]
    assert connector.request("POST", "/chunk?site=conf&k=mouse&seq=0&sid=" + sid, b"[]",
                             {"Origin": "https://conf.example"})[0] == 204  # mouse_moves is off for "conf"
    assert [r["site_ok"] for r in connector.rows("replay.db", "SELECT site_ok FROM chunks WHERE session_id = ?",
                                                 (sid,))] == [0]


def test_low_disk_space_refuses_writes(connector, tmp_path):
    if connector.name == "worker":
        pytest.skip("D1 manages its own storage")
    running = _launch(connector.name, tmp_path, CONFIG.replace("[server]\n", "[server]\nmin_free_disk_mb = 1000000000\n"))
    try:
        status, headers, _ = running.post(_env(_vid()))
        assert status == 503 and headers.get("retry-after") == "300"
        assert running.request("POST", "/chunk?site=conf&k=replay&seq=0", b"[]", {"Origin": "https://conf.example"})[0] == 503
        status, _, body = running.request("GET", "/p.gif?site=conf")
        assert status == 200 and body.startswith(b"GIF89a")
        assert not os.path.exists(os.path.join(running.data_dir, "events.db")) or \
            running.rows("events.db", "SELECT COUNT(*) AS n FROM raw_requests")[0]["n"] == 0
    finally:
        _stop(running)


def test_server_cookie(connector):
    vid = _vid()
    _, headers, _ = connector.post(_env(vid))
    cookie = headers.get("set-cookie", "")
    assert cookie.startswith("ts_svid=%s.1728380000000; Domain=.conf.example; Path=/; Max-Age=34560000; SameSite=Lax" % vid)
    _, headers, _ = connector.post(_env(vid + "\n"))  # no header splitting
    assert "set-cookie" not in headers


def test_privacy_truncate_and_dnt(connector):
    vid = _vid()
    assert connector.post(_env(vid, site="priv"), headers={
        "Origin": "https://priv.example", "X-Forwarded-For": "203.0.113.77, 10.1.2.3", "X-Originating-IP": "203.0.113.78",
        "Fly-Client-IP": "2001:db8:1:2::5", "CloudFront-Viewer-Address": "198.51.100.9:46532",
        "X-App-Version": "1.2.3.4", "X-Remote-Addr": "198.51.100.23", "Via": "1.1 203.0.113.9 (proxy)",
        "X-Real-IP": "2002:cb00:7105:1234::1"})[0] == 204
    req = connector.request_for(vid)
    assert req["ip"] == "127.0.0.0"
    headers = json.loads(req["headers"])
    assert headers["x-forwarded-for"] == "203.0.113.0, 10.1.2.0"
    # Any header whose name contains ip, forwarded, client or addr is treated as IP-bearing, and Via.
    assert (headers["x-originating-ip"], headers["fly-client-ip"], headers["cloudfront-viewer-address"]) == \
        ("203.0.113.0", "2001:db8:1::", "198.51.100.0:46532")
    assert (headers["x-remote-addr"], headers["via"]) == ("198.51.100.0", "1.1 203.0.113.0 (proxy)")
    assert headers["x-real-ip"] == "2002:cb00:7100::"  # 6to4: the IPv4 address inside is cut to its /24
    assert headers["x-app-version"] == "1.2.3.4"
    vid2 = _vid()
    assert connector.post(_env(vid2, site="priv"), headers={"Origin": "https://priv.example", "DNT": "1"})[0] == 204
    assert connector.hits_for(vid2) == []


def test_merged_cookie_headers_never_leak_into_a_kept_value(connector):
    vid = _vid()
    connector.post(_env(vid), headers={"Cookie": "ts_vid=%s.1, sessionid=SECRET-SESSION; ts_ses=s.1" % vid})
    cookies = json.loads(connector.request_for(vid)["cookies"])
    assert cookies["kept"] == {"ts_vid": vid + ".1", "ts_ses": "s.1"}


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


def test_stored_referer_is_redacted(connector):
    """The Referer may hold the full page URL (beacons, the pixel): stored with its secrets redacted, by the
    site's redact_params and redact_paths, exactly as redact.py does (docs/connectors.md, "URL redaction")."""
    wrong = []
    for url, expected in VECTORS["cases"]:
        if not url:
            continue
        vid = _vid()
        status, _, _ = connector.request("GET", "/p.gif?site=redact&vid=%s" % vid, headers={"Referer": url})
        assert status == 200
        stored = json.loads(connector.request_for(vid)["headers"]).get("referer")
        page = connector.hits_for(vid)[0]["url"]
        if stored != expected or page != expected:
            wrong.append((url, expected, stored, page))
    assert not wrong


def test_referer_of_collect_and_chunk_is_redacted(connector):
    vid = _vid()
    assert connector.post(_env(vid), headers={"Referer": "https://conf.example/a?token=abc&x=1#sid=2"})[0] == 204
    stored = json.loads(connector.request_for(vid)["headers"])["referer"]
    assert stored == "https://conf.example/a?token=redacted&x=1#sid=redacted"
    sid = "s" + uuid.uuid4().hex[:15]
    assert connector.request("POST", "/chunk?site=conf&k=replay&sid=%s&seq=0" % sid, b"[]",
                             {"Origin": "https://conf.example",
                              "Referer": "https://conf.example/a;jsessionid=X?sig=1"})[0] == 204
    meta = json.loads(connector.rows("replay.db", "SELECT meta FROM chunks WHERE session_id = ?", (sid,))[0]["meta"])
    assert meta["referer"] == "https://conf.example/a;jsessionid=redacted?sig=redacted"
    vid = _vid()  # an unknown site: the default patterns
    connector.request("GET", "/p.gif?site=nosuchsite&vid=%s" % vid,
                      headers={"Referer": "https://user:pw@elsewhere.example/p?password=1"})
    stored = json.loads(connector.request_for(vid)["headers"])["referer"]
    assert stored == "https://redacted@elsewhere.example/p?password=redacted"


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
    assert text.startswith('(typeof window.tidesight=="function"?window.tidesight:window.tidesight=function(){'
                           '(tidesight.q=tidesight.q||[]).push(arguments)})("_config",')
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
