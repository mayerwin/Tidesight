"""Small helpers shared by the enrichment modules."""

from __future__ import annotations

import json
from typing import Any, Optional, Tuple
from urllib.parse import urlsplit


def jdump(v: Any) -> Optional[str]:
    """Compact JSON, or None."""
    return None if v is None else json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def num(v: Any) -> Optional[float]:
    """A finite number from JSON data, or None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if v == v and v not in (float("inf"), float("-inf")) else None


def to_int(v: Any) -> Optional[int]:
    n = num(v)
    return int(n) if n is not None else None


def to_str(v: Any, n: int = 500) -> Optional[str]:
    if isinstance(v, str):
        return v[:n]
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return None


def url_parts(url: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """(host, path, query, fragment) of a URL; tolerant of junk."""
    if not url:
        return None, None, None, None
    try:
        p = urlsplit(url)
        host = p.hostname
    except ValueError:
        return None, None, None, None
    return (host or None), (p.path or "/"), (p.query or None), (p.fragment or None)


def b36(n: int) -> str:
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    n = abs(int(n))
    while n:
        n, r = divmod(n, 36)
        out = chars[r] + out
    return out or "0"
