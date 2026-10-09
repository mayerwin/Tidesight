"""What the dashboard changes, and the property pages' details (standard library only). docs/api.md lists the endpoints.

Properties added in the dashboard live in properties.json (see properties.py); sites defined in the configuration
file are shown read-only, since the file is where they change. Only users listed in [dashboard] admins (every
user when the list is empty) may add, change or delete properties; every user may save segments and annotations.
Deleting a property's data is queued here and carried out by the next `jobs.py cron` run (maintenance.purge_queued),
never in a web request.
"""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import sqlite3
import time
from collections import Counter
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from . import properties
from .config import (FEATURE_DEFAULTS, PRIVACY_DEFAULTS, Config, ConfigError, ConfigHolder, SiteConfig,
                     normalize_origin)
from .query import ApiError, parse_filters
from .webdb import WebDB

MAX_BODY = 64 * 1024
PUBLIC_METRICS = ("visitors", "pageviews", "pages", "sources", "countries", "devices", "events", "realtime")
# Site settings the dashboard may change (features, privacy and per-feature options besides).
EDITABLE = ("name", "origins", "timezone", "public", "public_metrics", "share_token", "goals", "max_hits_per_day",
            "exclude_paths", "redact_paths", "redact_params", "search_params", "session_timeout_min", "hash_mode",
            "spa_replace")
_TEXT_LISTS = ("exclude_paths", "redact_paths", "redact_params", "search_params")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_TZ_RE = re.compile(r"^(UTC|[A-Za-z]+(/[A-Za-z0-9_+-]+){1,2})$")
# Starting points for a new property's data collection (docs/datapoints.md has every default).
PRESETS: Dict[str, Dict[str, Any]] = {
    "standard": {},
    "privacy": {"privacy": {"truncate_ip": True, "honour_dnt": True, "honour_gpc": True, "drop_fingerprint": True}},
    "complete": {"features": {"mouse_moves": True, "replay": True, "copy_paste": True, "long_tasks": True},
                 "replay": {"sample_rate": 0.1}},
}


def valid_timezone(name: str) -> bool:
    if not isinstance(name, str) or not _TZ_RE.match(name):
        return False
    if name == "UTC":
        return True
    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # pragma: no cover
        return True
    try:
        ZoneInfo(name)
        return True
    except Exception:
        try:  # no time zone database on this host: the name's shape has to do
            ZoneInfo("Europe/London")
            return False
        except Exception:
            return True


