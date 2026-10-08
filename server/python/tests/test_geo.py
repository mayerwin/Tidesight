"""Tests for geo lookups (tidesight.enrich.geo) and updates (geo_update).

Databases are built with tests/mmdb_writer.py in the shapes MaxMind GeoLite2,
DB-IP Lite and IPinfo Lite use. Downloads are served by a local http.server.
"""

from __future__ import annotations

import base64
import datetime
import gzip
import http.server
import io
import ipaddress
import logging
import os
import socket
import tarfile
import threading
import types
from typing import Any, Dict, Iterator, List

import pytest

from mmdb_writer import Raw, Uint16, Uint32, build_database, write_database
from tidesight.enrich import geo_update
from tidesight.enrich.geo import GEO_VERSION, RECORD_KEYS, GeoLookup, is_public_ip
from tidesight.enrich.geo_update import GeoUpdateError, download_dbip, download_maxmind

LONDON = {
    "city": {"geoname_id": 2643743, "names": {"de": "London", "en": "London", "fr": "Londres"}},
    "continent": {"code": "EU", "geoname_id": 6255148, "names": {"en": "Europe"}},
    "country": {"geoname_id": 2635167, "iso_code": "GB", "names": {"en": "United Kingdom"}},
    "location": {
        "accuracy_radius": Uint16(100),
        "latitude": 51.5142,
        "longitude": -0.0931,
        "time_zone": "Europe/London",
    },
    "postal": {"code": "EC2V"},
    "registered_country": {"geoname_id": 6252001, "iso_code": "US", "names": {"en": "United States"}},
    "subdivisions": [
        {"geoname_id": 6269131, "iso_code": "ENG", "names": {"en": "England"}},
        {"iso_code": "WSM", "names": {"en": "Westminster"}},
    ],
}
FRANCE = {"geoname_id": 3017382, "is_in_european_union": True, "iso_code": "FR", "names": {"en": "France"}}
PARIS = {
    "city": {"geoname_id": 2988507, "names": {"en": "Paris"}},
    "continent": {"code": "EU", "geoname_id": 6255148, "names": {"en": "Europe"}},
    "country": FRANCE,
    "location": {
        "accuracy_radius": Uint16(20),
        "latitude": 48.8566,
        "longitude": 2.3522,
        "time_zone": "Europe/Paris",
    },
    "postal": {"code": "75001"},
    "registered_country": FRANCE,
    "subdivisions": [{"iso_code": "IDF", "names": {"en": "Île-de-France"}}],
}
# Satellite and anycast networks often have no location, only a registration.
REGISTERED_ONLY = {
    "continent": {"code": "EU"},
    "registered_country": {"iso_code": "IT", "is_in_european_union": True, "names": {"en": "Italy"}},
}
MAXMIND_CITY = [
    ("81.2.69.0/24", LONDON),
    ("193.51.24.0/24", PARIS),
    ("2a01:cb00::/32", PARIS),
    ("5.5.5.0/24", REGISTERED_ONLY),
    ("9.9.9.0/24", {"location": {"latitude": 1.5}}),  # no country at all
]
MAXMIND_ASN = [
    ("81.2.0.0/16", {"autonomous_system_number": Uint32(20712), "autonomous_system_organization": "Andrews & Arnold Ltd"}),
    ("2a01:c000::/19", {"autonomous_system_number": Uint32(3215), "autonomous_system_organization": "Orange S.A."}),
    ("8.8.8.0/24", {"autonomous_system_number": Uint32(15169), "autonomous_system_organization": "Google LLC"}),
]
DBIP_CITY = [
    (
        "193.51.0.0/16",
        {
            "city": {"names": {"en": "Paris"}},
            "continent": {"code": "EU", "geoname_id": 6255148, "names": {"en": "Europe"}},
            "country": FRANCE,
            "location": {"latitude": 48.8534, "longitude": 2.3488},
            "subdivisions": [{"names": {"en": "Île-de-France"}}],
        },
    ),
    (
        "81.2.69.0/24",
        {
            "city": {"names": {"en": "London"}},
            "continent": {"code": "EU"},
            "country": {"is_in_european_union": False, "iso_code": "GB", "names": {"en": "United Kingdom"}},
            "location": {"latitude": 51.5085, "longitude": -0.1257},
        },
    ),
]
DBIP_ASN = [
    ("193.51.24.0/22", {"autonomous_system_number": 2200, "autonomous_system_organization": "Renater"}),
]
IPINFO = [
    (
        "1.1.1.0/24",
        {
            "asn": "AS13335",
            "as_name": "Cloudflare, Inc.",
            "as_domain": "cloudflare.com",
            "country_code": "AU",
            "country": "Australia",
            "continent_code": "OC",
            "continent": "Oceania",
        },
    ),
    ("81.2.69.0/24", {"asn": "AS20712", "as_name": "Andrews & Arnold Ltd", "country_code": "GB", "country": "United Kingdom", "continent_code": "EU", "continent": "Europe"}),
]


