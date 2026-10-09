"""Configuration loading for Tidesight (standard library only).

The configuration is a TOML file (see config/tidesight.example.toml and
docs/configuration.md). Every key is optional; the defaults below are the
single source of truth. Per-site settings are merged in three layers:
built-in defaults, then the file's [defaults] table, then [sites.<id>].
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

try:  # Python 3.11+
    import tomllib as _toml_impl  # type: ignore
except ImportError:  # pragma: no cover - depends on the interpreter
    try:
        import tomli as _toml_impl  # type: ignore
    except ImportError:
        from . import _toml as _toml_impl  # type: ignore

log = logging.getLogger("tidesight.config")

CONFIG_ENV = "TIDESIGHT_CONFIG"

SERVER_DEFAULTS: Dict[str, Any] = {
    "data_dir": "",
    "storage_mode": "sqlite",          # sqlite | spool
    "journal_mode": "wal",             # wal | delete
    "synchronous": "normal",           # normal | full
    "busy_timeout_ms": 10000,
    "max_payload_kb": 64,
    "max_chunk_kb": 1024,
    "max_decompressed_kb": 4096,
    "max_events_per_request": 500,
    "rate_limit_per_minute": 600,
    "rate_limit_key": "remote_addr",   # or a request header name, e.g. "cf-connecting-ip"
    "cors": "reflect",                 # reflect | allowed
    "accept_ch": True,
    "public_url": "",
    "secret": "",
    "static_dir": "",
    "dashboard_dir": "",
    "store_raw_body": False,
    "spool_fallback": True,            # write to the spool when SQLite is unavailable
    "log_file": "",
}

HEADERS_DEFAULTS: Dict[str, Any] = {
    "secret_patterns": ["*token*", "*key*", "*secret*", "*password*"],
    "drop": [],
    "cookie_names": ["ts_*"],
}

PRIVACY_DEFAULTS: Dict[str, Any] = {
    "hash_ip": False,
    "truncate_ip": False,
    "honour_dnt": False,
    "honour_gpc": False,
    "drop_fingerprint": False,
}

RETENTION_DEFAULTS: Dict[str, Any] = {
    "raw_days": 0,
    "replay_days": 0,
    "mouse_days": 0,
}

ENRICH_DEFAULTS: Dict[str, Any] = {
    "geo_dir": "",
    "ip_header": "auto",
    "trusted_proxies": [],
    "bot_list": "",
    "internal_ips": [],
    "maxmind_account_id": "",
    "maxmind_license_key": "",
    "batch_size": 5000,
}

DASHBOARD_DEFAULTS: Dict[str, Any] = {
    "users": {},
    "realtime_minutes": 30,
}

# Site-level settings (everything except features and per-feature options).
SITE_DEFAULTS: Dict[str, Any] = {
    "name": "",
    "origins": [],
    "timezone": "UTC",
    "session_timeout_min": 30,
    "split_session_at_midnight": True,
    "cookie_domain": "auto",
    "cookie_days": 400,
    "server_cookie_domain": "",
    "hash_mode": "route",              # route | all | off
    "spa_replace": "path",             # path | all | off
    "heartbeat_sec": 30,
    "idle_sec": 300,                   # engagement stops after this long without input
    "flush_sec": 5,
    "search_params": ["q", "s", "search", "query", "keyword"],
    "exclude_paths": [],
    "download_extensions": [
        "pdf", "zip", "dmg", "exe", "msi", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "txt",
        "rtf", "odt", "ods", "odp", "epub", "mp3", "mp4", "mov", "avi", "wav", "gz", "tgz", "rar",
        "7z", "apk", "iso", "pkg", "deb", "rpm", "xml", "json",
    ],
    "public": False,
    "public_metrics": ["visitors", "pageviews", "pages", "sources", "countries", "devices"],
    "share_token": "",
}

FEATURE_DEFAULTS: Dict[str, bool] = {
    "pageviews": True,
    "spa": True,
    "engagement": True,
    "context": True,
    "bot_signals": True,
    "adblock": False,
    "fingerprint": True,
    "canvas_fingerprint": True,
    "webgl_fingerprint": True,
    "audio_fingerprint": True,
    "font_fingerprint": False,
    "clicks": True,
    "outbound": True,
    "downloads": True,
    "heatmap_clicks": True,
    "rage_clicks": True,
    "dead_clicks": True,
    "error_clicks": True,
    "scroll": True,
    "forms": True,
    "vitals": True,
    "navigation_timing": True,
    "long_tasks": False,
    "errors": True,
    "resource_errors": True,
    "resize": True,
    "visibility": True,
    "copy_paste": False,
    "text_selection": False,
    "site_search": True,
    "not_found": True,
    "mouse_moves": False,
    "replay": False,
    "dom_snapshots": False,
}

# Per-feature options. Every feature accepts sample_rate (decided per session).
FEATURE_OPTION_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "replay": {
        "sample_rate": 0.1,
        "mask_all_inputs": True,
        "mask_text_class": "ts-mask",
        "block_class": "ts-block",
        "record_canvas": False,
        "max_minutes": 30,
    },
    "mouse_moves": {"sample_rate": 0.1, "interval_ms": 100, "max_points": 3000},
    "dom_snapshots": {"sample_rate": 0.01},
    "rage_clicks": {"sample_rate": 1.0, "count": 3, "window_ms": 1000, "radius_px": 30},
    "dead_clicks": {"sample_rate": 1.0, "wait_ms": 1000},
    "errors": {"sample_rate": 1.0, "max_per_page": 25},
    "scroll": {"sample_rate": 1.0, "marks": [25, 50, 75, 90, 100]},
    "clicks": {"sample_rate": 1.0, "text_max": 100},
    "copy_paste": {"sample_rate": 1.0},
}

_SECTIONS = {
    "server": SERVER_DEFAULTS,
    "headers": HEADERS_DEFAULTS,
    "privacy": PRIVACY_DEFAULTS,
    "retention": RETENTION_DEFAULTS,
    "enrich": ENRICH_DEFAULTS,
    "dashboard": DASHBOARD_DEFAULTS,
}

_CHOICES = {
    ("server", "storage_mode"): ("sqlite", "spool"),
    ("server", "journal_mode"): ("wal", "delete"),
    ("server", "synchronous"): ("normal", "full"),
    ("server", "cors"): ("reflect", "allowed"),
    ("site", "hash_mode"): ("route", "all", "off"),
    ("site", "spa_replace"): ("path", "all", "off"),
}


class ConfigError(ValueError):
    """Raised when the configuration file is invalid."""


def _type_ok(default: Any, value: Any) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list)
    if isinstance(default, dict):
        return isinstance(value, dict)
    return True


def _merge_checked(defaults: Dict[str, Any], values: Dict[str, Any], where: str,
                   warnings: List[str], kind: str = "") -> Dict[str, Any]:
    """Return defaults updated with values, type-checked against the defaults."""
    out = dict(defaults)
    for key, value in values.items():
        if key not in defaults:
            warnings.append("%s: unknown key %r (ignored)" % (where, key))
            continue
        if not _type_ok(defaults[key], value):
            raise ConfigError("%s.%s: expected %s, got %r" % (where, key, type(defaults[key]).__name__, value))
        choices = _CHOICES.get((kind, key))
        if choices and value not in choices:
            raise ConfigError("%s.%s: must be one of %s, got %r" % (where, key, ", ".join(choices), value))
        out[key] = value
    return out


def normalize_origin(origin: str) -> str:
    """Lower-case an origin and drop default ports and trailing slashes."""
    o = origin.strip().lower().rstrip("/")
    if o.startswith("https://") and o.endswith(":443"):
        o = o[:-4]
    elif o.startswith("http://") and o.endswith(":80"):
        o = o[:-3]
    return o


class SiteConfig:
    """Resolved configuration of one site."""

    def __init__(self, site_id: str, settings: Dict[str, Any], features: Dict[str, bool],
                 opts: Dict[str, Dict[str, Any]], privacy: Dict[str, Any]) -> None:
        self.id = site_id
        self.settings = settings
        self.features = features
        self.opts = opts
        self.privacy = privacy
        self.name = settings.get("name") or site_id
        self.timezone = settings["timezone"]
        self.origins = [normalize_origin(o) for o in settings.get("origins", [])]

    def origin_allowed(self, origin: Optional[str]) -> bool:
        """True when the origin is allowed, or when the site lists no origins."""
        if not self.origins:
            return True
        if not origin:
            return False
        o = normalize_origin(origin)
        for pattern in self.origins:
            if pattern == "*" or pattern == o:
                return True
            if "*" in pattern and fnmatch.fnmatchcase(o, pattern):
                return True
        return False

    def hosts(self) -> List[str]:
        """Host names from the origins list (wildcards stripped), for internal-referrer checks."""
        out = []
        for o in self.origins:
            host = o.split("://", 1)[-1].split(":", 1)[0].lstrip("*.")
            if host and host != "*":
                out.append(host)
        return out


class Config:
    """The whole resolved configuration."""

    def __init__(self, raw: Dict[str, Any], path: Optional[str] = None) -> None:
        self.path = path
        self.raw = raw
        self.warnings: List[str] = []
        base_dir = os.path.dirname(os.path.abspath(path)) if path else os.getcwd()

        for section in raw:
            if section not in _SECTIONS and section not in ("defaults", "sites"):
                self.warnings.append("unknown section [%s] (ignored)" % section)
        sections = {}
        for name, defaults in _SECTIONS.items():
            values = raw.get(name, {})
            if not isinstance(values, dict):
                raise ConfigError("[%s] must be a table" % name)
            sections[name] = _merge_checked(defaults, values, name, self.warnings, kind=name)
        self.server: Dict[str, Any] = sections["server"]
        self.headers: Dict[str, Any] = sections["headers"]
        self.privacy: Dict[str, Any] = sections["privacy"]
        self.retention: Dict[str, Any] = sections["retention"]
        self.enrich: Dict[str, Any] = sections["enrich"]
        self.dashboard: Dict[str, Any] = sections["dashboard"]

        def resolve(path: str) -> str:  # relative paths are relative to the config file
            path = os.path.expanduser(path)
            return os.path.abspath(path if os.path.isabs(path) else os.path.join(base_dir, path))

        self.data_dir = resolve(self.server["data_dir"] or "data")
        self.server["data_dir"] = self.data_dir
        for section, key in (("server", "static_dir"), ("server", "dashboard_dir"), ("server", "log_file"),
                             ("enrich", "geo_dir"), ("enrich", "bot_list")):
            values = getattr(self, section)
            if values[key]:
                values[key] = resolve(values[key])

        defaults = raw.get("defaults", {})
        if not isinstance(defaults, dict):
            raise ConfigError("[defaults] must be a table")
        self._site_layer = self._split_site_table(defaults, "defaults")

        self.sites: Dict[str, SiteConfig] = {}
        sites = raw.get("sites", {})
        if not isinstance(sites, dict):
            raise ConfigError("[sites] must be a table of sites")
        for site_id, table in sites.items():
            if not isinstance(table, dict):
                raise ConfigError("[sites.%s] must be a table" % site_id)
            self.sites[site_id] = self._build_site(site_id, table)
        self._default_site = self._build_site("", {})
        self._secret: Optional[str] = None

    # -- site resolution -------------------------------------------------

    def _split_site_table(self, table: Dict[str, Any], where: str) -> Tuple[Dict, Dict, Dict, Dict]:
        """Split a [defaults]/[sites.x] table into settings, features, options and privacy."""
        settings, features, opts, privacy = {}, {}, {}, {}
        for key, value in table.items():
            if key == "features":
                if not isinstance(value, dict):
                    raise ConfigError("%s.features must be a table" % where)
                for fname, enabled in value.items():
                    if fname not in FEATURE_DEFAULTS:
                        self.warnings.append("%s.features: unknown feature %r" % (where, fname))
                    elif not isinstance(enabled, bool):
                        raise ConfigError("%s.features.%s must be true or false" % (where, fname))
                    else:
                        features[fname] = enabled
            elif key == "privacy":
                if not isinstance(value, dict):
                    raise ConfigError("%s.privacy must be a table" % where)
                privacy = _merge_checked(PRIVACY_DEFAULTS, value, where + ".privacy", self.warnings)
                privacy = {k: v for k, v in privacy.items() if k in value}
            elif key in FEATURE_DEFAULTS:
                if not isinstance(value, dict):
                    raise ConfigError("%s.%s must be a table of options" % (where, key))
                known = dict(FEATURE_OPTION_DEFAULTS.get(key, {"sample_rate": 1.0}))
                known.setdefault("sample_rate", 1.0)
                checked = _merge_checked(known, value, "%s.%s" % (where, key), self.warnings)
                opts[key] = {k: v for k, v in checked.items() if k in value}
                rate = opts[key].get("sample_rate")
                if rate is not None and not 0 <= float(rate) <= 1:
                    raise ConfigError("%s.%s.sample_rate must be between 0 and 1" % (where, key))
            else:
                if key not in SITE_DEFAULTS:
                    self.warnings.append("%s: unknown key %r (ignored)" % (where, key))
                    continue
                if not _type_ok(SITE_DEFAULTS[key], value):
                    raise ConfigError("%s.%s: expected %s, got %r"
                                      % (where, key, type(SITE_DEFAULTS[key]).__name__, value))
                choices = _CHOICES.get(("site", key))
                if choices and value not in choices:
                    raise ConfigError("%s.%s: must be one of %s" % (where, key, ", ".join(choices)))
                settings[key] = value
        return settings, features, opts, privacy

    def _build_site(self, site_id: str, table: Dict[str, Any]) -> SiteConfig:
        d_settings, d_features, d_opts, d_privacy = self._site_layer
        s_settings, s_features, s_opts, s_privacy = self._split_site_table(table, "sites.%s" % site_id)
        settings = dict(SITE_DEFAULTS)
        settings.update(d_settings)
        settings.update(s_settings)
        features = dict(FEATURE_DEFAULTS)
        features.update(d_features)
        features.update(s_features)
        privacy = dict(self.privacy)
        privacy.update(d_privacy)
        privacy.update(s_privacy)
        if privacy.get("drop_fingerprint"):
            for f in ("fingerprint", "canvas_fingerprint", "webgl_fingerprint", "audio_fingerprint",
                      "font_fingerprint"):
                features[f] = False
        opts: Dict[str, Dict[str, Any]] = {}
        for fname in FEATURE_DEFAULTS:
            merged = {"sample_rate": 1.0}
            merged.update(FEATURE_OPTION_DEFAULTS.get(fname, {}))
            merged.update(d_opts.get(fname, {}))
            merged.update(s_opts.get(fname, {}))
            opts[fname] = merged
        return SiteConfig(site_id, settings, features, opts, privacy)

    def site(self, site_id: Optional[str]) -> Optional[SiteConfig]:
        if not site_id:
            return None
        return self.sites.get(site_id)

    def site_or_default(self, site_id: Optional[str]) -> SiteConfig:
        """The site's config, or the defaults (used for unknown sites, which are still tracked)."""
        return self.site(site_id) or self._default_site

    # -- secrets ---------------------------------------------------------

    @property
    def secret(self) -> str:
        """The deployment secret: from the config, else generated once in the data dir."""
        if self.server["secret"]:
            return self.server["secret"]
        if self._secret is None:
            self._secret = _load_or_create_secret(self.data_dir)
        return self._secret

    # -- public site configuration (config.js) ---------------------------

    def public_site_config(self, site_id: str) -> Dict[str, Any]:
        """The configuration the tracker receives. Contains nothing secret."""
        site = self.site(site_id)
        known = site is not None
        site = site or self._default_site
        s = site.settings
        enabled = {k: v for k, v in site.features.items()}
        opts = {k: v for k, v in site.opts.items() if enabled.get(k) and v != {"sample_rate": 1.0}}
        cfg = {
            "site": site_id,
            "known": known,
            "features": enabled,
            "opts": opts,
            "session_timeout_min": s["session_timeout_min"],
            "split_session_at_midnight": s["split_session_at_midnight"],
            "timezone": s["timezone"],
            "cookie_domain": s["cookie_domain"],
            "cookie_days": s["cookie_days"],
            "server_cookie": bool(s["server_cookie_domain"]),
            "hash_mode": s["hash_mode"],
            "spa_replace": s["spa_replace"],
            "heartbeat_sec": s["heartbeat_sec"],
            "idle_sec": s["idle_sec"],
            "flush_sec": s["flush_sec"],
            "search_params": s["search_params"],
            "exclude_paths": s["exclude_paths"],
            "download_extensions": s["download_extensions"],
            "dnt": bool(site.privacy.get("honour_dnt")),
            "gpc": bool(site.privacy.get("honour_gpc")),
        }
        digest = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        cfg["cv"] = digest
        return cfg


    # -- resolved configuration for other connectors -----------------------------

    def resolved(self) -> Dict[str, Any]:
        """Everything a connector needs, with defaults and layering already applied.

        Other connectors (PHP, Node, Worker) read this JSON instead of the TOML file:
        `python -m tidesight.jobs config-json > tidesight.json`. See docs/connectors.md.
        """
        def site_doc(site: SiteConfig) -> Dict[str, Any]:
            return {"name": site.name, "origins": site.origins, "timezone": site.timezone,
                    "server_cookie_domain": site.settings["server_cookie_domain"],
                    "cookie_days": site.settings["cookie_days"], "privacy": site.privacy}
        return {
            "format": "tidesight-resolved-config/1",
            "server": dict(self.server, secret=self.secret),
            "headers": self.headers,
            "privacy": self.privacy,
            "sites": {sid: site_doc(s) for sid, s in self.sites.items()},
            "default_site": site_doc(self._default_site),
            "public": {sid: self.public_site_config(sid) for sid in self.sites},
            "public_default": self.public_site_config(""),
        }


