"""URL redaction: secrets in URLs (login links, reset tokens, signed URLs…) become "redacted".

The tracker applies these rules to every URL it sends (tracker/src/core.js, `redactUrl`); the collector applies
them to the Referer header it stores, which a browser may fill with the full page URL (beacons from older
browsers, the pixel). Every connector implements exactly this algorithm (docs/connectors.md, "URL redaction");
the test vectors in tests/redact_vectors.json fix it. It runs in linear time whatever the URL: no regular
expression is built from the patterns.

Patterns are globs where only `*` is special (any run of characters, `/` included); ASCII letters match
case-insensitively. With the site's `redact_params` (parameter names) and `redact_paths` (paths):

- URLs with a scheme other than http, https, ws and wss (data:, mailto:, javascript:…) are left as they are.
- Credentials before the host (`https://user:pass@host/`) become `redacted@host`.
- A parameter whose name matches `redact_params` keeps its name and gets the value `redacted`: in the query
  string, in a fragment made of parameters (`#access_token=…&state=…`), in a hash route's query
  (`#/path?a=b`) and in path parameters (`/shop;jsessionid=…`). Names are percent-decoded first (`+` read as a
  space), as the browser's decodeURIComponent does, unless malformed (then they are compared as they are); a
  name longer than 256 characters always has its value redacted.
- A parameter value that is itself a URL or a query with a secret in it (a login page's
  `?next=%2Freset%3Ftoken%3D…`) is redacted whole: the value, decoded the same way, goes through these rules
  (as a URL, and as a query when it has no `?` or `#` but an `=`), two levels deep at most.
- The path, and a hash route's path (a fragment starting with `/` or `!/`), are compared with `redact_paths`:
  the first pattern that matches the whole path applies, and each non-empty part a `*` matched becomes
  `redacted` (`/reset-password/*` turns `/reset-password/abc123` into `/reset-password/redacted`).
Standard library only (imported by the collector).
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple

REDACTED = "redacted"
MAX_NAME = 256
MAX_NESTING = 2  # how deep parameter values are looked into (a URL in a URL in a URL)
SCHEMES = ("http", "https", "ws", "wss")

_FOLD = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}  # ASCII A-Z to a-z: lengths never change
_HEX = frozenset("0123456789abcdefABCDEF")
_SCHEME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+.-")


def fold(s: str) -> str:
    """ASCII lower case (other characters unchanged, so positions stay the same)."""
    return s.translate(_FOLD)


def glob_spans(pattern: str, text: str, case_insensitive: bool = True) -> Optional[List[Tuple[int, int]]]:
    """Where each `*` of the glob matched, as (start, end) positions in text, if the glob matches the whole
    text; else None. The pieces between stars are taken at their first occurrence, left to right, which finds a
    match whenever there is one; each piece is searched once, so the time is linear in the text."""
    if case_insensitive:
        pattern, text = fold(pattern), fold(text)
    parts = pattern.split("*")
    if len(parts) == 1:
        return [] if pattern == text else None
    head, tail = parts[0], parts[-1]
    end = len(text) - len(tail)
    if end < len(head) or not text.startswith(head) or not text.endswith(tail):
        return None
    spans = []
    pos = len(head)
    for piece in parts[1:-1]:
        at = text.find(piece, pos, end)
        if at < 0:
            return None
        spans.append((pos, at))
        pos = at + len(piece)
    spans.append((pos, end))
    return spans


def glob_match(pattern: str, text: str, case_insensitive: bool = True) -> bool:
    return glob_spans(pattern, text, case_insensitive) is not None


def patterns(value: Any) -> List[str]:
    """The strings of a configured pattern list (anything else is ignored)."""
    return [p for p in value if isinstance(p, str)] if isinstance(value, list) else []


def _is_scheme(s: str) -> bool:
    return bool(s) and s[0].isascii() and s[0].isalpha() and all(c in _SCHEME_CHARS for c in s)


def decode_name(name: str) -> str:
    """A parameter name (or value) as decodeURIComponent(name.replace(/\\+/g, " ")) reads it, or unchanged when
    that would fail (a `%` without two hex digits after it, or escapes that are not UTF-8)."""
    plus = name.replace("+", " ")
    if "%" not in plus:
        return plus
    out = []
    i, n = 0, len(plus)
    while i < n:
        if plus[i] != "%":
            j = plus.find("%", i)
            j = n if j < 0 else j
            out.append(plus[i:j])
            i = j
            continue
        run = bytearray()
        while i < n and plus[i] == "%":
            pair = plus[i + 1:i + 3]
            if len(pair) < 2 or pair[0] not in _HEX or pair[1] not in _HEX:
                return name
            run.append(int(pair, 16))
            i += 3
        try:
            out.append(run.decode("utf-8"))
        except UnicodeDecodeError:
            return name
    return "".join(out)


def _secret_inside(value: str, names: Sequence[str], paths: Sequence[str], depth: int) -> bool:
    """True when a parameter value, decoded, is a URL or a query that these rules would change."""
    inner = decode_name(value)
    if _redact(inner, names, paths, depth) != inner:
        return True
    return "?" not in inner and "#" not in inner and "=" in inner and _redact_pairs(inner, "&", names, paths,
                                                                                    depth) != inner


def _redact_pairs(s: str, sep: str, names: Sequence[str], paths: Sequence[str], depth: int) -> str:
    out = []
    for pair in s.split(sep):
        i = pair.find("=")
        if i >= 1:
            raw = pair[:i]
            if names and (len(raw) > MAX_NAME or any(glob_match(p, decode_name(raw)) for p in names)):
                pair = pair[:i + 1] + REDACTED
            elif depth < MAX_NESTING and _secret_inside(pair[i + 1:], names, paths, depth + 1):
                pair = pair[:i + 1] + REDACTED
        out.append(pair)
    return sep.join(out)


def _redact_path(path: str, names: Sequence[str], paths: Sequence[str], depth: int) -> str:
    if ";" in path:
        segments = []
        for seg in path.split("/"):
            i = seg.find(";")
            segments.append(seg if i < 0 else seg[:i + 1] + _redact_pairs(seg[i + 1:], ";", names, paths, depth))
        path = "/".join(segments)
    for pattern in paths:
        spans = glob_spans(pattern, path)
        if spans is not None:
            for start, end in reversed(spans):
                if end > start:
                    path = path[:start] + REDACTED + path[end:]
            break
    return path


def _redact_fragment(frag: str, names: Sequence[str], paths: Sequence[str], depth: int) -> str:
    q = frag.find("?")
    head = frag if q < 0 else frag[:q]
    if head.startswith("/") or head.startswith("!/"):
        k = 1 if head.startswith("!") else 0
        head = head[:k] + _redact_path(head[k:], names, paths, depth)
    elif q < 0:
        return _redact_pairs(frag, "&", names, paths, depth) if frag.find("=") > 0 else frag
    if q < 0:
        return head
    return head + "?" + _redact_pairs(frag[q + 1:], "&", names, paths, depth)


def _redact(url: str, names: Sequence[str], paths: Sequence[str], depth: int) -> str:
    h = url.find("#")
    main = url if h < 0 else url[:h]
    q = main.find("?")
    base = main if q < 0 else main[:q]
    c = base.find(":")
    start = -1
    if c > 0 and _is_scheme(base[:c]):
        if fold(base[:c]) not in SCHEMES:
            return url
        if base[c:c + 3] == "://":
            start = c + 3
    elif base.startswith("//"):
        start = 2
    origin, path = "", base
    if start >= 0:
        j = base.find("/", start)
        origin, path = (base, "") if j < 0 else (base[:j], base[j:])
        at = origin.rfind("@")
        if at > start:
            origin = origin[:start] + REDACTED + origin[at:]
    out = origin + _redact_path(path, names, paths, depth)
    if q >= 0:
        out += "?" + _redact_pairs(main[q + 1:], "&", names, paths, depth)
    if h >= 0:
        out += "#" + _redact_fragment(url[h + 1:], names, paths, depth)
    return out


def redact_url(url: Any, names: Sequence[str], paths: Sequence[str]) -> Any:
    """The URL with the secrets the patterns describe replaced (see the module docstring)."""
    if not isinstance(url, str) or not url or (not names and not paths):
        return url
    return _redact(url, names, paths, 0)