def read_body(environ: Dict[str, Any]) -> Any:
    """The request's JSON body (at most 64 KB)."""
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        raise ApiError(400, "bad Content-Length")
    if length > MAX_BODY:
        raise ApiError(413, "request too large")
    raw = environ["wsgi.input"].read(length) if length > 0 else b""
    if not raw:
        return {}
    ctype = (environ.get("CONTENT_TYPE") or "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        raise ApiError(415, "send JSON (Content-Type: application/json)")
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ApiError(400, "the body is not valid JSON")


def _ro(cfg: Config, name: str) -> Optional[sqlite3.Connection]:
    path = os.path.join(cfg.data_dir, name)
    if not os.path.exists(path):
        return None
    conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/").replace("?", "%3f").replace("#", "%23"),
                           uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _day(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def _now_ms() -> int:
    return int(time.time() * 1000)


class Admin:
    """Property, segment and annotation endpoints for one request."""

    def __init__(self, holder: ConfigHolder, user: str, is_admin: bool) -> None:
        self.holder = holder
        self.cfg = holder.get()
        self.user = user
        self.is_admin = is_admin
        self.web = WebDB(self.cfg.data_dir)

    def call(self, method: str, endpoint: str, params: Dict[str, str], body: Any) -> Any:
        route = {
            ("GET", "properties"): self.list_properties, ("POST", "properties"): self.create_property,
            ("GET", "property"): self.get_property, ("PUT", "property"): self.update_property,
            ("PATCH", "property"): self.update_property, ("DELETE", "property"): self.delete_property,
            ("POST", "segments"): self.create_segment, ("PUT", "segment"): self.update_segment,
            ("PATCH", "segment"): self.update_segment, ("DELETE", "segment"): self.delete_segment,
            ("POST", "annotations"): self.create_annotation, ("DELETE", "annotation"): self.delete_annotation,
        }.get((method, endpoint))
        if route is None:
            raise ApiError(405 if method != "GET" else 404, "unknown endpoint or method")
        if body is not None and not isinstance(body, dict):
            raise ApiError(400, "the body must be a JSON object")
        return route(params, body or {})

    def _require_admin(self) -> None:
        if not self.is_admin:
            raise ApiError(403, "only dashboard admins may change properties ([dashboard] admins)")

    def _site(self, params: Dict[str, str]) -> SiteConfig:
        site = self.cfg.site(params.get("site"))
        if site is None:
            raise ApiError(404, "unknown property")
        return site

    # -- properties: reading ------------------------------------------------------------

    def list_properties(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        cfg = self.cfg
        now = _now_ms()
        since_day = _day(now - 29 * 86400000)
        visits: Dict[str, Dict[str, int]] = {}
        dconn = _ro(cfg, "derived.db")
        if dconn is not None:
            try:
                for r in dconn.execute("SELECT site_id, day, sessions FROM daily WHERE day >= ?", (since_day,)):
                    visits.setdefault(r["site_id"], {})[r["day"]] = r["sessions"]
            except sqlite3.Error:
                pass
            finally:
                dconn.close()
        days = [_day(now - (29 - i) * 86400000) for i in range(30)]
        out = []
        for site in sorted(cfg.sites.values(), key=lambda s: s.display_name.lower()):
            doc = self._summary(site)
            per_day = visits.get(site.id, {})
            doc["visits_30d"] = sum(per_day.values())
            doc["visits_series"] = [per_day.get(d, 0) for d in days]
            doc.update(self._activity(site.id))
            out.append(doc)
        pending = {p["site_id"]: p for p in self.web.purges()}
        return {"properties": out, "can_edit": self.is_admin, "unknown": self._unknown_sites(now),
                "pending_deletions": list(pending.values()), "presets": list(PRESETS),
                "public_url": cfg.server["public_url"] or None}

    def get_property(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        site = self._site(params)
        return self._detail(site)

    def _summary(self, site: SiteConfig) -> Dict[str, Any]:
        return {"id": site.id, "name": site.settings.get("name") or "", "label": site.display_name,
                "domains": properties.hosts(site.origins), "origins": site.origins, "timezone": site.timezone,
                "managed": site.managed, "public": bool(site.settings["public"])}

    def _detail(self, site: SiteConfig) -> Dict[str, Any]:
        s = site.settings
        doc = self._summary(site)
        doc.update({
            "public_metrics": s["public_metrics"], "share_token": s["share_token"] if self.is_admin else bool(s["share_token"]),
            "goals": s["goals"], "max_hits_per_day": s["max_hits_per_day"], "exclude_paths": s["exclude_paths"],
            "redact_paths": s["redact_paths"], "redact_params": s["redact_params"], "search_params": s["search_params"],
            "session_timeout_min": s["session_timeout_min"], "hash_mode": s["hash_mode"], "spa_replace": s["spa_replace"],
            "features": dict(site.features), "privacy": {k: bool(site.privacy.get(k)) for k in PRIVACY_DEFAULTS},
            "options": {k: v for k, v in site.opts.items() if v != {"sample_rate": 1.0} or k in ("replay", "mouse_moves")},
            "can_edit": self.is_admin and site.managed,
        })
        doc.update(self._activity(site.id, detailed=True))
        return doc

    def _activity(self, site_id: str, detailed: bool = False) -> Dict[str, Any]:
        """When the property last received data, hits per UTC day (stored, refused), and what was flagged."""
        cfg = self.cfg
        out: Dict[str, Any] = {"last_hit": None, "hits_today": 0}
        conn = _ro(cfg, "events.db")
        if conn is None:
            return out
        now = _now_ms()
        try:
            row = conn.execute("SELECT received_at FROM raw_hits WHERE site_id = ? AND site_ok = 1 "
                               "ORDER BY received_at DESC LIMIT 1", (site_id,)).fetchone()
            out["last_hit"] = row[0] if row else None
            counters = conn.execute("SELECT day, name, n FROM counters WHERE site_id = ? AND day >= ?",
                                    (site_id, _day(now - 13 * 86400000))).fetchall()
            out["hits_today"] = sum(r["n"] for r in counters if r["name"] == "hits" and r["day"] == _day(now))
            if detailed:
                days = [_day(now - (13 - i) * 86400000) for i in range(14)]
                hits = {r["day"]: r["n"] for r in counters if r["name"] == "hits"}
                out["hits_series"] = [{"day": d, "hits": hits.get(d, 0)} for d in days]
                out["refused_today"] = {r["name"]: r["n"] for r in counters if r["name"] != "hits" and r["day"] == _day(now)}
                flagged = conn.execute(
                    "SELECT site_status, headers FROM raw_requests WHERE received_at > ? AND site_id = ? "
                    "AND site_status != 'ok' ORDER BY id DESC LIMIT 2000", (now - 7 * 86400000, site_id)).fetchall()
                statuses: Counter = Counter()
                origins: Counter = Counter()
                for r in flagged:
                    statuses[r["site_status"]] += 1
                    if r["site_status"] == "origin_mismatch":
                        origin = _request_origin(r["headers"])
                        if origin:
                            origins[origin] += 1
                out["flagged_7d"] = dict(statuses)
                out["rejected_origins"] = [{"origin": o, "requests": n} for o, n in origins.most_common(10)]
        except sqlite3.Error:
            pass
        finally:
            conn.close()
        return out

    def _unknown_sites(self, now: int) -> List[Dict[str, Any]]:
        """Site ids that sent data in the last two days without being configured (a property to add?)."""
        conn = _ro(self.cfg, "events.db")
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT site_id, COUNT(*) AS requests, MAX(received_at) AS last_seen FROM raw_requests "
                "WHERE received_at > ? AND site_status = 'unknown_site' AND site_id IS NOT NULL AND site_id != '' "
                "GROUP BY site_id ORDER BY requests DESC LIMIT 10", (now - 2 * 86400000,)).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()
        return [{"id": r["site_id"][:100], "requests": r["requests"], "last_seen": r["last_seen"],
                 "valid_id": bool(properties.ID_RE.match(r["site_id"]))} for r in rows]

    # -- properties: changing ------------------------------------------------------------------

    def create_property(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        self._require_admin()
        preset = body.get("preset", "standard")
        if preset not in PRESETS:
            raise ApiError(400, "preset is one of %s" % ", ".join(PRESETS))
        table: Dict[str, Any] = copy.deepcopy(PRESETS[preset])
        changes = {k: v for k, v in body.items() if k not in ("id", "preset", "domains")}
        if "domains" in body and "origins" not in body:
            changes["origins"] = body["domains"]
        if not changes.get("origins"):
            raise ApiError(400, "add at least one domain that will send data")
        changes.setdefault("timezone", "UTC")
        table = self._apply(table, changes)
        wanted = body.get("id")
        if wanted is not None and (not isinstance(wanted, str) or not properties.ID_RE.match(wanted)):
            raise ApiError(400, "a property id has 1 to 63 lower-case letters, digits and dashes")
        pending = {p["site_id"] for p in self.web.purges()}

        def add(tables: Dict[str, Dict[str, Any]]) -> str:
            taken = set(self.cfg.sites) | set(tables) | pending
            if wanted:
                if wanted in taken:
                    raise ApiError(409, "the id %r is taken%s" % (wanted, " (its data is being deleted)"
                                                                  if wanted in pending else ""))
                site_id = wanted
            else:
                base = properties.slug(properties.display_name("", table.get("name"), table["origins"]) or "site")
                site_id = properties.unique_id(base, sorted(taken))
            self._validate(site_id, table)
            tables[site_id] = table
            return site_id

        site_id = self._save(add)
        return self._detail(self.cfg.site(site_id))

    def update_property(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        self._require_admin()
        site = self._site(params)
        if not site.managed:
            raise ApiError(409, "this property is defined in the configuration file: change it there")

        def change(tables: Dict[str, Dict[str, Any]]) -> None:
            if site.id not in tables:
                raise ApiError(409, "this property was deleted meanwhile")
            table = self._apply(tables[site.id], body)
            if not table.get("origins"):
                raise ApiError(400, "keep at least one domain")
            self._validate(site.id, table)
            tables[site.id] = table

        self._save(change)
        return self._detail(self.cfg.site(site.id))

    def delete_property(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        self._require_admin()
        site = self._site(params)
        if not site.managed:
            raise ApiError(409, "this property is defined in the configuration file: remove it there")
        if body.get("confirm") != site.id:
            raise ApiError(400, "confirm with the property id")
        delete_data = bool(body.get("delete_data"))

        def remove(tables: Dict[str, Dict[str, Any]]) -> None:
            tables.pop(site.id, None)

        self._save(remove)
        if delete_data:
            self.web.add_purge(site.id, self.user)
        return {"deleted": site.id, "data": "scheduled for deletion" if delete_data else "kept"}

    def _save(self, change: Any) -> Any:
        try:
            result = properties.update(self.cfg.data_dir, change)
        except TimeoutError as exc:
            raise ApiError(503, str(exc))
        except OSError as exc:
            raise ApiError(503, "properties.json could not be saved (%s): try again" % (exc.strerror or exc))
        self.cfg = self.holder.reload()
        return result

    def _apply(self, table: Dict[str, Any], changes: Dict[str, Any]) -> Dict[str, Any]:
        """The table with the changes: values checked and normalized; null removes a setting (its default applies)."""
        out = copy.deepcopy(table)
        for key, value in changes.items():
            if key in EDITABLE:
                if value is None:
                    out.pop(key, None)
                else:
                    out[key] = self._check_setting(key, value)
            elif key in ("features", "privacy"):
                if not isinstance(value, dict):
                    raise ApiError(400, "%s is an object of true/false switches" % key)
                known = FEATURE_DEFAULTS if key == "features" else PRIVACY_DEFAULTS
                merged = dict(out.get(key) or {})
                for name, on in value.items():
                    if name not in known:
                        raise ApiError(400, "unknown %s switch %r" % (key[:-1] if key == "features" else key, name))
                    if on is None:
                        merged.pop(name, None)
                    elif not isinstance(on, bool):
                        raise ApiError(400, "%s.%s is true or false" % (key, name))
                    else:
                        merged[name] = on
                if merged:
                    out[key] = merged
                else:
                    out.pop(key, None)
            elif key in FEATURE_DEFAULTS:  # a feature's options, e.g. {"replay": {"sample_rate": 0.2}}
                if value is None:
                    out.pop(key, None)
                    continue
                if not isinstance(value, dict):
                    raise ApiError(400, "%s options are an object" % key)
                merged = dict(out.get(key) or {})
                for name, v in value.items():
                    if v is None:
                        merged.pop(name, None)
                    else:
                        merged[name] = v
                out[key] = merged
            else:
                raise ApiError(400, "unknown setting %r" % key)
        return out

    def _check_setting(self, key: str, value: Any) -> Any:
        if key == "name":
            if not isinstance(value, str) or len(value.strip()) > 100:
                raise ApiError(400, "the name is a text of at most 100 characters")
            return value.strip()
        if key == "origins":
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list) or len(value) > 100:
                raise ApiError(400, "domains: a list of at most 100")
            out: List[str] = []
            for item in value:
                if not isinstance(item, str) or not item.strip() or len(item) > 300:
                    raise ApiError(400, "each domain is a text like example.com")
                origin = "*" if item.strip() == "*" else normalize_origin(properties.origin_from_domain(item))
                if origin != "*" and not properties.valid_origin(origin):
                    raise ApiError(400, "%r is not a domain (example.com, *.example.com, http://localhost:3000)" % item)
                if origin not in out:
                    out.append(origin)
            return out
        if key == "timezone":
            if not valid_timezone(value):
                raise ApiError(400, "unknown time zone %r (an IANA name like Europe/Paris, or UTC)" % (value,))
            return value
        if key == "public":
            if not isinstance(value, bool):
                raise ApiError(400, "public is true or false")
            return value
        if key == "public_metrics":
            if not isinstance(value, list) or not all(v in PUBLIC_METRICS for v in value):
                raise ApiError(400, "public_metrics: a list of %s" % ", ".join(PUBLIC_METRICS))
            return list(dict.fromkeys(value))
        if key == "share_token":
            if value == "generate":
                return secrets.token_urlsafe(18)
            if value == "":
                return ""
            if not isinstance(value, str) or not _TOKEN_RE.match(value):
                raise ApiError(400, "a share token has 16 to 128 letters, digits, - and _ (or ask to generate one)")
            return value
        if key == "goals":
            from .config import check_goals
            try:
                return check_goals(value, "goals")
            except ConfigError as exc:
                raise ApiError(400, str(exc))
        if key in ("max_hits_per_day", "session_timeout_min"):
            top = 10 ** 9 if key == "max_hits_per_day" else 1440
            low = 0 if key == "max_hits_per_day" else 1
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= top:
                raise ApiError(400, "%s is a whole number from %d to %d" % (key, low, top))
            return value
        if key in _TEXT_LISTS:
            if not isinstance(value, list) or len(value) > 200 or not all(
                    isinstance(v, str) and 0 < len(v.strip()) <= 300 for v in value):
                raise ApiError(400, "%s: a list of at most 200 texts" % key)
            return [v.strip() for v in value]
        if key in ("hash_mode", "spa_replace"):
            choices = ("route", "all", "off") if key == "hash_mode" else ("path", "all", "off")
            if value not in choices:
                raise ApiError(400, "%s is one of %s" % (key, ", ".join(choices)))
            return value
        raise ApiError(400, "unknown setting %r" % key)  # pragma: no cover

    def _validate(self, site_id: str, table: Dict[str, Any]) -> None:
        """The configuration file's own rules (types, sample rates…): a property that would not load is refused."""
        scratch = Config(self.cfg.raw, self.cfg.path, load_properties=False)
        before = len(scratch.warnings)
        try:
            scratch._build_site(site_id, table)
        except ConfigError as exc:
            raise ApiError(400, str(exc))
        if len(scratch.warnings) > before:
            raise ApiError(400, scratch.warnings[before])

    # -- segments and annotations ---------------------------------------------------------------

    def _segment_fields(self, body: Dict[str, Any], partial: bool) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if "name" in body or not partial:
            name = body.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 100:
                raise ApiError(400, "a segment needs a name (at most 100 characters)")
            out["name"] = name.strip()
        if "filters" in body or not partial:
            filters = body.get("filters")
            if not isinstance(filters, list) or not filters:
                raise ApiError(400, "a segment needs at least one filter")
            site = self.cfg.site(body.get("site")) if body.get("site") else None
            check_site = site or next(iter(self.cfg.sites.values()), None)
            if check_site is None:
                raise ApiError(400, "add a property first")
            if site is None and any(isinstance(f, dict) and f.get("dim") == "goal" for f in filters):
                raise ApiError(400, "a segment with a goal filter belongs to one property")
            parsed = parse_filters({"filters": json.dumps(filters)}, check_site)
            out["filters"] = [{"dim": f.dim, "op": f.op, "values": list(f.values)} for f in parsed]
        return out

    def create_segment(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        site_id = body.get("site") or None
        if site_id is not None and self.cfg.site(site_id) is None:
            raise ApiError(404, "unknown property")
        fields = self._segment_fields(body, partial=False)
        seg_id = self.web.add_segment(site_id, fields["name"], fields["filters"], self.user)
        return {"id": seg_id, "site_id": site_id, **fields}

    def _segment_id(self, params: Dict[str, str]) -> int:
        try:
            return int(params.get("id", ""))
        except ValueError:
            raise ApiError(400, "id is a number")

    def update_segment(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        fields = self._segment_fields(body, partial=True)
        if not self.web.update_segment(self._segment_id(params), fields.get("name"), fields.get("filters")):
            raise ApiError(404, "unknown segment")
        return {"id": self._segment_id(params), **fields}

    def delete_segment(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        if not self.web.delete_segment(self._segment_id(params)):
            raise ApiError(404, "unknown segment")
        return {"deleted": self._segment_id(params)}

    def create_annotation(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        site = self.cfg.site(body.get("site"))
        if site is None:
            raise ApiError(404, "unknown property")
        day, text = body.get("day"), body.get("text")
        if not isinstance(day, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
            raise ApiError(400, "day is YYYY-MM-DD")
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise ApiError(400, "a note of 1 to 500 characters")
        ann_id = self.web.add_annotation(site.id, day, text.strip(), self.user)
        return {"id": ann_id, "day": day, "text": text.strip(), "created_by": self.user}

    def delete_annotation(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        try:
            ann_id = int(params.get("id", ""))
        except ValueError:
            raise ApiError(400, "id is a number")
        if not self.web.delete_annotation(ann_id, params.get("site", "")):
            raise ApiError(404, "unknown annotation")
        return {"deleted": ann_id}


def _request_origin(headers_json: Optional[str]) -> Optional[str]:
    """The Origin (or the Referer's origin) of a stored request: where a refused hit came from."""
    try:
        headers = json.loads(headers_json or "{}")
    except ValueError:
        return None
    if not isinstance(headers, dict):
        return None
    lower = {str(k).lower(): v for k, v in headers.items()}
    value = lower.get("origin") or lower.get("referer")
    if not isinstance(value, str) or len(value) > 2000:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return normalize_origin("%s://%s" % (parts.scheme, parts.netloc.rsplit("@", 1)[-1]))[:200]
