"""The deployable WSGI application.

    /dashboard/            static dashboard (HTTP Basic auth)
    /dashboard/api/<name>  read API (HTTP Basic auth)
    /share/<site>/         public stats page of a site with `public = true`
    /share/<site>/api/...  its restricted API
    everything else        the collector (collect.py)

Passenger, CGI and the dev server all import `application` from here.
"""

from __future__ import annotations

import hmac
import json
import logging
import math
import os
import re
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs

from .api import Api, ApiError
from .auth import Authenticator, Throttle
from .collect import ERRORS, Collector
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


def _finite(obj: Any) -> Any:
    """The same structure with infinite and NaN numbers as null: browsers cannot parse NaN or Infinity in JSON."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    return obj


def _json(start_response: StartResponse, status: str, obj: Any, extra: Optional[List[Tuple[str, str]]] = None) -> List[bytes]:
    try:
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str, allow_nan=False)
    except ValueError:  # a non-finite number from raw data
        text = json.dumps(_finite(obj), ensure_ascii=False, separators=(",", ":"), default=str, allow_nan=False)
    body = text.encode("utf-8")
    return _respond(start_response, status, [("Content-Type", "application/json; charset=utf-8"),
                                             ("Cache-Control", "no-store")] + SECURITY_HEADERS + (extra or []), body)


class App:
    SHARE_API_PER_MINUTE = 120  # public stats pages: API calls per client and minute

    def __init__(self, holder: ConfigHolder, auth: bool = True) -> None:
        self.holder = holder
        self.collector = Collector(holder)
        self.require_auth = auth
        self.throttle = Throttle()  # failed logins and share tokens, per client
        self._auth: Optional[Authenticator] = None
        self._auth_users: Optional[Dict[str, str]] = None
        self._share_hits: Dict[str, int] = {}  # calls per client in the current minute
        self._share_minute = 0
        self._share_lock = threading.Lock()

    def authenticator(self, cfg: Config) -> Authenticator:
        users = cfg.dashboard["users"]
        if self._auth is None or users != self._auth_users:
            self._auth = Authenticator(users, self.throttle)
            self._auth_users = dict(users)
        return self._auth

    def _client(self, cfg: Config, environ: Dict[str, Any]) -> str:
        return self.collector._rate_key(cfg, environ) or "-"

    def _share_allowed(self, client: str) -> bool:
        minute = int(time.time() // 60)
        with self._share_lock:
            if self._share_minute != minute:
                self._share_minute, self._share_hits = minute, {}
            if client not in self._share_hits and len(self._share_hits) >= 50000:
                return True  # bounded memory: a flood of identities is not tracked one by one (the API is cheap)
            n = self._share_hits.get(client, 0) + 1
            self._share_hits[client] = n
        return n <= self.SHARE_API_PER_MINUTE

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
        user, status = self.authenticator(cfg).verify(environ.get("HTTP_AUTHORIZATION"), self._client(cfg, environ))
        if status == "throttled":
            return _respond(start_response, "429 Too Many Requests", [("Retry-After", "60"), ("Content-Type", "text/plain")],
                            b"too many attempts: try again in a few minutes\n")
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
            if environ.get("HTTP_SEC_FETCH_SITE") == "cross-site":  # another site making the browser ask (CSRF)
                return _json(start_response, "403 Forbidden", {"error": "cross-site request"})
            return self.api(cfg, environ, start_response, path[len("/dashboard/api/"):], None)
        return self.static(cfg, environ, start_response, path[len("/dashboard/"):])

    def share(self, environ: Dict[str, Any], start_response: StartResponse, path: str) -> Iterable[bytes]:
        cfg = self.holder.get()
        parts = path.split("/")  # ['', 'share', site, ...]
        site = cfg.site(parts[2]) if len(parts) > 2 else None
        if site is None or not site.settings["public"]:
            return _respond(start_response, "404 Not Found", [("Content-Type", "text/plain")], b"not found\n")
        client = self._client(cfg, environ)
        rest = "/".join(parts[3:])
        if rest.startswith("api/"):
            # Only the data needs the token: the page and its assets hold none, and load without it.
            token = site.settings.get("share_token")
            if token:
                given = environ.get("HTTP_X_SHARE_TOKEN", "") or \
                    parse_qs(environ.get("QUERY_STRING", "")).get("t", [""])[0]
                if self.throttle.blocked("share:" + client):
                    return _json(start_response, "429 Too Many Requests", {"error": "too many attempts"},
                                 [("Retry-After", "60")])
                if not hmac.compare_digest(given.encode("utf-8"), token.encode("utf-8")):
                    if given:
                        self.throttle.failed("share:" + client)
                    return _json(start_response, "403 Forbidden", {"error": "a valid share token is needed"})
            if not self._share_allowed(client):
                return _json(start_response, "429 Too Many Requests", {"error": "too many requests"},
                             [("Retry-After", "60")])
            return self.api(cfg, environ, start_response, rest[4:], site.id)
        if rest == "" and not path.endswith("/"):
            return _respond(start_response, "301 Moved Permanently", [("Location", path + "/")])
        first = rest.split("/", 1)[0]
        if rest and first not in SHARE_FILES and first not in SHARE_DIRS:  # the page's own files, nothing else
            return _respond(start_response, "404 Not Found", [("Content-Type", "text/plain")], b"not found\n")
        return self.static(cfg, environ, start_response, rest, share=True)

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
            ERRORS.exception("API error on %s", endpoint)
            return _json(start_response, "500 Internal Server Error", {"error": "internal error"})
        return _json(start_response, "200 OK", data)

    def export(self, cfg: Config, start_response: StartResponse, params: Dict[str, str]) -> Iterable[bytes]:
        from .maintenance import EXPORTS, iter_export
        what, fmt = params.get("what", "sessions"), params.get("format", "csv")
        if what not in EXPORTS or fmt not in ("csv", "json", "jsonl") or cfg.site(params.get("site")) is None:
            return _json(start_response, "400 Bad Request", {"error": "bad export parameters"})
        cols, rows = iter_export(cfg, params["site"], what, params.get("from"), params.get("to"))
        ctype = {"csv": "text/csv", "json": "application/json", "jsonl": "application/x-ndjson"}[fmt]
        name = "tidesight-%s-%s.%s" % (params["site"], what, fmt)
        start_response("200 OK", [("Content-Type", ctype + "; charset=utf-8"),
                                  ("Content-Disposition", 'attachment; filename="%s"' % name),
                                  ("Cache-Control", "no-store")] + SECURITY_HEADERS)
        return _stream_export(cols, rows, fmt)

    def static(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse, rel: str,
               share: bool = False) -> Iterable[bytes]:
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
            headers.append(("Content-Security-Policy", SHARE_CSP if share else content_security_policy(cfg)))
        return _respond(start_response, "200 OK", headers, body,
                        head=environ.get("REQUEST_METHOD", "GET").upper() == "HEAD")


# An origin for the CSP: a host name, or a wildcard over the subdomains of a name with at least two labels
# ("https://*.example.com"; never "https://*" or "https://*.com", which would allow almost anything).
_CSP_SOURCE = re.compile(r"^https?://(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)+(:[0-9]{1,5})?$|^https?://localhost(:[0-9]{1,5})?$")

# Public share pages show charts and tables only (no replays, no framed pages): no foreign origin at all, and the
# configured sites (some private) are not listed.
SHARE_CSP = ("default-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
             "form-action 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; font-src 'self' data:; "
             "frame-src 'none'; frame-ancestors 'self'")
# What a share page may load from the dashboard folder (package.json and the like are not for the public).
SHARE_FILES = {"index.html", "app.css", "favicon.svg"}
SHARE_DIRS = {"js", "vendor"}


def content_security_policy(cfg: Config) -> str:
    """The dashboard's CSP. Replays, DOM snapshots and the framed live page may load images, styles, fonts and
    media only from the sites' own origins and [dashboard] asset_origins: a recording sent by anyone cannot make
    the analyst's browser contact another host (no "read receipts", no foreign frames)."""
    sources: List[str] = []
    for origin in [o for s in cfg.sites.values() for o in s.origins] + list(cfg.dashboard.get("asset_origins") or []):
        o = str(origin).strip().lower().rstrip("/")
        if _CSP_SOURCE.match(o) and o not in sources:
            sources.append(o)
    extra = (" " + " ".join(sources)) if sources else ""
    return ("default-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
            "form-action 'none'; img-src 'self' data: blob:%s; media-src 'self' data: blob:%s; "
            "style-src 'self' 'unsafe-inline'%s; font-src 'self' data:%s; frame-src 'self' blob:%s"
            % (extra, extra, extra, extra, extra))


