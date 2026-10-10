"""Tidesight command-line jobs, meant to run from cron.

    python -m tidesight.jobs --config ~/tidesight/tidesight.toml cron          # the usual every-few-minutes run
    python -m tidesight.jobs --config … enrich [--rebuild] [--max-seconds 50]
    python -m tidesight.jobs --config … rollup [--all]
    python -m tidesight.jobs --config … load-spool
    python -m tidesight.jobs --config … backup [--dest DIR] [--keep 7] [--with-replay]
    python -m tidesight.jobs --config … geo-update [--source dbip|maxmind]
    python -m tidesight.jobs --config … privacy-clean [--hash-ip|--truncate-ip] [--drop-fingerprint] ... [--apply]
    python -m tidesight.jobs --config … archive --before 2025-01-01 [--apply]
    python -m tidesight.jobs --config … export --site ID --what sessions --from … --to … [--format csv]
    python -m tidesight.jobs --config … purge-site ID [--apply]
    python -m tidesight.jobs --config … vacuum
    python -m tidesight.jobs --config … status
    python -m tidesight.jobs --config … user set NAME [--role viewer] | user link NAME | user remove NAME | user list
    python -m tidesight.jobs --config … setup-link        # the dashboard's first-account form, code filled in
    python -m tidesight.jobs hash-password

Run with server/python on PYTHONPATH (or from that directory).
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional

from . import __version__
from . import users as users_mod
from . import webcheck
from .auth import hash_password
from .config import Config, load
from .storage import load_spool

log = logging.getLogger("tidesight.jobs")


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_enrich(cfg: Config, args: argparse.Namespace) -> Dict[str, Any]:
    from .enrich import pipeline
    return pipeline.run(cfg, rebuild=args.rebuild, max_seconds=args.max_seconds)


def cmd_rollup(cfg: Config, args: argparse.Namespace) -> Dict[str, Any]:
    from .enrich import rollup
    from .enrich.db import JobLock, derived_path, open_derived
    with JobLock(cfg.data_dir):
        conn = open_derived(derived_path(cfg.data_dir))
        try:
            if args.all:
                conn.execute("INSERT OR IGNORE INTO dirty_days(site_id, day) SELECT DISTINCT site_id, day FROM sessions")
            return rollup.run(conn, cfg)
        finally:
            conn.close()


def cmd_load_spool(cfg: Config, args: argparse.Namespace) -> Dict[str, Any]:
    s = cfg.server
    return load_spool(cfg.data_dir, s["journal_mode"], s["synchronous"], s["busy_timeout_ms"])


def cmd_cron(cfg: Config, args: argparse.Namespace) -> Dict[str, Any]:
    """Load the spool (if any), enrich within a time budget, roll up; delete the data of properties deleted in the
    dashboard; refresh [server] resolved_config_file. Safe to run every minute."""
    from .enrich import pipeline
    from .maintenance import purge_queued
    out: Dict[str, Any] = {}
    if os.path.isdir(os.path.join(cfg.data_dir, "spool")):
        out["load_spool"] = cmd_load_spool(cfg, args)
    try:
        out["enrich"] = pipeline.run(cfg, max_seconds=args.max_seconds)
    except RuntimeError as exc:  # another run holds the lock
        out["enrich"] = {"skipped": str(exc)}
    purged = purge_queued(cfg)
    if purged:
        out["purged"] = purged
    if cfg.server["resolved_config_file"]:
        out["resolved_config"] = write_resolved(cfg, cfg.server["resolved_config_file"], only_if_changed=True)
    if cfg.dashboard["users"]:  # a setup code made by a process that had not seen the first user yet
        users_mod.remove_setup_code(cfg.data_dir)
    # Files inside the website's folder: can the web download them? Every 10 minutes, every run while it could.
    last = webcheck.state(cfg)
    if not last or last.get("exposed") or time.time() * 1000 - (last.get("checked_at") or 0) > 600000:
        result = webcheck.check(cfg)
        if result.get("exposed") or result.get("unverified") or result.get("warnings"):
            out["web_check"] = result
    return out


def cmd_backup(cfg: Config, args: argparse.Namespace) -> Dict[str, Any]:
    from .maintenance import backup
    return backup(cfg, dest=args.dest, keep=args.keep, with_replay=args.with_replay, with_derived=args.with_derived)


def cmd_geo_update(cfg: Config, args: argparse.Namespace) -> Dict[str, Any]:
    from .enrich import geo_update
    dest = cfg.enrich["geo_dir"] or os.path.join(cfg.data_dir, "geo")
    os.makedirs(dest, exist_ok=True)
    updated: List[str] = []
    errors: List[str] = []
    sources = [args.source] if args.source else (["maxmind"] if cfg.enrich["maxmind_license_key"] else []) + ["dbip"]
    for source in sources:
        try:
            if source == "maxmind":
                if not cfg.enrich["maxmind_license_key"] or not cfg.enrich["maxmind_account_id"]:
                    raise SystemExit("maxmind needs [enrich] maxmind_account_id and maxmind_license_key")
                updated += geo_update.download_maxmind(dest, cfg.enrich["maxmind_account_id"],
                                                       cfg.enrich["maxmind_license_key"])
            else:
                updated += geo_update.download_dbip(dest)
        except geo_update.GeoUpdateError as exc:  # some files may still have been updated
            updated += list(getattr(exc, "updated", []) or [])
            errors.append("%s: %s" % (source, exc))
    out: Dict[str, Any] = {"updated": updated, "dir": dest}
    if errors:
        out["errors"] = errors
    return out


def cmd_privacy_clean(cfg: Config, args: argparse.Namespace) -> Dict[str, Any]:
    from .maintenance import privacy_clean
    return privacy_clean(cfg, hash_ip=args.hash_ip, truncate_ip=args.truncate_ip,
                         drop_fingerprint=args.drop_fingerprint, drop_replay=args.drop_replay,
                         drop_mouse=args.drop_mouse, apply_retention=args.retention, apply=args.apply,
                         backup_first=not args.no_backup)


def cmd_archive(cfg: Config, args: argparse.Namespace) -> Dict[str, Any]:
    from .maintenance import archive
    return archive(cfg, before=args.before, apply=args.apply)


def cmd_export(cfg: Config, args: argparse.Namespace) -> Dict[str, Any]:
    from .maintenance import export
    return export(cfg, site=args.site, what=args.what, start=args.start, end=args.end, fmt=args.format,
                  out=args.out)


def cmd_vacuum(cfg: Config, args: argparse.Namespace) -> Dict[str, Any]:
    from .maintenance import vacuum
    return vacuum(cfg)


def cmd_status(cfg: Config, args: argparse.Namespace) -> Dict[str, Any]:
    out: Dict[str, Any] = {"version": __version__, "data_dir": cfg.data_dir, "storage_mode": cfg.server["storage_mode"],
                           "sites": sorted(cfg.sites), "dashboard_users": len(cfg.dashboard["users"]),
                           "warnings": cfg.warnings, "files": {}}
    if not cfg.dashboard["users"]:
        out["next_step"] = ("no dashboard user yet: open the dashboard and enter the setup code of %s (or run "
                            "jobs.py setup-link, or jobs.py user set NAME)" % users_mod.setup_path(cfg.data_dir))
    check = webcheck.state(cfg)
    if check:
        out["web_check"] = check  # files inside the website's folder: could the web download them?
    for name in ("events.db", "replay.db", "derived.db"):
        path = os.path.join(cfg.data_dir, name)
        if os.path.exists(path):
            info: Dict[str, Any] = {"mb": round(os.path.getsize(path) / 1e6, 2)}
            conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
            try:
                tables = {"events.db": ("raw_requests", "raw_hits"), "replay.db": ("chunks",),
                          "derived.db": ("sessions", "pageviews", "visitors")}[name]
                for t in tables:
                    info[t] = conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                if name == "derived.db":
                    row = conn.execute("SELECT version, last_id, updated_at FROM job_state WHERE job='enrich'").fetchone()
                    if row:
                        info["enrich"] = {"version": row[0], "last_hit_id": row[1], "updated_at": row[2]}
            except sqlite3.Error as exc:
                info["error"] = str(exc)
            finally:
                conn.close()
            out["files"][name] = info
    spool = os.path.join(cfg.data_dir, "spool")
    if os.path.isdir(spool):
        out["spool_files"] = sum(len(f) for _r, _d, f in os.walk(spool))
    return out


def write_resolved(cfg: Config, out: str, only_if_changed: bool = False) -> Dict[str, Any]:
    """Write the resolved configuration: a new file, mode 600, renamed over the old one (an existing file keeps no
    looser mode)."""
    text = json.dumps(cfg.resolved(), indent=2, sort_keys=True) + "\n"
    if only_if_changed:
        try:
            with open(out, "r", encoding="utf-8") as fh:
                if fh.read() == text:
                    return {"unchanged": out}
        except OSError:
            pass
    tmp = out + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, out)
    return {"written": out}


def cmd_config_json(cfg: Config, args: argparse.Namespace) -> Dict[str, Any]:
    """The resolved configuration as JSON, for the PHP/Node/Worker connectors (contains the secret)."""
    if args.out and args.out != "-":
        return write_resolved(cfg, args.out)
    print(json.dumps(cfg.resolved(), indent=2, sort_keys=True))
    return {}


def cmd_purge_site(cfg: Config, args: argparse.Namespace) -> Dict[str, Any]:
    from .maintenance import purge_site
    if args.apply and cfg.site(args.site) is not None:
        raise SystemExit("%r is still configured: remove it from the configuration (or the dashboard) first" % args.site)
    return purge_site(cfg, args.site, apply=args.apply)


def _new_password(name: str, from_stdin: bool) -> str:
    if from_stdin:  # for scripts: the first line of standard input
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("Password for %s: " % name)
        if getpass.getpass("Repeat it: ") != password:
            raise SystemExit("The passwords differ: nothing changed.")
    if len(password) < users_mod.MIN_PASSWORD:
        raise SystemExit("Use at least %d characters: nothing changed." % users_mod.MIN_PASSWORD)
    return password


def cmd_user(cfg: Config, args: argparse.Namespace) -> Dict[str, Any]:
    """Dashboard users kept in data_dir/users.json (those of the configuration file are changed there)."""
    try:
        return _user(cfg, args)
    except (users_mod.UsersError, TimeoutError) as exc:
        raise SystemExit("%s: nothing changed." % exc)


def _user(cfg: Config, args: argparse.Namespace) -> Dict[str, Any]:
    if args.action == "list":
        stored = users_mod.records(cfg.data_dir)
        rows = [{"user": n, "where": "configuration file", "role": cfg.role(n)} for n in sorted(cfg.file_users)]
        rows += [{"user": n, "where": "users.json", "role": cfg.role(n),
                  "password": "set" if stored[n]["hash"] else "not yet (invited)"}
                 for n in sorted(stored) if n not in cfg.file_users]
        return {"users": rows}
    name = args.name or ""
    if not name:
        raise SystemExit("Name the user: user %s NAME" % args.action)
    if name in cfg.file_users:
        raise SystemExit("%s is defined in the configuration file (%s, [dashboard] users): change it there."
                         % (name, cfg.path))
    if args.action == "remove":
        def remove(table: Dict[str, Dict[str, Any]]) -> bool:
            if table.pop(name, None) is None:
                return False
            if not users_mod.active_admins(cfg.file_users, cfg.dashboard.get("admins") or [], table):
                raise SystemExit("%s is the last admin who can sign in: make someone else an admin first." % name)
            return True
        if not users_mod.update(cfg.data_dir, remove):
            raise SystemExit("There is no user %s in %s." % (name, users_mod.path(cfg.data_dir)))
        from .webdb import WebDB
        WebDB(cfg.data_dir).forget_user(name)
        return {"removed": name}
    if args.action == "link":
        return _user_link(cfg, name)
    if not users_mod.NAME_RE.match(name):
        raise SystemExit("A user name has 1 to 64 letters, digits and . _ @ + - (and starts with a letter or digit).")
    encoded = hash_password(_new_password(name, args.password_stdin))

    def put(table: Dict[str, Dict[str, Any]]) -> bool:
        existed = name in table
        rec = dict(table.get(name) or {"created_at": int(time.time() * 1000)}, hash=encoded)
        if args.role:  # else the role stays as it is (none: users.role_of decides)
            rec["role"] = args.role
        table[name] = rec
        return existed

    changed = users_mod.update(cfg.data_dir, put)
    users_mod.remove_setup_code(cfg.data_dir)  # a user exists: the dashboard's first-account form is closed
    from .webdb import WebDB
    web = WebDB(cfg.data_dir)
    web.delete_user_sessions(name)
    web.delete_links(name)  # a password link made before the new password must not override it
    url = (cfg.server.get("public_url") or "").rstrip("/")
    return {"user": name, "role": Config(cfg.raw, cfg.path).role(name),
            "password": "changed (their sessions ended)" if changed else "set", "sign_in": (url or "") + "/dashboard/"}


def _user_link(cfg: Config, name: str) -> Dict[str, Any]:
    """A one-time link to choose a password, printed: for someone who cannot sign in (the last admin too)."""
    from .webdb import WebDB
    rec = users_mod.records(cfg.data_dir).get(name)
    if rec is None:
        raise SystemExit("There is no user %s in %s." % (name, users_mod.path(cfg.data_dir)))
    kind = "reset" if rec["hash"] else "invite"
    token, expires = WebDB(cfg.data_dir).create_link(name, kind, None, users_mod.RESET_HOURS if kind == "reset"
                                                     else users_mod.INVITE_HOURS)
    url = (cfg.server.get("public_url") or "").rstrip("/") or "https://<Tidesight's address>"
    print("Open this address to choose the password of %s (once, before %s UTC):"
          % (name, time.strftime("%Y-%m-%d %H:%M", time.gmtime(expires / 1000))))
    print("  %s/dashboard/#password=%s" % (url, token))
    return {}


def cmd_setup_link(cfg: Config, args: argparse.Namespace) -> Dict[str, Any]:
    """The address that opens the dashboard's first-account form with the setup code filled in."""
    if cfg.dashboard["users"]:
        raise SystemExit("Tidesight already has users (jobs.py user list): `user set NAME` adds one or changes a "
                         "password.")
    code = users_mod.setup_code(cfg.data_dir)
    if not code:
        raise SystemExit("The setup code could not be written in %s." % cfg.data_dir)
    url = (cfg.server.get("public_url") or "").rstrip("/") or "https://<Tidesight's address>"
    # The code travels after the #: browsers never send that part to the server, so no log keeps it.
    print("Open this address to create the first user (the code also is in %s):" % users_mod.setup_path(cfg.data_dir))
    print("  %s/dashboard/#setup=%s" % (url, code))
    return {}