def _load_or_create_secret(data_dir: str) -> str:
    path = os.path.join(data_dir, ".secret")
    try:
        with open(path, "r", encoding="ascii") as fh:
            value = fh.read().strip()
            if value:
                return value
    except OSError:
        pass
    value = secrets.token_hex(32)
    try:
        os.makedirs(data_dir, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(value)
    except FileExistsError:  # another process created it first
        with open(path, "r", encoding="ascii") as fh:
            return fh.read().strip()
    except OSError as exc:
        log.warning("could not persist the generated secret in %s: %s", data_dir, exc)
    return value


def find_config_path(explicit: Optional[str] = None) -> Optional[str]:
    """The config file to use: explicit path, $TIDESIGHT_CONFIG, or a conventional location."""
    if explicit:
        return explicit
    env = os.environ.get(CONFIG_ENV)
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(os.getcwd(), "tidesight.toml"),
        os.path.join(os.path.expanduser("~"), "tidesight", "tidesight.toml"),
        os.path.join(here, "..", "..", "..", "config", "tidesight.toml"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return os.path.abspath(c)
    return None


def loads(text: str, path: Optional[str] = None) -> Config:
    if text.startswith("﻿"):  # tolerate a UTF-8 byte-order mark (TOML parsers reject it)
        text = text[1:]
    try:
        raw = _toml_impl.loads(text)
    except Exception as exc:  # TOMLDecodeError from any implementation
        raise ConfigError("%s: invalid TOML: %s" % (path or "<config>", exc))
    return Config(raw, path)


def load(path: Optional[str] = None) -> Config:
    """Load the configuration; without a file, return the defaults (data in ./data)."""
    path = find_config_path(path)
    if not path:
        return Config({}, None)
    with open(path, "rb") as fh:
        text = fh.read().decode("utf-8")
    cfg = loads(text, path)
    for w in cfg.warnings:
        log.warning("config: %s", w)
    return cfg


class ConfigHolder:
    """Holds the current config and reloads it when the file changes (checked every few seconds)."""

    def __init__(self, path: Optional[str] = None, config: Optional[Config] = None,
                 check_interval: float = 5.0) -> None:
        self._lock = threading.Lock()
        self._path = find_config_path(path) if config is None else (config.path or path)
        self._config = config if config is not None else load(self._path)
        self._mtime = self._stat()
        self._checked = time.monotonic()
        self._interval = check_interval
        self._static = config is not None and path is None

    def _stat(self) -> Optional[float]:
        try:
            return os.stat(self._path).st_mtime if self._path else None
        except OSError:
            return None

    def get(self) -> Config:
        if self._static or not self._path:
            return self._config
        now = time.monotonic()
        if now - self._checked < self._interval:
            return self._config
        with self._lock:
            self._checked = now
            mtime = self._stat()
            if mtime is not None and mtime != self._mtime:
                try:
                    self._config = load(self._path)
                    self._mtime = mtime
                    log.info("config reloaded from %s", self._path)
                except Exception as exc:  # keep serving with the previous config
                    log.error("config reload failed, keeping the previous one: %s", exc)
                    self._mtime = mtime
        return self._config
