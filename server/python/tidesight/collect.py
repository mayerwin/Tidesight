"""The Tidesight collector: a WSGI app for the ingest endpoints (standard library only).

Endpoints (docs/protocol.md):
    POST /collect      batch of events (JSON envelope, optionally gzip)
    POST /chunk        heavy data chunk (replay, mouse movement), stored unparsed
    GET  /p.gif        pixel hit
    GET  /config.js    site configuration for the tracker (also /config.json)
    GET  /health       liveness and storage check
    GET  /ts.js, /ts/* tracker files, when no web server serves them

The request path validates, bounds and appends. It never enriches, never
calls the network, and stores malformed or unknown-site hits flagged
instead of dropping them.
"""

from __future__ import annotations

import fnmatch
import gzip
import ipaddress
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import sys
import time
import zlib
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from . import __version__, webcheck
from .anonymize import anonymize_headers, anonymize_ip, hash_ip
from .config import Config, ConfigHolder, SiteConfig
from .enrich.ip import in_networks, networks
from .errorlog import ERRORS, ErrorLog  # noqa: F401 (ErrorLog re-exported)
from .redact import patterns, redact_url
from .storage import (RATE_LIMITED, Budget, ChunkRecord, HitRecord, Limits, RequestRecord, StorageError,
                      is_flagged, open_store)

log = logging.getLogger("tidesight.collect")

StartResponse = Callable[..., Any]

GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,"
       b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")

ACCEPT_CH = ("Sec-CH-UA, Sec-CH-UA-Mobile, Sec-CH-UA-Platform, Sec-CH-UA-Platform-Version, Sec-CH-UA-Model, "
             "Sec-CH-UA-Full-Version-List, Sec-CH-UA-Arch, Sec-CH-UA-Bitness, Sec-CH-UA-Form-Factors, "
             "Sec-CH-UA-WoW64, Viewport-Width, DPR, Device-Memory, RTT, Downlink, ECT, Sec-CH-Prefers-Color-Scheme, "
             "Sec-CH-Prefers-Reduced-Motion")

CHUNK_KINDS = ("replay", "mouse", "snapshot")
CHUNK_FEATURES = {"replay": "replay", "mouse": "mouse_moves", "snapshot": "dom_snapshots"}
DISK_CHECK_SECONDS = 10
MAX_HEADER_VALUE = 8192
MAX_KEPT_COOKIES = 20
REDACTED = "[redacted]"
ALWAYS_SECRET = ("authorization", "proxy-authorization")
SERVER_ENV_KEYS = ("SERVER_PROTOCOL", "REQUEST_SCHEME", "HTTPS", "SSL_PROTOCOL", "SSL_CIPHER", "HTTP2",
                   "H2PUSH", "SERVER_NAME", "SERVER_PORT", "REMOTE_PORT", "SERVER_SOFTWARE")

# \Z rather than $, which also matches before a trailing newline.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}\Z")
_ORIGIN_RE = re.compile(r"^(null|[A-Za-z][A-Za-z0-9+.-]*://[^\s/?#,]+)\Z")
_NONCE_RE = re.compile(r"[0-9a-f]{16,64}")  # with fullmatch
_STATIC_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_DOMAIN_RE = re.compile(r"^\.?[A-Za-z0-9.-]+\Z")
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_SURROGATE_ESCAPE_RE = re.compile(r"\\u[dD][89a-fA-F]")
_V4_PORT_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):\d{1,5}\Z")
_V6_PORT_RE = re.compile(r"^\[([0-9A-Fa-f:.]+)\](?::\d{1,5})?\Z")
HIT_COST = 256              # bytes charged per stored hit (a row costs about that much, however small its JSON)
MAX_JSON_DEPTH = 64         # objects and arrays nested deeper than this: an unreadable body (bad_json)
MAX_DECLARED_LENGTH = 2 ** 53 - 1  # larger Content-Length values are recorded as this
_STATIC_TYPES = {".js": "application/javascript; charset=utf-8", ".map": "application/json; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
                 ".txt": "text/plain; charset=utf-8"}

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))




def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _str(value: Any, max_len: int) -> Optional[str]:
    if isinstance(value, str):
        return value[:max_len] if value else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)[:max_len]
    return None


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if -2 ** 63 <= value < 2 ** 63 else None
    if isinstance(value, float) and value == value and abs(value) < 9.2e18:
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        try:
            return _int(int(value.strip()))
        except ValueError:
            return None
    return None


def web_root_exposure(cfg: Config, environ: Dict[str, Any]) -> Optional[str]:
    """Why the app must refuse to work: its data, configuration or log file lies inside the web server's folder and
    can be downloaded from the web (webcheck.py: protected there by .htaccess, and checked by jobs.py cron)."""
    return webcheck.refusal(cfg, environ)


def _reject_constant(name: str) -> Any:
    raise ValueError("invalid JSON constant %s" % name)


