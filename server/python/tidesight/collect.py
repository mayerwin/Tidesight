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
import json
import logging
import mimetypes
import os
import re
import sys
import time
import zlib
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .anonymize import anonymize_headers, anonymize_ip
from .config import Config, ConfigHolder, SiteConfig
from .storage import (RATE_LIMITED, ChunkRecord, HitRecord, RequestRecord, StorageError, open_store)

log = logging.getLogger("tidesight.collect")

StartResponse = Callable[..., Any]

GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,"
       b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")

ACCEPT_CH = ("Sec-CH-UA, Sec-CH-UA-Mobile, Sec-CH-UA-Platform, Sec-CH-UA-Platform-Version, Sec-CH-UA-Model, "
             "Sec-CH-UA-Full-Version-List, Sec-CH-UA-Arch, Sec-CH-UA-Bitness, Sec-CH-UA-Form-Factors, "
             "Sec-CH-UA-WoW64, Viewport-Width, DPR, Device-Memory, RTT, Downlink, ECT, Sec-CH-Prefers-Color-Scheme, "
             "Sec-CH-Prefers-Reduced-Motion")

CHUNK_KINDS = ("replay", "mouse", "snapshot")
MAX_HEADER_VALUE = 8192
REDACTED = "[redacted]"
ALWAYS_SECRET = ("authorization", "proxy-authorization")
SERVER_ENV_KEYS = ("SERVER_PROTOCOL", "REQUEST_SCHEME", "HTTPS", "SSL_PROTOCOL", "SSL_CIPHER", "HTTP2",
                   "H2PUSH", "SERVER_NAME", "SERVER_PORT", "REMOTE_PORT", "SERVER_SOFTWARE")

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_ORIGIN_RE = re.compile(r"^(null|[A-Za-z][A-Za-z0-9+.-]*://[^\s/?#,]+)$")
_STATIC_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
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


