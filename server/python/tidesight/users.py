"""Dashboard users kept in the data directory (standard library only: the configuration loads them).

`python -m tidesight.jobs user set NAME` adds a user, or changes a password, without editing the configuration
file: users.json in the data directory maps each name to a password hash (PBKDF2, never the password itself). At
load they are merged with the configuration file's [dashboard] users, which win: a user defined there is changed
there. The web app picks changes up within seconds (ConfigHolder watches the file), and a changed password ends
the user's sessions.

The first user can also be created in the dashboard, with the setup code of setup-code.txt in the data
directory: only someone who can read the server's files has it, so a stranger who finds a new installation first
cannot claim it (bots watch for new HTTPS certificates and try setup pages within minutes). The code stops
working once a user exists.
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
import time
from typing import Any, Callable, Dict, Optional

from . import properties

FILE = "users.json"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}\Z")  # used with match(): \Z, so "name\n" fails
HASH_PREFIX = "pbkdf2_sha256$"
MIN_PASSWORD = 8
SETUP_FILE = "setup-code.txt"
SETUP_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford's base 32: no I, L, O or U to misread
SETUP_LENGTH = 16
_SETUP_TYPOS = str.maketrans({"O": "0", "I": "1", "L": "1"})


class UsersError(Exception):
    """users.json exists but cannot be read or parsed: nothing may be written over it."""


def path(data_dir: str) -> str:
    return os.path.join(data_dir, FILE)


def read(data_dir: str) -> Dict[str, str]:
    """{name: password hash} ({} without a file). A file that cannot be read or parsed raises UsersError."""
    table = properties.read_json_table(path(data_dir), "users", UsersError)
    return {k: v for k, v in table.items() if isinstance(v, str) and v.startswith(HASH_PREFIX)}


def update(data_dir: str, change: Callable[[Dict[str, str]], Any]) -> Any:
    """Read the users, let `change` modify them in place, write them back, under a lock."""
    with properties.locked(data_dir, name=".users.lock"):
        table = read(data_dir)  # a file that cannot be read is never written over
        result = change(table)
        properties.write_json(path(data_dir), {"format": "tidesight-users/1",
                                               "updated_at": int(time.time() * 1000), "users": table})
        return result


def mtime(data_dir: str) -> Optional[float]:
    try:
        return os.stat(path(data_dir)).st_mtime
    except OSError:
        return None


def setup_path(data_dir: str) -> str:
    return os.path.join(data_dir, SETUP_FILE)


def _normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper().translate(_SETUP_TYPOS) if ch.isalnum())


def _valid_code(normalized: str) -> bool:
    return len(normalized) == SETUP_LENGTH and all(ch in SETUP_ALPHABET for ch in normalized)


def _read_code(p: str) -> Optional[str]:
    """The code in the file; None without a file, "" when the file holds no valid code (empty, damaged)."""
    try:
        with open(p, "r", encoding="ascii", errors="replace") as fh:
            code = fh.read(200).strip()
    except FileNotFoundError:
        return None
    except OSError:
        return ""
    return code if _valid_code(_normalize_code(code)) else ""


def setup_code(data_dir: str, create: bool = True) -> Optional[str]:
    """The setup code (data_dir/setup-code.txt, readable by this account only), created when missing or damaged:
    16 random characters of Crockford's base 32 (80 bits), shown in groups of four. None when there is none and
    `create` is false, or when the file cannot be written."""
    p = setup_path(data_dir)
    for _ in range(5):
        code = _read_code(p)
        if code or not create:
            return code or None
        if code == "":  # damaged: replaced
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
            except OSError:
                return None
        code = "-".join("".join(secrets.choice(SETUP_ALPHABET) for _ in range(4)) for _ in range(4))
        # Written in full under another name, then linked into place: the file is never seen half written, and
        # the link fails if another process put a code there meanwhile (that one is used).
        tmp = "%s.%d.%s.tmp" % (p, os.getpid(), secrets.token_hex(4))
        try:
            os.makedirs(data_dir, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(code + "\n")
            try:
                os.link(tmp, p)
            except FileExistsError:
                continue
            except OSError:  # a filesystem without hard links
                os.replace(tmp, p)
            return code
        except OSError:
            return None
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return None


def setup_code_matches(data_dir: str, given: Any) -> bool:
    """Whether `given` is the setup code (case, spaces, dashes and the look-alikes O/0, I/L/1 do not matter). A
    missing or damaged code matches nothing."""
    code = setup_code(data_dir, create=False)
    if not code or not isinstance(given, str) or len(given) > 100:
        return False
    typed = _normalize_code(given)
    if not _valid_code(typed):
        return False
    return hmac.compare_digest(typed.encode("ascii"), _normalize_code(code).encode("ascii"))


def remove_setup_code(data_dir: str) -> None:
    """Once a user exists, the setup code has no use left: it is deleted (a code left over by a failed delete is
    harmless: setting up is refused as soon as a user exists)."""
    try:
        os.remove(setup_path(data_dir))
    except OSError:
        pass