def cmd_hash_password(cfg: Optional[Config], args: argparse.Namespace) -> Dict[str, Any]:
    password = args.password
    if not password:
        password = getpass.getpass("Password: ")
        if getpass.getpass("Repeat: ") != password:
            raise SystemExit("passwords differ")
    encoded = hash_password(password)
    print("Add this to the [dashboard] users table of your config (or let `user set %s` keep it in users.json):"
          % args.user)
    print('  %s = "%s"' % (args.user, encoded))
    return {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="tidesight.jobs", description="Tidesight maintenance jobs (cron).")
    ap.add_argument("--config", help="config file (default: $TIDESIGHT_CONFIG or conventional locations)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true", help="print nothing unless something fails (for cron)")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("cron", help="load the spool, enrich, roll up (run every few minutes)")
    p.add_argument("--max-seconds", type=float, default=50.0)
    p.set_defaults(func=cmd_cron)

    p = sub.add_parser("enrich", help="process new raw hits into derived.db")
    p.add_argument("--rebuild", action="store_true", help="rebuild derived.db from scratch")
    p.add_argument("--max-seconds", type=float, default=None)
    p.set_defaults(func=cmd_enrich)

    p = sub.add_parser("rollup", help="recompute daily aggregates")
    p.add_argument("--all", action="store_true", help="every day, not only the changed ones")
    p.set_defaults(func=cmd_rollup)

    p = sub.add_parser("load-spool", help="import spool files into SQLite")
    p.set_defaults(func=cmd_load_spool)

    p = sub.add_parser("backup", help="online backup of the databases, rotated")
    p.add_argument("--dest", default=None, help="backup folder (default <data_dir>/backups)")
    p.add_argument("--keep", type=int, default=7, help="backups to keep (default 7)")
    p.add_argument("--with-replay", action="store_true", help="also back up replay.db")
    p.add_argument("--with-derived", action="store_true", help="also back up derived.db (it can be rebuilt)")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("geo-update", help="download or refresh the geo databases")
    p.add_argument("--source", choices=["dbip", "maxmind"], default=None)
    p.set_defaults(func=cmd_geo_update)

    p = sub.add_parser("privacy-clean", help="anonymise or drop personal data (dry run unless --apply)")
    p.add_argument("--hash-ip", action="store_true", help="replace stored IPs by keyed hashes")
    p.add_argument("--truncate-ip", action="store_true", help="keep only the /24 (IPv4) or /48 (IPv6)")
    p.add_argument("--drop-fingerprint", action="store_true", help="delete fingerprint events and signals")
    p.add_argument("--drop-replay", action="store_true", help="delete session replay chunks")
    p.add_argument("--drop-mouse", action="store_true", help="delete mouse-movement chunks")
    p.add_argument("--retention", action="store_true", help="apply the [retention] windows")
    p.add_argument("--apply", action="store_true", help="really change data (default: dry run)")
    p.add_argument("--no-backup", action="store_true", help="skip the automatic backup before --apply")
    p.set_defaults(func=cmd_privacy_clean)

    p = sub.add_parser("archive", help="move old raw data into yearly archive databases (dry run unless --apply)")
    p.add_argument("--before", required=True, help="archive raw data received before this date (YYYY-MM-DD)")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_archive)

    p = sub.add_parser("export", help="export derived data as CSV or JSON")
    p.add_argument("--site", required=True)
    p.add_argument("--what", default="sessions", choices=["sessions", "pageviews", "events", "visitors", "daily",
                                                          "errors", "vitals", "clicks", "forms", "raw"])
    p.add_argument("--from", dest="start", default=None, help="first day (YYYY-MM-DD)")
    p.add_argument("--to", dest="end", default=None, help="last day (YYYY-MM-DD)")
    p.add_argument("--format", default="csv", choices=["csv", "json", "jsonl"])
    p.add_argument("--out", default="-", help="output file (default stdout)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("purge-site", help="delete every row stored for a site (dry run unless --apply)")
    p.add_argument("site")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_purge_site)

    p = sub.add_parser("vacuum", help="optimise and compact the databases")
    p.set_defaults(func=cmd_vacuum)

    p = sub.add_parser("status", help="show storage, counts and job state")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("config-json", help="write the resolved configuration as JSON (for other connectors)")
    p.add_argument("--out", default="-", help="output file (default stdout); created with mode 600")
    p.set_defaults(func=cmd_config_json)

    p = sub.add_parser("user", help="dashboard users: set (add, or change a password), link (a one-time password "
                                    "link), remove, list")
    p.add_argument("action", choices=["set", "link", "remove", "list"])
    p.add_argument("name", nargs="?", default=None)
    p.add_argument("--password-stdin", action="store_true", help="read the password from standard input")
    p.add_argument("--role", choices=["admin", "viewer"], default=None,
                   help="for set: the role (else unchanged; a new user without one is an admin while [dashboard] "
                        "admins is empty)")
    p.set_defaults(func=cmd_user)

    p = sub.add_parser("setup-link", help="the address of the dashboard's first-account form, setup code included")
    p.set_defaults(func=cmd_setup_link)

    p = sub.add_parser("hash-password", help="hash a dashboard password for the config")
    p.add_argument("--user", default="admin")
    p.add_argument("--password", default=None, help="(prompted when omitted)")
    p.set_defaults(func=cmd_hash_password, no_config=True)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    os.umask(0o077)  # databases, backups, archives and exports are for this account only
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = None if getattr(args, "no_config", False) else load(args.config)
    started = time.time()
    result = args.func(cfg, args)
    if result and not args.quiet:
        if isinstance(result, dict):
            result.setdefault("seconds", round(time.time() - started, 2))
        _print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
