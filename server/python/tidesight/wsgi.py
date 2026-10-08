"""The deployable WSGI application.

    /dashboard/            static dashboard (HTTP Basic auth)
    /dashboard/api/<name>  read API (HTTP Basic auth)
    /share/<site>/         public stats page of a site with `public = true`
    /share/<site>/api/...  its restricted API
    everything else        the collector (collect.py)

Passenger, CGI and the dev server all import `application` from here.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs

from .api import Api, ApiError
from .auth import Authenticator
from .collect import Collector
from .config import Config, ConfigHolder

log = logging.getLogger("tidesight.web")

StartResponse = Callable[..., Any]

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".mjs": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                 ".ico": "image/x-icon", ".woff2": "font/woff2", ".map": "application/json; charset=utf-8",
                 ".txt": "text/plain; charset=utf-8"}
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SECURITY_HEADERS = [("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "SAMEORIGIN"),
                    ("Referrer-Policy", "same-origin"), ("X-Robots-Tag", "noindex, nofollow")]


def _respond(start_response: StartResponse, status: str, headers: List[Tuple[str, str]], body: bytes = b"",
             head: bool = False) -> List[bytes]:
    start_response(status, headers + [("Content-Length", str(len(body)))])
    return [b"" if head else body]


def _json(start_response: StartResponse, status: str, obj: Any, extra: Optional[List[Tuple[str, str]]] = None) -> List[bytes]:
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    return _respond(start_response, status, [("Content-Type", "application/json; charset=utf-8"),
                                             ("Cache-Control", "no-store")] + SECURITY_HEADERS + (extra or []), body)


class App:
    def __init__(self, holder: ConfigHolder, auth: bool = True) -> None:
        self.holder = holder
        self.collector = Collector(holder)
        self.require_auth = auth
        self._auth: Optional[Authenticator] = None
        self._auth_users: Optional[Dict[str, str]] = None

    def authenticator(self, cfg: Config) -> Authenticator:
        users = cfg.dashboard["users"]
        if self._auth is None or users != self._auth_users:
            self._auth = Authenticator(users)
            self._auth_users = dict(users)
        return self._auth

    def __call__(self, environ: Dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        path = environ.get("PATH_INFO") or "/"
        if path == "/dashboard" or path.startswith("/dashboard/"):
            return self.dashboard(environ, start_response, path)
        if path.startswith("/share/"):
            return self.share(environ, start_response, path)
        return self.collector(environ, start_response)

    # -- dashboard -----------------------------------------------------------

    def _check_auth(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse) -> Optional[List[bytes]]:
        if not self.require_auth:
            return None
        if not cfg.dashboard["users"]:
            return _respond(start_response, "403 Forbidden", [("Content-Type", "text/plain; charset=utf-8")],
                            b"The dashboard is disabled until you add users to [dashboard] in the config.\n"
                            b"Create a password hash with: python -m tidesight.jobs hash-password\n")
        user = self.authenticator(cfg).check(environ.get("HTTP_AUTHORIZATION"))
        if user is None:
            return _respond(start_response, "401 Unauthorized",
                            [("WWW-Authenticate", 'Basic realm="Tidesight", charset="UTF-8"'),
                             ("Content-Type", "text/plain")], b"authentication required\n")
        environ["tidesight.user"] = user
        return None

    def dashboard(self, environ: Dict[str, Any], start_response: StartResponse, path: str) -> Iterable[bytes]:
        cfg = self.holder.get()
        denied = self._check_auth(cfg, environ, start_response)
        if denied is not None:
            return denied
        if path == "/dashboard":
            return _respond(start_response, "301 Moved Permanently", [("Location", "/dashboard/")])
        if path.startswith("/dashboard/api/"):
            return self.api(cfg, environ, start_response, path[len("/dashboard/api/"):], None)
        return self.static(cfg, environ, start_response, path[len("/dashboard/"):])

    def share(self, environ: Dict[str, Any], start_response: StartResponse, path: str) -> Iterable[bytes]:
        cfg = self.holder.get()
        parts = path.split("/")  # ['', 'share', site, ...]
        site = cfg.site(parts[2]) if len(parts) > 2 else None
        if site is None or not site.settings["public"]:
            return _respond(start_response, "404 Not Found", [("Content-Type", "text/plain")], b"not found\n")
        token = site.settings.get("share_token")
        if token:
            given = parse_qs(environ.get("QUERY_STRING", "")).get("t", [""])[0] or environ.get("HTTP_X_SHARE_TOKEN", "")
            if given != token:
                return _respond(start_response, "403 Forbidden", [("Content-Type", "text/plain")], b"forbidden\n")
        rest = "/".join(parts[3:])
        if rest.startswith("api/"):
            return self.api(cfg, environ, start_response, rest[4:], site.id)
        if rest == "" and not path.endswith("/"):
            return _respond(start_response, "301 Moved Permanently", [("Location", path + "/")])
        return self.static(cfg, environ, start_response, rest)

    def api(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse, endpoint: str,
            public_site: Optional[str]) -> Iterable[bytes]:
        if environ.get("REQUEST_METHOD", "GET").upper() not in ("GET", "HEAD"):
            return _json(start_response, "405 Method Not Allowed", {"error": "read-only API"})
        params = {k: v[0] for k, v in parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).items()}
        if endpoint == "export" and public_site is None:
            return self.export(cfg, start_response, params)
        try:
            data = Api(cfg).call(endpoint, params, public_site)
        except ApiError as exc:
            return _json(start_response, "%d %s" % (exc.status, _REASONS.get(exc.status, "Error")), {"error": exc.message})
        except Exception:
            log.exception("API error on %s", endpoint)
            return _json(start_response, "500 Internal Server Error", {"error": "internal error"})
        return _json(start_response, "200 OK", data)

    def export(self, cfg: Config, start_response: StartResponse, params: Dict[str, str]) -> Iterable[bytes]:
        import io

        from .maintenance import EXPORTS, iter_export, write_export
        what, fmt = params.get("what", "sessions"), params.get("format", "csv")
        if what not in EXPORTS or fmt not in ("csv", "json", "jsonl") or cfg.site(params.get("site")) is None:
            return _json(start_response, "400 Bad Request", {"error": "bad export parameters"})
        cols, rows = iter_export(cfg, params["site"], what, params.get("from"), params.get("to"))
        buf = io.StringIO()
        write_export(cols, rows, fmt, buf)
        body = buf.getvalue().encode("utf-8")
        ctype = {"csv": "text/csv", "json": "application/json", "jsonl": "application/x-ndjson"}[fmt]
        name = "tidesight-%s-%s.%s" % (params["site"], what, fmt)
        return _respond(start_response, "200 OK", [("Content-Type", ctype + "; charset=utf-8"),
                                                   ("Content-Disposition", 'attachment; filename="%s"' % name),
                                                   ("Cache-Control", "no-store")] + SECURITY_HEADERS, body)

    def static(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse, rel: str) -> Iterable[bytes]:
        root = cfg.server["dashboard_dir"] or os.path.join(_REPO_ROOT, "dashboard")
        segments = [s for s in rel.split("/") if s]
        if not all(_SAFE_SEGMENT.match(s) for s in segments):
            return _respond(start_response, "404 Not Found", [("Content-Type", "text/plain")], b"not found\n")
        file_path = os.path.join(root, *segments) if segments else os.path.join(root, "index.html")
        ext = os.path.splitext(file_path)[1].lower()
        if not os.path.isfile(file_path):
            if ext:  # a missing asset
                return _respond(start_response, "404 Not Found", [("Content-Type", "text/plain")], b"not found\n")
            file_path, ext = os.path.join(root, "index.html"), ".html"  # client-side route
        if ext not in _STATIC_TYPES or not os.path.isfile(file_path):
            return _respond(start_response, "404 Not Found", [("Content-Type", "text/plain")], b"not found\n")
        with open(file_path, "rb") as fh:
            body = fh.read()
        cache = "no-cache" if ext == ".html" else "public, max-age=300"
        headers = [("Content-Type", _STATIC_TYPES[ext]), ("Cache-Control", cache)] + SECURITY_HEADERS
        if ext == ".html":
            headers.append(("Content-Security-Policy",
                            "default-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
                            "img-src 'self' data: blob: https: http:; media-src 'self' data: blob: https:; "
                            "style-src 'self' 'unsafe-inline' https:; font-src 'self' data: https:; "
                            "frame-src 'self' blob: https: http:"))
        return _respond(start_response, "200 OK", headers, body,
                        head=environ.get("REQUEST_METHOD", "GET").upper() == "HEAD")


_REASONS = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 503: "Service Unavailable"}

_app: Optional[App] = None


def create_app(config_path: Optional[str] = None, config: Optional[Config] = None, auth: bool = True) -> App:
    """Build the full application."""
    return App(ConfigHolder(config_path, config), auth=auth)


def application(environ: Dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
    """Lazily created on the first request, using $TIDESIGHT_CONFIG or a conventional path."""
    global _app
    if _app is None:
        _app = create_app()
    return _app(environ, start_response)
