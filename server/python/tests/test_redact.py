"""URL redaction (redact.py): the shared test vectors, linear time, configuration and the collector's Referer."""

from __future__ import annotations

import json
import os
import time

import pytest

from tidesight.config import ConfigError, SITE_DEFAULTS, loads
from tidesight.redact import decode_name, glob_match, glob_spans, patterns, redact_url

VECTORS = json.load(open(os.path.join(os.path.dirname(__file__), "redact_vectors.json"), encoding="utf-8"))


GROUPS = [VECTORS] + VECTORS.get("more", [])


@pytest.mark.parametrize("group,url,expected", [(g, c[0], c[1]) for g in GROUPS for c in g["cases"]])
def test_vectors(group, url, expected):
    assert redact_url(url, group["redact_params"], group["redact_paths"]) == expected


def test_nesting_is_bounded():
    # Three levels of encoded URLs: the third is not looked into (the cost stays linear in the URL).
    url = ("https://ex.com/a?r=" + "https%3A%2F%2Fa.com%2F%3Fn%3D" + "https%253A%252F%252Fb.com%252F%253Fm%253D"
           + "https%25253A%25252F%25252Fc.com%25252F%25253Fsid%25253DX")
    assert redact_url(url, ["sid"], []) == url
    hostile = "https://ex.com/a?" + "&".join("v%d=%s" % (i, "%253F" * 50) for i in range(2000))
    t = time.perf_counter()
    redact_url(hostile, ["sid"], ["/reset/*"])
    assert time.perf_counter() - t < 1.0


def test_vectors_cover_the_name_length_limit():
    long = [c for c in VECTORS["cases"] if "k" * 256 in c[0]]
    assert [c[1].endswith("=v") for c in long] == [True, False]


def test_nothing_to_do_without_patterns():
    url = "https://ex.com/reset/x?token=1"
    assert redact_url(url, [], []) == url
    assert redact_url(None, ["*"], []) is None
    assert redact_url(url, [], ["/reset/*"]) == "https://ex.com/reset/redacted?token=1"


def test_glob_spans():
    assert glob_spans("*a*b*", "xaybz") == [(0, 1), (2, 3), (4, 5)]
    assert glob_spans("/a/*", "/A/x") == [(3, 4)]
    assert glob_spans("/a/*", "/A/x", case_insensitive=False) is None
    assert glob_spans("ab", "AB") == [] and glob_spans("ab", "abc") is None
    assert glob_spans("a*a", "a") is None  # head and tail never overlap
    assert glob_match("*", "") and glob_match("**", "x") and not glob_match("a*", "")


def test_decode_name_is_as_strict_as_decode_uri_component():
    assert decode_name("a%C3%A9+b") == "a\N{LATIN SMALL LETTER E WITH ACUTE} b"
    for bad in ("a%C3x", "%E9", "%zz", "%4", "%ED%A0%80", "%C0%80", "%F4%90%80%80"):
        assert decode_name(bad) == bad, bad
    assert decode_name("x+y") == "x y" and decode_name("x+%zz") == "x+%zz"


def test_linear_time_on_hostile_urls():
    names, paths = SITE_DEFAULTS["redact_params"], ["/*a*b*c*d*e*/x"]
    hostile = [
        "https://victim.example/any#" + "token" * 60000 + "%0A=1",          # the regex version took minutes
        "https://victim.example/any?" + "&".join("tokenx%d=v" % i for i in range(30000)),
        "https://victim.example/" + "a" * 200000 + "?x=1",
        "https://victim.example/" + "abcd" * 50000,
        "https://victim.example/p?" + "%41" * 80 + "=1&" * 20000,
    ]
    for url in hostile:
        t = time.perf_counter()
        redact_url(url, names, paths)
        assert time.perf_counter() - t < 1.0


def test_patterns_ignores_non_strings():
    assert patterns(["a", 1, None, "b"]) == ["a", "b"] and patterns("a") == [] and patterns(None) == []


def test_configuration():
    cfg = loads('[sites.s]\nredact_paths = ["/reset/*"]\n')
    public = cfg.public_site_config("s")
    assert public["redact_paths"] == ["/reset/*"] and public["redact_params"] == SITE_DEFAULTS["redact_params"]
    assert cfg.public_site_config("other")["redact_paths"] == []
    for bad in ('redact_params = ["a", 1]', 'redact_paths = "x"', 'exclude_paths = [true]'):
        with pytest.raises(ConfigError):
            loads("[sites.s]\n" + bad + "\n")
    with pytest.raises(ConfigError):
        loads("[defaults]\nredact_params = [1]\n")


def test_default_patterns_cover_common_secrets():
    names = SITE_DEFAULTS["redact_params"]
    for name in ("token", "access_token", "id_token", "password", "new_password", "passwd", "pwd", "pass", "secret",
                 "client_secret", "key", "api_key", "x-api-key", "apikey", "access_key", "aws_access_key_id",
                 "private_key", "auth", "authorization", "jwt", "otp", "mfa_code", "session", "sessionid",
                 "session_id", "jsessionid", "PHPSESSID", "sid", "ticket", "signature", "X-Amz-Signature", "sig",
                 "credential", "X-Amz-Credential", "reset_key", "activation-key"):
        assert any(glob_match(p, name) for p in names), name
    for name in ("q", "page", "utm_source", "ref", "id", "lang", "sort", "keyword", "monkey"):
        assert not any(glob_match(p, name) for p in names), name


def test_stored_referer_is_redacted(harness):
    harness.call("GET", "/p.gif", query="site=example&vid=v1",
                 headers={"Referer": "https://example.com/welcome?token=abc&step=2#access_token=xyz"})
    req = harness.events("SELECT headers FROM raw_requests")[0]
    expected = "https://example.com/welcome?token=redacted&step=2#access_token=redacted"
    assert json.loads(req["headers"])["referer"] == expected
    assert harness.events("SELECT url FROM raw_hits")[0]["url"] == expected
