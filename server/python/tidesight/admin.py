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

from . import properties, users
from .config import (FEATURE_DEFAULTS, PRIVACY_DEFAULTS, Config, ConfigError, ConfigHolder, SiteConfig,
                     normalize_origin)
from .query import ApiError, parse_filters
from .webdb import WebDB

MAX_BODY = 64 * 1024
MAX_SEGMENTS = 200      # per property, and for every property together
MAX_ANNOTATIONS = 2000  # per property
PUBLIC_METRICS = ("visitors", "pageviews", "pages", "sources", "countries", "devices", "events", "realtime")
# Site settings the dashboard may change (features, privacy and per-feature options besides).
EDITABLE = ("name", "origins", "timezone", "public", "public_metrics", "share_token", "goals", "max_hits_per_day",
            "exclude_paths", "redact_paths", "redact_params", "search_params", "session_timeout_min", "hash_mode",
            "spa_replace", "search_console", "bing_site")
_TEXT_LISTS = ("exclude_paths", "redact_paths", "redact_params", "search_params")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")  # always with fullmatch (a "$" lets a trailing newline in)
_TZ_RE = re.compile(r"UTC|[A-Za-z]+(/[A-Za-z0-9_+-]+){1,2}")
# Starting points for a new property's data collection (docs/datapoints.md has every default).
PRESETS: Dict[str, Dict[str, Any]] = {
    "standard": {},
    "privacy": {"privacy": {"truncate_ip": True, "honour_dnt": True, "honour_gpc": True, "drop_fingerprint": True}},
    "complete": {"features": {"mouse_moves": True, "replay": True, "copy_paste": True, "long_tasks": True},
                 "replay": {"sample_rate": 0.1}},
}


