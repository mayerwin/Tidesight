"""Local development server (standard library only).

    python server/python/devserver.py --config config/dev.toml --port 8787

Options for the browser tests:
    --static DIR --static-port N   also serve a static site (e.g. the tracker's fixture site) on another port,
                                   so tracker and collector run on different origins as in production
    --test-api                     expose /__test/* endpoints to inspect and reset stored data (never in production)
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sqlite3
import sys
import threading
from socketserver import ThreadingMixIn
from typing import Any, Callable, Dict, Iterable, List
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from tidesight.config import ConfigHolder  # noqa: E402
from tidesight.wsgi import create_app  # noqa: E402


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if os.environ.get("TIDESIGHT_DEV_LOG"):
            super().log_message(format, *args)


def _json(start_response: Callable[..., Any], obj: Any, status: str = "200 OK") -> List[bytes]:
    body = json.dumps(obj).encode("utf-8")
    start_response(status, [("Content-Type", "application/json"), ("Cache-Control", "no-store"),
                            ("Access-Control-Allow-Origin", "*"), ("Content-Length", str(len(body)))])
    return [body]


def test_api(app: Callable[..., Iterable[bytes]], holder: ConfigHolder) -> Callable[..., Iterable[bytes]]:
    """Wrap the app with /__test/* endpoints that read the raw databases."""
    lock = threading.Lock()

    def rows(db: str, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        path = os.path.join(holder.get().data_dir, db)
        if not os.path.exists(path):
            return []
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def wrapped(environ: Dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        path = environ.get("PATH_INFO", "")
        if not path.startswith("/__test/"):
            return app(environ, start_response)
        from urllib.parse import parse_qs
        qs = {k: v[0] for k, v in parse_qs(environ.get("QUERY_STRING", "")).items()}
        since = int(qs.get("since", "0") or 0)
        if path == "/__test/hits":
            out = rows("events.db", "SELECT h.*, r.ip, r.headers, r.cookies, r.envelope, r.site_status, "
                       "r.endpoint FROM raw_hits h JOIN raw_requests r ON r.id = h.request_id "
                       "WHERE h.id > ? ORDER BY h.id", (since,))
            for r in out:
                r["data"] = json.loads(r["data"])
                for k in ("headers", "cookies", "envelope"):
                    r[k] = json.loads(r[k]) if r.get(k) else None
            return _json(start_response, out)
        if path == "/__test/requests":
            out = rows("events.db", "SELECT id, received_at, site_id, site_status, endpoint, error, event_count, "
                       "content_type, body_encoding, envelope FROM raw_requests WHERE id > ? ORDER BY id", (since,))
            return _json(start_response, out)
        if path == "/__test/chunks":
            out = rows("replay.db", "SELECT id, kind, site_id, visitor_id, session_id, page_id, seq, client_ts, "
                       "length(payload) AS bytes, meta FROM chunks WHERE id > ? ORDER BY id", (since,))
            return _json(start_response, out)
        if path == "/__test/reset":
            with lock:
                for db, tables in (("events.db", ("raw_hits", "raw_requests", "rate_limits", "counters")),
                                   ("replay.db", ("chunks", "rate_limits"))):
                    p = os.path.join(holder.get().data_dir, db)
                    if os.path.exists(p):
                        conn = sqlite3.connect(p, timeout=10)
                        for t in tables:
                            try:
                                conn.execute("DELETE FROM %s" % t)
                            except sqlite3.OperationalError:
                                pass
                        conn.commit()
                        conn.close()
            return _json(start_response, {"ok": True})
        return _json(start_response, {"error": "unknown test endpoint"}, "404 Not Found")

    return wrapped


def static_app(root: str) -> Callable[..., Iterable[bytes]]:
    """Serve files from root; /spa/* falls back to /spa/index.html (History API routing)."""
    root = os.path.abspath(root)

    def app(environ: Dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        path = environ.get("PATH_INFO", "/")
        if path.endswith("/"):
            path += "index.html"
        full = os.path.abspath(os.path.join(root, path.lstrip("/")))
        if not full.startswith(root) or not os.path.isfile(full):
            if path.startswith("/spa/"):
                full = os.path.join(root, "spa", "index.html")
            else:
                start_response("404 Not Found", [("Content-Type", "text/html")])
                return [b"<!doctype html><title>Not found</title><h1>Not found</h1>"]
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            body = fh.read()
        start_response("200 OK", [("Content-Type", ctype), ("Content-Length", str(len(body))),
                                  ("Cache-Control", "no-store")])
        return [body]

    return app


def serve(app: Callable[..., Iterable[bytes]], host: str, port: int) -> ThreadingWSGIServer:
    server = make_server(host, port, app, server_class=ThreadingWSGIServer, handler_class=QuietHandler)
    return server


def main(argv: List[str] = None) -> None:
    ap = argparse.ArgumentParser(description="Tidesight development server")
    ap.add_argument("--config", help="config file (default: $TIDESIGHT_CONFIG or conventional locations)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--static", help="directory to serve as a static site on --static-port")
    ap.add_argument("--static-port", type=int, default=8788)
    ap.add_argument("--test-api", action="store_true", help="enable /__test/* endpoints (tests only)")
    args = ap.parse_args(argv)

    holder = ConfigHolder(args.config)
    app = create_app(args.config)
    if args.test_api:
        app = test_api(app, holder)
    servers = [serve(app, args.host, args.port)]
    print("Tidesight collector on http://%s:%d (data: %s)" % (args.host, args.port, holder.get().data_dir),
          flush=True)
    if args.static:
        servers.append(serve(static_app(args.static), args.host, args.static_port))
        print("Static site %s on http://%s:%d" % (args.static, args.host, args.static_port), flush=True)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
