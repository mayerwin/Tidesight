"""Password hashing, sign-in checks and their throttling (standard library only).

Hashes look like ``pbkdf2_sha256$<iterations>$<salt>$<hash>`` (base64 salt and hash),
created with ``python -m tidesight.jobs hash-password``. The dashboard signs in with a form (a session cookie,
see wsgi.py and webdb.py); scripts may send HTTP Basic credentials with each request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 600_000


def hash_password(password: str, iterations: int = ITERATIONS, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "%s$%d$%s$%s" % (ALGORITHM, iterations, base64.b64encode(salt).decode("ascii"),
                            base64.b64encode(digest).decode("ascii"))


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_b64, hash_b64 = encoded.split("$")
        if algorithm != ALGORITHM:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def parse_basic(header: Optional[str]) -> Optional[Tuple[str, str]]:
    """(user, password) from an Authorization: Basic header."""
    if not header or not header.lower().startswith("basic ") or len(header) > 4096:
        return None
    try:
        raw = base64.b64decode(header[6:].strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if ":" not in raw:
        return None
    user, password = raw.split(":", 1)
    return user, password


def password_tag(encoded: Optional[str]) -> str:
    """A short digest of a user's password hash: a login session is valid only while the password is unchanged."""
    return hashlib.sha256(("tidesight-session\0%s" % (encoded or "")).encode("utf-8")).hexdigest()[:24]