def write(directory: Any, name: str, networks: List[Any], **options: Any) -> str:
    return write_database(os.path.join(str(directory), name), networks, **options)


def record(**values: Any) -> Dict[str, Any]:
    result = dict.fromkeys(RECORD_KEYS)
    result.update(values)
    return result


@pytest.fixture
def geo_dir(tmp_path) -> str:
    write(tmp_path, "GeoLite2-City.mmdb", MAXMIND_CITY, database_type="GeoLite2-City", build_epoch=1000)
    write(tmp_path, "GeoLite2-ASN.mmdb", MAXMIND_ASN, database_type="GeoLite2-ASN", build_epoch=1001)
    write(tmp_path, "GeoLite2-Country.mmdb", MAXMIND_CITY, database_type="GeoLite2-Country", build_epoch=5000)
    write(tmp_path, "dbip-city-lite-2026-09.mmdb", DBIP_CITY[:1], database_type="DBIP-City-Lite", build_epoch=2000)
    write(tmp_path, "dbip-city-lite-2026-10.mmdb", DBIP_CITY, database_type="DBIP-City-Lite", build_epoch=3000)
    write(tmp_path, "dbip-city-lite.mmdb", DBIP_CITY[:1], database_type="DBIP-City-Lite", build_epoch=2500)
    write(tmp_path, "dbip-asn-lite.mmdb", DBIP_ASN, database_type="DBIP-ASN-Lite", build_epoch=3001)
    write(tmp_path, "ipinfo_lite.mmdb", IPINFO, database_type="ipinfo lite", build_epoch=4000)
    (tmp_path / "README.txt").write_text("not a database")
    (tmp_path / "notes.mmdb").write_bytes(b"unrelated")
    return str(tmp_path)


# --- is_public_ip -------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "8.8.8.8",
        "1.1.1.1",
        "81.2.69.160",
        "100.63.255.255",
        "100.128.0.0",
        "172.15.255.255",
        "172.32.0.0",
        "192.169.0.1",
        "198.17.255.255",
        "198.20.0.0",
        "223.255.255.255",
        "2001:4860:4860::8888",
        "2001:200::1",
        "2a01:cb00::1",
        "2002:c000:204::1",
        "::ffff:8.8.8.8",
        " 8.8.8.8 ",
        ipaddress.ip_address("8.8.8.8"),
        ipaddress.ip_address("2a01:cb00::1"),
    ],
)
def test_public_addresses(ip):
    assert is_public_ip(ip) is True


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.1",
        "10.255.255.255",
        "172.16.0.0",
        "172.31.255.255",
        "192.168.1.1",
        "127.0.0.1",
        "127.255.255.254",
        "169.254.1.1",
        "100.64.0.0",
        "100.127.255.255",
        "224.0.0.1",
        "239.255.255.255",
        "240.0.0.1",
        "255.255.255.255",
        "0.0.0.0",
        "0.1.2.3",
        "192.0.0.8",
        "192.0.2.1",
        "198.18.0.1",
        "198.19.255.255",
        "198.51.100.7",
        "203.0.113.9",
        "::",
        "::1",
        "fe80::1",
        "fe80::1%eth0",
        "fc00::1",
        "fd12:3456::1",
        "ff02::1",
        "2001:db8::1",
        "3fff::1",
        "2001::1",
        "::ffff:10.0.0.1",
        "::ffff:127.0.0.1",
        "::1.2.3.4",
        "64:ff9b::808:808",
        "100::1",
        "",
        "abc",
        "1.2.3",
        "1.2.3.4.5",
        "256.1.1.1",
        "1.2.3.4/24",
        None,
        12345,
        b"8.8.8.8",
    ],
)
def test_non_public_or_invalid_addresses(ip):
    assert is_public_ip(ip) is False