def _finite_float(text: str) -> float:
    """A JSON number as a float; one too large for a float (1e999) makes the body unreadable, as NaN does: it
    would be stored as Infinity, which is not JSON (SQLite's JSON functions reject the whole row)."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("number out of range")
    return value


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _replace_surrogates(value: Any) -> Any:
    """Lone UTF-16 surrogates become U+FFFD in every string, keys included.

    JSON can carry them as \\uD83D escapes (a string cut in the middle of an emoji) but UTF-8, and so
    SQLite, cannot: without this the whole request would fail to store.
    """
    if isinstance(value, str):
        return _SURROGATE_RE.sub("\N{REPLACEMENT CHARACTER}", value)
    if isinstance(value, dict):
        return {_replace_surrogates(k): _replace_surrogates(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace_surrogates(v) for v in value]
    return value


def _deeper_than(obj: Any, limit: int) -> bool:
    """True when objects and arrays nest more than `limit` levels (the top level is 1). Iterative."""
    stack = [(obj, 1)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, (dict, list)):
            if depth > limit:
                return True
            stack.extend((v, depth + 1) for v in (value.values() if isinstance(value, dict) else value))
    return False


def _address(value: str) -> Optional[Any]:
    """The IP address in a value, ignoring a port (`1.2.3.4:5678`, `[2001:db8::1]:443`); None if it is not one."""
    v = value.strip()
    m = _V6_PORT_RE.match(v) or _V4_PORT_RE.match(v)
    try:
        return ipaddress.ip_address(m.group(1) if m else v)
    except ValueError:
        return None


def client_key(value: Optional[str], ipv6_prefix: int = 64) -> Optional[str]:
    """The rate-limit identity of a client: its IPv4 address, or its IPv6 network (one host can use a whole
    /64, so per-address limits would not hold); a port (`1.2.3.4:5678`, `[2001:db8::1]:443`) is ignored.
    Anything else (a custom key header) is used as it is."""
    if not value:
        return None
    addr = _address(value)
    if addr is None:
        return value[:64]
    if addr.version == 6:
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        prefix = max(16, min(128, int(ipv6_prefix)))
        return str(ipaddress.IPv6Network((int(addr), prefix), strict=False))
    return str(addr)


def _kb(n: int) -> int:
    """Kilobytes, rounded up (a request of 1 byte uses 1 KB of a budget)."""
    return max(1, -(-int(n) // 1024))


def header_bytes(headers: Dict[str, str], cookies: Optional[Dict[str, Any]]) -> int:
    """What a request's stored headers and kept cookies weigh (names and values), charged to its client."""
    n = sum(len(k) + len(v) for k, v in headers.items())
    if cookies:
        n += sum(len(k) + len(v) for k, v in (cookies.get("kept") or {}).items())
    return n


def origin_of(url: Optional[str]) -> Optional[str]:
    """scheme://host[:port] of a URL, or None."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return "%s://%s" % (parts.scheme.lower(), parts.netloc.lower())


def parse_cookie_header(value: str) -> List[Tuple[str, str]]:
    out = []
    for part in value.split(";"):
        if "=" in part:
            name, val = part.split("=", 1)
            out.append((name.strip(), val.strip()))
        elif part.strip():
            out.append((part.strip(), ""))
    return out


def extract_headers(environ: Dict[str, Any], cfg: Config) -> Tuple[Dict[str, str], Optional[Dict[str, Any]]]:
    """All request headers (lower-case names), with secrets redacted, plus Tidesight's own cookies."""
    hcfg = cfg.headers
    secret_patterns = [p.lower() for p in hcfg["secret_patterns"]]
    drop = {d.lower() for d in hcfg["drop"]}
    cookie_patterns = hcfg["cookie_names"]
    headers: Dict[str, str] = {}
    cookies: Optional[Dict[str, Any]] = None
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            name = key[5:].replace("_", "-").lower()
        elif key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            if not value:
                continue
            name = key.replace("_", "-").lower()
        else:
            continue
        if not isinstance(value, str) or name in drop:
            continue
        if name == "cookie":
            kept: Dict[str, str] = {}
            others = 0
            for cname, cval in parse_cookie_header(value):
                # The dashboard's sign-in cookie (ts_login, wsgi.py) is never kept, whatever cookie_names says.
                if cname != "ts_login" and len(kept) < MAX_KEPT_COOKIES and \
                        any(fnmatch.fnmatchcase(cname, p) for p in cookie_patterns):
                    # A comma is not a cookie character: past it is another Cookie header the server merged in.
                    kept[cname] = cval.split(",", 1)[0].rstrip()[:512]
                else:
                    others += 1
            cookies = {"kept": kept, "others": others}
            continue
        if name in ALWAYS_SECRET or any(fnmatch.fnmatchcase(name, p) for p in secret_patterns):
            headers[name] = REDACTED
            continue
        headers[name] = value[:MAX_HEADER_VALUE]
    return headers, cookies


def server_info(environ: Dict[str, Any]) -> Dict[str, Any]:
    info: Dict[str, Any] = {"connector": "python", "version": __version__,
                            "scheme": environ.get("wsgi.url_scheme"),
                            "path": (environ.get("SCRIPT_NAME", "") + environ.get("PATH_INFO", ""))[:512]}
    for key in SERVER_ENV_KEYS:
        value = environ.get(key)
        if value:
            info[key.lower()] = str(value)[:200]
    return info


def read_body(environ: Dict[str, Any], limit: int) -> Tuple[Optional[bytes], int]:
    """Read at most `limit` bytes. Returns (body or None when too large, declared/observed size)."""
    try:
        length = max(0, min(MAX_DECLARED_LENGTH, int(environ.get("CONTENT_LENGTH") or 0)))
    except ValueError:
        length = 0
    stream = environ.get("wsgi.input")
    if stream is None:
        return b"", 0
    if length > limit:
        return None, length
    if length > 0:
        body = stream.read(length)
        return body, len(body)
    if environ.get("wsgi.input_terminated"):
        body = stream.read(limit + 1)
        if len(body) > limit:
            return None, len(body)
        return body, len(body)
    return b"", 0


