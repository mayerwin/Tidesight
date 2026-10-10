"""New versions of Tidesight: looked for once a day, installed in place when an admin asks.

`jobs.py cron` asks GitHub, once a day, which release of the repository (__repository__) is the latest: the
address github.com/<repository>/releases/latest redirects to that release, so no API key is needed and no API
allowance is shared with the other accounts of a shared server. The answer is kept in data_dir/update.json, and
the dashboard shows admins when a newer version exists.

An update an admin asks for (the Status page, or `jobs.py update --install`) is carried out by the next cron run,
never inside a web request: the release's archive is downloaded from GitHub over HTTPS, unpacked beside the code,
checked (it must announce the version asked for, and every Python file must compile), then swapped in. The previous
code stays beside it (<code folder>.previous) for `jobs.py update --rollback`. Files of the old folder that the
release does not have (a configuration file kept there by the layout of earlier versions) are carried over, and
the copies of tidesight.fcgi and of the .htaccess in the website's folder are refreshed when nobody edited them.
Running FastCGI processes pick the new code up by themselves; database migrations run as usual. A git checkout is
left to `git pull`.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import secrets
import shutil
import stat
import ssl
import tarfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import __repository__, __version__

log = logging.getLogger("tidesight.updates")

STATE_FILE = "update.json"
REQUEST_FILE = "update-request.json"
CHECK_EVERY_MS = 24 * 3600 * 1000
MAX_ARCHIVE = 50 * 1024 * 1024    # bytes downloaded
MAX_UNPACKED = 300 * 1024 * 1024  # bytes unpacked
MAX_MEMBERS = 20000
VERSION_RE = re.compile(r"v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})\Z")
CODE_DIR = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))

Head = Callable[[str], Tuple[int, Optional[str]]]
Download = Callable[[str, str, int], None]


class UpdateError(Exception):
    """An update could not be made; nothing was changed."""


def parse_version(text: Any) -> Optional[Tuple[int, int, int]]:
    m = VERSION_RE.match(text) if isinstance(text, str) else None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _path(cfg: Any, name: str) -> str:
    return os.path.join(cfg.data_dir, name)


def _read(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: str, doc: Dict[str, Any]) -> None:
    tmp = "%s.%d.%s.tmp" % (path, os.getpid(), secrets.token_hex(4))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(tmp, path)


def state(cfg: Any) -> Dict[str, Any]:
    """What the dashboard shows: the running version, the latest release, a pending request, the last update."""
    doc = _read(_path(cfg, STATE_FILE))
    latest = doc.get("latest")
    available = bool(parse_version(latest) and parse_version(latest) > parse_version(__version__))
    return {"current": __version__, "repository": __repository__, "latest": latest, "available": available,
            "release_url": doc.get("release_url"), "checked_at": doc.get("checked_at"), "error": doc.get("error"),
            "request": _read(_path(cfg, REQUEST_FILE)) or None, "last_update": doc.get("last_update"),
            "git": os.path.isdir(os.path.join(CODE_DIR, ".git"))}


def _head(url: str) -> Tuple[int, Optional[str]]:
    """Status and Location of a HEAD request, redirects not followed."""
    parts = urlsplit(url)
    conn = http.client.HTTPSConnection(parts.netloc, timeout=20, context=ssl.create_default_context())
    try:
        conn.request("HEAD", parts.path or "/", headers={"User-Agent": "Tidesight/%s update check" % __version__})
        res = conn.getresponse()
        return res.status, res.getheader("Location")
    finally:
        conn.close()


def check(cfg: Any, head: Optional[Head] = None) -> Dict[str, Any]:
    """Ask GitHub for the latest release and remember it."""
    head = head or _head
    doc = _read(_path(cfg, STATE_FILE))
    doc["checked_at"] = int(time.time() * 1000)
    try:
        status, location = head(__repository__ + "/releases/latest")
    except Exception as exc:  # offline, DNS, TLS: try again tomorrow
        doc["error"] = "the latest release could not be asked for: %s" % exc
        _write(_path(cfg, STATE_FILE), doc)
        return state(cfg)
    tag = location.rsplit("/releases/tag/", 1)[1] if location and "/releases/tag/" in location else ""
    if status in (301, 302, 303, 307, 308) and parse_version(tag):
        doc.update({"latest": tag.lstrip("v"), "release_url": location, "error": None})
    else:
        doc.update({"latest": None, "release_url": None,
                    "error": "no release published at %s yet" % __repository__ if status in (301, 302, 404) or not tag
                    else "GitHub answered %s" % status})
    _write(_path(cfg, STATE_FILE), doc)
    return state(cfg)


def due(cfg: Any) -> bool:
    checked = _read(_path(cfg, STATE_FILE)).get("checked_at")
    return not isinstance(checked, int) or time.time() * 1000 - checked > CHECK_EVERY_MS


def request(cfg: Any, version: Any, user: Optional[str]) -> Dict[str, Any]:
    """An admin asks for the latest release: the next cron run installs it."""
    st = state(cfg)
    if not st["available"] or version != st["latest"]:
        raise UpdateError("%s is not the newer release Tidesight knows of (latest: %s)" % (version, st["latest"]))
    if st["git"]:
        raise UpdateError("this installation is a git checkout: update it with git pull")
    if st["request"] and st["request"].get("started_at"):
        raise UpdateError("an update is being installed")
    doc = {"version": version, "requested_by": user, "requested_at": int(time.time() * 1000)}
    _write(_path(cfg, REQUEST_FILE), doc)
    return doc


def cancel(cfg: Any) -> None:
    try:
        os.remove(_path(cfg, REQUEST_FILE))
    except FileNotFoundError:
        pass


def _download(url: str, dest: str, limit: int) -> None:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Tidesight/%s update" % __version__})
    with urllib.request.urlopen(req, timeout=120, context=ssl.create_default_context()) as res, open(dest, "wb") as out:
        total = 0
        while True:
            chunk = res.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise UpdateError("the archive is larger than %d MB" % (limit // (1024 * 1024)))
            out.write(chunk)


def _unpack(archive: str, dest: str) -> None:
    """Unpack the release archive into `dest`, without its top folder; only plain files and folders, every path
    inside `dest` (an archive cannot write anywhere else)."""
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if not members or len(members) > MAX_MEMBERS:
            raise UpdateError("unexpected archive (%d entries)" % len(members))
        top = members[0].name.split("/")[0]
        size = 0
        for m in members:
            parts = m.name.split("/")
            if parts[0] != top or ".." in parts or m.name.startswith("/") or "\\" in m.name:
                raise UpdateError("unexpected path in the archive: %r" % m.name)
            rel = parts[1:]
            if not rel or not rel[-1]:
                continue
            target = os.path.join(dest, *rel)
            if m.isdir():
                os.makedirs(target, exist_ok=True)
            elif m.isfile():
                size += m.size
                if size > MAX_UNPACKED:
                    raise UpdateError("the archive unpacks to more than %d MB" % (MAX_UNPACKED // (1024 * 1024)))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                src = tar.extractfile(m)
                if src is None:
                    raise UpdateError("unreadable entry %r" % m.name)
                with src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(target, 0o755 if m.mode & 0o111 else 0o644)
            # links and devices are skipped: a release has none


def _announced_version(code: str) -> Optional[str]:
    try:
        with open(os.path.join(code, "server", "python", "tidesight", "__init__.py"), encoding="utf-8") as fh:
            m = re.search(r'^__version__ = "([^"]+)"', fh.read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


def _compile_all(code: str) -> None:
    """Every Python file of the new code parses (nothing is written: the check leaves no .pyc behind)."""
    for root, dirs, files in os.walk(os.path.join(code, "server", "python", "tidesight")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(root, name)
                try:
                    with open(path, "rb") as fh:
                        compile(fh.read(), path, "exec", dont_inherit=True)
                except (SyntaxError, ValueError) as exc:
                    raise UpdateError("the new code does not compile: %s" % exc)


def _same_file(a: str, b: str) -> bool:
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def install(cfg: Any, version: str, code_dir: str = CODE_DIR, download: Optional[Download] = None,
            web_dirs: Optional[List[str]] = None) -> Dict[str, Any]:
    """Download, check and swap in a release; returns what was done. Raises UpdateError, changing nothing."""
    download = download or _download
    if parse_version(version) is None:
        raise UpdateError("not a version: %r" % version)
    if os.path.isdir(os.path.join(code_dir, ".git")):
        raise UpdateError("this installation is a git checkout: update it with git pull")
    data = os.path.realpath(cfg.data_dir)
    if data == code_dir or data.startswith(code_dir.rstrip(os.sep) + os.sep):
        raise UpdateError("the data directory is inside the code folder: update by hand (docs/deploy-dreamhost.md)")
    parent = os.path.dirname(code_dir)
    for name in os.listdir(parent):  # left by an update that was stopped half-way (none runs: the lock is held)
        if name.startswith(".%s.update-" % os.path.basename(code_dir)):
            shutil.rmtree(os.path.join(parent, name), ignore_errors=True)
    work = os.path.join(parent, ".%s.update-%s" % (os.path.basename(code_dir), secrets.token_hex(4)))
    fresh = os.path.join(work, "code")
    os.makedirs(fresh)
    notes: List[str] = []
    try:
        archive = os.path.join(work, "release.tar.gz")
        download("%s/archive/refs/tags/v%s.tar.gz" % (__repository__, version), archive, MAX_ARCHIVE)
        _unpack(archive, fresh)
        announced = _announced_version(fresh)
        if announced != version:
            raise UpdateError("the archive announces version %s, not %s" % (announced, version))
        _compile_all(fresh)
        for name in os.listdir(code_dir):  # what the release does not have stays (a configuration file kept here)
            src, dst = os.path.join(code_dir, name), os.path.join(fresh, name)
            if not os.path.exists(dst):
                if os.path.isdir(src):
                    shutil.copytree(src, dst, symlinks=True)
                else:
                    shutil.copy2(src, dst)
                notes.append("kept %s" % name)
        os.chmod(fresh, stat.S_IMODE(os.stat(code_dir).st_mode))  # as private as the code it replaces (chmod 700)
        previous = code_dir + ".previous"
        if os.path.exists(previous):
            shutil.rmtree(previous)
        os.rename(code_dir, previous)
        try:
            os.rename(fresh, code_dir)
        except OSError:
            os.rename(previous, code_dir)
            raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    # The copies in the website's folder follow, unless someone edited them.
    for web in web_dirs or []:
        for copy, old, new in (("tidesight.fcgi", "server/python/tidesight.fcgi", "server/python/tidesight.fcgi"),
                               (".htaccess", "server/python/web.htaccess", "server/python/web.htaccess")):
            target = os.path.join(web, copy)
            if not os.path.exists(target):
                continue
            if _same_file(target, os.path.join(previous, *old.split("/"))):
                if not _same_file(target, os.path.join(code_dir, *new.split("/"))):
                    tmp = target + ".new"
                    shutil.copy2(os.path.join(code_dir, *new.split("/")), tmp)
                    os.replace(tmp, target)
                    notes.append("refreshed %s" % target)
            else:
                notes.append("%s was edited by hand: compare it with %s" % (target, os.path.join(code_dir, *new.split("/"))))
    return {"from": __version__, "to": version, "at": int(time.time() * 1000), "previous": previous, "notes": notes}


def _lock(cfg: Any) -> Any:
    """One update (or rollback) at a time: overlapping cron runs, or a cron run and the command line."""
    from .enrich.db import JobLock
    return JobLock(cfg.data_dir, "update")


def _remember(cfg: Any, result: Dict[str, Any]) -> None:
    """The last update's outcome, for the Status page."""
    doc = _read(_path(cfg, STATE_FILE))
    doc["last_update"] = result
    _write(_path(cfg, STATE_FILE), doc)