# --- discovery ------------------------------------------------------------------


def test_discovery_picks_the_newest_file_of_each_kind(geo_dir):
    with GeoLookup(geo_dir) as geo:
        assert geo.providers == ["dbip", "ipinfo", "maxmind"]
        databases = {(db["provider"], db["kind"]): db for db in geo.databases}
    assert sorted(databases) == [
        ("dbip", "asn"),
        ("dbip", "city"),
        ("ipinfo", "country_asn"),
        ("maxmind", "asn"),
        ("maxmind", "city"),
    ]
    assert os.path.basename(databases[("dbip", "city")]["path"]) == "dbip-city-lite-2026-10.mmdb"
    assert databases[("dbip", "city")]["build_epoch"] == 3000
    assert databases[("maxmind", "city")]["database_type"] == "GeoLite2-City"


def test_version_is_deterministic_and_tracks_builds(geo_dir):
    with GeoLookup(geo_dir) as first, GeoLookup(geo_dir) as second:
        version = first.version()
        assert version == second.version()
    assert version == (
        f"{GEO_VERSION};dbip.city=3000;dbip.asn=3001;ipinfo.country_asn=4000;"
        "maxmind.city=1000;maxmind.asn=1001"
    )
    write(geo_dir, "GeoLite2-ASN.mmdb", MAXMIND_ASN, build_epoch=1002)
    with GeoLookup(geo_dir) as geo:
        assert geo.version() == version.replace("maxmind.asn=1001", "maxmind.asn=1002")


def test_country_database_is_used_without_a_city_database(tmp_path):
    write(tmp_path, "GeoLite2-Country.mmdb", MAXMIND_CITY, build_epoch=7)
    with GeoLookup(str(tmp_path)) as geo:
        assert [(db["provider"], db["kind"]) for db in geo.databases] == [("maxmind", "country")]
        assert geo.lookup("81.2.69.160")["maxmind"]["country"] == "GB"


@pytest.mark.parametrize(
    ("name", "provider", "kind"),
    [
        ("GEOLITE2-CITY.MMDB", "maxmind", "city"),
        ("geoip2-city.mmdb", "maxmind", "city"),
        ("GeoIP2-Country.mmdb", "maxmind", "country"),
        ("GeoIP2-ISP.mmdb", "maxmind", "asn"),
        ("DBIP-Country-Lite-2026-10.mmdb", "dbip", "country"),
        ("dbip-asn-lite-2026-10.mmdb", "dbip", "asn"),
        ("ipinfo-lite.mmdb", "ipinfo", "country_asn"),
    ],
)
def test_file_names_are_case_insensitive(tmp_path, name, provider, kind):
    write(tmp_path, name, MAXMIND_CITY)
    with GeoLookup(str(tmp_path)) as geo:
        assert [(db["provider"], db["kind"]) for db in geo.databases] == [(provider, kind)]


def test_explicit_files(geo_dir, tmp_path, caplog):
    other = tmp_path / "elsewhere"
    other.mkdir()
    outside = write(other, "dbip-asn-lite.mmdb", DBIP_ASN, build_epoch=9)
    files = ["GeoLite2-City.mmdb", outside, "README.txt"]
    with caplog.at_level(logging.WARNING, logger="tidesight.enrich.geo"):
        with GeoLookup(geo_dir, files=files) as geo:
            assert [(db["provider"], db["kind"]) for db in geo.databases] == [
                ("dbip", "asn"),
                ("maxmind", "city"),
            ]
    assert "README.txt" in caplog.text


# --- lookups ----------------------------------------------------------------------


