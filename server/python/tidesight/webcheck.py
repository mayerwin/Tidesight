"""Tidesight's files inside the website's folder, kept out of reach (standard library only: the collector uses it).

Tidesight may live in a single folder that is also the website's folder (docs/deploy-dreamhost.md): code,
configuration and data side by side, as WordPress does. Then two things keep the files private: the folder's
.htaccess sends every request to the app, so the web server never hands out a file of the folder, and the data
directory gets an .htaccess of its own that refuses everything (written here, at the first request). The files
are also readable by the account only, which on many hosts keeps the web server itself out.

A rule can still fail unnoticed (an .htaccess the host ignores, a server moved to another web server), so this is
checked as an outsider would: `jobs.py cron` first makes sure that [server] public_url reaches this very
installation (its /health answers a keyed digest of a random value), then puts a file with a random content in
each of those folders and asks the website for it. If one comes back, the files can be downloaded: the collector
and the dashboard refuse to work (503, logged, and said in the dashboard) until a later check finds them out of
reach again. A check that cannot conclude changes nothing, and says why (jobs.py status, the Status page).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import ssl
import stat
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

log = logging.getLogger("tidesight.webcheck")

ROOT_FILE = "web-root.json"     # the web server's folders, as the web app saw them (data_dir)
STATE_FILE = "web-check.json"   # the last check (data_dir)
CANARY = "tidesight-web-check-%s.txt"
MAX_ROOTS = 4
DENY = """# Written by Tidesight: nothing in this folder is for the web.
<IfModule mod_authz_core.c>
Require all denied
</IfModule>
<IfModule !mod_authz_core.c>
Order deny,allow
Deny from all
</IfModule>
"""
CODE_DIR = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))

Fetch = Callable[[str, float], Tuple[Optional[int], bytes]]
_inside_cache: Dict[Tuple[str, str, str, str], List[Tuple[str, str]]] = {}
_done: Dict[str, Tuple[str, str]] = {}  # data_dir -> the folders this process recorded and protected
_state_cache: Dict[str, Tuple[Optional[Tuple[int, int]], Dict[str, Any]]] = {}


def _real(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _within(path: str, root: str) -> bool:
    real = _real(path)
    return real == root or real.startswith(root.rstrip(os.sep) + os.sep)


def inside(cfg: Any, document_root: Optional[str]) -> List[Tuple[str, str]]:
    """Which of Tidesight's folders and files lie inside the web server's folder: [(what, path)], empty when none
    does. The code counts too: its folder may hold files that are not for the web either."""
    if not document_root:
        return []
    key = (document_root, cfg.data_dir, cfg.path or "", cfg.server.get("log_file") or "")
    if key not in _inside_cache:
        root = _real(document_root)
        candidates = (("the data directory", cfg.data_dir), ("the configuration file", cfg.path),
                      ("the log file", cfg.server.get("log_file")), ("the code", CODE_DIR))
        found = [(what, path) for what, path in candidates if path and _within(path, root)]
        if len(_inside_cache) > 100:
            _inside_cache.clear()
        _inside_cache[key] = found
    return _inside_cache[key]


def _write(path: str, text: str, mode: int = 0o600) -> None:
    tmp = "%s.%d.%s.tmp" % (path, os.getpid(), secrets.token_hex(4))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, mode)  # whatever the umask
    os.replace(tmp, path)


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _seen(cfg: Any) -> List[Dict[str, str]]:
    seen = _read_json(os.path.join(cfg.data_dir, ROOT_FILE)).get("seen")
    return [s for s in seen if isinstance(s, dict) and isinstance(s.get("document_root"), str)
            and isinstance(s.get("script_dir"), str)] if isinstance(seen, list) else []


def prepare(cfg: Any, document_root: str, script_dir: str) -> None:
    """With files inside the web server's folder: the data directory's .htaccess (refusing everything), and a note
    of the web server's folder and of the entry script's for the check (both from the web server, never from the
    request; once per process, a file is written only when it changes)."""
    if _done.get(cfg.data_dir) == (document_root, script_dir):
        return
    try:
        os.makedirs(cfg.data_dir, exist_ok=True)
        deny = os.path.join(cfg.data_dir, ".htaccess")
        try:
            with open(deny, "r", encoding="utf-8") as fh:
                current: Optional[str] = fh.read()
        except OSError:
            current = None
        if current != DENY:
            _write(deny, DENY, 0o644)  # the web server has to read it to obey it
        entry = {"document_root": document_root, "script_dir": script_dir}
        seen = _seen(cfg)
        if not seen or seen[0] != entry:
            seen = [entry] + [s for s in seen if s != entry][:MAX_ROOTS - 1]
            _write(os.path.join(cfg.data_dir, ROOT_FILE), json.dumps({"seen": seen}) + "\n")
        _done[cfg.data_dir] = (document_root, script_dir)
    except OSError as exc:
        log.error("could not protect %s: %s", cfg.data_dir, exc)


def state(cfg: Any) -> Dict[str, Any]:
    """The last check ({} before the first one), re-read when its file changes."""
    path = os.path.join(cfg.data_dir, STATE_FILE)
    try:
        st = os.stat(path)
        stamp: Optional[Tuple[int, int]] = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    cached = _state_cache.get(path)
    if cached is None or cached[0] != stamp:
        cached = (stamp, _read_json(path) if stamp is not None else {})
        _state_cache[path] = cached
    return cached[1]


def refusal(cfg: Any, environ: Dict[str, Any]) -> Optional[str]:
    """Why the app must not work now (its files can be downloaded), or None. Called for every web request: cheap."""
    if cfg.server.get("allow_data_in_web_root"):
        return None
    document_root = environ.get("DOCUMENT_ROOT")
    found = inside(cfg, document_root)
    if not found:
        return None
    script = environ.get("SCRIPT_FILENAME") or ""
    prepare(cfg, document_root or "", os.path.dirname(script) if script else document_root or "")
    st = state(cfg)
    if st.get("exposed") is True:
        return "%s can be downloaded from the web (%s)" % (found[0][0], st.get("url") or "checked by jobs.py cron")
    return None


def check_digest(cfg: Any, nonce: str) -> str:
    """What /health answers to ?check=NONCE: a digest only this installation can make (its own key, derived from
    the deployment secret and used for nothing else)."""
    key = hmac.new(cfg.secret.encode("utf-8"), b"tidesight web check", hashlib.sha256).digest()
    return hmac.new(key, nonce.encode("ascii", "replace"), hashlib.sha256).hexdigest()[:32]


def _fetch(url: str, timeout: float) -> Tuple[Optional[int], bytes]:
    import urllib.error
    import urllib.request
    request = urllib.request.Request(url, headers={"User-Agent": "Tidesight web check", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as res:
            return res.status, res.read(4096)
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def _loose(path: str, what: str) -> Optional[str]:
    """A file or folder others on the server may read."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return None
    if stat.S_IMODE(mode) & 0o077:
        return "%s (%s) can be read by other accounts: chmod %s it" % (what, path, "700" if stat.S_ISDIR(mode) else "600")
    return None