def _carry_out(cfg: Any, version: str, user: Optional[str], code_dir: str, download: Optional[Download],
               web_dirs: Optional[List[str]]) -> Dict[str, Any]:
    """Install `version` (the update lock held), and remember how it went: an error leaves the code as it was."""
    try:
        result = install(cfg, version, code_dir=code_dir, download=download, web_dirs=web_dirs)
        result["requested_by"] = user
        log.info("updated Tidesight from %s to %s", result["from"], result["to"])
    except Exception as exc:  # nothing changed: say why on the Status page
        result = {"error": str(exc), "to": version, "at": int(time.time() * 1000), "requested_by": user}
        log.error("the update to %s failed: %s", version, exc)
    _remember(cfg, result)
    return result


def run_requested(cfg: Any, web_dirs: Optional[List[str]] = None, download: Optional[Download] = None,
                  code_dir: str = CODE_DIR) -> Optional[Dict[str, Any]]:
    """The update an admin asked for, if any (jobs.py cron): marked as started (the Status page says so), carried out,
    then spent whatever happened. One found already started was interrupted (the process was stopped): it is not
    tried again."""
    if not os.path.exists(_path(cfg, REQUEST_FILE)):
        return None
    lock = _lock(cfg)
    if not lock.acquire():  # another run is updating
        return None
    try:
        req = _read(_path(cfg, REQUEST_FILE))
        if not req:
            return None
        version, user = str(req.get("version")), req.get("requested_by")
        if req.get("started_at"):
            cancel(cfg)
            result = {"error": "its installation was interrupted (see the log); ask for it again", "to": version,
                      "at": int(time.time() * 1000), "requested_by": user}
            _remember(cfg, result)
            return result
        _write(_path(cfg, REQUEST_FILE), dict(req, started_at=int(time.time() * 1000)))
        try:
            return _carry_out(cfg, version, user, code_dir, download, web_dirs)
        finally:
            cancel(cfg)
    finally:
        lock.release()