def valid_timezone(name: str) -> bool:
    if not isinstance(name, str) or not _TZ_RE.fullmatch(name):
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
        body = json.loads(raw.decode("utf-8"))
        # A lone surrogate ("\ud800" is valid JSON) is not text: it could be neither stored nor hashed.
        json.dumps(body, ensure_ascii=False).encode("utf-8")
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ApiError(400, "the body is not valid JSON")
    return body


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

    def __init__(self, holder: ConfigHolder, user: str, is_admin: bool, signed_in: bool = True) -> None:
        self.holder = holder
        self.cfg = holder.get()
        self.user = user
        self.is_admin = is_admin
        self.signed_in = signed_in  # False on the dev server's --no-auth: no user to check against users.json
        self.web = WebDB(self.cfg.data_dir)

    def call(self, method: str, endpoint: str, params: Dict[str, str], body: Any) -> Any:
        route = {
            ("GET", "properties"): self.list_properties, ("POST", "properties"): self.create_property,
            ("GET", "property"): self.get_property, ("PUT", "property"): self.update_property,
            ("PATCH", "property"): self.update_property, ("DELETE", "property"): self.delete_property,
            ("POST", "segments"): self.create_segment, ("PUT", "segment"): self.update_segment,
            ("PATCH", "segment"): self.update_segment, ("DELETE", "segment"): self.delete_segment,
            ("POST", "annotations"): self.create_annotation, ("DELETE", "annotation"): self.delete_annotation,
            ("GET", "users"): self.list_users, ("POST", "users"): self.create_user,
            ("PATCH", "user"): self.update_user, ("DELETE", "user"): self.delete_user,
            ("POST", "user_link"): self.create_user_link,
            ("GET", "update"): self.get_update, ("POST", "update"): self.request_update,
            ("DELETE", "update"): self.cancel_update,
            ("GET", "search_engines"): self.get_search_engines, ("PUT", "search_engines"): self.connect_search_engine,
            ("DELETE", "search_engines"): self.disconnect_search_engine,
            ("GET", "search_engine_sites"): self.search_engine_sites,
        }.get((method, endpoint))
        if route is None:
            raise ApiError(405 if method != "GET" else 404, "unknown endpoint or method")
        if body is not None and not isinstance(body, dict):
            raise ApiError(400, "the body must be a JSON object")
        return route(params, body or {})

    def _require_admin(self) -> None:
        if not self.is_admin:
            raise ApiError(403, "only admins may change properties")

    def _site(self, params: Dict[str, str]) -> SiteConfig:
        site = self.cfg.site(params.get("site"))
        if site is None:
            raise ApiError(404, "unknown property")
        return site

    # -- users ----------------------------------------------------------------------------
    # Admins invite people (a one-time link to choose a password, valid a week), send password links to people who
    # forgot theirs (valid a day), change roles and remove users. Tidesight sends no email: the admin passes the
    # link on. Users of the configuration file are listed, and changed in that file.

    def _require_user_admin(self) -> None:
        if not self.is_admin:
            raise ApiError(403, "only admins may manage users")

    def _save_users(self, change: Any) -> Any:
        """Change users.json, once more making sure, under its lock, that whoever asks is still an admin who can
        sign in (another process may have changed their role a moment ago)."""
        admins = self.cfg.dashboard.get("admins") or []

        def guarded(table: Dict[str, Dict[str, Any]]) -> Any:
            if self.signed_in and self.user not in self.cfg.file_users:
                rec = table.get(self.user) or {}
                roles = {n: r.get("role") for n, r in table.items()}
                if not rec.get("hash") or users.role_of(self.user, self.cfg.file_users, admins, roles) != "admin":
                    raise ApiError(403, "only admins may manage users")
            return change(table)

        try:
            return users.update(self.cfg.data_dir, guarded)
        except (users.UsersError, TimeoutError, OSError) as exc:
            raise ApiError(503, "users.json could not be saved (%s): try again" % exc)

    def _keep_an_admin(self, table: Dict[str, Dict[str, Any]]) -> None:
        if not users.active_admins(self.cfg.file_users, self.cfg.dashboard.get("admins") or [], table):
            raise ApiError(409, "Tidesight needs at least one admin who can sign in")

    def _stored_user(self, params: Dict[str, Any]) -> str:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise ApiError(400, "name: a user name")
        if name in self.cfg.file_users:
            raise ApiError(409, "%s is defined in the configuration file ([dashboard] users): change it there" % name)
        return name

    def list_users(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        self._require_user_admin()
        cfg = self.cfg
        try:
            stored = users.records(cfg.data_dir)
        except users.UsersError as exc:
            raise ApiError(503, str(exc))
        links, seen = self.web.links(), self.web.last_sign_ins()
        rows = []
        for name in sorted(set(cfg.file_users) | set(stored), key=lambda n: n.lower()):
            in_file = name in cfg.file_users
            rec = {} if in_file else stored[name]
            link = links.get(name)
            if in_file or rec.get("hash"):
                status = "active"
            else:
                status = "invited" if link else "invitation expired"
            rows.append({"name": name, "role": cfg.role(name), "source": "config" if in_file else "dashboard",
                         "role_locked": name in (cfg.dashboard.get("admins") or []),
                         "status": status, "you": name == self.user, "last_sign_in": seen.get(name),
                         "link": {"kind": link["kind"], "expires_at": link["expires"]} if link else None,
                         "created_at": rec.get("created_at"), "created_by": rec.get("created_by")})
        return {"users": rows, "min_password": users.MIN_PASSWORD}

    def create_user(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        """Invite someone: the user exists without a password until they open the link and choose one."""
        self._require_user_admin()
        name, role = body.get("name"), body.get("role", "viewer")
        if not isinstance(name, str) or not users.NAME_RE.match(name):
            raise ApiError(400, "a user name has 1 to 64 letters, digits and . _ @ + - (and starts with a letter or digit)")
        if role not in users.ROLES:
            raise ApiError(400, "role: admin or viewer")
        if name in self.cfg.file_users:
            raise ApiError(409, "%s is already a user (in the configuration file)" % name)
        if role == "viewer" and name in (self.cfg.dashboard.get("admins") or []):
            raise ApiError(409, "%s is named in [dashboard] admins of the configuration file: an admin" % name)

        def add(table: Dict[str, Dict[str, Any]]) -> bool:
            if name in table:
                return False
            table[name] = {"hash": None, "role": role, "created_at": _now_ms(), "created_by": self.user}
            return True

        if not self._save_users(add):
            raise ApiError(409, "%s is already a user" % name)
        token, expires = self.web.create_link(name, "invite", self.user, users.INVITE_HOURS)
        self.holder.reload()
        return {"name": name, "role": role, "kind": "invite", "token": token, "expires_at": expires}

    def update_user(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        """Change someone's role (not your own: another admin does that)."""
        self._require_user_admin()
        name = self._stored_user(params)
        role = body.get("role")
        if role not in users.ROLES:
            raise ApiError(400, "role: admin or viewer")
        if name == self.user:
            raise ApiError(409, "your own role is changed by another admin")
        if name in (self.cfg.dashboard.get("admins") or []):
            raise ApiError(409, "%s is named in [dashboard] admins of the configuration file: change it there" % name)

        def change(table: Dict[str, Dict[str, Any]]) -> None:
            if name not in table:
                raise ApiError(404, "unknown user")
            table[name] = dict(table[name], role=role)
            self._keep_an_admin(table)

        self._save_users(change)
        return {"name": name, "role": self.holder.reload().role(name)}

    def delete_user(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        """Remove someone (not yourself): their sessions end at once."""
        self._require_user_admin()
        name = self._stored_user(params)
        if name == self.user:
            raise ApiError(409, "you cannot remove yourself: another admin can")

        def remove(table: Dict[str, Dict[str, Any]]) -> None:
            if name not in table:
                raise ApiError(404, "unknown user")
            del table[name]
            self._keep_an_admin(table)

        self._save_users(remove)
        self.web.forget_user(name)
        self.holder.reload()
        return {"deleted": name}

    def create_user_link(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        """A new one-time link for someone: an invitation again, or a password reset. Their current password keeps
        working until the link is used; a newer link replaces an older one."""
        self._require_user_admin()
        name = self._stored_user(dict(params, name=params.get("name") or body.get("name") or ""))
        try:
            rec = users.records(self.cfg.data_dir).get(name)
        except users.UsersError as exc:
            raise ApiError(503, str(exc))
        if rec is None:
            raise ApiError(404, "unknown user")
        kind = "reset" if rec.get("hash") else "invite"
        token, expires = self.web.create_link(name, kind, self.user,
                                              users.RESET_HOURS if kind == "reset" else users.INVITE_HOURS)
        if name not in users.records(self.cfg.data_dir):  # removed meanwhile: the link must not outlive them
            self.web.delete_links(name)
            raise ApiError(404, "unknown user")
        return {"name": name, "kind": kind, "token": token, "expires_at": expires}

    # -- updates ----------------------------------------------------------------------------
    # The latest release is looked for once a day by jobs.py cron; an admin's request to install it is carried out by
    # the next cron run (updates.py), never in a web request.

    def _require_update_admin(self) -> None:
        if not self.is_admin:
            raise ApiError(403, "only admins may update Tidesight")

    def get_update(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        from . import updates
        self._require_update_admin()
        return updates.state(self.cfg)

    def request_update(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        from . import updates
        self._require_update_admin()
        try:
            updates.request(self.cfg, body.get("version"), self.user)
        except updates.UpdateError as exc:
            raise ApiError(409, str(exc))
        return updates.state(self.cfg)

    def cancel_update(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        from . import updates
        self._require_update_admin()
        req = updates.state(self.cfg)["request"]
        if req and req.get("started_at"):
            raise ApiError(409, "the update is being installed: it can no longer be withdrawn")
        updates.cancel(self.cfg)
        return updates.state(self.cfg)

    # -- search engines ----------------------------------------------------------------------
    # The Google and Bing accounts (one each, for every property) are checked with the engine before they are kept;
    # their keys never come back. Imports run in jobs.py cron (searchengines.py), never in a web request.

    def _require_search_admin(self) -> None:
        if not self.is_admin:
            raise ApiError(403, "only admins may connect search engines")

    def _engine(self, params: Dict[str, str], body: Dict[str, Any]) -> str:
        from .searchengines import ENGINES
        engine = params.get("engine") or body.get("engine")
        if engine not in ENGINES:
            raise ApiError(400, "engine is google or bing")
        return engine

    def get_search_engines(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        from . import searchengines
        self._require_search_admin()
        out = searchengines.describe(searchengines.credentials(self.cfg.data_dir))
        if params.get("site"):
            site = self._site(params)
            out["imports"] = searchengines.status(self.cfg, site.id)
        return out

    def connect_search_engine(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        from . import searchengines
        self._require_search_admin()
        engine = self._engine(params, body)
        try:
            found = searchengines.connect_engine(self.cfg, engine, body.get("key"), self.user)
        except searchengines.SearchEngineError as exc:
            raise ApiError(400 if exc.kind == "config" else 502, str(exc))
        except TimeoutError as exc:
            raise ApiError(503, str(exc))
        out = searchengines.describe(searchengines.credentials(self.cfg.data_dir))
        out["sites"] = found["sites"]
        return out

    def disconnect_search_engine(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        from . import searchengines
        self._require_search_admin()
        engine = self._engine(params, body)
        try:
            searchengines.disconnect_engine(self.cfg, engine)
        except (searchengines.SearchEngineError, TimeoutError) as exc:
            raise ApiError(503, str(exc))
        return searchengines.describe(searchengines.credentials(self.cfg.data_dir))

    def search_engine_sites(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        """The sites the connected account sees (asked of the engine now)."""
        from . import searchengines
        self._require_search_admin()
        engine = self._engine(params, body)
        if self.cfg.dashboard["demo"]:  # no call leaves a demo: the sites its properties name
            setting = searchengines.SETTINGS[engine]
            return {"sites": [{"url": s.settings[setting], "permission": "siteFullUser"}
                              for s in self.cfg.sites.values() if s.settings.get(setting)]}
        try:
            return {"sites": searchengines.engine_sites(self.cfg, engine)}
        except searchengines.SearchEngineError as exc:
            raise ApiError(400 if exc.kind == "config" else 502, str(exc))

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
            "search_console": s["search_console"], "bing_site": s["bing_site"],
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
        if "search_console" in body or "bing_site" in body:  # the next cron run imports at once
            from .searchengines import clear_retries
            clear_retries(self.cfg, site_id=site.id)
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
        except properties.PropertiesError as exc:
            raise ApiError(503, "%s: fix or restore it before changing properties (nothing was written)" % exc)
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
            if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
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
        if key in ("search_console", "bing_site"):
            from .searchengines import SearchEngineError, check_target
            try:
                return check_target("google" if key == "search_console" else "bing", value)
            except SearchEngineError as exc:
                raise ApiError(400, str(exc))
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
        if self.web.count_segments(site_id) >= MAX_SEGMENTS:
            raise ApiError(409, "at most %d segments: delete some first" % MAX_SEGMENTS)
        seg_id = self.web.add_segment(site_id, fields["name"], fields["filters"], self.user)
        return {"id": seg_id, "site_id": site_id, **fields}

    def _own_segment(self, params: Dict[str, str]) -> Dict[str, Any]:
        """The segment, when the user may change it: its author, or an admin."""
        seg = self.web.segment(self._segment_id(params))
        if seg is None:
            raise ApiError(404, "unknown segment")
        if not self.is_admin and seg["created_by"] != self.user:
            raise ApiError(403, "only its author or an admin may change this segment")
        return seg

    def _segment_id(self, params: Dict[str, str]) -> int:
        try:
            return int(params.get("id", ""))
        except ValueError:
            raise ApiError(400, "id is a number")

    def update_segment(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        seg = self._own_segment(params)
        # Filters are checked against the segment's own property (goals belong to one).
        fields = self._segment_fields(dict(body, site=seg["site_id"]), partial=True)
        if not self.web.update_segment(seg["id"], fields.get("name"), fields.get("filters")):
            raise ApiError(404, "unknown segment")
        return {"id": seg["id"], **fields}

    def delete_segment(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        seg = self._own_segment(params)
        if not self.web.delete_segment(seg["id"]):
            raise ApiError(404, "unknown segment")
        return {"deleted": seg["id"]}

    def create_annotation(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        site = self.cfg.site(body.get("site"))
        if site is None:
            raise ApiError(404, "unknown property")
        day, text = body.get("day"), body.get("text")
        if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ApiError(400, "day is YYYY-MM-DD")
        try:
            time.strptime(day, "%Y-%m-%d")
        except ValueError:
            raise ApiError(400, "day is not a date")
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise ApiError(400, "a note of 1 to 500 characters")
        if self.web.count_annotations(site.id) >= MAX_ANNOTATIONS:
            raise ApiError(409, "at most %d notes per property: delete some first" % MAX_ANNOTATIONS)
        ann_id = self.web.add_annotation(site.id, day, text.strip(), self.user)
        return {"id": ann_id, "day": day, "text": text.strip(), "created_by": self.user}

    def delete_annotation(self, params: Dict[str, str], body: Dict[str, Any]) -> Any:
        try:
            ann_id = int(params.get("id", ""))
        except ValueError:
            raise ApiError(400, "id is a number")
        ann = self.web.annotation(ann_id, params.get("site", ""))
        if ann is None:
            raise ApiError(404, "unknown annotation")
        if not self.is_admin and ann["created_by"] != self.user:
            raise ApiError(403, "only its author or an admin may delete this note")
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
