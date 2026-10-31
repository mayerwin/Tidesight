"""Small helpers shared by the enrichment modules."""

from __future__ import annotations

import json
import math
from typing import Any, Optional, Tuple
from urllib.parse import urlsplit


def jdump(v: Any) -> Optional[str]:
    """Compact JSON, or None."""
    return None if v is None else json.dumps(v, ensure_ascii=False, separators=(",", ":"))


# Raw data is whatever a client sent: numbers can be 1e999 or have 400 digits. Anything a derived column
# receives goes through these helpers, which only ever return plain, bounded values.
MAX_SAFE = 2 ** 53


def num(v: Any) -> Optional[float]:
    """A finite number within +/- 2^53 from JSON data, or None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, int):
        return float(v) if -MAX_SAFE <= v <= MAX_SAFE else None
    return v if math.isfinite(v) and -MAX_SAFE <= v <= MAX_SAFE else None


def to_int(v: Any) -> Optional[int]:
    n = num(v)
    return int(n) if n is not None else None


def status_code(v: Any) -> Optional[int]:
    """A page status (e.g. 404) from a number or a numeric string, or None."""
    if isinstance(v, str):
        v = v.strip()
        if not (v.isascii() and v.isdecimal() and len(v) <= 3):
            return None
        v = int(v)
    n = to_int(v)
    return n if n is not None and 100 <= n <= 999 else None


def to_str(v: Any, n: int = 500) -> Optional[str]:
    """A string of at most n characters from a JSON string or number, else None."""
    if isinstance(v, str):
        return v[:n]
    if num(v) is not None:
        return str(v)[:n]
    return None


def _route_fragment(frag: str, hash_mode: str) -> Tuple[Optional[str], Optional[str]]:
    """(fragment_path, fragment_query) when the fragment counts as part of the page path."""
    if not frag or hash_mode == "off":
        return None, None
    if frag.startswith("/") or frag.startswith("!"):
        head, _, fquery = frag.partition("?")
        head = head.split("#", 1)[0]
        return (None if head in ("/", "!", "!/") else head), (fquery or None)
    if hash_mode == "all":
        return frag, None
    return None, None


def url_parts(url: Optional[str],
              hash_mode: str = "route") -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """(host, path, query, fragment) of a URL; tolerant of junk."""
    if not url or not isinstance(url, str):
        return None, None, None, None
    try:
        p = urlsplit(url)
        host = p.hostname
    except ValueError:
        return None, None, None, None
    if p.scheme.lower() not in ("http", "https") or not host:
        # A page is an absolute web URL. Anything else (".evil.example/x", "@evil.example") must not become a
        # path: the dashboard joins paths to the site's origin, e.g. to frame the page for a heatmap.
        return None, None, None, None
    base_path = p.path or "/"
    query = p.query or None
    frag = p.fragment or None
    frag_path, frag_query = _route_fragment(p.fragment, hash_mode)
    if frag_query:
        query = "%s&%s" % (query, frag_query) if query else frag_query
    path = "%s#%s" % (base_path, frag_path) if frag_path else base_path
    return host, path, query, frag


def b36(n: int) -> str:
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    n = abs(int(n))
    while n:
        n, r = divmod(n, 36)
        out = chars[r] + out
    return out or "0"
