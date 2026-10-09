"""IP anonymisation (anonymize.py): truncation, hashing, and which headers carry addresses."""

from __future__ import annotations

from tidesight.anonymize import anonymize_headers, hash_ip, ip_bearing, truncate_ip


def test_truncation_keeps_a_network():
    assert truncate_ip("203.0.113.77") == "203.0.113.0"
    assert truncate_ip("2001:db8:1:2:3:4:5:6") == "2001:db8:1::"
    assert truncate_ip("::ffff:198.51.100.7") == "198.51.100.0"
    assert truncate_ip("not an ip") == "not an ip"


def test_a_6to4_address_loses_its_ipv4_host_part():
    # 2002:cb00:7105::1 carries 203.0.113.5; a /48 would keep all of it.
    assert truncate_ip("2002:cb00:7105:1234::1") == "2002:cb00:7100::"


def test_ip_bearing_headers():
    for name in ("x-forwarded-for", "x-real-ip", "x-remote-addr", "remote-addr", "x-real-addr", "via",
                 "cloudfront-viewer-address", "x-client-ip", "fly-client-ip"):
        assert ip_bearing(name), name
    assert not ip_bearing("accept-language")


def test_headers_are_anonymised():
    out = anonymize_headers({"x-remote-addr": "198.51.100.23", "via": "1.1 203.0.113.9 (proxy)",
                             "accept": "text/html"}, False, True, "s")
    assert out == {"x-remote-addr": "198.51.100.0", "via": "1.1 203.0.113.0 (proxy)", "accept": "text/html"}
    assert anonymize_headers({"x-remote-addr": "198.51.100.23"}, True, False, "s")["x-remote-addr"] == \
        hash_ip("198.51.100.23", "s")
