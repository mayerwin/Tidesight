"""The deployable WSGI application.

    /dashboard/            static dashboard (its sign-in page when signed out)
    /dashboard/api/<name>  API: reads (api.py), changes (admin.py), sign-in, exports
    /share/<site>/         public stats page of a site with `public = true`
    /share/<site>/api/...  its restricted API
    everything else        the collector (collect.py)

Signing in: the dashboard posts a user name and password to /dashboard/api/login and receives a session cookie
(ts_login: HttpOnly, SameSite=Strict, limited to the dashboard's path); scripts may instead send HTTP Basic
credentials with each request. Every request that changes something must carry an X-Tidesight header, which a
page on another origin cannot add without a CORS permission this app never gives, and its Origin must be this
host. The FastCGI and CGI entry points and Passenger import `application` from here; the dev server, create_app.
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
from urllib.parse import parse_qs, urlsplit

from .api import Api, ApiError
from .auth import Authenticator, SharedThrottle, Throttle, password_tag
from .collect import ERRORS, Collector, web_root_exposure
from .config import Config, ConfigHolder

log = logging.getLogger("tidesight.web")

StartResponse = Callable[..., Any]

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".mjs": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                 ".ico": "image/x-icon", ".woff2": "font/woff2", ".map": "application/json; charset=utf-8",
                 ".txt": "text/plain; charset=utf-8"}
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")  # with fullmatch: no trailing newline
SECURITY_HEADERS = [("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "SAMEORIGIN"),
                    ("Referrer-Policy", "same-origin"), ("X-Robots-Tag", "noindex, nofollow")]
COOKIE = "ts_login"
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
ADMIN_READS = ("properties", "property", "users", "update")
REPORT_EXPORTS = ("breakdown", "overview", "goals")
_COOKIE_PATH = re.compile(r"/[A-Za-z0-9._~%/-]*")  # with fullmatch
# What the dashboard folder serves (package.json, node_modules and the like are not for anyone).
DASHBOARD_FILES = {"index.html", "app.css", "favicon.svg"}
DASHBOARD_DIRS = {"js", "vendor"}
# Files other than pages run nothing: an SVG opened on its own cannot run a script.
ASSET_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; sandbox"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
PASSWORD_ENDPOINTS = ("login", "setup", "password_link", "password")


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


def _status(code: int) -> str:
    return "%d %s" % (code, _REASONS.get(code, "Error"))


def _https(environ: Dict[str, Any]) -> bool:
    return (environ.get("wsgi.url_scheme") == "https" or str(environ.get("HTTPS", "")).lower() in ("on", "1")
            or environ.get("HTTP_X_FORWARDED_PROTO", "").split(",")[0].strip().lower() == "https")


def _home_path(path: str) -> Optional[str]:
    """`path` written from the home folder (~/…), or None when it lies elsewhere: an absolute path would show the
    server's layout and the account's name to anyone."""
    home = os.path.realpath(os.path.expanduser("~"))
    real = os.path.realpath(path)
    if real.startswith(home.rstrip(os.sep) + os.sep):
        return "~/" + os.path.relpath(real, home).replace(os.sep, "/")
    return None


def _dashboard_path(environ: Dict[str, Any]) -> str:
    """The URL path of the dashboard as the browser sees it (the cookie's Path): whatever comes before
    /dashboard/ in the request (a CGI script name, a folder) is kept."""
    uri = environ.get("REQUEST_URI") or (environ.get("SCRIPT_NAME", "") + (environ.get("PATH_INFO") or ""))
    path = uri.split("?", 1)[0]
    i = path.find("/dashboard/")
    prefix = path[:i] if i >= 0 else environ.get("SCRIPT_NAME", "").rstrip("/")
    cookie_path = prefix + "/dashboard/"
    return cookie_path if _COOKIE_PATH.fullmatch(cookie_path) and len(cookie_path) < 300 else "/dashboard/"


def _cookie(environ: Dict[str, Any], name: str) -> Optional[str]:
    """The value of a cookie sent exactly once. Two values mean another site of the same domain (a sibling
    subdomain) planted one: neither is trusted."""
    raw = environ.get("HTTP_COOKIE")
    if not raw or len(raw) > 65536:
        return None
    found = [part.split("=", 1)[1].strip() for part in raw.split(";")
             if "=" in part and part.split("=", 1)[0].strip() == name]
    if len(found) != 1 or not found[0] or len(found[0]) > 200:
        return None
    return found[0]


class App:
    SHARE_API_PER_MINUTE = 120  # public stats pages: API calls per client and minute

    def __init__(self, holder: ConfigHolder, auth: bool = True) -> None:
        self.holder = holder
        self.collector = Collector(holder)
        self.require_auth = auth
        self.throttle = Throttle()  # failed sign-ins and share tokens per client (also kept in web.db)
        self._auth: Optional[Authenticator] = None
        self._auth_users: Optional[Dict[str, str]] = None
        self._auth_dir: Optional[str] = None
        self._share_hits: Dict[str, int] = {}  # calls per client in the current minute
        self._share_minute = 0
        self._share_lock = threading.Lock()

    def shared_throttle(self, cfg: Config) -> SharedThrottle:
        return SharedThrottle(self.throttle, cfg.data_dir)

    def authenticator(self, cfg: Config) -> Authenticator:
        users = cfg.dashboard["users"]
        if self._auth is None or users != self._auth_users or cfg.data_dir != self._auth_dir:
            self._auth = Authenticator(users, self.shared_throttle(cfg))
            self._auth_users = dict(users)
            self._auth_dir = cfg.data_dir
        return self._auth

    def _client(self, cfg: Config, environ: Dict[str, Any]) -> str:
        """The client in throttles: a keyed hash of its address (web.db never holds an address)."""
        return self.collector._client_id(cfg, environ)

    def _share_allowed(self, cfg: Config, environ: Dict[str, Any], client: str) -> bool:
        """Public API calls per client and minute: counted in memory, and in web.db when each request is its own
        process (CGI), where memory forgets everything between requests."""
        if environ.get("wsgi.run_once"):
            from .webdb import WebDB
            try:
                return WebDB(cfg.data_dir).count_call(client, int(time.time() // 60), self.SHARE_API_PER_MINUTE)
            except Exception:
                return True
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
        dashboard = path == "/dashboard" or path.startswith("/dashboard/")
        if dashboard or path.startswith("/share/"):
            # As the collector does: never run with the data (sessions, users, the setup code) where the web server
            # would hand them out.
            exposed = web_root_exposure(self.holder.get(), environ)
            if exposed:
                ERRORS.error("refusing to run: %s (see the .htaccess in docs/deploy-dreamhost.md)", exposed)
                message = ("Tidesight has stopped: its files could be downloaded from the web. Check the .htaccess of its "
                           "folder (docs/deploy-dreamhost.md); the log says more.")
                if path.startswith("/dashboard/api/"):
                    return _json(start_response, "503 Service Unavailable", {"error": message})
                return _respond(start_response, "503 Service Unavailable", [("Content-Type", "text/plain; charset=utf-8")],
                                message.encode("utf-8") + b"\n")
        if dashboard:
            return self.dashboard(environ, start_response, path)
        if path.startswith("/share/"):
            return self.share(environ, start_response, path)
        return self.collector(environ, start_response)

    # -- signing in ---------------------------------------------------------------

    def is_admin(self, cfg: Config, user: str) -> bool:
        return not self.require_auth or cfg.role(user) == "admin"

    def session_user(self, cfg: Config, environ: Dict[str, Any]) -> Optional[str]:
        """The user of a valid session cookie (a session ends when its user's password changes)."""
        from .webdb import WebDB
        token = _cookie(environ, COOKIE)
        if not token:
            return None
        try:
            found = WebDB(cfg.data_dir).session_user(token)
        except Exception:
            return None
        if not found:
            return None
        encoded = cfg.dashboard["users"].get(found["user"])
        if encoded is None or not hmac.compare_digest(found["pw_tag"], password_tag(encoded)):
            return None
        return found["user"]

    def _authenticate(self, cfg: Config, environ: Dict[str, Any],
                      start_response: StartResponse) -> Tuple[Optional[str], str, Optional[List[bytes]]]:
        """(user, how, None) or (None, "", the response refusing the request)."""
        if not self.require_auth:
            return "local", "none", None
        user = self.session_user(cfg, environ)
        if user:
            return user, "session", None
        header = environ.get("HTTP_AUTHORIZATION")
        if header:
            user, status = self.authenticator(cfg).verify(header, self._client(cfg, environ))
            if status == "throttled":
                return None, "", _json(start_response, "429 Too Many Requests",
                                       {"error": "too many attempts: try again in a few minutes"}, [("Retry-After", "60")])
            if user:
                return user, "basic", None
        extra: List[Tuple[str, str]] = []
        if not environ.get("HTTP_X_TIDESIGHT"):  # scripts and curl get the standard challenge; the dashboard its form
            extra.append(("WWW-Authenticate", 'Basic realm="Tidesight", charset="UTF-8"'))
        if _cookie(environ, COOKIE):  # an expired or revoked session: forget the cookie
            extra.append(("Set-Cookie", self._cookie_header(environ, "", 0)))
        return None, "", _json(start_response, "401 Unauthorized", {"error": "sign in required"}, extra)

    def _cookie_header(self, environ: Dict[str, Any], value: str, max_age: Optional[int]) -> str:
        parts = ["%s=%s" % (COOKIE, value), "Path=" + _dashboard_path(environ), "HttpOnly", "SameSite=Strict"]
        if max_age is not None:
            parts.append("Max-Age=%d" % max_age)
        if _https(environ):
            parts.append("Secure")
        return "; ".join(parts)

    def login(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        from .admin import read_body
        if environ.get("REQUEST_METHOD", "GET").upper() != "POST":
            return _json(start_response, "405 Method Not Allowed", {"error": "POST a user name and password"})
        if self.require_auth and not cfg.dashboard["users"]:
            return _json(start_response, "403 Forbidden", {"error": "no dashboard users are configured", "setup": True})
        try:
            body = read_body(environ)
        except ApiError as exc:
            return _json(start_response, _status(exc.status), {"error": exc.message})
        if not isinstance(body, dict):
            return _json(start_response, "400 Bad Request", {"error": "send {user, password}"})
        client = self._client(cfg, environ)
        user, status = self.authenticator(cfg).login(body.get("user", ""), body.get("password", ""), client)
        if status == "throttled":
            return _json(start_response, "429 Too Many Requests",
                         {"error": "Too many attempts from your network. Try again in a few minutes."},
                         [("Retry-After", "60")])
        if not user:
            return _json(start_response, "401 Unauthorized", {"error": "Wrong user name or password."})
        return self._signed_in(cfg, environ, start_response, user, body.get("remember") is True)

    def _signed_in(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse, user: str,
                   remember: bool) -> Iterable[bytes]:
        """Open a session for `user` and answer with its cookie."""
        from .webdb import WebDB
        days = max(1, int(cfg.dashboard.get("session_days") or 30)) if remember else 0.5
        token = WebDB(cfg.data_dir).create_session(user, password_tag(cfg.dashboard["users"][user]), days)
        cookie = self._cookie_header(environ, token, int(days * 86400) if remember else None)
        return _json(start_response, "200 OK", {"user": user, "admin": self.is_admin(cfg, user)},
                     [("Set-Cookie", cookie)])

    @staticmethod
    def _safe_for_passwords(cfg: Config, environ: Dict[str, Any]) -> bool:
        """A password may come over this request: HTTPS, this computer, or a deployment without HTTPS at all."""
        host = (environ.get("HTTP_HOST") or environ.get("SERVER_NAME") or "").lower()
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]", 1)[0] + "]"
        return (_https(environ) or name in LOCAL_HOSTS
                or not str(cfg.server.get("public_url") or "").lower().startswith("https://"))

    def setup(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        """The first user, created in the dashboard: only with the setup code of data_dir/setup-code.txt (only
        someone who can read the server's files has it), and only while there is no user at all."""
        from . import users
        from .admin import read_body
        from .auth import hash_password
        if environ.get("REQUEST_METHOD", "GET").upper() != "POST":
            return _json(start_response, "405 Method Not Allowed", {"error": "POST a setup code, user name and password"})
        if not self.require_auth or cfg.dashboard["users"]:
            return _json(start_response, "409 Conflict", {"error": "Tidesight already has users: sign in."})
        try:
            body = read_body(environ)
        except ApiError as exc:
            return _json(start_response, _status(exc.status), {"error": exc.message})
        if not isinstance(body, dict):
            return _json(start_response, "400 Bad Request", {"error": "send {code, user, password}"})
        client = self._client(cfg, environ)
        throttle = self.shared_throttle(cfg)
        if not throttle.reserve(client):  # counted before the check, like a password
            return _json(start_response, "429 Too Many Requests",
                         {"error": "Too many attempts from your network. Try again in a few minutes."},
                         [("Retry-After", "60")])
        if not users.setup_code_matches(cfg.data_dir, body.get("code")):
            return _json(start_response, "401 Unauthorized",
                         {"error": "Wrong setup code: copy it again from setup-code.txt on the server."})
        throttle.forgive(client)
        name, password = body.get("user"), body.get("password")
        if not isinstance(name, str) or not users.NAME_RE.match(name):
            return _json(start_response, "400 Bad Request", {"error": "A user name has 1 to 64 letters, digits and "
                                                             ". _ @ + - (and starts with a letter or digit)."})
        if not isinstance(password, str) or not users.MIN_PASSWORD <= len(password) <= 1024:
            return _json(start_response, "400 Bad Request",
                         {"error": "Use a password of at least %d characters." % users.MIN_PASSWORD})
        cfg = self.holder.reload()  # the users of every file as they are now, not as this process last saw them
        if cfg.dashboard["users"]:
            return _json(start_response, "409 Conflict", {"error": "Tidesight already has users: sign in."})
        encoded = hash_password(password)

        def first(table: Dict[str, Dict[str, Any]]) -> bool:
            if table:  # another request created a user meanwhile
                return False
            table[name] = {"hash": encoded, "role": "admin", "created_at": int(time.time() * 1000)}
            return True

        try:
            created = users.update(cfg.data_dir, first)
        except (users.UsersError, TimeoutError, OSError) as exc:
            log.error("could not save the first user: %s", exc)
            return _json(start_response, "503 Service Unavailable", {"error": "The user could not be saved: try again."})
        if not created:
            return _json(start_response, "409 Conflict", {"error": "Tidesight already has users: sign in."})
        users.remove_setup_code(cfg.data_dir)
        cfg = self.holder.reload()
        if cfg.dashboard["users"].get(name) != encoded:
            # Not the account just made (the configuration file defines that name too, and wins), or users.json
            # could not be read back: no session for it.
            return _json(start_response, "409 Conflict", {"error": "The user was saved, but cannot sign in this way: "
                                                         "sign in, or choose another name with jobs.py user set."})
        return self._signed_in(cfg, environ, start_response, name, body.get("remember") is not False)

    def _new_password(self, body: Dict[str, Any], key: str = "password") -> Tuple[Optional[str], Optional[str]]:
        """(the password, None) or (None, why it is refused)."""
        from . import users
        password = body.get(key)
        if not isinstance(password, str) or not users.MIN_PASSWORD <= len(password) <= 1024:
            return None, "Use a password of at least %d characters." % users.MIN_PASSWORD
        return password, None

    def _set_password(self, cfg: Config, name: str, encoded: str) -> Optional[Config]:
        """Store a new password hash for a user of users.json and end their sessions; the reloaded configuration,
        or None when the user is gone (or could not be saved)."""
        from . import users
        from .webdb import WebDB

        def put(table: Dict[str, Dict[str, Any]]) -> bool:
            if name not in table:
                return False
            table[name] = dict(table[name], hash=encoded)
            return True

        try:
            if not users.update(cfg.data_dir, put):
                return None
        except (users.UsersError, TimeoutError, OSError) as exc:
            log.error("could not save a password: %s", exc)
            return None
        web = WebDB(cfg.data_dir)
        web.delete_user_sessions(name)  # a new password ends every session (others sign in again)
        web.delete_links(name)  # and every password link made before it
        fresh = self.holder.reload()
        return fresh if fresh.dashboard["users"].get(name) == encoded else None

    def password_link(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        """A one-time link (an invitation, or a password reset): {token} says whose it is; {token, password} sets
        the password, spends the link and signs in. Wrong tokens count like failed sign-ins."""
        from .admin import read_body
        from .auth import hash_password
        from .webdb import WebDB
        if environ.get("REQUEST_METHOD", "GET").upper() != "POST":
            return _json(start_response, "405 Method Not Allowed", {"error": "POST the link's token"})
        try:
            body = read_body(environ)
        except ApiError as exc:
            return _json(start_response, _status(exc.status), {"error": exc.message})
        if not isinstance(body, dict):
            return _json(start_response, "400 Bad Request", {"error": "send {token, password}"})
        client = self._client(cfg, environ)
        throttle = self.shared_throttle(cfg)
        if not throttle.reserve(client):
            return _json(start_response, "429 Too Many Requests",
                         {"error": "Too many attempts from your network. Try again in a few minutes."},
                         [("Retry-After", "60")])
        web = WebDB(cfg.data_dir)
        token = body.get("token")
        found = web.link(token)
        gone = {"error": "This link does not work any more (used, replaced or expired): ask an admin for a new one."}
        if found is None or found["user"] in cfg.file_users:
            return _json(start_response, "401 Unauthorized", gone)
        throttle.forgive(client)
        name = found["user"]
        if "password" not in body:
            return _json(start_response, "200 OK", {"user": name, "kind": found["kind"]})
        password, problem = self._new_password(body)
        if problem:
            return _json(start_response, "400 Bad Request", {"error": problem})
        encoded = hash_password(password)
        if web.link(token, use=True) is None:  # spent by a request a moment ago
            return _json(start_response, "401 Unauthorized", gone)
        fresh = self._set_password(cfg, name, encoded)
        if fresh is None:
            return _json(start_response, "410 Gone", {"error": "This user does not exist any more."})
        return self._signed_in(fresh, environ, start_response, name, body.get("remember") is not False)

    def change_password(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse,
                        user: str) -> Iterable[bytes]:
        """A signed-in user changes their own password (the current one is asked again): their other sessions end,
        this one goes on."""
        from .admin import read_body
        from .auth import hash_password
        from .webdb import WebDB
        if environ.get("REQUEST_METHOD", "GET").upper() != "POST":
            return _json(start_response, "405 Method Not Allowed", {"error": "POST {current, password}"})
        if not self.require_auth or user in cfg.file_users:
            return _json(start_response, "409 Conflict",
                         {"error": "This password is in the configuration file ([dashboard] users): change it there."})
        try:
            body = read_body(environ)
        except ApiError as exc:
            return _json(start_response, _status(exc.status), {"error": exc.message})
        if not isinstance(body, dict):
            return _json(start_response, "400 Bad Request", {"error": "send {current, password}"})
        checked, status = self.authenticator(cfg).login(user, body.get("current", ""), self._client(cfg, environ))
        if status == "throttled":
            return _json(start_response, "429 Too Many Requests",
                         {"error": "Too many attempts from your network. Try again in a few minutes."},
                         [("Retry-After", "60")])
        if checked != user:  # 403, not 401: this session is still good
            return _json(start_response, "403 Forbidden", {"error": "The current password is not right."})
        password, problem = self._new_password(body)
        if problem:
            return _json(start_response, "400 Bad Request", {"error": problem})
        session = WebDB(cfg.data_dir).session_user(_cookie(environ, COOKIE)) or {}
        remember = int(session.get("expires") or 0) - int(time.time() * 1000) > 86400000
        fresh = self._set_password(cfg, user, hash_password(password))
        if fresh is None:
            return _json(start_response, "503 Service Unavailable", {"error": "The password could not be saved: try again."})
        return self._signed_in(fresh, environ, start_response, user, remember)

    def logout(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        from .webdb import WebDB
        if environ.get("REQUEST_METHOD", "GET").upper() != "POST":
            return _json(start_response, "405 Method Not Allowed", {"error": "POST to sign out"})
        token = _cookie(environ, COOKIE)
        if token:
            try:
                WebDB(cfg.data_dir).delete_session(token)
            except Exception:
                pass
        return _json(start_response, "200 OK", {"signed_out": True},
                     [("Set-Cookie", self._cookie_header(environ, "", 0))])

    @staticmethod
    def _write_problem(cfg: Config, environ: Dict[str, Any]) -> Optional[str]:
        """Why a request that changes something is refused (forged from another site), or None."""
        if not environ.get("HTTP_X_TIDESIGHT"):
            return "requests that change something need the X-Tidesight header"
        fetch_site = environ.get("HTTP_SEC_FETCH_SITE")
        if fetch_site and fetch_site not in ("same-origin", "none"):
            return "cross-site request"
        origin = environ.get("HTTP_ORIGIN")
        if origin:
            try:
                host = urlsplit(origin).netloc.lower()
            except ValueError:
                return "bad Origin"
            allowed = {(environ.get("HTTP_HOST") or "").lower(), (environ.get("HTTP_X_FORWARDED_HOST") or "").split(",")[0].strip().lower()}
            public = cfg.server.get("public_url") or ""
            if public:
                try:
                    allowed.add(urlsplit(public).netloc.lower())
                except ValueError:
                    pass
            if origin == "null" or host not in allowed - {""}:
                return "cross-origin request"
        return None

    # -- dashboard -----------------------------------------------------------

    def dashboard(self, environ: Dict[str, Any], start_response: StartResponse, path: str) -> Iterable[bytes]:
        cfg = self.holder.get()
        if not self.require_auth:  # --no-auth (dev server): only for this computer, not for a rebinding DNS name
            host = (environ.get("HTTP_HOST") or environ.get("SERVER_NAME") or "").lower()
            name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]", 1)[0] + "]"
            if name not in LOCAL_HOSTS:
                return _respond(start_response, "403 Forbidden", [("Content-Type", "text/plain")],
                                b"The dashboard without sign-in only answers on localhost.\n")
        if path == "/dashboard":
            return _respond(start_response, "301 Moved Permanently", [("Location", "dashboard/")])
        if path.startswith("/dashboard/api/"):
            return self.dashboard_api(cfg, environ, start_response, path[len("/dashboard/api/"):])
        # The page and its scripts hold no data: they load signed out too (the sign-in form, or the first account's
        # form while there is no user). Only a signed-in
        # page gets the CSP that lists the sites' origins (replays and heatmaps load their assets).
        rel = path[len("/dashboard/"):]
        parts = [s for s in rel.split("/") if s]
        own = (not parts or parts[0] in DASHBOARD_DIRS  # its scripts and vendored files
               or (len(parts) == 1 and ("." not in parts[0] or parts[0] in DASHBOARD_FILES)))  # a page, a route
        if not own:  # package.json, node_modules and the like are not served
            return _respond(start_response, "404 Not Found", [("Content-Type", "text/plain")], b"not found\n")
        signed_out = False
        if os.path.splitext(rel)[1].lower() in ("", ".html"):
            signed_out = self.require_auth and self.session_user(cfg, environ) is None
        return self.static(cfg, environ, start_response, rel, share=signed_out)

    def dashboard_api(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse,
                      endpoint: str) -> Iterable[bytes]:
        method = environ.get("REQUEST_METHOD", "GET").upper()
        # Only the dashboard's own pages (or a typed address, or a script) may call it: not another site, nor a
        # sibling subdomain, which SameSite cookies do not keep out.
        if environ.get("HTTP_SEC_FETCH_SITE", "same-origin") not in ("same-origin", "none"):
            return _json(start_response, "403 Forbidden", {"error": "cross-site request"})
        cfg = self.holder.fresh_users()  # roles, passwords and people as they are now, in every process
        if method in WRITE_METHODS:
            problem = self._write_problem(cfg, environ)
            if problem:
                return _json(start_response, "403 Forbidden", {"error": problem})
        elif method not in ("GET", "HEAD"):
            return _json(start_response, "405 Method Not Allowed", {"error": "unsupported method"})
        if endpoint in PASSWORD_ENDPOINTS and not self._safe_for_passwords(cfg, environ):
            return _json(start_response, "403 Forbidden", {"error": "Passwords travel over HTTPS only: open the dashboard "
                                                           "with https://"})
        if endpoint == "login":
            return self.login(cfg, environ, start_response)
        if endpoint == "logout":
            return self.logout(cfg, environ, start_response)
        if endpoint == "setup":
            return self.setup(cfg, environ, start_response)
        if endpoint == "password_link":
            return self.password_link(cfg, environ, start_response)
        if self.require_auth and not cfg.dashboard["users"]:
            # No user yet: the page offers to create the first one, with the setup code written on the server.
            from . import users
            if users.setup_code(cfg.data_dir) is None:
                log.error("could not write the setup code in %s", cfg.data_dir)
            if endpoint == "me":  # where the code is, as its owner types it (~/…: the account's name stays private)
                return _json(start_response, "200 OK", {"user": None, "setup": True,
                                                        "code_file": _home_path(users.setup_path(cfg.data_dir))})
            return _json(start_response, "403 Forbidden", {"error": "Tidesight has no user yet: create the first one "
                                                           "in the dashboard, with the setup code.", "setup": True})
        user, how, denied = self._authenticate(cfg, environ, start_response)
        if denied is not None:
            return denied
        assert user is not None
        params = {k: v[0] for k, v in parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).items()}
        if endpoint == "me":
            return _json(start_response, "200 OK", {
                "user": user, "admin": self.is_admin(cfg, user), "auth": how, "can_sign_out": how == "session",
                "can_change_password": how == "session" and user not in cfg.file_users,
                "public_url": cfg.server["public_url"] or None})
        if endpoint == "password":
            return self.change_password(cfg, environ, start_response, user)
        if endpoint in ADMIN_READS or method in WRITE_METHODS:
            return self.admin(cfg, environ, start_response, method, endpoint, params, user)
        if endpoint == "export":
            return self.export(cfg, start_response, params)
        return self.api(cfg, environ, start_response, endpoint, None, params=params)

    def admin(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse, method: str, endpoint: str,
              params: Dict[str, str], user: str) -> Iterable[bytes]:
        from .admin import Admin, read_body
        try:
            body = read_body(environ) if method in WRITE_METHODS else None
            data = Admin(self.holder, user, self.is_admin(cfg, user), signed_in=self.require_auth).call(method, endpoint, params, body)
        except ApiError as exc:
            return _json(start_response, _status(exc.status), {"error": exc.message})
        except Exception:
            ERRORS.exception("dashboard change failed on %s %s", method, endpoint)
            return _json(start_response, "500 Internal Server Error", {"error": "internal error"})
        return _json(start_response, "201 Created" if method == "POST" else "200 OK", data)

    def share(self, environ: Dict[str, Any], start_response: StartResponse, path: str) -> Iterable[bytes]:
        cfg = self.holder.get()
        parts = path.split("/")  # ['', 'share', site, ...]
        site = cfg.site(parts[2]) if len(parts) > 2 else None
        if site is None or not site.settings["public"]:
            return _respond(start_response, "404 Not Found", [("Content-Type", "text/plain")], b"not found\n")
        client = self._client(cfg, environ)
        rest = "/".join(parts[3:])
        if rest.startswith("api/"):
            if environ.get("REQUEST_METHOD", "GET").upper() not in ("GET", "HEAD"):
                return _json(start_response, "405 Method Not Allowed", {"error": "read-only API"})
            # Only the data needs the token: the page and its assets hold none, and load without it.
            token = site.settings.get("share_token")
            if token:
                given = environ.get("HTTP_X_SHARE_TOKEN", "") or \
                    parse_qs(environ.get("QUERY_STRING", "")).get("t", [""])[0]
                throttle = self.shared_throttle(cfg)
                if throttle.blocked("share:" + client):
                    return _json(start_response, "429 Too Many Requests", {"error": "too many attempts"},
                                 [("Retry-After", "60")])
                if not hmac.compare_digest(given.encode("utf-8"), token.encode("utf-8")):
                    if given:
                        throttle.failed("share:" + client)
                    return _json(start_response, "403 Forbidden", {"error": "a valid share token is needed"})
            if not self._share_allowed(cfg, environ, client):
                return _json(start_response, "429 Too Many Requests", {"error": "too many requests"},
                             [("Retry-After", "60")])
            return self.api(cfg, environ, start_response, rest[4:], site.id)
        if rest == "" and not path.endswith("/"):
            return _respond(start_response, "301 Moved Permanently", [("Location", parts[2] + "/")])
        first = rest.split("/", 1)[0]
        if rest and first not in SHARE_FILES and first not in SHARE_DIRS:  # the page's own files, nothing else
            return _respond(start_response, "404 Not Found", [("Content-Type", "text/plain")], b"not found\n")
        return self.static(cfg, environ, start_response, rest, share=True)

    def api(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse, endpoint: str,
            public_site: Optional[str], params: Optional[Dict[str, str]] = None) -> Iterable[bytes]:
        if environ.get("REQUEST_METHOD", "GET").upper() not in ("GET", "HEAD"):
            return _json(start_response, "405 Method Not Allowed", {"error": "read-only API"})
        if params is None:
            params = {k: v[0] for k, v in parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).items()}
        fmt = params.get("format", "")
        if fmt and (public_site is not None or endpoint not in REPORT_EXPORTS):
            fmt = ""  # file downloads are for the private dashboard's reports
        try:
            api = Api(cfg)
            data = api.call(endpoint, params, public_site)
        except ApiError as exc:
            return _json(start_response, _status(exc.status), {"error": exc.message})
        except Exception:
            ERRORS.exception("API error on %s", endpoint)
            return _json(start_response, "500 Internal Server Error", {"error": "internal error"})
        if fmt:
            return self.report_file(cfg, start_response, endpoint, params, data, fmt)
        return _json(start_response, "200 OK", data)

    def report_file(self, cfg: Config, start_response: StartResponse, endpoint: str, params: Dict[str, str],
                    data: Dict[str, Any], fmt: str) -> Iterable[bytes]:
        from . import tabular
        from .query import DIM_LABELS, parse_filters
        if fmt not in tabular.FORMATS:
            return _json(start_response, "400 Bad Request", {"error": "format is csv, tsv, json or xlsx"})
        site = cfg.site(params.get("site"))
        if site is None:
            return _json(start_response, "404 Not Found", {"error": "unknown site"})
        cols, rows = tabular.TABLES[endpoint](data)
        if endpoint == "breakdown":
            name = data["dim"] + ("-" + data["dim2"] if data.get("dim2") else "")
            what = DIM_LABELS.get(data["dim"], data["dim"]) + (" × " + DIM_LABELS.get(data["dim2"], data["dim2"])
                                                               if data.get("dim2") else "")
        elif endpoint == "overview":
            name, what = "trend", "Trend (%s)" % data.get("interval", "day")
        else:
            name, what = "goals", "Goals"
        try:
            filters = parse_filters(params, site)
        except ApiError:
            filters = []
        meta = tabular.describe(site.display_name, site.id, params, data, filters, what)
        body = tabular.render(fmt, cols, rows, meta, what)
        fname = tabular.filename(site.id, name, data.get("from", ""), data.get("to", ""), fmt)
        ctype = tabular.FORMATS[fmt] + ("; charset=utf-8" if fmt != "xlsx" else "")
        return _respond(start_response, "200 OK", [("Content-Type", ctype),
                                                   ("Content-Disposition", 'attachment; filename="%s"' % fname),
                                                   ("Cache-Control", "no-store")] + SECURITY_HEADERS, body)

    def export(self, cfg: Config, start_response: StartResponse, params: Dict[str, str]) -> Iterable[bytes]:
        from datetime import datetime

        from .maintenance import EXPORTS, iter_export
        what, fmt = params.get("what", "sessions"), params.get("format", "csv")

        def day_ok(value: Optional[str]) -> bool:
            if not value:
                return True
            try:
                return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)) and datetime.strptime(value, "%Y-%m-%d") is not None
            except ValueError:
                return False
        days_ok = day_ok(params.get("from")) and day_ok(params.get("to"))
        if what not in EXPORTS or fmt not in ("csv", "json", "jsonl") or cfg.site(params.get("site")) is None or not days_ok:
            return _json(start_response, "400 Bad Request", {"error": "bad export parameters"})
        try:
            cols, rows = iter_export(cfg, params["site"], what, params.get("from"), params.get("to"))
        except Exception:
            ERRORS.exception("export of %s failed", what)
            return _json(start_response, "500 Internal Server Error", {"error": "the export failed"})
        ctype = {"csv": "text/csv", "json": "application/json", "jsonl": "application/x-ndjson"}[fmt]
        name = "tidesight-%s-%s%s.%s" % (params["site"], what,
                                         "-%s_%s" % (params["from"], params["to"]) if params.get("from") and params.get("to") else "",
                                         fmt)
        start_response("200 OK", [("Content-Type", ctype + "; charset=utf-8"),
                                  ("Content-Disposition", 'attachment; filename="%s"' % name),
                                  ("Cache-Control", "no-store")] + SECURITY_HEADERS)
        return _stream_export(cols, rows, fmt)

    def static(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse, rel: str,
               share: bool = False) -> Iterable[bytes]:
        root = cfg.server["dashboard_dir"] or os.path.join(_REPO_ROOT, "dashboard")
        segments = [s for s in rel.split("/") if s]
        if not all(_SAFE_SEGMENT.fullmatch(s) for s in segments):
            return _respond(start_response, "404 Not Found", [("Content-Type", "text/plain")], b"not found\n")
        file_path = os.path.join(root, *segments) if segments else os.path.join(root, "index.html")
        ext = os.path.splitext(file_path)[1].lower()
        if not os.path.isfile(file_path):
            if ext:  # a missing asset
                return _respond(start_response, "404 Not Found", [("Content-Type", "text/plain")], b"not found\n")
            file_path, ext = os.path.join(root, "index.html"), ".html"  # client-side route
        if ext not in _STATIC_TYPES or not os.path.isfile(file_path):
            return _respond(start_response, "404 Not Found", [("Content-Type", "text/plain")], b"not found\n")
        # Scripts and styles are revalidated on every load (an update never mixes old and new modules); unchanged
        # files answer 304.
        st = os.stat(file_path)
        etag = '"%x-%x%s"' % (st.st_mtime_ns, st.st_size, "-s" if share else "")
        cache = "no-cache" if ext in (".html", ".js", ".css") else "public, max-age=300"
        headers = [("Content-Type", _STATIC_TYPES[ext]), ("Cache-Control", cache), ("ETag", etag)] + SECURITY_HEADERS
        if ext == ".html":
            headers.append(("Content-Security-Policy", SHARE_CSP if share else content_security_policy(cfg)))
            headers.append(("Vary", "Cookie"))
            if _https(environ):  # browsers then never ask this host over plain HTTP again
                headers.append(("Strict-Transport-Security", "max-age=31536000"))
        else:
            headers.append(("Content-Security-Policy", ASSET_CSP))
        if environ.get("HTTP_IF_NONE_MATCH") == etag and ext != ".html":
            start_response("304 Not Modified", [h for h in headers if h[0] != "Content-Type"])
            return [b""]
        with open(file_path, "rb") as fh:
            body = fh.read()
        return _respond(start_response, "200 OK", headers, body,
                        head=environ.get("REQUEST_METHOD", "GET").upper() == "HEAD")


# An origin for the CSP: a host name, or a wildcard over the subdomains of a name with at least two labels
# ("https://*.example.com"; never "https://*" or "https://*.com", which would allow almost anything).
_CSP_SOURCE = re.compile(r"https?://(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)+(:[0-9]{1,5})?|https?://localhost(:[0-9]{1,5})?")

# Public share pages and the signed-out dashboard show charts, tables and a form only (no replays, no framed
# pages): no foreign origin at all, and the configured sites (some private) are not listed.
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
        if _CSP_SOURCE.fullmatch(o) and o not in sources:
            sources.append(o)
    extra = (" " + " ".join(sources)) if sources else ""
    return ("default-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
            "form-action 'none'; img-src 'self' data: blob:%s; media-src 'self' data: blob:%s; "
            "style-src 'self' 'unsafe-inline'%s; font-src 'self' data:%s; frame-src 'self' blob:%s; "
            "frame-ancestors 'self'" % (extra, extra, extra, extra, extra))


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


_REASONS = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
            409: "Conflict", 413: "Payload Too Large", 415: "Unsupported Media Type", 429: "Too Many Requests",
            503: "Service Unavailable"}

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