class Throttle:
    """Failed attempts per client, in memory (per process).

    A client that failed `max_failures` times within `window` seconds is refused without a password check
    until the window ends, so passwords cannot be guessed at speed. A password check first reserves an attempt
    (`reserve`: counted before the check, so concurrent guesses cannot all slip past the limit) and gives it back
    when the password was right (`forgive`)."""

    MAX_CLIENTS = 20000  # beyond, the oldest entries are forgotten: memory and time stay bounded under a flood

    def __init__(self, max_failures: int = 10, window: float = 600.0) -> None:
        self.max_failures = max_failures
        self.window = window
        # client -> (failures, first failure), in order of first failure: expired entries are always first.
        self._failures: "OrderedDict[str, Tuple[int, float]]" = OrderedDict()
        self._lock = threading.Lock()

    def _expire(self, now: float) -> None:
        while self._failures:
            since = next(iter(self._failures.values()))[1]
            if now - since <= self.window and len(self._failures) < self.MAX_CLIENTS:
                return
            self._failures.popitem(last=False)

    def blocked(self, client: str) -> bool:
        with self._lock:
            self._expire(time.monotonic())
            return self._failures.get(client, (0, 0.0))[0] >= self.max_failures

    def failed(self, client: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._expire(now)
            count, since = self._failures.pop(client, (0, now))
            self._failures[client] = (count + 1, since)  # re-inserted last: entries stay in order of `since`

    def reserve(self, client: str) -> bool:
        """Count an attempt now, unless the client is at its limit (then False): atomic, so a burst of concurrent
        attempts cannot all pass before any of them is counted."""
        with self._lock:
            now = time.monotonic()
            self._expire(now)
            count, since = self._failures.get(client, (0, now))
            if count >= self.max_failures:
                return False
            self._failures.pop(client, None)
            self._failures[client] = (count + 1, since)
            return True

    def forgive(self, client: str) -> None:
        """Give back one reserved attempt (the password was right)."""
        with self._lock:
            if client in self._failures:
                count, since = self._failures[client]
                self._failures[client] = (max(0, count - 1), since)


_DUMMY_HASH: List[str] = []


def _dummy_hash() -> str:
    """A hash to check unknown user names against, so they take as long as known ones."""
    if not _DUMMY_HASH:
        _DUMMY_HASH.append(hash_password(secrets.token_hex(16)))
    return _DUMMY_HASH[0]


class SharedThrottle:
    """A Throttle kept in memory and in web.db at once: blocked when either is. In memory, it costs nothing under
    Passenger; in web.db, it holds across processes (CGI starts one per request) and restarts."""

    def __init__(self, memory: Throttle, data_dir: str) -> None:
        from .webdb import WebDB
        self.memory = memory
        self.web = WebDB(data_dir)
        self.max_failures = memory.max_failures
        self.window = memory.window

    def blocked(self, client: str) -> bool:
        if self.memory.blocked(client):
            return True
        try:
            return self.web.throttle_blocked(client, self.max_failures, self.window)
        except Exception:  # web.db unavailable: the in-memory count still applies
            return False

    def failed(self, client: str) -> None:
        self.memory.failed(client)
        try:
            self.web.throttle_failed(client, self.window)
        except Exception:
            pass

    def reserve(self, client: str) -> bool:
        if not self.memory.reserve(client):
            return False
        try:
            if self.web.throttle_reserve(client, self.max_failures, self.window):
                return True
        except Exception:  # web.db unavailable: the in-memory count still applies
            return True
        self.memory.forgive(client)
        return False

    def forgive(self, client: str) -> None:
        self.memory.forgive(client)
        try:
            self.web.throttle_forgive(client)
        except Exception:
            pass


class Authenticator:
    """Checks credentials against [dashboard] users.

    Successful checks are cached (PBKDF2 costs a few hundred milliseconds of CPU, and the dashboard sends the
    credentials with every request). Uncached checks run one at a time per process: concurrent requests with
    the same new credentials wait for the first check and then use its result, and a flood of guesses cannot
    use more than one CPU; a request that waits more than `wait_seconds` is told to retry (throttled)."""

    def __init__(self, users: Dict[str, str], throttle: Optional[Throttle] = None, wait_seconds: float = 3.0) -> None:
        self.users = dict(users)
        self.throttle = throttle or Throttle()
        self.wait_seconds = wait_seconds
        self._ok: Dict[str, str] = {}
        self._checking = threading.Lock()

    def check(self, header: Optional[str], client: str = "-") -> Optional[str]:
        """The authenticated user name, or None."""
        return self.verify(header, client)[0]

    def _cached(self, user: str, token: str, encoded: Optional[str]) -> bool:
        return bool(encoded) and hmac.compare_digest(self._ok.get(user, ""), token)

    def verify(self, header: Optional[str], client: str = "-") -> Tuple[Optional[str], str]:
        """(user or None, "ok" | "denied" | "throttled") for an Authorization: Basic header."""
        creds = parse_basic(header)
        if creds is None:
            return None, "denied"
        return self.login(creds[0], creds[1], client)

    def login(self, user: str, password: str, client: str = "-") -> Tuple[Optional[str], str]:
        """(user or None, "ok" | "denied" | "throttled") for a user name and password."""
        if not isinstance(user, str) or not isinstance(password, str) or len(user) > 200 or len(password) > 1024:
            return None, "denied"
        try:  # a lone surrogate is not text: no password has one
            user.encode("utf-8"), password.encode("utf-8")
        except UnicodeEncodeError:
            return None, "denied"
        encoded = self.users.get(user)
        token = hashlib.sha256(("%s\0%s\0%s" % (user, password, encoded or "")).encode("utf-8")).hexdigest()
        if self._cached(user, token, encoded):
            return user, "ok"
        # The attempt counts before the (slow) check: a burst of concurrent guesses stops at the limit.
        if not self.throttle.reserve(client):
            return None, "throttled"
        if not self._checking.acquire(timeout=self.wait_seconds):
            self.throttle.forgive(client)  # not checked: not an attempt
            return None, "throttled"
        try:
            ok = self._cached(user, token, encoded)  # checked by a concurrent request meanwhile
            if not ok and verify_password(password, encoded or _dummy_hash()) and encoded:
                self._ok[user] = token
                ok = True
        finally:
            self._checking.release()
        if ok:
            self.throttle.forgive(client)
            return user, "ok"
        return None, "denied"