def gunzip_bounded(body: bytes, max_out: int) -> bytes:
    """Decompress gzip data, refusing to produce more than max_out bytes."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(body, max_out + 1)
    if len(out) > max_out or d.unconsumed_tail:
        raise ValueError("decompressed body exceeds %d bytes" % max_out)
    if not d.eof:
        raise ValueError("truncated gzip data")
    return out


# ---------------------------------------------------------------------------
# The application
# ---------------------------------------------------------------------------

class Collector:
    """WSGI callable for the ingest endpoints."""

    def __init__(self, holder: ConfigHolder, store: Any = None) -> None:
        self.holder = holder
        self._store = store
        self._store_key: Optional[Tuple] = None
        self._logging_configured = False
        self._disk_checked = 0.0
        self._disk_free: Optional[int] = None
        self._disk_warned = 0.0

    # -- infrastructure --------------------------------------------------

    def store(self, cfg: Config) -> Any:
        s = cfg.server
        key = (cfg.data_dir, s["storage_mode"], s["journal_mode"], s["synchronous"], s["busy_timeout_ms"],
               s["spool_fallback"])
        if self._store is None or (self._store_key is not None and key != self._store_key):
            if self._store is not None:
                self._store.close()
            self._store = open_store(cfg.data_dir, s["storage_mode"], s["journal_mode"], s["synchronous"],
                                     s["busy_timeout_ms"], s["spool_fallback"])
            self._store_key = key
        elif self._store_key is None:
            self._store_key = key
        return self._store

    def _configure_logging(self, cfg: Config) -> None:
        if self._logging_configured:
            return
        self._logging_configured = True
        root = logging.getLogger("tidesight")
        if root.handlers:
            return
        path = cfg.server["log_file"]
        handler: logging.Handler
        try:
            handler = logging.FileHandler(path) if path else logging.StreamHandler(sys.stderr)
        except OSError:
            handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        root.addHandler(handler)
        root.setLevel(logging.INFO)

    def __call__(self, environ: Dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        try:
            cfg = self.holder.get()
            self._configure_logging(cfg)
        except Exception:
            ERRORS.exception("configuration unavailable")
            return _respond(start_response, "503 Service Unavailable", [("Content-Type", "text/plain")],
                            b"configuration error\n")
        path = environ.get("PATH_INFO") or "/"
        method = environ.get("REQUEST_METHOD", "GET").upper()
        exposed = web_root_exposure(cfg, environ)
        if exposed:
            ERRORS.error("refusing to run: %s (see the .htaccess in docs/deploy-dreamhost.md)", exposed)
            return _respond(start_response, "503 Service Unavailable", [("Content-Type", "text/plain")],
                            b"configuration error\n")
        try:
            if path == "/collect":
                if method == "POST":
                    return self.handle_collect(cfg, environ, start_response)
                if method == "OPTIONS":
                    return self.handle_preflight(cfg, environ, start_response)
                return _method_not_allowed(start_response, "POST, OPTIONS")
            if path == "/chunk":
                if method == "POST":
                    return self.handle_chunk(cfg, environ, start_response)
                if method == "OPTIONS":
                    return self.handle_preflight(cfg, environ, start_response)
                return _method_not_allowed(start_response, "POST, OPTIONS")
            if path == "/p.gif":
                return self.handle_pixel(cfg, environ, start_response)
            if path in ("/config.js", "/config.json"):
                return self.handle_config(cfg, environ, start_response, as_json=path.endswith(".json"))
            if path == "/health":
                return self.handle_health(cfg, environ, start_response)
            if path == "/" and method in ("GET", "HEAD"):
                return _respond(start_response, "200 OK", [("Content-Type", "text/plain; charset=utf-8")],
                                b"Tidesight\n", head=method == "HEAD")
            if method in ("GET", "HEAD") and (path.startswith("/ts") or path == "/robots.txt"):
                return self.handle_static(cfg, environ, start_response, path)
        except Exception:
            ERRORS.exception("unhandled error on %s %s", method, path)
            if path == "/p.gif":
                return _respond(start_response, "200 OK", _gif_headers(), GIF)
            return _respond(start_response, "500 Internal Server Error", [("Content-Type", "text/plain")],
                            b"error\n")
        return _respond(start_response, "404 Not Found", [("Content-Type", "text/plain")], b"not found\n")

    # -- shared pieces ---------------------------------------------------

    def _cors(self, cfg: Config, environ: Dict[str, Any], site: Optional[SiteConfig]) -> List[Tuple[str, str]]:
        origin = environ.get("HTTP_ORIGIN")
        if not origin or not _ORIGIN_RE.match(origin):
            return []
        if cfg.server["cors"] == "allowed" and (site is None or not site.origin_allowed(origin)):
            return [("Vary", "Origin")]
        return [("Access-Control-Allow-Origin", origin), ("Access-Control-Allow-Credentials", "true"),
                ("Vary", "Origin")]

    def _base_headers(self, cfg: Config) -> List[Tuple[str, str]]:
        headers = [("Cache-Control", "no-store"), ("X-Robots-Tag", "noindex, nofollow")]
        if cfg.server["accept_ch"]:
            headers.append(("Accept-CH", ACCEPT_CH))
        return headers

    def _site_status(self, cfg: Config, site_id: Optional[str], environ: Dict[str, Any],
                     require_origin: bool) -> Tuple[str, Optional[SiteConfig]]:
        if not site_id:
            return "missing_site", None
        site = cfg.site(site_id)
        if site is None:
            return "unknown_site", None
        origin = environ.get("HTTP_ORIGIN") or origin_of(environ.get("HTTP_REFERER"))
        if origin:
            return ("ok" if site.origin_allowed(origin) else "origin_mismatch"), site
        if site.origins and require_origin:
            return "no_origin", site
        return "ok", site

    def _privacy_drop(self, cfg: Config, site: Optional[SiteConfig], environ: Dict[str, Any]) -> Optional[str]:
        privacy = site.privacy if site else cfg.privacy
        if privacy.get("honour_dnt") and environ.get("HTTP_DNT", "").strip() == "1":
            return "dropped_dnt"
        if privacy.get("honour_gpc") and environ.get("HTTP_SEC_GPC", "").strip() == "1":
            return "dropped_gpc"
        return None

    def _identity(self, cfg: Config, site: Optional[SiteConfig], environ: Dict[str, Any],
                  headers: Dict[str, str]) -> Tuple[Optional[str], Dict[str, str]]:
        """What is kept about the requester: the address (anonymized in privacy mode) and the headers. A Referer
        can hold the full page URL (beacons, the pixel): its secrets are redacted with the site's redact_params
        and redact_paths (redact.py), as the tracker does for the URLs it sends."""
        privacy = site.privacy if site else cfg.privacy
        ip = environ.get("REMOTE_ADDR") or None
        if privacy.get("hash_ip") or privacy.get("truncate_ip"):
            secret = cfg.secret
            ip = anonymize_ip(ip, privacy["hash_ip"], privacy["truncate_ip"], secret)
            headers = anonymize_headers(headers, privacy["hash_ip"], privacy["truncate_ip"], secret)
        if "referer" in headers:
            settings = (site or cfg.site_or_default(None)).settings
            headers["referer"] = redact_url(headers["referer"], patterns(settings["redact_params"]),
                                            patterns(settings["redact_paths"]))
        return ip, headers

    def _rate_key(self, cfg: Config, environ: Dict[str, Any]) -> Optional[str]:
        """The client's identity for the allowances: the connecting address, or the address the configured
        header gives, believed only when the connection comes from a listed proxy (rate_limit_proxies) and the
        value is an IP address: anyone else could invent a new identity per request, or borrow a victim's."""
        remote = environ.get("REMOTE_ADDR") or None
        key_name = cfg.server["rate_limit_key"]
        value = None
        if key_name != "remote_addr" and remote and in_networks(remote, networks(tuple(cfg.rate_limit_proxies()))):
            header = environ.get("HTTP_" + key_name.upper().replace("-", "_"))
            if header:  # the last value: the one the nearest proxy appended (earlier ones are the client's say)
                candidate = header.split(",")[-1].strip()[:64]
                if _address(candidate) is not None:
                    value = candidate
        return client_key(value or remote, cfg.server["rate_limit_ipv6_prefix"])

    def _client_id(self, cfg: Config, environ: Dict[str, Any]) -> str:
        """The client in allowance keys: its key (_rate_key), hashed with the secret as hash_ip does, so the
        rate_limits table (and its backups) never holds an address, whatever the privacy settings."""
        return hash_ip(self._rate_key(cfg, environ) or "-", cfg.secret)

    def _refused_early(self, cfg: Config, environ: Dict[str, Any], site: Optional[SiteConfig], chunk: bool,
                       size: int) -> bool:
        """A client already over its request or kilobyte allowance this minute is refused before its body is
        decompressed, parsed or compressed (the costly part of a request): charged (r: 1, then kb: the body
        received) and counted as any refusal, under the site the query string names if it is configured."""
        s = cfg.server
        client = self._client_id(cfg, environ)
        caps = {"r:" + client: s["chunk_rate_limit_per_minute"] if chunk else s["rate_limit_per_minute"],
                "kb:" + client: s["chunk_kb_per_minute"] if chunk else s["rate_limit_kb_per_minute"]}
        caps = {k: v for k, v in caps.items() if v > 0}
        if not caps or self._disk_low(cfg):  # low disk: the normal path answers 503 without touching the database
            return False
        stream, now = ("chunks" if chunk else "events"), now_ms()
        store = self.store(cfg)
        try:
            used = store.usage(stream, list(caps), now)
            if not any(used.get(k, 0) >= cap for k, cap in caps.items()):
                return False
            limits = self._limits(cfg, environ, site, chunk=chunk, size=size, flagged=False)
            store.refuse(stream, Limits(budgets=limits.budgets, site=limits.site), now)
        except Exception:  # never refuse because the check itself failed
            ERRORS.exception("could not check allowances")
            return False
        return True

    def _limits(self, cfg: Config, environ: Dict[str, Any], site: Optional[SiteConfig], chunk: bool,
                size: int, flagged: bool, hits: int = 0) -> Limits:
        """What this request may consume (docs/configuration.md, "Limits and abuse protection").

        Per client and minute: requests and kilobytes (separate allowances for /chunk); requests that would
        only be stored flagged have their own, smaller allowance, per client and for all clients together;
        a configured site has a daily quota of stored hits."""
        s = cfg.server
        client = self._client_id(cfg, environ)
        # Every key carries a prefix, so no client value (a header can say anything) can alias another row.
        budgets = [
            Budget("r:" + client, 1, s["chunk_rate_limit_per_minute"] if chunk else s["rate_limit_per_minute"],
                   "chunk_rate_limited" if chunk else "rate_limited"),
            Budget("kb:" + client, _kb(size), s["chunk_kb_per_minute"] if chunk else s["rate_limit_kb_per_minute"],
                   "chunk_rate_limited" if chunk else "rate_limited"),
        ]
        if flagged:
            budgets.append(Budget("f:" + client, 1, s["flagged_per_minute"], "flagged_limited"))
            budgets.append(Budget("*f", 1, s["flagged_global_per_minute"], "flagged_limited"))
        quota = 0
        if site is not None and not flagged and not chunk:
            quota = max(0, int(site.settings.get("max_hits_per_day") or 0))
        return Limits(budgets=tuple(budgets), site=site.id if site is not None else "", quota=quota,
                      hits=hits if site is not None and not flagged and not chunk else 0)

    def _disk_low(self, cfg: Config) -> bool:
        """True when the data directory's file system has less free space than min_free_disk_mb."""
        need = int(cfg.server["min_free_disk_mb"] or 0)
        if need <= 0:
            return False
        now = time.monotonic()
        if now - self._disk_checked >= DISK_CHECK_SECONDS or self._disk_checked == 0.0:
            self._disk_checked = now
            path = os.path.abspath(cfg.data_dir or ".")
            while not os.path.exists(path) and os.path.dirname(path) != path:
                path = os.path.dirname(path)
            try:
                self._disk_free = shutil.disk_usage(path).free
            except OSError:
                self._disk_free = None
        low = self._disk_free is not None and self._disk_free < need * 1024 * 1024
        if low and now - self._disk_warned > 60:
            self._disk_warned = now
            log.warning("less than %d MB free in %s: refusing writes (503)", need, cfg.data_dir)
        return low

    def _write(self, cfg: Config, kind: str, rec: Any, limits: Limits) -> str:
        store = self.store(cfg)
        try:
            if kind == "chunk":
                return store.write_chunk(rec, limits)
            return store.write_request(rec, limits)
        except (sqlite3.Error, OSError) as exc:  # no spool fallback configured, or the spool failed too
            raise StorageError("could not store the record: %s" % exc) from exc

    def _count(self, cfg: Config, site: Optional[SiteConfig], name: str, environ: Dict[str, Any]) -> None:
        """Count a request that stores nothing else (a DNT/GPC drop, an oversize chunk), under a configured site
        only (never an id from the request), and only within the client's request allowance: a flood of them
        must not become a flood of writes."""
        limits = Limits(budgets=(Budget("r:" + self._client_id(cfg, environ), 1,
                                        cfg.server["rate_limit_per_minute"], ""),),
                        site=site.id if site is not None else "")
        try:
            self.store(cfg).record_counter(time.strftime("%Y-%m-%d", time.gmtime()),
                                           site.id if site is not None else "", name, 1, limits)
        except Exception:
            ERRORS.exception("could not record counter %s", name)

    # -- /collect --------------------------------------------------------

    def handle_preflight(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse):
        headers = self._cors(cfg, environ, None) + [
            ("Access-Control-Allow-Methods", "POST, GET, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type, Content-Encoding"),
            ("Access-Control-Max-Age", "86400"),
            ("Cache-Control", "no-store"),
        ]
        return _respond(start_response, "204 No Content", headers)

    def handle_collect(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse):
        received = now_ms()
        s = cfg.server
        query = environ.get("QUERY_STRING") or ""
        qs = parse_qs(query, keep_blank_values=True)
        content_type = environ.get("CONTENT_TYPE") or None
        headers, cookies = extract_headers(environ, cfg)
        body, size = read_body(environ, s["max_payload_kb"] * 1024)
        if body is not None:
            q_site = cfg.site(_str(qs.get("site", [None])[0], 100))
            if self._refused_early(cfg, environ, q_site, chunk=False, size=size + len(query)):
                return _respond(start_response, "429 Too Many Requests", self._base_headers(cfg) +
                                self._cors(cfg, environ, q_site) + [("Retry-After", "60")])

        rec = RequestRecord(received_at=received, endpoint="collect", method="POST", site_id=None,
                            site_status="invalid", ip=None, headers=headers, cookies=cookies,
                            query=query or None, content_type=content_type, body_bytes=size,
                            server=server_info(environ))
        status = "204 No Content"
        envelope: Optional[Dict[str, Any]] = None
        stored_size = size  # what this request would store, for the per-minute KB budget

        if body is None:
            rec.error = "too_large"
            rec.site_status = "too_large"
            rec.site_id = _str(qs.get("site", [None])[0], 100)
            status = "413 Payload Too Large"
            stored_size = s["max_payload_kb"] * 1024 + 1  # what was read, not what the request claims
        else:
            envelope, error, encoding, data = self._parse_envelope(body, environ, s, content_type)
            rec.body_encoding = encoding
            stored_size = max(size, len(data))
            if error:
                rec.error = error
                rec.body = body
                status = "400 Bad Request"
            elif s["store_raw_body"]:
                rec.body = body

        if envelope is not None:
            site_id = _str(envelope.get("site"), 100) or _str(qs.get("site", [None])[0], 100)
            rec.site_id = site_id
            rec.site_status, site = self._site_status(cfg, site_id, environ, require_origin=True)
        else:
            rec.site_id = rec.site_id or _str(qs.get("site", [None])[0], 100)
            site = cfg.site(rec.site_id)
        cors = self._cors(cfg, environ, site)
        dropped = self._privacy_drop(cfg, site, environ)
        if dropped:
            self._count(cfg, site, dropped, environ)
            return _respond(start_response, "204 No Content", self._base_headers(cfg) + cors)
        if self._disk_low(cfg):
            return _respond(start_response, "503 Service Unavailable",
                            self._base_headers(cfg) + cors + [("Retry-After", "300")])
        rec.ip, rec.headers = self._identity(cfg, site, environ, rec.headers)

        if envelope is not None:
            hits, errors = build_hits(envelope, s["max_events_per_request"], site)
            rec.hits = hits
            rec.proto_v = _int(envelope.get("v"))
            rec.envelope = {k: v for k, v in envelope.items() if k != "events"}
            if errors:
                rec.error = ",".join(errors)
                if any(e in ("no_events", "not_an_object") or e.startswith("too_many_events") for e in errors):
                    status = "400 Bad Request"
                if any(e.startswith("too_many_events") for e in errors):
                    rec.body = body  # the events not stored as hits stay in the raw body

        flagged = is_flagged(rec)  # a batch with a few invalid events still has good hits: normal allowances
        # What the request stores: its body, its hits as serialized (re-serialized numbers can be longer than
        # sent: 9e15 becomes 9000000000000000.0), at least HIT_COST a hit, and the headers.
        stored_hits = sum(len(h.data) for h in rec.hits if h.data)
        charged = (max(stored_size, HIT_COST * len(rec.hits), stored_hits) + len(query)
                   + header_bytes(rec.headers, rec.cookies))
        limits = self._limits(cfg, environ, site, chunk=False, size=charged, flagged=flagged, hits=len(rec.hits))
        try:
            outcome = self._write(cfg, "request", rec, limits)
        except StorageError:
            ERRORS.exception("storage unavailable")
            return _respond(start_response, "503 Service Unavailable",
                            self._base_headers(cfg) + cors + [("Retry-After", "60")])
        headers_out = self._base_headers(cfg) + cors
        if outcome == RATE_LIMITED and status.startswith("204"):  # 400 and 413 stand: retrying cannot help
            return _respond(start_response, "429 Too Many Requests", headers_out + [("Retry-After", "60")])
        if outcome == RATE_LIMITED:
            return _respond(start_response, status, headers_out)
        if status.startswith("204") and site is not None and rec.site_status == "ok" and envelope is not None:
            cookie = self._server_cookie(site, envelope, environ)
            if cookie:
                headers_out.append(("Set-Cookie", cookie))
        return _respond(start_response, status, headers_out)

    def _parse_envelope(self, body: bytes, environ: Dict[str, Any], s: Dict[str, Any],
                        content_type: Optional[str]) -> Tuple[Optional[Dict[str, Any]], Optional[str], str, bytes]:
        """Decompress, decode and parse. Returns (envelope, error, encoding, decoded bytes)."""
        encoding = "identity"
        data = body
        content_encoding = (environ.get("HTTP_CONTENT_ENCODING") or "").lower()
        if body[:2] == b"\x1f\x8b" or "gzip" in content_encoding:
            encoding = "gzip"
            try:
                data = gunzip_bounded(body, s["max_decompressed_kb"] * 1024)
            except (ValueError, zlib.error) as exc:
                return None, "bad_gzip: %s" % exc, encoding, body
        elif content_encoding and content_encoding != "identity":
            return None, "unsupported_encoding: %s" % content_encoding[:40], content_encoding[:40], body
        if not data:
            return None, "empty_body", encoding, data
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None, "bad_utf8", encoding, data
        if text.startswith("\ufeff"):
            text = text[1:]
        if content_type and "application/x-www-form-urlencoded" in content_type.lower():
            form = parse_qs(text, keep_blank_values=True)
            text = (form.get("d") or form.get("data") or [""])[0]
        try:
            obj = json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float)
        except (ValueError, RecursionError) as exc:
            return None, "bad_json: %s" % str(exc)[:120], encoding, data
        if not isinstance(obj, dict):
            return None, "not_an_object", encoding, data
        if _deeper_than(obj, MAX_JSON_DEPTH):  # nothing the tracker sends nests deeper than a few levels
            return None, "bad_json: nested deeper than %d levels" % MAX_JSON_DEPTH, encoding, data
        if _SURROGATE_ESCAPE_RE.search(text):  # only \uD800-\uDFFF escapes can produce lone surrogates
            obj = _replace_surrogates(obj)
        return obj, None, encoding, data

    def _server_cookie(self, site: SiteConfig, envelope: Dict[str, Any], environ: Dict[str, Any]) -> Optional[str]:
        domain = site.settings.get("server_cookie_domain")
        vid = envelope.get("vid")
        if not domain or not isinstance(vid, str) or not _ID_RE.match(vid):
            return None
        vt = _int(envelope.get("vt"))
        value = "%s.%d" % (vid, vt) if vt is not None and vt > 0 else vid
        if not _DOMAIN_RE.match(domain):
            return None
        secure = environ.get("wsgi.url_scheme") == "https" or environ.get("HTTPS", "").lower() in ("on", "1")
        parts = ["ts_svid=%s" % value, "Domain=%s" % domain, "Path=/",
                 "Max-Age=%d" % (int(site.settings.get("cookie_days", 400)) * 86400), "SameSite=Lax"]
        if secure:
            parts.append("Secure")
        return "; ".join(parts)

    # -- /chunk ----------------------------------------------------------

    def handle_chunk(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse):
        received = now_ms()
        s = cfg.server
        query = environ.get("QUERY_STRING") or ""
        qs = {k: v[0] for k, v in parse_qs(query, keep_blank_values=True).items()}
        site_id = _str(qs.get("site"), 100)
        site_status, site = self._site_status(cfg, site_id, environ, require_origin=True)
        dropped = self._privacy_drop(cfg, site, environ)
        cors = self._cors(cfg, environ, site)
        if dropped:
            self._count(cfg, site, dropped, environ)
            return _respond(start_response, "204 No Content", self._base_headers(cfg) + cors)
        body, size = read_body(environ, s["max_chunk_kb"] * 1024)
        if body is None:
            self._count(cfg, site, "chunk_too_large", environ)
            return _respond(start_response, "413 Payload Too Large", self._base_headers(cfg) + cors)
        if self._disk_low(cfg):
            return _respond(start_response, "503 Service Unavailable",
                            self._base_headers(cfg) + cors + [("Retry-After", "300")])
        if self._refused_early(cfg, environ, site, chunk=True, size=size + len(query)):
            return _respond(start_response, "429 Too Many Requests", self._base_headers(cfg) + cors +
                            [("Retry-After", "60")])
        kind = qs.get("k") if qs.get("k") in CHUNK_KINDS else "_invalid"
        error = None if kind != "_invalid" else "unknown_kind"
        if not body:
            error = "empty_body"
        if site is not None and site_status == "ok" and kind in CHUNK_FEATURES \
                and not site.features.get(CHUNK_FEATURES[kind]):
            site_status = "feature_off"  # the site does not record this kind: stored flagged, within its allowance
        if body[:2] == b"\x1f\x8b":
            payload, encoding = body, "gzip"
        else:
            payload, encoding = gzip.compress(body, compresslevel=6), "gzip"
        headers, _cookies = extract_headers(environ, cfg)
        ip, headers = self._identity(cfg, site, environ, headers)
        meta: Dict[str, Any] = dict(qs)
        meta["bytes"] = size
        meta["sent_encoding"] = "gzip" if body[:2] == b"\x1f\x8b" else "identity"
        for h in ("user-agent", "origin", "referer"):
            if h in headers:
                meta[h] = headers[h]
        rec = ChunkRecord(received_at=received, kind=kind, site_id=site_id, site_status=site_status,
                          payload=payload, encoding=encoding, meta=meta, visitor_id=_str(qs.get("vid"), 64),
                          session_id=_str(qs.get("sid"), 64), page_id=_str(qs.get("pid"), 64),
                          seq=_int(qs.get("seq")), client_ts=_int(qs.get("ts")), url=_str(qs.get("url"), 2048),
                          ip=ip, error=error)
        charged = size + len(query) + header_bytes({h: meta[h] for h in ("user-agent", "origin", "referer") if h in meta},
                                                   None)
        limits = self._limits(cfg, environ, site, chunk=True, size=charged,
                              flagged=site_status != "ok" or error is not None)
        try:
            outcome = self._write(cfg, "chunk", rec, limits)
        except StorageError:
            ERRORS.exception("storage unavailable")
            return _respond(start_response, "503 Service Unavailable",
                            self._base_headers(cfg) + cors + [("Retry-After", "60")])
        if outcome == RATE_LIMITED and not error:
            return _respond(start_response, "429 Too Many Requests", self._base_headers(cfg) + cors +
                            [("Retry-After", "60")])
        return _respond(start_response, "400 Bad Request" if error else "204 No Content",
                        self._base_headers(cfg) + cors)

    # -- /p.gif ----------------------------------------------------------

    def handle_pixel(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse):
        received = now_ms()
        method = environ.get("REQUEST_METHOD", "GET").upper()
        query = environ.get("QUERY_STRING") or ""
        parsed = parse_qs(query, keep_blank_values=True)
        data: Dict[str, Any] = {k: (v[0] if len(v) == 1 else v) for k, v in parsed.items()}
        first = {k: v[0] for k, v in parsed.items()}
        site_id = _str(first.get("site"), 100)
        site_status, site = self._site_status(cfg, site_id, environ, require_origin=False)
        if self._privacy_drop(cfg, site, environ):
            return _respond(start_response, "200 OK", _gif_headers(), GIF, head=method == "HEAD")
        headers, cookies = extract_headers(environ, cfg)
        ip, headers = self._identity(cfg, site, environ, headers)
        url = first.get("u") or first.get("url") or headers.get("referer")
        hit = HitRecord(seq=0, data=_dumps(data), type=_str(first.get("t"), 32) or "pageview",
                        name=_str(first.get("n"), 200), event_id=_str(first.get("id"), 64),
                        visitor_id=_str(first.get("vid"), 64), session_id=_str(first.get("sid"), 64),
                        user_id=_str(first.get("uid"), 200), url=_str(url, 2048),
                        client_ts=_int(first.get("ts")))
        rec = RequestRecord(received_at=received, endpoint="pixel", method=method, site_id=site_id,
                            site_status=site_status, ip=ip, headers=headers, cookies=cookies,
                            query=query or None, server=server_info(environ), hits=[hit])
        if not self._disk_low(cfg):
            limits = self._limits(cfg, environ, site, chunk=False, flagged=is_flagged(rec), hits=1,
                                  size=max(len(query), HIT_COST) + header_bytes(headers, cookies))
            try:
                self._write(cfg, "request", rec, limits)
            except StorageError:
                ERRORS.exception("storage unavailable (pixel)")
        return _respond(start_response, "200 OK", _gif_headers(), GIF, head=method == "HEAD")

    # -- /config.js ------------------------------------------------------

    def handle_config(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse,
                      as_json: bool):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        if method not in ("GET", "HEAD"):
            return _method_not_allowed(start_response, "GET, HEAD")
        qs = parse_qs(environ.get("QUERY_STRING") or "")
        site_id = _str(qs.get("site", [""])[0], 100) or ""
        public = cfg.public_site_config(site_id)
        etag = '"%s"' % public["cv"]
        headers = [("Cache-Control", "public, max-age=300"), ("ETag", etag), ("Access-Control-Allow-Origin", "*"),
                   ("X-Content-Type-Options", "nosniff"), ("X-Robots-Tag", "noindex, nofollow")]
        if environ.get("HTTP_IF_NONE_MATCH") == etag:
            return _respond(start_response, "304 Not Modified", headers)
        body = json.dumps(public, separators=(",", ":"), sort_keys=True)
        if as_json:
            headers.append(("Content-Type", "application/json; charset=utf-8"))
            payload = body
        else:
            headers.append(("Content-Type", "application/javascript; charset=utf-8"))
            # Queues the configuration for the tracker, or hands it over if the tracker runs already. Never throws,
            # even if markup on the page made window.tidesight an element (<a id="tidesight">).
            payload = ('(typeof window.tidesight=="function"?window.tidesight:window.tidesight=function(){'
                       '(tidesight.q=tidesight.q||[]).push(arguments)})("_config",%s);\n' % body)
        return _respond(start_response, "200 OK", headers, payload.encode("utf-8"), head=method == "HEAD")

    # -- /health ---------------------------------------------------------

    def handle_health(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse):
        store = self.store(cfg)
        ok = bool(store.healthy())
        body = {"status": "ok" if ok else "error", "version": __version__,
                "storage": cfg.server["storage_mode"], "time": now_ms()}
        nonce = parse_qs(environ.get("QUERY_STRING") or "").get("check", [""])[0]
        if _NONCE_RE.fullmatch(nonce):  # jobs.py cron making sure public_url leads here (webcheck.py)
            body["check"] = webcheck.check_digest(cfg, nonce)
        return _respond(start_response, "200 OK" if ok else "503 Service Unavailable",
                        [("Content-Type", "application/json"), ("Cache-Control", "no-store")],
                        json.dumps(body).encode("utf-8"))

    # -- static tracker files --------------------------------------------

    def handle_static(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse, path: str):
        root = cfg.server["static_dir"] or os.path.join(_REPO_ROOT, "tracker", "build")
        if path == "/robots.txt":
            return _respond(start_response, "200 OK", [("Content-Type", "text/plain")],
                            b"User-agent: *\nDisallow: /\n")
        parts = [p for p in path.split("/") if p]
        if not parts or not all(_STATIC_NAME_RE.match(p) for p in parts) or len(parts) > 2:
            return _respond(start_response, "404 Not Found", [("Content-Type", "text/plain")], b"not found\n")
        if len(parts) == 2 and parts[0] != "ts":
            return _respond(start_response, "404 Not Found", [("Content-Type", "text/plain")], b"not found\n")
        ext = os.path.splitext(parts[-1])[1].lower()
        ctype = _STATIC_TYPES.get(ext)
        file_path = os.path.join(root, *parts)
        if not ctype or not os.path.isfile(file_path):
            return _respond(start_response, "404 Not Found", [("Content-Type", "text/plain")], b"not found\n")
        with open(file_path, "rb") as fh:
            content = fh.read()
        versioned = "v=" in (environ.get("QUERY_STRING") or "")
        headers = [("Content-Type", ctype), ("Access-Control-Allow-Origin", "*"),
                   ("X-Content-Type-Options", "nosniff"),
                   ("Cache-Control", "public, max-age=31536000, immutable" if versioned else "public, max-age=3600")]
        return _respond(start_response, "200 OK", headers, content,
                        head=environ.get("REQUEST_METHOD", "GET").upper() == "HEAD")