def test_lookup_merges_location_and_asn_per_provider(geo_dir):
    with GeoLookup(geo_dir) as geo:
        result = geo.lookup("81.2.69.160")
    assert result == {
        "maxmind": record(
            country="GB",
            country_name="United Kingdom",
            continent="EU",
            is_eu=False,
            region="ENG",
            region_name="England",
            city="London",
            postal="EC2V",
            lat=51.5142,
            lon=-0.0931,
            accuracy_km=100,
            tz="Europe/London",
            asn=20712,
            as_org="Andrews & Arnold Ltd",
            network="81.2.69.0/24",
        ),
        "dbip": record(
            country="GB",
            country_name="United Kingdom",
            continent="EU",
            is_eu=False,
            city="London",
            lat=51.5085,
            lon=-0.1257,
            network="81.2.69.0/24",
        ),
        "ipinfo": record(
            country="GB",
            country_name="United Kingdom",
            continent="EU",
            asn=20712,
            as_org="Andrews & Arnold Ltd",
            network="81.2.69.0/24",
        ),
    }


def test_dbip_shape(geo_dir):
    with GeoLookup(geo_dir) as geo:
        result = geo.lookup("193.51.24.1")
    assert result["dbip"] == record(
        country="FR",
        country_name="France",
        continent="EU",
        is_eu=True,
        region=None,
        region_name="Île-de-France",
        city="Paris",
        lat=48.8534,
        lon=2.3488,
        asn=2200,
        as_org="Renater",
        network="193.51.0.0/16",
    )
    assert result["maxmind"]["is_eu"] is True and result["maxmind"]["region"] == "IDF"


def test_ipinfo_shape(geo_dir):
    with GeoLookup(geo_dir) as geo:
        assert geo.lookup("1.1.1.1") == {
            "ipinfo": record(
                country="AU",
                country_name="Australia",
                continent="OC",
                asn=13335,
                as_org="Cloudflare, Inc.",
                network="1.1.1.0/24",
            )
        }


def test_asn_only_answer_uses_the_asn_network(geo_dir):
    with GeoLookup(geo_dir) as geo:
        assert geo.lookup("8.8.8.8") == {
            "maxmind": record(asn=15169, as_org="Google LLC", network="8.8.8.0/24")
        }


def test_registered_country_stands_in_for_an_unknown_location(geo_dir):
    with GeoLookup(geo_dir) as geo:
        result = geo.lookup("5.5.5.5")["maxmind"]
        assert (result["country"], result["country_name"], result["is_eu"]) == ("IT", "Italy", True)
        assert result["city"] is None and result["lat"] is None
        unknown = geo.lookup("9.9.9.9")["maxmind"]
        assert unknown["country"] is None and unknown["is_eu"] is None and unknown["lat"] == 1.5


def test_ipv6_lookups(geo_dir):
    with GeoLookup(geo_dir) as geo:
        result = geo.lookup("2a01:cb00:1234::1")
    assert result["maxmind"]["city"] == "Paris"
    assert result["maxmind"]["asn"] == 3215
    assert result["maxmind"]["network"] == "2a01:cb00::/32"


def test_ipv4_mapped_addresses_are_looked_up_as_ipv4(geo_dir):
    with GeoLookup(geo_dir) as geo:
        assert geo.lookup("::ffff:81.2.69.160") == geo.lookup("81.2.69.160")
        assert geo.lookup(" 81.2.69.160 ") == geo.lookup("81.2.69.160")


@pytest.mark.parametrize("ip", ["10.1.2.3", "127.0.0.1", "::1", "fd00::1", "not an ip", "", None])
def test_non_public_addresses_are_not_looked_up(geo_dir, ip):
    with GeoLookup(geo_dir) as geo:
        assert geo.lookup(ip) == {}


def test_unknown_public_address(geo_dir):
    with GeoLookup(geo_dir) as geo:
        assert geo.lookup("4.4.4.4") == {}
        assert geo.lookup("2600::1") == {}


def test_ipv6_address_in_an_ipv4_only_database(tmp_path):
    write(tmp_path, "GeoLite2-ASN.mmdb", MAXMIND_ASN[:1] + MAXMIND_ASN[2:], ip_version=4)
    with GeoLookup(str(tmp_path)) as geo:
        assert geo.lookup("2a01:cb00::1") == {}
        assert geo.lookup("8.8.8.8")["maxmind"]["asn"] == 15169


# --- degradation ------------------------------------------------------------------


@pytest.mark.parametrize("geo_dir", [None, ""])
def test_no_geo_dir(geo_dir):
    with GeoLookup(geo_dir) as geo:
        assert geo.providers == [] and geo.databases == []
        assert geo.lookup("8.8.8.8") == {}
        assert geo.version() == str(GEO_VERSION)


