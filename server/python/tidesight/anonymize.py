"""IP address anonymisation (standard library only).

Used by the collector when a privacy switch says so ([privacy] hash_ip /
truncate_ip) and by `jobs.py privacy-clean` to rewrite stored data.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
from typing import Dict, Optional

# Request headers that can carry client IP addresses.
IP_HEADERS = (
    "x-forwarded-for", "x-real-ip", "forwarded", "cf-connecting-ip", "true-client-ip",
    "x-client-ip", "x-cluster-client-ip", "fastly-client-ip", "x-original-forwarded-for",
    "x-forwarded", "forwarded-for", "x-appengine-user-ip", "cf-connecting-ipv6", "via",
)
IP_HEADER_PARTS = ("ip", "forwarded", "client", "addr")

HASH_PREFIX = "h:"

# Candidate IPv4/IPv6 tokens inside header values (validated with ipaddress).
_IP_TOKEN = re.compile(r"\[?([0-9A-Fa-f:.]{2,45}(?:%[0-9A-Za-z]+)?)\]?")


def truncate_ip(ip: str) -> str:
    """Keep the /24 network of an IPv4 address or the /48 of an IPv6 address (the /40 of a 6to4 address,
    2002::/16, whose next 32 bits are an IPv4 address: its /24, as for IPv4)."""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return ip
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    prefix = 24 if addr.version == 4 else 40 if addr.sixtofour is not None else 48
    net = ipaddress.ip_network("%s/%d" % (addr, prefix), strict=False)
    return str(net.network_address)


def hash_ip(ip: str, secret: str) -> str:
    """A keyed, non-reversible stand-in for an IP address ('h:' + 16 hex chars)."""
    if ip.startswith(HASH_PREFIX):
        return ip
    digest = hmac.new(secret.encode("utf-8"), ip.encode("utf-8"), hashlib.sha256).hexdigest()
    return HASH_PREFIX + digest[:16]


def anonymize_ip(ip: Optional[str], do_hash: bool, do_truncate: bool, secret: str) -> Optional[str]:
    """Apply the configured transformations: truncate first, then hash."""
    if not ip:
        return ip
    out = ip
    if do_truncate:
        out = truncate_ip(out)
    if do_hash:
        out = hash_ip(out, secret)
    return out


def _is_ip(token: str) -> bool:
    try:
        ipaddress.ip_address(token.split("%", 1)[0])
        return True
    except ValueError:
        return False


def anonymize_header_value(value: str, do_hash: bool, do_truncate: bool, secret: str) -> str:
    """Replace every IP address inside a header value (lists, Forwarded syntax, ports)."""
    def repl(m: "re.Match[str]") -> str:
        token = m.group(1)
        candidate = token
        # "1.2.3.4:5678" (IPv4 with port) — keep the port out of the address.
        port = ""
        if candidate.count(":") == 1 and "." in candidate:
            candidate, port = candidate.split(":")
            port = ":" + port
        if not _is_ip(candidate):
            return m.group(0)
        new = anonymize_ip(candidate, do_hash, do_truncate, secret) or candidate
        return m.group(0).replace(token, new + port)
    return _IP_TOKEN.sub(repl, value)


def ip_bearing(name: str) -> bool:
    """Whether a header (lower-case name) may carry client addresses: the known ones, and any name containing
    "ip", "forwarded", "client" or "address" (proxies and CDNs invent their own: x-originating-ip,
    fly-client-ip, cloudfront-viewer-address, x-vercel-forwarded-for…). Only tokens that parse as IP
    addresses are changed in them."""
    return name in IP_HEADERS or any(part in name for part in IP_HEADER_PARTS)


def anonymize_headers(headers: Dict[str, str], do_hash: bool, do_truncate: bool, secret: str) -> Dict[str, str]:
    """A copy of the header map with addresses in IP-bearing headers anonymised."""
    if not (do_hash or do_truncate):
        return headers
    out = dict(headers)
    for name, value in headers.items():
        if isinstance(value, str) and ip_bearing(name):
            out[name] = anonymize_header_value(value, do_hash, do_truncate, secret)
    return out
