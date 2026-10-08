"""Password hashing and HTTP Basic authentication helpers (standard library only).

Hashes look like ``pbkdf2_sha256$<iterations>$<salt>$<hash>`` (base64 salt and hash),
created with ``python -m tidesight.jobs hash-password``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Dict, Optional, Tuple

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
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header[6:].strip()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if ":" not in raw:
        return None
    user, password = raw.split(":", 1)
    return user, password


class Authenticator:
    """Checks Basic credentials against [dashboard] users, caching successful checks (PBKDF2 is slow)."""

    def __init__(self, users: Dict[str, str]) -> None:
        self.users = dict(users)
        self._ok: Dict[str, str] = {}

    def check(self, header: Optional[str]) -> Optional[str]:
        """The authenticated user name, or None."""
        creds = parse_basic(header)
        if creds is None:
            return None
        user, password = creds
        encoded = self.users.get(user)
        if not encoded:
            return None
        token = hashlib.sha256(("%s\0%s\0%s" % (user, password, encoded)).encode("utf-8")).hexdigest()
        if self._ok.get(user) == token:
            return user
        if verify_password(password, encoded):
            self._ok[user] = token
            return user
        return None