def install_now(cfg: Any, version: str, web_dirs: Optional[List[str]] = None, download: Optional[Download] = None,
                code_dir: str = CODE_DIR) -> Dict[str, Any]:
    """`jobs.py update --install`: at once, in place of a pending request. The result has `error` on failure."""
    lock = _lock(cfg)
    if not lock.acquire():
        raise UpdateError("another update is running")
    try:
        cancel(cfg)
        return _carry_out(cfg, version, None, code_dir, download, web_dirs)
    finally:
        lock.release()


def rollback(cfg: Any, code_dir: str = CODE_DIR) -> Dict[str, Any]:
    """Back to the code the last update replaced (the two folders swap places)."""
    lock = _lock(cfg)
    if not lock.acquire():
        raise UpdateError("an update is running")
    try:
        previous = code_dir + ".previous"
        if not os.path.isdir(previous):
            raise UpdateError("there is no previous version to go back to (%s)" % previous)
        swap = code_dir + ".rollback-%s" % secrets.token_hex(4)
        os.rename(code_dir, swap)
        try:
            os.rename(previous, code_dir)
        except OSError:
            os.rename(swap, code_dir)
            raise
        os.rename(swap, previous)
        result = {"from": _announced_version(previous), "to": _announced_version(code_dir), "rollback": True,
                  "at": int(time.time() * 1000), "previous": previous}
        _remember(cfg, result)
        return result
    finally:
        lock.release()
