"""Deciding the visitor's IP address, and the cookieless daily hash.

The collector stores REMOTE_ADDR and every forwarding header as received.
Which address is the visitor's depends on the deployment (direct, behind
Cloudflare, behind a load balancer), so it is decided here, at enrichment,
from [enrich] ip_header and trusted_proxies.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import time
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

IP_VERSION = 1

# Cloudflare's published edge ranges (https://www.cloudflare.com/ips/). With ip_header = "auto",
# CF-Connecting-IP is trusted only when the request came from one of these.
CLOUDFLARE = (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18",
    "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17",
    "162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32",
    "2a06:98c0::/29", "2c0f:f248::/32",
)

def _parse(ip: Optional[str]) -> Optional["ipaddress._BaseAddress"]:
    if not ip:
        return None
    ip = ip.strip().strip('"')
    if ip.startswith("[") and "]" in ip:  # [v6]:port
        ip = ip[1:ip.index("]")]
    elif ip.count(":") == 1 and "." in ip:  # v4:port
        ip = ip.split(":")[0]
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return None
    if addr.version == 6 and addr.ipv4_mapped:
        return addr.ipv4_mapped
    return addr


def normalize(ip: Optional[str]) -> Optional[str]:
    addr = _parse(ip)
    return str(addr) if addr else None


@lru_cache(maxsize=64)
def networks(cidrs: Tuple[str, ...]) -> Tuple:
    out = []
    for c in cidrs:
        try:
            out.append(ipaddress.ip_network(c.strip(), strict=False))
        except ValueError:
            continue
    return tuple(out)


def in_networks(ip: Optional[str], nets: Sequence) -> bool:
    addr = _parse(ip)
    return bool(addr) and any(addr.version == n.version and addr in n for n in nets)


def _is_private(ip: Optional[str]) -> bool:
    addr = _parse(ip)
    return bool(addr) and (addr.is_private or addr.is_loopback or addr.is_link_local)


def _xff(value: str) -> List[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


_FORWARDED_FOR = re.compile(r'for=("?\[?[^;,"\]]+\]?(?::\d+)?"?)', re.I)


def _forwarded(value: str) -> List[str]:
    return [m.group(1).strip('"') for m in _FORWARDED_FOR.finditer(value)]


def client_ip(remote_addr: Optional[str], headers: Dict[str, str], mode: str = "auto",
              trusted: Iterable[str] = ()) -> Tuple[Optional[str], str]:
    """Return (visitor IP, where it came from)."""
    trusted_nets = networks(tuple(trusted))
    remote = normalize(remote_addr)
    h = {k.lower(): v for k, v in (headers or {}).items() if isinstance(v, str)}

    def first_valid(candidates: List[str]) -> Optional[str]:
        for c in candidates:
            n = normalize(c)
            if n:
                return n
        return None

    if mode == "remote_addr":
        return remote, "remote_addr"
    if mode in ("cf-connecting-ip", "x-real-ip", "true-client-ip"):
        return (normalize(h.get(mode)) or remote), (mode if normalize(h.get(mode)) else "remote_addr")
    if mode == "x-forwarded-for":  # the client end of the chain
        ip = first_valid(_xff(h.get("x-forwarded-for", "")))
        return (ip or remote), ("x-forwarded-for" if ip else "remote_addr")
    if mode == "forwarded":
        ip = first_valid(_forwarded(h.get("forwarded", "")))
        return (ip or remote), ("forwarded" if ip else "remote_addr")

    # auto: trust forwarding headers only from proxies we know about.
    cf = normalize(h.get("cf-connecting-ip"))
    if cf and in_networks(remote, networks(CLOUDFLARE)):
        return cf, "cf-connecting-ip"
    if remote and (in_networks(remote, trusted_nets) or _is_private(remote)):
        chain = [normalize(x) for x in _xff(h.get("x-forwarded-for", ""))]
        chain = [x for x in chain if x]
        # Walk from the nearest hop: the first address that is not itself a trusted proxy.
        for ip in reversed(chain):
            if not (in_networks(ip, trusted_nets) or _is_private(ip)):
                return ip, "x-forwarded-for"
        real = normalize(h.get("x-real-ip"))
        if real:
            return real, "x-real-ip"
        if chain:
            return chain[0], "x-forwarded-for"
    return remote, "remote_addr"


def day_salt(secret: str, day: str) -> bytes:
    """A salt that changes every UTC day and is derived from the deployment secret."""
    return hmac.new(secret.encode("utf-8"), ("tidesight-salt:" + day).encode("utf-8"), hashlib.sha256).digest()


def cookieless_hash(secret: str, received_ms: int, site_id: Optional[str], ip: Optional[str],
                    ua: Optional[str]) -> Optional[str]:
    """Plausible-style daily visitor hash: salt(day) + site + IP + user agent."""
    if not ip:
        return None
    day = time.strftime("%Y-%m-%d", time.gmtime(received_ms / 1000.0))
    data = "\n".join([site_id or "", ip, ua or ""]).encode("utf-8")
    return hmac.new(day_salt(secret, day), data, hashlib.sha256).hexdigest()[:20]