def _stream_export(cols: List[str], rows: Iterable[Any], fmt: str) -> Iterable[bytes]:
    """The export in pieces of about 64 KB, so a large one never sits in memory."""
    import csv
    import io

    from .maintenance import _csv_cell
    buf = io.StringIO()
    writer = csv.writer(buf)
    if fmt == "csv":
        writer.writerow(cols)
    elif fmt == "json":
        buf.write("[")
    n = 0
    for row in rows:
        if fmt == "csv":
            writer.writerow([_csv_cell(v) for v in row])
        else:
            item = json.dumps(_finite(dict(zip(cols, row))), ensure_ascii=False, default=lambda b: b.hex())
            buf.write(item + "\n" if fmt == "jsonl" else ("," if n else "") + "\n" + item)
        n += 1
        if buf.tell() > 65536:
            yield buf.getvalue().encode("utf-8")
            buf.seek(0)
            buf.truncate()
    if fmt == "json":
        buf.write("\n]\n")
    yield buf.getvalue().encode("utf-8")


_REASONS = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 503: "Service Unavailable"}

_app: Optional[App] = None


def create_app(config_path: Optional[str] = None, config: Optional[Config] = None, auth: bool = True) -> App:
    """Build the full application."""
    os.umask(0o077)  # databases, backups, logs and spool files are for this account only
    return App(ConfigHolder(config_path, config), auth=auth)


def application(environ: Dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
    """Lazily created on the first request, using $TIDESIGHT_CONFIG or a conventional path."""
    global _app
    if _app is None:
        _app = create_app()
    return _app(environ, start_response)
