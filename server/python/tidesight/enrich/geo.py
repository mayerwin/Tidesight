"""Geolocation from offline MaxMind DB files, normalised across providers.

:class:`GeoLookup` finds the databases in a folder (``enrich.geo_dir``),
opens the newest file of each kind, and answers ``lookup(ip)`` with one
normalised record per provider, so several providers can be stored side by
side::

    {"maxmind": {"country": "FR", "city": "Paris", "asn": 3215, ...},
     "dbip": {...}}

Recognised file names (case-insensitive):

==========  ===========  ==================================================
provider    kind         file names
==========  ===========  ==================================================
maxmind     city         GeoLite2-City.mmdb, GeoIP2-City.mmdb
maxmind     country      GeoLite2-Country.mmdb, GeoIP2-Country.mmdb
maxmind     asn          GeoLite2-ASN.mmdb, GeoIP2-ISP.mmdb
dbip        city         dbip-city-lite*.mmdb
dbip        country      dbip-country-lite*.mmdb
dbip        asn          dbip-asn-lite*.mmdb
ipinfo      country_asn  ipinfo_lite.mmdb, ipinfo-lite.mmdb
==========  ===========  ==================================================

When several files match the same provider and kind, the one with the newest
``build_epoch`` wins. A country database is only used when its provider has
no city database. A provider's location and ASN answers are merged into one
record whose keys are :data:`RECORD_KEYS` (None when unknown).

Results depend only on the database files and :data:`GEO_VERSION`, which
:meth:`GeoLookup.version` combines, so derived rows can be recomputed when
either changes. Missing folders and corrupt files are logged and skipped:
geolocation degrades, enrichment never fails because of it.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import logging
import math
import os
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple, Union

from . import mmdb

__all__ = ["GEO_VERSION", "RECORD_KEYS", "GeoLookup", "is_public_ip"]

GEO_VERSION = 1  # bump when the normalisation below changes

RECORD_KEYS = (
    "country",  # ISO 3166-1 alpha-2
    "country_name",  # English
    "continent",  # two-letter code: AF AN AS EU NA OC SA
    "is_eu",  # bool, or None when the provider does not say
    "region",  # ISO 3166-2 code of the first subdivision, without the country ("IDF")
    "region_name",
    "city",
    "postal",
    "lat",
    "lon",
    "accuracy_km",
    "tz",  # IANA time zone
    "asn",  # int
    "as_org",
    "network",  # CIDR of the matched network (the location answer's when there is one)
)

logger = logging.getLogger(__name__)

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]

_FILE_PATTERNS: Tuple[Tuple[str, str, str], ...] = (
    ("geolite2-city.mmdb", "maxmind", "city"),
    ("geoip2-city.mmdb", "maxmind", "city"),
    ("geolite2-country.mmdb", "maxmind", "country"),
    ("geoip2-country.mmdb", "maxmind", "country"),
    ("geolite2-asn.mmdb", "maxmind", "asn"),
    ("geoip2-isp.mmdb", "maxmind", "asn"),
    ("dbip-city-lite*.mmdb", "dbip", "city"),
    ("dbip-country-lite*.mmdb", "dbip", "country"),
    ("dbip-asn-lite*.mmdb", "dbip", "asn"),
    ("ipinfo_lite.mmdb", "ipinfo", "country_asn"),
    ("ipinfo-lite.mmdb", "ipinfo", "country_asn"),
)

# Location kinds come first, so the network of the location answer wins.
_KIND_ORDER = ("city", "country", "country_asn", "asn")

_OPEN_ERRORS = (OSError, ValueError, MemoryError, mmdb.InvalidDatabaseError)


def _ranges(*cidrs: str) -> Tuple[Tuple[int, int], ...]:
    networks = [ipaddress.ip_network(cidr) for cidr in cidrs]
    return tuple((int(network.network_address), int(network.netmask)) for network in networks)


# Explicit lists rather than ipaddress.is_global/is_private, whose answers
# differ between Python versions: results must not depend on the interpreter.
_NON_PUBLIC_V4 = _ranges(
    "0.0.0.0/8",  # "this network", including the unspecified address
    "10.0.0.0/8",  # private
    "100.64.0.0/10",  # shared address space (carrier-grade NAT)
    "127.0.0.0/8",  # loopback
    "169.254.0.0/16",  # link-local
    "172.16.0.0/12",  # private
    "192.0.0.0/24",  # IETF protocol assignments
    "192.0.2.0/24",  # documentation (TEST-NET-1)
    "192.168.0.0/16",  # private
    "198.18.0.0/15",  # benchmarking
    "198.51.100.0/24",  # documentation (TEST-NET-2)
    "203.0.113.0/24",  # documentation (TEST-NET-3)
    "224.0.0.0/4",  # multicast
    "240.0.0.0/4",  # reserved, including the broadcast address
)
# IPv6 is public only inside global unicast 2000::/3 (which excludes
# loopback, unspecified, ULA fc00::/7, link-local, multicast and the other
# reserved blocks), minus these special-purpose blocks.
_NON_PUBLIC_V6 = _ranges(
    "2001::/23",  # IETF protocol assignments (Teredo, benchmarking, ORCHID, ...)
    "2001:db8::/32",  # documentation
    "3fff::/20",  # documentation
)


def is_public_ip(ip: Any) -> bool:
    """True when *ip* is a valid, globally routable unicast address.

    False for private, loopback, link-local, carrier-grade NAT (100.64/10),
    multicast, reserved, unspecified and documentation ranges, IPv6 unique
    local addresses (fc00::/7), and anything that is not an address string.
    IPv4-mapped IPv6 addresses (``::ffff:a.b.c.d``) are judged as IPv4.
    """
    return _public_address(ip) is not None


class GeoLookup:
    """Looks up addresses in every geolocation database found in a folder.

    *geo_dir* is scanned for the recognised file names; *files*, when given,
    replaces the scan with an explicit list of paths (relative ones are
    resolved against *geo_dir*). Neither has to exist: without databases,
    every lookup returns ``{}``. With ``use_mmap=True`` the pure-Python
    reader maps files instead of reading them into memory. Like the readers
    it holds, a GeoLookup is not thread-safe.
    """

    def __init__(
        self,
        geo_dir: Optional[str],
        files: Optional[List[str]] = None,
        *,
        use_mmap: bool = False,
    ) -> None:
        self.geo_dir = geo_dir
        self._databases: List[_Database] = []
        candidates = _discover(geo_dir, files)
        for provider in sorted({candidate.provider for candidate in candidates}):
            self._databases.extend(_open_provider(provider, candidates, use_mmap))
        self._by_provider: Dict[str, List[_Database]] = {}
        for database in self._databases:
            self._by_provider.setdefault(database.provider, []).append(database)

    @property
    def providers(self) -> List[str]:
        """Providers with at least one database loaded, sorted."""
        return list(self._by_provider)

    @property
    def databases(self) -> List[Dict[str, Any]]:
        """The loaded databases: provider, kind, path, database_type, build_epoch."""
        return [
            {
                "provider": database.provider,
                "kind": database.kind,
                "path": database.path,
                "database_type": database.database_type,
                "build_epoch": database.build_epoch,
            }
            for database in self._databases
        ]

    def version(self) -> str:
        """Identify the normalisation and the data it uses.

        For example ``"1;dbip.city=1759276800;maxmind.asn=1759305600"``: it
        changes when GEO_VERSION is bumped or any database is replaced by
        another build, which is the signal to recompute stored geo data.
        """
        parts = [str(GEO_VERSION)]
        parts.extend(f"{db.provider}.{db.kind}={db.build_epoch}" for db in self._databases)
        return ";".join(parts)

    def lookup(self, ip: str) -> Dict[str, Dict[str, Any]]:
        """Return ``{provider: record}`` for *ip*.

        Providers that know nothing about the address are left out; the
        result is ``{}`` for invalid or non-public addresses, or when no
        database is loaded.
        """
        address = _public_address(ip)
        if address is None or not self._by_provider:
            return {}
        query = str(address)
        results: Dict[str, Dict[str, Any]] = {}
        for provider, databases in self._by_provider.items():
            record = _lookup_provider(databases, address, query)
            if record is not None:
                results[provider] = record
        return results

    def close(self) -> None:
        """Close every database; later lookups return ``{}``."""
        databases, self._databases, self._by_provider = self._databases, [], {}
        for database in databases:
            database.reader.close()

    def __enter__(self) -> "GeoLookup":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<GeoLookup {self.version()!r}>"


class _Candidate(NamedTuple):
    provider: str
    kind: str
    path: str
    build_epoch: int


class _Database:
    """An open database and what it provides."""

    def __init__(self, provider: str, kind: str, path: str, reader: mmdb.ReaderLike) -> None:
        self.provider = provider
        self.kind = kind
        self.path = path
        self.reader = reader
        self.database_type = reader.metadata.database_type
        self.build_epoch = reader.metadata.build_epoch
        self.failed = False  # a corrupt-data warning was logged already


def _discover(geo_dir: Optional[str], files: Optional[List[str]]) -> List[_Candidate]:
    """Recognised database files with readable metadata."""
    candidates = []
    for path, explicit in _candidate_paths(geo_dir, files):
        match = _classify(path)
        if match is None:
            if explicit:
                logger.warning("geo: ignoring %s: not a recognised database file name", path)
            continue
        try:
            metadata = mmdb.read_metadata(path)
        except (OSError, mmdb.InvalidDatabaseError) as exc:
            logger.warning("geo: skipping %s: %s", path, exc)
            continue
        candidates.append(_Candidate(match[0], match[1], path, metadata.build_epoch))
    return candidates


def _candidate_paths(geo_dir: Optional[str], files: Optional[List[str]]) -> List[Tuple[str, bool]]:
    """``(path, explicitly listed)`` pairs to consider."""
    if files is not None:
        base = geo_dir or ""
        return [(os.path.abspath(os.path.join(base, name)), True) for name in files]
    if not geo_dir:
        return []
    try:
        names = sorted(os.listdir(geo_dir))
    except OSError as exc:
        logger.warning("geo: cannot read geo_dir %s: %s", geo_dir, exc)
        return []
    paths = (os.path.abspath(os.path.join(geo_dir, name)) for name in names)
    return [(path, False) for path in paths if os.path.isfile(path)]


def _classify(path: str) -> Optional[Tuple[str, str]]:
    """``(provider, kind)`` for a recognised file name, else None."""
    name = os.path.basename(path).lower()
    for pattern, provider, kind in _FILE_PATTERNS:
        if fnmatch.fnmatchcase(name, pattern):
            return provider, kind
    return None


def _open_provider(provider: str, candidates: List[_Candidate], use_mmap: bool) -> List[_Database]:
    """Open the newest working database of each kind for *provider*."""
    opened: List[_Database] = []
    kinds = {candidate.kind for candidate in candidates if candidate.provider == provider}
    for kind in _KIND_ORDER:
        if kind not in kinds or (kind == "country" and any(db.kind == "city" for db in opened)):
            continue
        group = [c for c in candidates if c.provider == provider and c.kind == kind]
        group.sort(key=lambda c: (c.build_epoch, os.path.basename(c.path).lower(), c.path), reverse=True)
        for candidate in group:
            try:
                reader = mmdb.open_reader(candidate.path, use_mmap=use_mmap)
            except _OPEN_ERRORS as exc:
                logger.warning("geo: skipping %s: %s", candidate.path, exc)
                continue
            opened.append(_Database(provider, kind, candidate.path, reader))
            break
    return opened


def _lookup_provider(
    databases: List[_Database], address: IPAddress, query: str
) -> Optional[Dict[str, Any]]:
    """Merge the answers of one provider's databases into one record."""
    record: Optional[Dict[str, Any]] = None
    for database in databases:
        data, prefix_len = _query(database, query)
        if data is None:
            continue
        if record is None:
            record = dict.fromkeys(RECORD_KEYS)
        for key, value in _NORMALISERS[database.kind](data).items():
            if record[key] is None:
                record[key] = value
        if record["network"] is None:
            record["network"] = _network(address, prefix_len)
    return record