def _reject_constant(name: str) -> Any:
    raise ValueError("invalid JSON constant %s" % name)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


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
                if any(fnmatch.fnmatchcase(cname, p) for p in cookie_patterns):
                    kept[cname] = cval[:512]
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
        length = int(environ.get("CONTENT_LENGTH") or 0)
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
            log.exception("configuration unavailable")
            return _respond(start_response, "503 Service Unavailable", [("Content-Type", "text/plain")],
                            b"configuration error\n")
        path = environ.get("PATH_INFO") or "/"
        method = environ.get("REQUEST_METHOD", "GET").upper()
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
            log.exception("unhandled error on %s %s", method, path)
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
        privacy = site.privacy if site else cfg.privacy
        ip = environ.get("REMOTE_ADDR") or None
        if privacy.get("hash_ip") or privacy.get("truncate_ip"):
            secret = cfg.secret
            ip = anonymize_ip(ip, privacy["hash_ip"], privacy["truncate_ip"], secret)
            headers = anonymize_headers(headers, privacy["hash_ip"], privacy["truncate_ip"], secret)
        return ip, headers

    def _rate_key(self, cfg: Config, environ: Dict[str, Any]) -> Optional[str]:
        key_name = cfg.server["rate_limit_key"]
        if key_name == "remote_addr":
            return environ.get("REMOTE_ADDR") or None
        value = environ.get("HTTP_" + key_name.upper().replace("-", "_"))
        if value:
            return value.split(",")[0].strip()[:64]
        return environ.get("REMOTE_ADDR") or None

    def _write(self, cfg: Config, kind: str, rec: Any, environ: Dict[str, Any]) -> str:
        store = self.store(cfg)
        limit = cfg.server["rate_limit_per_minute"]
        key = self._rate_key(cfg, environ)
        if kind == "chunk":
            return store.write_chunk(rec, key, limit)
        return store.write_request(rec, key, limit)

    def _count(self, cfg: Config, site_id: Optional[str], name: str) -> None:
        try:
            self.store(cfg).record_counter(time.strftime("%Y-%m-%d", time.gmtime()), site_id, name)
        except Exception:
            log.exception("could not record counter %s", name)

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

        rec = RequestRecord(received_at=received, endpoint="collect", method="POST", site_id=None,
                            site_status="invalid", ip=None, headers=headers, cookies=cookies,
                            query=query or None, content_type=content_type, body_bytes=size,
                            server=server_info(environ))
        status = "204 No Content"
        envelope: Optional[Dict[str, Any]] = None

        if body is None:
            rec.error = "too_large"
            rec.site_status = "too_large"
            rec.site_id = _str(qs.get("site", [None])[0], 100)
            status = "413 Payload Too Large"
        else:
            envelope, error, encoding, data = self._parse_envelope(body, environ, s, content_type)
            rec.body_encoding = encoding
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
        dropped = self._privacy_drop(cfg, site, environ)
        if dropped:
            self._count(cfg, rec.site_id, dropped)
            return _respond(start_response, "204 No Content", self._base_headers(cfg) + self._cors(cfg, environ, site))
        rec.ip, rec.headers = self._identity(cfg, site, environ, rec.headers)

        if envelope is not None:
            hits, errors = build_hits(envelope, s["max_events_per_request"], site)
            rec.hits = hits
            rec.proto_v = _int(envelope.get("v"))
            rec.envelope = {k: v for k, v in envelope.items() if k != "events"}
            if errors:
                rec.error = ",".join(errors)
                if "no_events" in errors or "not_an_object" in errors:
                    status = "400 Bad Request"

        try:
            outcome = self._write(cfg, "request", rec, environ)
        except StorageError:
            log.exception("storage unavailable")
            return _respond(start_response, "503 Service Unavailable",
                            self._base_headers(cfg) + self._cors(cfg, environ, site) + [("Retry-After", "60")])
        headers_out = self._base_headers(cfg) + self._cors(cfg, environ, site)
        if outcome == RATE_LIMITED:
            return _respond(start_response, "429 Too Many Requests", headers_out + [("Retry-After", "60")])
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
            obj = json.loads(text, parse_constant=_reject_constant)
        except (ValueError, RecursionError) as exc:
            return None, "bad_json: %s" % str(exc)[:120], encoding, data
        if not isinstance(obj, dict):
            return None, "not_an_object", encoding, data
        return obj, None, encoding, data

    def _server_cookie(self, site: SiteConfig, envelope: Dict[str, Any], environ: Dict[str, Any]) -> Optional[str]:
        domain = site.settings.get("server_cookie_domain")
        vid = envelope.get("vid")
        if not domain or not isinstance(vid, str) or not _ID_RE.match(vid):
            return None
        vt = _int(envelope.get("vt"))
        value = "%s.%d" % (vid, vt) if vt is not None and vt > 0 else vid
        if not re.match(r"^\.?[A-Za-z0-9.-]+$", domain):
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
            self._count(cfg, site_id, dropped)
            return _respond(start_response, "204 No Content", self._base_headers(cfg) + cors)
        body, size = read_body(environ, s["max_chunk_kb"] * 1024)
        if body is None:
            self._count(cfg, site_id, "chunk_too_large")
            return _respond(start_response, "413 Payload Too Large", self._base_headers(cfg) + cors)
        kind = qs.get("k") if qs.get("k") in CHUNK_KINDS else "_invalid"
        error = None if kind != "_invalid" else "unknown_kind"
        if not body:
            error = "empty_body"
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
        try:
            outcome = self._write(cfg, "chunk", rec, environ)
        except StorageError:
            log.exception("storage unavailable")
            return _respond(start_response, "503 Service Unavailable",
                            self._base_headers(cfg) + cors + [("Retry-After", "60")])
        if outcome == RATE_LIMITED:
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
        url = first.get("u") or first.get("url") or environ.get("HTTP_REFERER")
        hit = HitRecord(seq=0, data=_dumps(data), type=_str(first.get("t"), 32) or "pageview",
                        name=_str(first.get("n"), 200), event_id=_str(first.get("id"), 64),
                        visitor_id=_str(first.get("vid"), 64), session_id=_str(first.get("sid"), 64),
                        user_id=_str(first.get("uid"), 200), url=_str(url, 2048),
                        client_ts=_int(first.get("ts")))
        rec = RequestRecord(received_at=received, endpoint="pixel", method=method, site_id=site_id,
                            site_status=site_status, ip=ip, headers=headers, cookies=cookies,
                            query=query or None, server=server_info(environ), hits=[hit])
        try:
            self._write(cfg, "request", rec, environ)
        except StorageError:
            log.exception("storage unavailable (pixel)")
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
            payload = ('(window.tidesight=window.tidesight||function(){(tidesight.q=tidesight.q||[])'
                       '.push(arguments)})("_config",%s);\n' % body)
        return _respond(start_response, "200 OK", headers, payload.encode("utf-8"), head=method == "HEAD")

    # -- /health ---------------------------------------------------------

    def handle_health(self, cfg: Config, environ: Dict[str, Any], start_response: StartResponse):
        store = self.store(cfg)
        ok = bool(store.healthy())
        body = {"status": "ok" if ok else "error", "version": __version__,
                "storage": cfg.server["storage_mode"], "time": now_ms()}
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
    """Split an envelope into hit records with typed columns. Never drops an event."""
    errors: List[str] = []
    if not isinstance(envelope.get("v"), int) or isinstance(envelope.get("v"), bool):
        errors.append("bad_version")
    events = envelope.get("events")
    if not isinstance(events, list):
        errors.append("no_events")
        return [], errors
    if len(events) > max_events:
        errors.append("too_many_events")
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
