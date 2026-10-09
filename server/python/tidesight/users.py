"""Dashboard users kept in the data directory (standard library only: the configuration loads them).

`python -m tidesight.jobs user set NAME` adds a user, or changes a password, without editing the configuration
file: users.json in the data directory maps each name to a password hash (PBKDF2, never the password itself). At
load they are merged with the configuration file's [dashboard] users, which win: a user defined there is changed
there. The web app picks changes up within seconds (ConfigHolder watches the file), and a changed password ends
the user's sessions.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Callable, Dict, Optional

from . import properties

FILE = "users.json"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}\Z")  # used with match(): \Z, so "name\n" fails
HASH_PREFIX = "pbkdf2_sha256$"
MIN_PASSWORD = 8


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