def _query(database: _Database, query: str) -> Tuple[Optional[Any], int]:
    try:
        return database.reader.get_with_prefix_len(query)
    except ValueError:  # e.g. an IPv6 address in an IPv4-only database
        return None, 0
    except mmdb.InvalidDatabaseError as exc:
        if not database.failed:
            database.failed = True
            logger.warning("geo: corrupt data in %s: %s", database.path, exc)
        return None, 0


def _public_address(ip: Any) -> Optional[IPAddress]:
    """The parsed address when it is public, else None."""
    address = _parse(ip)
    if address is None:
        return None
    value = int(address)
    if address.version == 4:
        ranges = _NON_PUBLIC_V4
    elif value >> 125 == 0b001:  # inside 2000::/3
        ranges = _NON_PUBLIC_V6
    else:
        return None
    for network, mask in ranges:
        if value & mask == network:
            return None
    return address


def _parse(ip: Any) -> Optional[IPAddress]:
    """Parse an address; IPv4-mapped IPv6 becomes IPv4 and zone ids are dropped."""
    if isinstance(ip, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        address: IPAddress = ip
    elif isinstance(ip, str):
        try:
            address = ipaddress.ip_address(ip.strip())
        except ValueError:
            return None
    else:
        return None
    if address.version == 6:
        mapped = address.ipv4_mapped
        if mapped is not None:
            return mapped
        if getattr(address, "scope_id", None):
            return ipaddress.IPv6Address(int(address))
    return address


def _network(address: IPAddress, prefix_len: int) -> Optional[str]:
    """CIDR of the network containing *address* with *prefix_len* bits."""
    network_class = ipaddress.IPv4Network if address.version == 4 else ipaddress.IPv6Network
    try:
        return str(network_class((int(address), prefix_len), strict=False))
    except ValueError:
        return None


def _normalise_location(record: Any) -> Dict[str, Any]:
    """GeoIP2/GeoLite2 City or Country records; DB-IP Lite uses the same shape."""
    if not isinstance(record, dict):
        return {}
    # Where the network is registered stands in when its location is unknown.
    country = _map(record.get("country")) or _map(record.get("registered_country"))
    country_code = _code(country.get("iso_code"))
    location = _map(record.get("location"))
    subdivision = _first_map(record.get("subdivisions"))
    return {
        "country": country_code,
        "country_name": _english_name(country),
        "continent": _code(_map(record.get("continent")).get("code")),
        "is_eu": _eu_flag(country, country_code),
        "region": _text(subdivision.get("iso_code")),
        "region_name": _english_name(subdivision),
        "city": _english_name(_map(record.get("city"))),
        "postal": _text(_map(record.get("postal")).get("code")),
        "lat": _number(location.get("latitude")),
        "lon": _number(location.get("longitude")),
        "accuracy_km": _integer(location.get("accuracy_radius")),
        "tz": _text(location.get("time_zone")),
    }


def _normalise_asn(record: Any) -> Dict[str, Any]:
    """GeoLite2-ASN / GeoIP2-ISP / DB-IP ASN Lite records."""
    if not isinstance(record, dict):
        return {}
    return {
        "asn": _integer(record.get("autonomous_system_number")),
        "as_org": _text(record.get("autonomous_system_organization")),
    }


def _normalise_ipinfo(record: Any) -> Dict[str, Any]:
    """IPinfo Lite records: country and ASN in one flat map."""
    if not isinstance(record, dict):
        return {}
    return {
        "country": _code(record.get("country_code")),
        "country_name": _text(record.get("country")),
        "continent": _code(record.get("continent_code")),
        "asn": _as_number(record.get("asn")),
        "as_org": _text(record.get("as_name")),
    }


_NORMALISERS: Dict[str, Callable[[Any], Dict[str, Any]]] = {
    "city": _normalise_location,
    "country": _normalise_location,
    "asn": _normalise_asn,
    "country_asn": _normalise_ipinfo,
}


def _eu_flag(country: Dict[str, Any], country_code: Optional[str]) -> Optional[bool]:
    """GeoIP2 data only includes ``is_in_european_union`` when it is true."""
    if country_code is None:
        return None
    flag = country.get("is_in_european_union")
    return flag if isinstance(flag, bool) else False


def _map(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_map(value: Any) -> Dict[str, Any]:
    if isinstance(value, list) and value:
        return _map(value[0])
    return {}


def _english_name(entity: Dict[str, Any]) -> Optional[str]:
    return _text(_map(entity.get("names")).get("en"))


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _code(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return value.strip().upper() or None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _integer(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_number(value: Any) -> Optional[int]:
    """13335 from "AS13335", "13335" or 13335."""
    if isinstance(value, str):
        text = value.strip().upper()
        if text.startswith("AS"):
            text = text[2:]
        return int(text) if text.isascii() and text.isdigit() else None
    return _integer(value)
