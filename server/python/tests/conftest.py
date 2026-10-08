"""Shared pytest fixtures for the Python server tests."""

from __future__ import annotations

import io
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional
from wsgiref.util import setup_testing_defaults

import pytest

from tidesight.collect import Collector
from tidesight.config import Config, ConfigHolder, loads

BASE_TOML = """
[server]
data_dir = "{data_dir}"
rate_limit_per_minute = {rate}
storage_mode = "{storage_mode}"
journal_mode = "{journal_mode}"

[sites.example]
name = "Example"
origins = ["https://example.com", "https://*.example.org"]
timezone = "Europe/Paris"
"""


class Response:
    def __init__(self, status: str, headers: List, body: bytes) -> None:
        self.status = status
        self.code = int(status.split()[0])
        self.header_list = headers
        self.headers = {k.lower(): v for k, v in headers}
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def call(app: Any, method: str = "GET", path: str = "/", body: bytes = b"", headers: Optional[Dict[str, str]] = None,
         query: str = "", remote_addr: str = "203.0.113.9", scheme: str = "https") -> Response:
    environ: Dict[str, Any] = {}
    setup_testing_defaults(environ)
    environ.update({"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query,
                    "REMOTE_ADDR": remote_addr, "wsgi.input": io.BytesIO(body), "wsgi.url_scheme": scheme,
                    "CONTENT_LENGTH": str(len(body)) if body else ""})
    environ.pop("CONTENT_TYPE", None)
    for k, v in (headers or {}).items():
        key = k.upper().replace("-", "_")
        if key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            environ[key] = v
        else:
            environ["HTTP_" + key] = v
    captured: Dict[str, Any] = {}

    def start_response(status: str, hdrs: List, exc_info: Any = None) -> None:
        captured["status"] = status
        captured["headers"] = hdrs

    out = b"".join(app(environ, start_response))
    return Response(captured["status"], captured["headers"], out)


def query_db(path: str, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


class Harness:
    """A collector bound to a temporary data directory."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.app = Collector(ConfigHolder(config=cfg))
        self.data_dir = cfg.data_dir

    def call(self, *args: Any, **kwargs: Any) -> Response:
        return call(self.app, *args, **kwargs)

    def post(self, payload: Any, headers: Optional[Dict[str, str]] = None, path: str = "/collect",
             query: str = "", raw: Optional[bytes] = None, **kw: Any) -> Response:
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        h = {"Content-Type": "text/plain;charset=UTF-8", "Origin": "https://example.com",
             "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/129.0 Safari/537.36"}
        h.update(headers or {})
        return self.call("POST", path, body=body, headers=h, query=query, **kw)

    def events(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        return query_db(os.path.join(self.data_dir, "events.db"), sql, params)

    def replay(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        return query_db(os.path.join(self.data_dir, "replay.db"), sql, params)

    def close(self) -> None:
        store = self.app._store
        if store is not None:
            store.close()


@pytest.fixture
def make_harness(tmp_path):
    harnesses: List[Harness] = []

    def _make(extra: str = "", rate: int = 600, storage_mode: str = "sqlite", journal_mode: str = "wal") -> Harness:
        data_dir = str(tmp_path / ("data%d" % len(harnesses))).replace("\\", "/")
        text = BASE_TOML.format(data_dir=data_dir, rate=rate, storage_mode=storage_mode,
                                journal_mode=journal_mode) + extra
        h = Harness(loads(text, str(tmp_path / "tidesight.toml")))
        harnesses.append(h)
        return h

    yield _make
    for h in harnesses:
        h.close()


@pytest.fixture
def harness(make_harness) -> Harness:
    return make_harness()


def envelope(events: Optional[List[Dict[str, Any]]] = None, **kw: Any) -> Dict[str, Any]:
    env = {"v": 1, "site": "example", "vid": "visitor00000001", "vt": 1728380000000, "sid": "session0000001",
           "sent": 1728380012345, "lib": "ts.js/test", "events": events if events is not None else [
               {"t": "pageview", "id": "ev1", "ts": 1728380012001, "pid": "page0001",
                "url": "https://example.com/a?utm_source=x#top", "title": "A"}]}
    env.update(kw)
    return env
