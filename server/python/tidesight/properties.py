"""Properties (sites) managed from the dashboard (standard library only: the collector loads them).

They live in properties.json in the data directory, in the same shape as the configuration file's [sites.<id>]
tables, and are merged with them at load (a site the configuration file defines wins, and stays read-only in
the dashboard). The collector picks changes up within seconds (ConfigHolder watches the file).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import time
from typing import Any, Callable, Dict, Iterator, List, Optional

log = logging.getLogger("tidesight.config")

FILE = "properties.json"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def path(data_dir: str) -> str:
    return os.path.join(data_dir, FILE)


def load_tables(data_dir: str) -> Dict[str, Dict[str, Any]]:
    """The managed site tables, {} when there are none (a broken file is reported and ignored)."""
    p = path(data_dir)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.error("%s could not be read (%s): its properties are ignored", p, exc)
        return {}
    sites = doc.get("sites") if isinstance(doc, dict) else None
    if not isinstance(sites, dict):
        return {}
    return {k: v for k, v in sites.items() if isinstance(k, str) and isinstance(v, dict)}


def save_tables(data_dir: str, tables: Dict[str, Dict[str, Any]]) -> None:
    """Write atomically (a reader never sees half a file), readable by this account only."""
    os.makedirs(data_dir, exist_ok=True)
    p = path(data_dir)
    tmp = "%s.%d.tmp" % (p, os.getpid())
    doc = {"format": "tidesight-properties/1", "updated_at": int(time.time() * 1000), "sites": tables}
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    for attempt in range(40):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:  # Windows: a reader, a backup or a sync tool holds the file for a moment
            if attempt == 39:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            time.sleep(0.05)


@contextlib.contextmanager
def locked(data_dir: str, timeout: float = 10.0) -> Iterator[None]:
    """One writer at a time (several dashboard processes may save at once): an exclusive lock on a lock file."""
    os.makedirs(data_dir, exist_ok=True)
    fh = open(os.path.join(data_dir, ".properties.lock"), "a+")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError("properties.json is locked by another process")
                time.sleep(0.05)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def update(data_dir: str, change: Callable[[Dict[str, Dict[str, Any]]], Any]) -> Any:
    """Read the tables, let `change` modify them in place, write them back: under the lock, from the file as it is
    now (not from a configuration loaded earlier), so concurrent edits are not lost."""
    with locked(data_dir):
        tables = load_tables(data_dir)
        result = change(tables)
        save_tables(data_dir, tables)
        return result


def mtime(data_dir: str) -> Optional[float]:
    try:
        return os.stat(path(data_dir)).st_mtime
    except OSError:
        return None


def hosts(origins: List[str]) -> List[str]:
    """Host names of a site's origins, most precise first (more labels first, wildcards last)."""
    out = []
    for o in origins or []:
        rest = str(o).split("://", 1)[-1].split("/", 1)[0].strip().lower()
        if rest.startswith("[") and "]" in rest:  # an IPv6 literal keeps its colons
            host = rest[:rest.index("]") + 1]
        else:
            host = rest.split(":", 1)[0]
        if host and host != "*":
            out.append(host)
    return sorted(dict.fromkeys(out), key=lambda h: (h.startswith("*"), -h.count("."), h))


def display_name(site_id: str, name: Optional[str], origins: List[str]) -> str:
    """What the dashboard calls a site: its name, else the most precise domain that sends it data (www. aside:
    example.com and www.example.com show as example.com), else its id."""
    if name and name != site_id:
        return name
    found = hosts(origins)
    plain = [h for h in found if not h.startswith("*")]
    if plain:
        bare = {h[4:] if h.startswith("www.") else h for h in plain}
        return sorted(bare, key=lambda h: (-h.count("."), h))[0]
    return found[0].lstrip("*.") if found else (name or site_id)


def slug(text: str) -> str:
    """A site id from a domain or a name: lower case, [a-z0-9-], at most 63 characters."""
    s = re.sub(r"[^a-z0-9]+", "-", text.strip().lower().split("://", 1)[-1].split("/", 1)[0]).strip("-")
    return (s or "site")[:63].strip("-") or "site"


def unique_id(base: str, taken: List[str]) -> str:
    candidate, n = base, 2
    while candidate in taken:
        suffix = "-%d" % n
        candidate = base[:63 - len(suffix)] + suffix
        n += 1
    return candidate


# An origin a property accepts: http(s), a host name (a leading "*." for its subdomains), an IPv4 or IPv6 address,
# or localhost, with an optional port.
ORIGIN_RE = re.compile(r"^https?://(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*"
                       r"(:[0-9]{1,5})?$|^https?://\[[0-9a-f:.]+\](:[0-9]{1,5})?$")


def valid_origin(origin: str) -> bool:
    """An origin a property may list: a host name with a dot (example.com, *.example.com), localhost, or an IP."""
    if not ORIGIN_RE.match(origin):
        return False
    host = origin.split("://", 1)[1]
    if host.startswith("["):
        return True
    host = host.split(":", 1)[0]
    if host.startswith("*."):  # subdomains of a name, never of a whole top-level domain (*.com)
        return "." in host[2:]
    return "." in host or host == "localhost"


def origin_from_domain(domain: str) -> str:
    """An origin (https://host) from what someone typed: a domain, a URL, or a wildcard (*.example.com). The result
    may still be invalid: check it with ORIGIN_RE."""
    d = domain.strip()
    if "://" not in d:
        d = "https://" + d
    scheme, rest = d.split("://", 1)
    host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip().lower().rstrip(".")
    if "@" in host:  # user:password@host
        host = host.rsplit("@", 1)[1]
    origin = "%s://%s" % (scheme.lower(), host)
    if origin.startswith("https://") and origin.endswith(":443"):
        origin = origin[:-4]
    elif origin.startswith("http://") and origin.endswith(":80"):
        origin = origin[:-3]
    try:  # internationalized domain names: their ASCII (punycode) form, as browsers send it
        head, _, port = host.partition(":") if not host.startswith("[") else (host, "", "")
        wildcard = head.startswith("*.")
        ascii_host = (head[2:] if wildcard else head).encode("idna").decode("ascii")
        origin = "%s://%s%s%s" % (scheme.lower(), "*." if wildcard else "", ascii_host, (":" + port) if port else "")
        if origin.endswith(":443") and origin.startswith("https://"):
            origin = origin[:-4]
        elif origin.endswith(":80") and origin.startswith("http://"):
            origin = origin[:-3]
    except (UnicodeError, ValueError):
        pass
    return origin
