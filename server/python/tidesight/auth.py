"""Password hashing and HTTP Basic authentication helpers (standard library only).

Hashes look like ``pbkdf2_sha256$<iterations>$<salt>$<hash>`` (base64 salt and hash),
created with ``python -m tidesight.jobs hash-password``.
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


class Throttle:
    """Failed attempts per client, in memory (per process).

    A client that failed `max_failures` times within `window` seconds is refused without a password check
    until the window ends, so passwords cannot be guessed at speed."""

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


_DUMMY_HASH: List[str] = []


def _dummy_hash() -> str:
    """A hash to check unknown user names against, so they take as long as known ones."""
    if not _DUMMY_HASH:
        _DUMMY_HASH.append(hash_password(secrets.token_hex(16)))
    return _DUMMY_HASH[0]


class Authenticator:
    """Checks Basic credentials against [dashboard] users.

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
        """(user or None, "ok" | "denied" | "throttled")."""
        creds = parse_basic(header)
        if creds is None:
            return None, "denied"
        user, password = creds
        encoded = self.users.get(user)
        token = hashlib.sha256(("%s\0%s\0%s" % (user, password, encoded or "")).encode("utf-8")).hexdigest()
        if self._cached(user, token, encoded):
            return user, "ok"
        if self.throttle.blocked(client):
            return None, "throttled"
        if not self._checking.acquire(timeout=self.wait_seconds):
            return None, "throttled"
        try:
            if self._cached(user, token, encoded):  # checked by a concurrent request meanwhile
                return user, "ok"
            if verify_password(password, encoded or _dummy_hash()) and encoded:
                self._ok[user] = token
                return user, "ok"
        finally:
            self._checking.release()
        self.throttle.failed(client)
        return None, "denied"