def _save(cfg: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    _write(os.path.join(cfg.data_dir, STATE_FILE), json.dumps(result) + "\n")
    return result


def _unverified(cfg: Any, reason: str, previous: Dict[str, Any]) -> Dict[str, Any]:
    """No verdict this time: the previous one stands (an exposure found earlier keeps the app stopped)."""
    result = {"checked_at": int(time.time() * 1000), "exposed": previous.get("exposed") is True,
              "unverified": reason, "verified_at": previous.get("verified_at"), "url": previous.get("url")}
    return _save(cfg, result)


def check(cfg: Any, fetch: Optional[Fetch] = None, timeout: float = 15.0) -> Dict[str, Any]:
    """Ask the website, as an outsider, for a file put in each of Tidesight's folders that lie inside the web
    server's folder; store and return the outcome. Run by `jobs.py cron`."""
    fetch = fetch or _fetch
    previous = state(cfg)
    if cfg.server.get("allow_data_in_web_root"):
        return _save(cfg, {"checked_at": int(time.time() * 1000), "exposed": False,
                           "skipped": "allow_data_in_web_root is set: not checked"})
    seen = _seen(cfg)
    if not seen:
        return _unverified(cfg, "no web request seen yet", previous)
    public = cfg.server.get("public_url") or ""
    parts = urlsplit(public)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return _unverified(cfg, "set [server] public_url to let Tidesight check that its files cannot be downloaded",
                           previous)
    base = public if public.endswith("/") else public + "/"
    origin = "%s://%s/" % (parts.scheme, parts.netloc)
    targets: List[Tuple[str, str, str]] = []  # (what, folder, url of a file in it, without the file name)
    for entry in seen:
        for what, path in inside(cfg, entry["document_root"]):
            folder = os.path.realpath(path if os.path.isdir(path) else os.path.dirname(path))
            # Addresses follow public_url from the entry script's folder; else the site's root, from the web server's.
            for root, prefix in ((entry["script_dir"], base), (entry["document_root"], origin)):
                rel = os.path.relpath(folder, os.path.realpath(root)).replace(os.sep, "/")
                if rel == "." or not rel.startswith(".."):
                    url = prefix + ("" if rel == "." else quote(rel) + "/")
                    if all(t[2] != url for t in targets):
                        targets.append((what, folder, url))
                    break
    if not targets:
        return _save(cfg, {"checked_at": int(time.time() * 1000), "exposed": False, "inside_web_root": False})
    nonce = secrets.token_hex(16)
    try:
        status, body = fetch(base + "health?check=" + nonce, timeout)
        answer = json.loads(body.decode("utf-8")).get("check") if status == 200 else None
    except Exception as exc:  # unreachable from here: no verdict
        log.warning("web check: %shealth unreachable: %s", base, exc)
        return _unverified(cfg, "%shealth could not be reached: %s" % (base, exc), previous)
    if answer != check_digest(cfg, nonce):
        return _unverified(cfg, "%s does not lead to this installation (its /health did not answer as expected): "
                                "check [server] public_url" % base, previous)
    result: Dict[str, Any] = {"checked_at": int(time.time() * 1000), "exposed": False, "folders": [], "warnings": []}
    for what, folder, url in targets:
        name = CANARY % secrets.token_hex(6)
        canary = os.path.join(folder, name)
        token = secrets.token_hex(16)
        try:
            _write(canary, token + "\n", 0o644)  # readable like any file one might upload: the rules alone are tested
            status, body = fetch(url + name + "?" + secrets.token_hex(4), timeout)
        except Exception as exc:
            log.warning("web check of %s failed: %s", url, exc)
            return _unverified(cfg, "%s%s: %s" % (url, name, exc), previous)
        finally:
            try:
                os.remove(canary)
            except OSError:
                pass
        exposed = status == 200 and token.encode("ascii") in body
        result["folders"].append({"what": what, "folder": folder, "url": url, "status": status, "exposed": exposed})
        if exposed and not result["exposed"]:
            result["exposed"], result["url"] = True, url
    for what, path in (("the configuration file", cfg.path), ("the data directory", cfg.data_dir)):
        if path and any(what == t[0] for t in targets):
            warning = _loose(path, what)
            if warning:
                result["warnings"].append(warning)
    result["verified_at"] = result["checked_at"]
    if result["exposed"]:
        log.error("Tidesight's files can be downloaded from %s: the collector and the dashboard refuse to work "
                  "until they cannot (docs/deploy-dreamhost.md, .htaccess)", result["url"])
    for warning in result["warnings"]:
        log.warning("web check: %s", warning)
    return _save(cfg, result)