def test_missing_geo_dir(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="tidesight.enrich.geo"):
        geo = GeoLookup(str(tmp_path / "missing"))
    assert geo.providers == [] and geo.lookup("8.8.8.8") == {}
    assert "missing" in caplog.text


def test_corrupt_files_are_skipped(tmp_path, caplog):
    (tmp_path / "GeoLite2-City.mmdb").write_bytes(b"\x00" * 1000)
    write(tmp_path, "GeoLite2-ASN.mmdb", MAXMIND_ASN)
    write(tmp_path, "dbip-city-lite-2026-09.mmdb", DBIP_CITY, build_epoch=1)
    content = build_database(DBIP_CITY, build_epoch=2)
    (tmp_path / "dbip-city-lite-2026-10.mmdb").write_bytes(content[: len(content) // 2])
    with caplog.at_level(logging.WARNING, logger="tidesight.enrich.geo"):
        with GeoLookup(str(tmp_path)) as geo:
            assert [(db["provider"], db["kind"], db["build_epoch"]) for db in geo.databases] == [
                ("dbip", "city", 1),
                ("maxmind", "asn", 1_700_000_000),
            ]
            assert geo.lookup("81.2.69.160")["maxmind"]["asn"] == 20712
    assert "GeoLite2-City.mmdb" in caplog.text and "dbip-city-lite-2026-10.mmdb" in caplog.text


def test_corrupt_data_is_logged_once_and_skipped(tmp_path, caplog):
    write(tmp_path, "GeoLite2-City.mmdb", [("81.2.69.0/24", Raw(b"\x00\x09"))], dedupe=False)
    write(tmp_path, "GeoLite2-ASN.mmdb", MAXMIND_ASN)
    with caplog.at_level(logging.WARNING, logger="tidesight.enrich.geo"):
        with GeoLookup(str(tmp_path)) as geo:
            for _ in range(3):
                assert geo.lookup("81.2.69.160") == {
                    "maxmind": record(asn=20712, as_org="Andrews & Arnold Ltd", network="81.2.0.0/16")
                }
    assert caplog.text.count("corrupt data") == 1


def test_close(geo_dir):
    geo = GeoLookup(geo_dir)
    assert geo.lookup("81.2.69.160")
    geo.close()
    geo.close()
    assert geo.providers == [] and geo.lookup("81.2.69.160") == {}
    assert geo.version() == str(GEO_VERSION)


def test_mmap_mode(geo_dir):
    with GeoLookup(geo_dir, use_mmap=True) as mapped, GeoLookup(geo_dir) as loaded:
        for ip in ("81.2.69.160", "193.51.24.1", "2a01:cb00::1", "1.1.1.1", "8.8.8.8"):
            assert mapped.lookup(ip) == loaded.lookup(ip)


# --- downloads ----------------------------------------------------------------------

MAXMIND_PATH = "/geoip/databases/{}/download?suffix=tar.gz"


@pytest.fixture
def server(monkeypatch) -> Iterator[types.SimpleNamespace]:
    """A local HTTP server: set ``routes[path] = (status, headers, body)``."""
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("no_proxy", "*")
    routes: Dict[str, Any] = {}
    log: List[Dict[str, Any]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            log.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "user_agent": self.headers.get("User-Agent"),
                }
            )
            status, headers, body = routes.get(self.path, (404, {}, b"not found"))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            if "Content-Length" not in headers:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield types.SimpleNamespace(url=f"http://127.0.0.1:{httpd.server_address[1]}", routes=routes, log=log)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


def maxmind_archive(edition: str, content: bytes, member: str = "") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        folder = f"{edition}_20261007"
        for name, data in (
            (f"{folder}/COPYRIGHT.txt", b"Database and Contents Copyright (c) MaxMind"),
            (f"{folder}/{member or edition + '.mmdb'}", content),
            (f"{folder}/LICENSE.txt", b"GeoLite2 End User License Agreement"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def city_db(epoch: int = 5000) -> bytes:
    return build_database(MAXMIND_CITY, database_type="GeoLite2-City", build_epoch=epoch)


def asn_db(epoch: int = 5001) -> bytes:
    return build_database(MAXMIND_ASN, database_type="GeoLite2-ASN", build_epoch=epoch)


def basic(account: str, key: str) -> str:
    return "Basic " + base64.b64encode(f"{account}:{key}".encode()).decode()


def assert_no_partial_files(directory: Any) -> None:
    assert [name for name in os.listdir(str(directory)) if name.endswith(".part")] == []


def test_download_maxmind(server, tmp_path):
    server.routes[MAXMIND_PATH.format("GeoLite2-City")] = (200, {}, maxmind_archive("GeoLite2-City", city_db()))
    server.routes[MAXMIND_PATH.format("GeoLite2-ASN")] = (200, {}, maxmind_archive("GeoLite2-ASN", asn_db()))
    dest = tmp_path / "geo"
    paths = download_maxmind(str(dest), "123456", "secret-key", base_url=server.url)
    assert paths == [str(dest / "GeoLite2-City.mmdb"), str(dest / "GeoLite2-ASN.mmdb")]
    assert (dest / "GeoLite2-City.mmdb").read_bytes() == city_db()
    assert sorted(os.listdir(str(dest))) == ["GeoLite2-ASN.mmdb", "GeoLite2-City.mmdb"]
    assert [entry["path"] for entry in server.log] == [
        MAXMIND_PATH.format("GeoLite2-City"),
        MAXMIND_PATH.format("GeoLite2-ASN"),
    ]
    assert {entry["authorization"] for entry in server.log} == {basic("123456", "secret-key")}
    assert {entry["user_agent"] for entry in server.log} == {"tidesight-geo-update"}
    with GeoLookup(str(dest)) as geo:
        assert geo.lookup("81.2.69.160")["maxmind"]["asn"] == 20712
        assert "maxmind.city=5000" in geo.version()


def test_download_maxmind_replaces_an_existing_file(server, tmp_path):
    (tmp_path / "GeoLite2-City.mmdb").write_bytes(city_db(epoch=1))
    server.routes[MAXMIND_PATH.format("GeoLite2-City")] = (200, {}, maxmind_archive("GeoLite2-City", city_db(epoch=2)))
    download_maxmind(str(tmp_path), "1", "k", editions=["GeoLite2-City"], base_url=server.url)
    assert (tmp_path / "GeoLite2-City.mmdb").read_bytes() == city_db(epoch=2)
    assert_no_partial_files(tmp_path)


def test_download_maxmind_credentials_do_not_follow_redirects(server, tmp_path):
    storage = "/storage/GeoLite2-City.tar.gz?X-Amz-Signature=abc"
    server.routes[MAXMIND_PATH.format("GeoLite2-City")] = (302, {"Location": storage}, b"")
    server.routes[storage] = (200, {}, maxmind_archive("GeoLite2-City", city_db()))
    download_maxmind(str(tmp_path), "1", "k", editions=["GeoLite2-City"], base_url=server.url)
    assert [entry["path"] for entry in server.log] == [MAXMIND_PATH.format("GeoLite2-City"), storage]
    assert server.log[0]["authorization"] == basic("1", "k")
    assert server.log[1]["authorization"] is None
    assert (tmp_path / "GeoLite2-City.mmdb").read_bytes() == city_db()


def test_download_maxmind_bad_credentials(server, tmp_path):
    (tmp_path / "GeoLite2-City.mmdb").write_bytes(city_db(epoch=1))
    server.routes[MAXMIND_PATH.format("GeoLite2-City")] = (401, {}, b"Invalid license key")
    with pytest.raises(GeoUpdateError) as caught:
        download_maxmind(str(tmp_path), "1", "wrong-key", editions=["GeoLite2-City"], base_url=server.url)
    message = str(caught.value)
    assert "GeoLite2-City: HTTP 401" in message and "license key" in message
    assert "wrong-key" not in message
    assert caught.value.updated == []
    assert (tmp_path / "GeoLite2-City.mmdb").read_bytes() == city_db(epoch=1)
    assert os.listdir(str(tmp_path)) == ["GeoLite2-City.mmdb"]


def test_download_maxmind_reports_every_edition(server, tmp_path):
    server.routes[MAXMIND_PATH.format("GeoLite2-City")] = (200, {}, maxmind_archive("GeoLite2-City", city_db()))
    server.routes[MAXMIND_PATH.format("GeoLite2-ASN")] = (500, {}, b"oops")
    with pytest.raises(GeoUpdateError) as caught:
        download_maxmind(str(tmp_path), "1", "k", base_url=server.url)
    assert "GeoLite2-ASN: HTTP 500" in str(caught.value)
    assert caught.value.updated == [str(tmp_path / "GeoLite2-City.mmdb")]
    assert sorted(os.listdir(str(tmp_path))) == ["GeoLite2-City.mmdb"]


def broken_archives() -> List[Any]:
    archive = maxmind_archive("GeoLite2-City", city_db())
    return [
        pytest.param(maxmind_archive("GeoLite2-City", b"not a database"), "not a valid MaxMind DB", id="bad-mmdb"),
        pytest.param(maxmind_archive("GeoLite2-City", city_db()[: len(city_db()) // 2]), "not a valid MaxMind DB", id="cut-mmdb"),
        pytest.param(maxmind_archive("GeoLite2-City", city_db(), "Other.mmdb"), "does not contain", id="no-member"),
        pytest.param(archive[: len(archive) // 2], None, id="cut-archive"),
        pytest.param(b"<html>maintenance</html>", None, id="not-an-archive"),
    ]


@pytest.mark.parametrize(("body", "message"), broken_archives())
def test_download_maxmind_broken_downloads_leave_no_trace(server, tmp_path, body, message):
    (tmp_path / "GeoLite2-City.mmdb").write_bytes(city_db(epoch=1))
    server.routes[MAXMIND_PATH.format("GeoLite2-City")] = (200, {}, body)
    with pytest.raises(GeoUpdateError, match=message):
        download_maxmind(str(tmp_path), "1", "k", editions=["GeoLite2-City"], base_url=server.url)
    assert os.listdir(str(tmp_path)) == ["GeoLite2-City.mmdb"]
    assert (tmp_path / "GeoLite2-City.mmdb").read_bytes() == city_db(epoch=1)


def test_download_maxmind_interrupted_transfer(server, tmp_path):
    archive = maxmind_archive("GeoLite2-City", city_db())
    headers = {"Content-Length": str(len(archive))}
    server.routes[MAXMIND_PATH.format("GeoLite2-City")] = (200, headers, archive[: len(archive) // 2])
    with pytest.raises(GeoUpdateError):
        download_maxmind(str(tmp_path), "1", "k", editions=["GeoLite2-City"], base_url=server.url)
    assert os.listdir(str(tmp_path)) == []


def test_download_size_limit(server, tmp_path, monkeypatch):
    monkeypatch.setattr(geo_update, "MAX_DATABASE_SIZE", 1000)
    server.routes[MAXMIND_PATH.format("GeoLite2-City")] = (200, {}, maxmind_archive("GeoLite2-City", city_db()))
    with pytest.raises(GeoUpdateError, match="larger than 1000 bytes"):
        download_maxmind(str(tmp_path), "1", "k", editions=["GeoLite2-City"], base_url=server.url)
    assert os.listdir(str(tmp_path)) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"account_id": "", "license_key": "k"},
        {"account_id": "1", "license_key": " "},
        {"account_id": "1", "license_key": "k", "editions": ["../GeoLite2-City"]},
        {"account_id": "1", "license_key": "k", "editions": []},
        {"account_id": "1", "license_key": "k", "base_url": "file:///etc"},
    ],
)
def test_download_maxmind_rejects_bad_arguments(tmp_path, kwargs):
    with pytest.raises(ValueError):
        download_maxmind(str(tmp_path), **kwargs)


def test_unreachable_server(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(GeoUpdateError, match="cannot connect"):
        download_dbip(str(tmp_path), kinds=["country"], month="2026-10", base_url=f"http://127.0.0.1:{port}")
    assert os.listdir(str(tmp_path)) == []


def dbip_path(kind: str, month: str) -> str:
    return f"/free/dbip-{kind}-lite-{month}.mmdb.gz"


def set_today(monkeypatch, year: int, month: int, day: int) -> None:
    monkeypatch.setattr(geo_update, "_utc_today", lambda: datetime.date(year, month, day))


def test_download_dbip_current_month(server, tmp_path, monkeypatch):
    set_today(monkeypatch, 2026, 10, 8)
    city = build_database(DBIP_CITY, build_epoch=7000)
    asn = build_database(DBIP_ASN, build_epoch=7001)
    server.routes[dbip_path("city", "2026-10")] = (200, {}, gzip.compress(city))
    server.routes[dbip_path("asn", "2026-10")] = (200, {}, gzip.compress(asn))
    paths = download_dbip(str(tmp_path), base_url=server.url + "/free")
    assert paths == [str(tmp_path / "dbip-city-lite.mmdb"), str(tmp_path / "dbip-asn-lite.mmdb")]
    assert (tmp_path / "dbip-city-lite.mmdb").read_bytes() == city
    assert {entry["user_agent"] for entry in server.log} == {"tidesight-geo-update"}
    assert {entry["authorization"] for entry in server.log} == {None}
    with GeoLookup(str(tmp_path)) as geo:
        assert geo.lookup("193.51.24.1")["dbip"]["asn"] == 2200


def test_download_dbip_falls_back_to_the_previous_month(server, tmp_path, monkeypatch):
    set_today(monkeypatch, 2026, 1, 1)  # also crosses a year boundary
    server.routes[dbip_path("country", "2025-12")] = (200, {}, gzip.compress(build_database(DBIP_CITY)))
    assert download_dbip(str(tmp_path), kinds=["country"], base_url=server.url + "/free") == [
        str(tmp_path / "dbip-country-lite.mmdb")
    ]
    assert [entry["path"] for entry in server.log] == [
        dbip_path("country", "2026-01"),
        dbip_path("country", "2025-12"),
    ]


def test_download_dbip_nothing_published(server, tmp_path, monkeypatch):
    set_today(monkeypatch, 2026, 10, 1)
    with pytest.raises(GeoUpdateError, match="not published") as caught:
        download_dbip(str(tmp_path), kinds=["city"], base_url=server.url + "/free")
    assert "dbip-city-lite-2026-10.mmdb.gz" in str(caught.value)
    assert "dbip-city-lite-2026-09.mmdb.gz" in str(caught.value)
    assert os.listdir(str(tmp_path)) == []


def test_download_dbip_explicit_month_has_no_fallback(server, tmp_path):
    with pytest.raises(GeoUpdateError, match="not published"):
        download_dbip(str(tmp_path), kinds=["city"], month="2026-08", base_url=server.url + "/free")
    assert [entry["path"] for entry in server.log] == [dbip_path("city", "2026-08")]


def test_download_dbip_server_error_is_not_a_fallback(server, tmp_path, monkeypatch):
    set_today(monkeypatch, 2026, 10, 8)
    server.routes[dbip_path("city", "2026-10")] = (503, {}, b"busy")
    with pytest.raises(GeoUpdateError, match="HTTP 503"):
        download_dbip(str(tmp_path), kinds=["city"], base_url=server.url + "/free")
    assert len(server.log) == 1


@pytest.mark.parametrize(
    ("body", "message"),
    [
        pytest.param(gzip.compress(build_database(DBIP_CITY))[:-30], None, id="cut-gzip"),
        pytest.param(gzip.compress(b"not a database" * 100), "not a valid MaxMind DB", id="bad-mmdb"),
        pytest.param(b"not gzip at all", None, id="not-gzip"),
    ],
)
def test_download_dbip_broken_downloads_leave_no_trace(server, tmp_path, monkeypatch, body, message):
    set_today(monkeypatch, 2026, 10, 8)
    old = build_database(DBIP_CITY, build_epoch=1)
    (tmp_path / "dbip-city-lite.mmdb").write_bytes(old)
    server.routes[dbip_path("city", "2026-10")] = (200, {}, body)
    with pytest.raises(GeoUpdateError, match=message):
        download_dbip(str(tmp_path), kinds=["city"], base_url=server.url + "/free")
    assert os.listdir(str(tmp_path)) == ["dbip-city-lite.mmdb"]
    assert (tmp_path / "dbip-city-lite.mmdb").read_bytes() == old


@pytest.mark.parametrize(
    "kwargs",
    [{"kinds": ["region"]}, {"kinds": []}, {"month": "2026-13"}, {"month": "26-10"}, {"base_url": "ftp://x"}],
)
def test_download_dbip_rejects_bad_arguments(tmp_path, kwargs):
    with pytest.raises(ValueError):
        download_dbip(str(tmp_path), **kwargs)