# ---------------------------------------------------------------------------
# Envelope → hits
# ---------------------------------------------------------------------------

def build_hits(envelope: Dict[str, Any], max_events: int,
               site: Optional[SiteConfig] = None) -> Tuple[List[HitRecord], List[str]]:
    """Split an envelope into hit records with typed columns.

    At most `max_events` become hits: one request must not turn into an unbounded number of rows. The
    caller keeps the raw body of a longer batch, flagged "too_many_events:<count>", so nothing is lost."""
    errors: List[str] = []
    if not isinstance(envelope.get("v"), int) or isinstance(envelope.get("v"), bool):
        errors.append("bad_version")
    events = envelope.get("events")
    if not isinstance(events, list):
        errors.append("no_events")
        return [], errors
    if len(events) > max_events:
        errors.append("too_many_events:%d" % len(events))
        events = events[:max_events]
    drop_fp = bool(site and site.privacy.get("drop_fingerprint"))
    vid = _str(envelope.get("vid"), 64)
    sid = _str(envelope.get("sid"), 64)
    uid = _str(envelope.get("uid"), 200)
    hits: List[HitRecord] = []
    bad = 0
    for seq, ev in enumerate(events):
        if not isinstance(ev, dict):
            bad += 1
            hits.append(HitRecord(seq=seq, data=_dumps(ev), type="_invalid", visitor_id=vid, session_id=sid,
                                  user_id=uid))
            continue
        etype = _str(ev.get("t"), 32)
        if etype is None:
            bad += 1
            etype = "_invalid"
        if drop_fp and etype == "fp":
            continue
        hits.append(HitRecord(
            seq=seq, data=_dumps(ev), type=etype, name=ev["n"][:200] if isinstance(ev.get("n"), str) and ev["n"] else None,
            client_ts=_int(ev.get("ts")), event_id=_str(ev.get("id"), 64),
            visitor_id=_str(ev.get("vid"), 64) or vid, session_id=_str(ev.get("sid"), 64) or sid,
            page_id=_str(ev.get("pid"), 64), user_id=_str(ev.get("uid"), 200) or uid,
            url=_str(ev.get("url"), 2048)))
    if bad:
        errors.append("bad_events:%d" % bad)
    return hits, errors


# ---------------------------------------------------------------------------
# WSGI plumbing
# ---------------------------------------------------------------------------

def _respond(start_response: StartResponse, status: str, headers: List[Tuple[str, str]],
             body: bytes = b"", head: bool = False) -> List[bytes]:
    out = list(headers)
    if not status.startswith(("204", "304")):
        out.append(("Content-Length", str(len(body))))
    start_response(status, out)
    if head or status.startswith(("204", "304")):
        return [b""]
    return [body]


def _gif_headers() -> List[Tuple[str, str]]:
    return [("Content-Type", "image/gif"), ("Cache-Control", "no-store, max-age=0"),
            ("X-Robots-Tag", "noindex, nofollow"), ("Access-Control-Allow-Origin", "*")]


def _method_not_allowed(start_response: StartResponse, allow: str) -> List[bytes]:
    return _respond(start_response, "405 Method Not Allowed", [("Allow", allow), ("Content-Type", "text/plain")],
                    b"method not allowed\n")


def make_app(config_path: Optional[str] = None, config: Optional[Config] = None) -> Collector:
    """Build the collector WSGI app from a config file (or an already loaded Config)."""
    holder = ConfigHolder(config_path, config)
    return Collector(holder)
