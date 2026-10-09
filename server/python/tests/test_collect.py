"""Collector tests: the ingest endpoints, validation, flagging, limits and privacy switches."""

from __future__ import annotations

import gzip
import json
import os
import sqlite3
from urllib.parse import quote

import pytest

from conftest import envelope
from tidesight import collect


def test_valid_batch_is_stored_raw(harness):
    env = envelope(events=[
        {"t": "pageview", "id": "e1", "ts": 1, "pid": "p1", "url": "https://example.com/x", "title": "X",
         "ctx": {"sw": 1920, "tz": "Europe/Paris"}},
        {"t": "event", "id": "e2", "ts": 2, "pid": "p1", "n": "signup", "p": {"plan": "pro"}},
    ])
    r = harness.post(env, headers={"Cookie": "ts_vid=abc.1; other=secret; ts_ses=s.1.2.3.4",
                                   "Authorization": "Bearer xyz", "X-Api-Key": "k",
                                   "Accept-Language": "fr-FR,fr;q=0.9", "X-Forwarded-For": "198.51.100.1"})
    assert r.code == 204
    assert r.headers["access-control-allow-origin"] == "https://example.com"
    assert r.headers["cache-control"] == "no-store"
    req = harness.events("SELECT * FROM raw_requests")[0]
    assert req["site_id"] == "example" and req["site_status"] == "ok" and req["error"] is None
    assert req["ip"] == "203.0.113.9" and req["proto_v"] == 1 and req["event_count"] == 2
    headers = json.loads(req["headers"])
    assert headers["accept-language"] == "fr-FR,fr;q=0.9"
    assert headers["x-forwarded-for"] == "198.51.100.1"
    assert headers["authorization"] == "[redacted]" and headers["x-api-key"] == "[redacted]"
    assert "cookie" not in headers
    cookies = json.loads(req["cookies"])
    assert cookies == {"kept": {"ts_vid": "abc.1", "ts_ses": "s.1.2.3.4"}, "others": 1}
    env_stored = json.loads(req["envelope"])
    assert "events" not in env_stored and env_stored["vid"] == "visitor00000001"
    assert req["body"] is None
    hits = harness.events("SELECT * FROM raw_hits ORDER BY seq")
    assert [h["type"] for h in hits] == ["pageview", "event"]
    assert hits[0]["visitor_id"] == "visitor00000001" and hits[0]["session_id"] == "session0000001"
    assert hits[0]["page_id"] == "p1" and hits[0]["url"] == "https://example.com/x" and hits[0]["client_ts"] == 1
    assert hits[1]["name"] == "signup" and hits[1]["event_id"] == "e2"
    assert json.loads(hits[0]["data"]) == env["events"][0]
    assert all(h["site_ok"] == 1 for h in hits)


@pytest.mark.parametrize("ctype", ["text/plain;charset=UTF-8", "application/json", "text/plain"])
def test_beacon_and_json_content_types(harness, ctype):
    r = harness.post(envelope(), headers={"Content-Type": ctype})
    assert r.code == 204
    assert len(harness.events("SELECT id FROM raw_hits")) == 1


def test_form_urlencoded_body(harness):
    body = ("d=" + quote(json.dumps(envelope()))).encode()
    r = harness.post(None, raw=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.code == 204
    assert harness.events("SELECT type FROM raw_hits")[0]["type"] == "pageview"


def test_gzip_sniffed_and_header(harness):
    raw = gzip.compress(json.dumps(envelope()).encode())
    assert harness.post(None, raw=raw).code == 204
    assert harness.post(None, raw=raw, headers={"Content-Encoding": "gzip"}).code == 204
    reqs = harness.events("SELECT body_encoding FROM raw_requests")
    assert [r["body_encoding"] for r in reqs] == ["gzip", "gzip"]
    assert len(harness.events("SELECT id FROM raw_hits")) == 2


def test_bad_gzip_is_stored_flagged(harness):
    raw = b"\x1f\x8b" + b"not really gzip"
    r = harness.post(None, raw=raw)
    assert r.code == 400
    req = harness.events("SELECT * FROM raw_requests")[0]
    assert req["error"].startswith("bad_gzip") and req["body"] == raw


def test_gzip_bomb_is_refused(make_harness):
    h = make_harness(extra="")
    h.cfg.server["max_decompressed_kb"] = 16
    raw = gzip.compress(b"[" + b"0," * 50000 + b"0]")
    r = h.post(None, raw=raw)
    assert r.code == 400
    assert "exceeds" in h.events("SELECT error FROM raw_requests")[0]["error"]


@pytest.mark.parametrize("raw,error", [
    (b"{not json", "bad_json"),
    (b"\xff\xfe\x00", "bad_utf8"),
    (b"[1,2,3]", "not_an_object"),
    (b'{"v":1,"site":"example","events":[NaN]}', "bad_json"),
])
def test_malformed_bodies_are_stored_flagged(harness, raw, error):
    r = harness.post(None, raw=raw)
    assert r.code == 400
    req = harness.events("SELECT * FROM raw_requests")[0]
    assert req["error"].startswith(error)
    assert req["body"] == raw
    assert harness.events("SELECT COUNT(*) AS n FROM raw_hits")[0]["n"] == 0


def test_empty_body(harness):
    r = harness.call("POST", "/collect", headers={"Content-Type": "text/plain", "Origin": "https://example.com"})
    assert r.code == 400
    assert harness.events("SELECT error FROM raw_requests")[0]["error"] == "empty_body"


def test_oversize_payload_metadata_kept(make_harness):
    h = make_harness()
    h.cfg.server["max_payload_kb"] = 1
    big = envelope(events=[{"t": "event", "n": "x", "p": {"blob": "y" * 5000}}])
    r = h.post(big, query="site=example")
    assert r.code == 413
    req = h.events("SELECT * FROM raw_requests")[0]
    assert req["site_status"] == "too_large" and req["error"] == "too_large" and req["body"] is None
    assert req["site_id"] == "example" and req["body_bytes"] > 1024


def test_unknown_site_is_stored_and_flagged(harness):
    r = harness.post(envelope(site="nope"))
    assert r.code == 204
    req = harness.events("SELECT site_id, site_status FROM raw_requests")[0]
    assert req == {"site_id": "nope", "site_status": "unknown_site"}
    assert harness.events("SELECT site_ok FROM raw_hits")[0]["site_ok"] == 0


def test_missing_site_and_query_site(harness):
    env = envelope()
    del env["site"]
    assert harness.post(env).code == 204
    assert harness.post(env, query="site=example").code == 204
    rows = harness.events("SELECT site_id, site_status FROM raw_requests ORDER BY id")
    assert rows == [{"site_id": None, "site_status": "missing_site"}, {"site_id": "example", "site_status": "ok"}]


@pytest.mark.parametrize("origin,status", [
    ("https://example.com", "ok"),
    ("https://EXAMPLE.com:443", "ok"),
    ("https://shop.example.org", "ok"),
    ("https://evil.com", "origin_mismatch"),
    ("http://example.com", "origin_mismatch"),
])
def test_origin_checks(harness, origin, status):
    r = harness.post(envelope(), headers={"Origin": origin})
    assert r.code == 204
    assert harness.events("SELECT site_status FROM raw_requests")[0]["site_status"] == status


def test_no_origin_is_flagged_when_site_lists_origins(harness):
    r = harness.call("POST", "/collect", body=json.dumps(envelope()).encode(), headers={"Content-Type": "text/plain"})
    assert r.code == 204
    assert harness.events("SELECT site_status FROM raw_requests")[0]["site_status"] == "no_origin"


def test_referer_used_when_no_origin(harness):
    r = harness.call("POST", "/collect", body=json.dumps(envelope()).encode(),
                     headers={"Content-Type": "text/plain", "Referer": "https://example.com/page"})
    assert r.code == 204
    assert harness.events("SELECT site_status FROM raw_requests")[0]["site_status"] == "ok"


def test_invalid_events_are_kept(harness):
    env = envelope(events=[{"t": "pageview", "url": "https://example.com/"}, "garbage", {"no": "type"}, 42])
    r = harness.post(env)
    assert r.code == 204
    hits = harness.events("SELECT type, data FROM raw_hits ORDER BY seq")
    assert [h["type"] for h in hits] == ["pageview", "_invalid", "_invalid", "_invalid"]
    assert json.loads(hits[1]["data"]) == "garbage" and json.loads(hits[3]["data"]) == 42
    assert harness.events("SELECT error FROM raw_requests")[0]["error"] == "bad_events:3"


def test_missing_events_and_bad_version(harness):
    r = harness.post({"v": "one", "site": "example"})
    assert r.code == 400
    assert harness.events("SELECT error FROM raw_requests")[0]["error"] == "bad_version,no_events"


def test_typed_columns_are_bounded_and_typed(harness):
    env = envelope(events=[{"t": "x" * 100, "id": 12345, "ts": "17", "pid": ["bad"], "url": "u" * 5000,
                            "n": "n" * 500, "sid": "override-session"}])
    assert harness.post(env).code == 204
    h = harness.events("SELECT * FROM raw_hits")[0]
    assert len(h["type"]) == 32 and h["event_id"] == "12345" and h["client_ts"] == 17
    assert h["page_id"] is None and len(h["url"]) == 2048 and len(h["name"]) == 200
    assert h["session_id"] == "override-session"
    assert json.loads(h["data"])["url"] == "u" * 5000  # the raw event keeps everything


def test_too_many_events_keep_the_raw_body(make_harness):
    # One request must not become an unbounded number of rows: the first events become hits, the raw body
    # keeps the whole batch.
    h = make_harness()
    h.cfg.server["max_events_per_request"] = 3
    env = envelope(events=[{"t": "event", "n": str(i)} for i in range(5)])
    assert h.post(env).code == 400
    assert [r["name"] for r in h.events("SELECT name FROM raw_hits ORDER BY seq")] == ["0", "1", "2"]
    req = h.events("SELECT error, body FROM raw_requests")[0]
    assert req["error"] == "too_many_events:5" and json.loads(req["body"]) == env


def test_rate_limit(make_harness):
    h = make_harness(rate=3)
    codes = [h.post(envelope(), query="site=example").code for _ in range(5)]
    assert codes == [204, 204, 204, 429, 429]
    assert len(h.events("SELECT id FROM raw_requests")) == 3
    counters = {r["name"]: r["n"] for r in h.events("SELECT name, n FROM counters WHERE site_id = 'example'")}
    assert counters == {"rate_limited": 2, "hits": 3}
    # Another IP is not affected.
    assert h.post(envelope(), remote_addr="198.51.100.77").code == 204


def test_client_key_groups_ipv6_by_network():
    assert collect.client_key("203.0.113.9") == "203.0.113.9"
    assert collect.client_key("2001:db8:1:2:aaaa::1") == "2001:db8:1:2::/64"
    assert collect.client_key("2001:DB8:1:2:ffff:ffff:ffff:ffff") == "2001:db8:1:2::/64"
    assert collect.client_key("2001:db8:1:2::1", 48) == "2001:db8:1::/48"
    assert collect.client_key("::ffff:198.51.100.7") == "198.51.100.7"
    assert collect.client_key("[2001:db8::1]") == "2001:db8::/64"
    assert collect.client_key("not an address") == "not an address"
    assert collect.client_key("") is None


def test_rate_limit_counts_an_ipv6_network_as_one_client(make_harness):
    h = make_harness(rate=2)
    codes = [h.post(envelope(), remote_addr="2001:db8:5:6::%x" % i).code for i in range(1, 4)]
    assert codes == [204, 204, 429]
    assert h.post(envelope(), remote_addr="2001:db8:5:7::1").code == 204  # another /64


def test_kilobyte_budget_per_client(make_harness):
    h = make_harness()
    h.cfg.server["rate_limit_kb_per_minute"] = 3
    big = envelope(events=[{"t": "event", "n": "x" * 1500}])  # about 1.7 KB: 2 KB of the budget
    assert [h.post(big, query="site=example").code for _ in range(3)] == [204, 429, 429]
    assert h.post(big, remote_addr="198.51.100.80").code == 204
    assert h.events("SELECT n FROM counters WHERE name = 'rate_limited'")[0]["n"] == 2


def test_each_hit_and_the_headers_are_charged(make_harness):
    h = make_harness()
    h.cfg.server["rate_limit_kb_per_minute"] = 12
    h.cfg.server["max_events_per_request"] = 20
    tiny = envelope(events=[{"t": "e"}] * 20)  # 20 hits cost at least 20 × 256 bytes: 5 KB, however small
    assert [h.post(tiny).code for _ in range(3)] == [204, 204, 429]
    padded = {"X-Pad": "p" * 5000}  # stored headers are charged too
    assert [h.post(envelope(), headers=padded, remote_addr="198.51.100.31").code for _ in range(3)] == [204, 204, 429]


def test_the_rate_limit_key_header_uses_its_last_value_and_ignores_ports(make_harness):
    h = make_harness(rate=2)
    h.cfg.server["rate_limit_key"] = "x-forwarded-for"
    h.cfg.server["rate_limit_proxies"] = ["203.0.113.0/24"]  # the harness connects from 203.0.113.9
    hdr = lambda first, last: {"X-Forwarded-For": "%s, %s" % (first, last)}  # noqa: E731
    codes = [h.post(envelope(), headers=hdr("10.0.0.%d" % i, "198.51.100.40:%d" % (1000 + i))).code for i in range(3)]
    assert codes == [204, 204, 429]  # the client's own first values do not make it a new client
    assert h.post(envelope(), headers=hdr("10.0.0.9", "198.51.100.41")).code == 204  # another client
    assert collect.client_key("[2001:db8::1]:443") == "2001:db8::/64"


def test_the_rate_limit_key_header_is_believed_only_from_listed_proxies(make_harness):
    h = make_harness(rate=2)
    h.cfg.server["rate_limit_key"] = "x-forwarded-for"
    codes = [h.post(envelope(), headers={"X-Forwarded-For": "198.51.100.%d" % i}).code for i in range(3)]
    assert codes == [204, 204, 429]  # not from a listed proxy: the connecting address counts
    h.cfg.server["rate_limit_proxies"] = ["cloudflare"]
    assert h.cfg.rate_limit_proxies()[0] == "173.245.48.0/20"


def test_counter_only_requests_stay_within_the_allowance(make_harness):
    h = make_harness(rate=3, extra="\n[privacy]\nhonour_dnt = true\n")
    assert [h.post(envelope(), headers={"DNT": "1"}).code for _ in range(5)] == [204] * 3 + [429] * 2
    assert h.events("SELECT n FROM counters WHERE name = 'dropped_dnt'")[0]["n"] == 3


def test_a_client_over_its_allowance_is_refused_before_parsing(make_harness, monkeypatch):
    """Decompressing and parsing are the costly part of a request: once a client is over its allowance for the
    minute, its next requests are refused without them (charged and counted as any refusal)."""
    h = make_harness(rate=2)
    assert [h.post(envelope(), query="site=example").code for _ in range(2)] == [204, 204]

    def parse(*a, **kw):
        raise AssertionError("parsed a request from a client over its allowance")

    monkeypatch.setattr(collect.Collector, "_parse_envelope", parse)
    assert h.post(None, raw=b"{not json", query="site=example").code == 429  # not 400: never looked at
    assert h.call("POST", "/collect", body=b"x" * 70000).code == 413  # an oversize body still gets its 413
    assert len(h.events("SELECT id FROM raw_requests")) == 2  # (the oversize metadata too was over the allowance)
    counters = {r["name"]: r["n"] for r in h.events("SELECT name, n FROM counters WHERE site_id = 'example'")}
    assert counters["rate_limited"] == 1
    monkeypatch.undo()
    assert h.post(envelope(), remote_addr="198.51.100.81").code == 204  # another client


def test_allowance_keys_never_hold_an_address(make_harness):
    h = make_harness(rate=5)
    h.post(envelope(), remote_addr="198.51.100.82")
    keys = [r["key"] for r in h.events("SELECT key FROM rate_limits")]
    assert keys and not any("198.51.100.82" in k for k in keys)
    assert all(k.startswith(("r:h:", "kb:h:")) and len(k.split(":", 1)[1]) == 18 for k in keys)


def test_a_few_invalid_events_do_not_make_a_batch_flagged(make_harness):
    h = make_harness()
    h.cfg.server["flagged_per_minute"] = 1
    env = envelope(events=[{"t": "event", "n": "ok"}, "junk"])
    assert [h.post(env).code for _ in range(3)] == [204, 204, 204]


def test_a_huge_declared_length_is_refused_cleanly(make_harness):
    h = make_harness()
    r = h.call("POST", "/collect", body=b"{}", headers={"Content-Type": "text/plain", "Content-Length": str(10 ** 30),
                                                     "Origin": "https://example.com"})
    assert r.code == 413
    assert h.events("SELECT body_bytes FROM raw_requests")[0]["body_bytes"] == 2 ** 53 - 1


def test_there_is_no_daily_quota_by_default(make_harness):
    h = make_harness()
    assert h.cfg.site("example").settings["max_hits_per_day"] == 0
    assert h.post(envelope()).code == 204


def test_flagged_requests_have_their_own_allowance(make_harness):
    h = make_harness()
    h.cfg.server["flagged_per_minute"] = 2
    h.cfg.server["flagged_global_per_minute"] = 3
    unknown = [h.post(envelope(site="nope-%d" % i)).code for i in range(3)]
    assert unknown == [204, 204, 429]
    # Malformed bodies still answer 400 (retrying cannot help), stored or not.
    assert h.post(None, raw=b"{bad", remote_addr="198.51.100.81").code == 400
    assert h.post(None, raw=b"{bad", remote_addr="198.51.100.82").code == 400  # over the global allowance
    assert len(h.events("SELECT id FROM raw_requests")) == 3
    # Valid traffic is not affected, and counters never use a site id taken from the request.
    assert h.post(envelope()).code == 204
    rows = h.events("SELECT site_id, name, n FROM counters WHERE name = 'flagged_limited'")
    assert rows == [{"site_id": "", "name": "flagged_limited", "n": 2}]


def test_daily_quota_per_site(make_harness):
    h = make_harness(extra="max_hits_per_day = 3\n")
    two = envelope(events=[{"t": "event", "n": "a"}, {"t": "event", "n": "b"}])
    assert h.post(two).code == 204
    assert h.post(two, remote_addr="198.51.100.90").code == 429  # 2 + 2 > 3
    assert h.post(envelope(events=[{"t": "event", "n": "c"}])).code == 204
    counters = {r["name"]: r["n"] for r in h.events("SELECT name, n FROM counters WHERE site_id = 'example'")}
    assert counters == {"hits": 3, "over_quota": 1}


def test_low_disk_space_refuses_writes(make_harness, monkeypatch):
    h = make_harness()
    h.cfg.server["min_free_disk_mb"] = 10 ** 12  # more than any disk
    r = h.post(envelope())
    assert r.code == 503 and r.headers["retry-after"] == "300"
    assert h.call("GET", "/p.gif", query="site=example").code == 200  # the pixel still answers
    r = h.call("POST", "/chunk", body=b"[]", query="site=example&k=replay&seq=0",
               headers={"Origin": "https://example.com"})
    assert r.code == 503
    assert not os.path.exists(os.path.join(h.data_dir, "events.db"))


def test_rate_limit_disabled(make_harness):
    h = make_harness(rate=0)
    assert all(h.post(envelope()).code == 204 for _ in range(10))


def test_preflight_and_cors(harness):
    r = harness.call("OPTIONS", "/collect", headers={"Origin": "https://example.com",
                                                     "Access-Control-Request-Method": "POST"})
    assert r.code == 204
    assert r.headers["access-control-allow-origin"] == "https://example.com"
    assert "POST" in r.headers["access-control-allow-methods"]
    r = harness.post(envelope(), headers={"Origin": "https://unlisted.net"})
    assert r.headers["access-control-allow-origin"] == "https://unlisted.net"
    assert r.headers["access-control-allow-credentials"] == "true"


def test_cors_allowed_mode(make_harness):
    h = make_harness()
    h.cfg.server["cors"] = "allowed"
    r = h.post(envelope(), headers={"Origin": "https://unlisted.net"})
    assert "access-control-allow-origin" not in r.headers
    assert h.post(envelope()).headers["access-control-allow-origin"] == "https://example.com"


def test_bad_origin_header_not_reflected(harness):
    r = harness.post(envelope(), headers={"Origin": "https://x.com\r\nSet-Cookie: a=b"})
    assert "access-control-allow-origin" not in r.headers


def test_accept_ch_header(harness):
    r = harness.post(envelope())
    assert "Sec-CH-UA-Model" in r.headers["accept-ch"]


def test_method_not_allowed_and_404(harness):
    assert harness.call("GET", "/collect").code == 405
    assert harness.call("GET", "/nope").code == 404


def test_pixel(harness):
    r = harness.call("GET", "/p.gif", query="site=example&t=email_open&n=newsletter&vid=v1&x=1&x=2",
                     headers={"Referer": "https://example.com/landing"})
    assert r.code == 200 and r.headers["content-type"] == "image/gif" and r.body.startswith(b"GIF89a")
    req = harness.events("SELECT * FROM raw_requests")[0]
    assert req["endpoint"] == "pixel" and req["site_status"] == "ok"
    hit = harness.events("SELECT * FROM raw_hits")[0]
    assert hit["type"] == "email_open" and hit["name"] == "newsletter" and hit["visitor_id"] == "v1"
    assert hit["url"] == "https://example.com/landing"
    assert json.loads(hit["data"])["x"] == ["1", "2"]


def test_pixel_without_referer_is_ok(harness):
    r = harness.call("GET", "/p.gif", query="site=example")
    assert r.code == 200
    assert harness.events("SELECT site_status FROM raw_requests")[0]["site_status"] == "ok"
    assert harness.events("SELECT type FROM raw_hits")[0]["type"] == "pageview"


def test_pixel_head(harness):
    r = harness.call("HEAD", "/p.gif", query="site=example")
    assert r.code == 200 and r.body == b""


def test_chunk_stored_compressed(harness):
    events = [{"type": 2, "data": {"node": {}}, "timestamp": 1}]
    raw = json.dumps(events).encode()
    r = harness.call("POST", "/chunk", body=raw, query="site=example&k=replay&vid=v1&sid=s1&pid=p1&seq=0&ts=5",
                     headers={"Content-Type": "text/plain", "Origin": "https://example.com"})
    assert r.code == 204
    row = harness.replay("SELECT * FROM chunks")[0]
    assert row["kind"] == "replay" and row["session_id"] == "s1" and row["seq"] == 0 and row["client_ts"] == 5
    assert row["encoding"] == "gzip" and json.loads(gzip.decompress(row["payload"])) == events
    meta = json.loads(row["meta"])
    assert meta["sent_encoding"] == "identity" and meta["site"] == "example"
    gz = gzip.compress(raw)
    assert harness.call("POST", "/chunk", body=gz, query="site=example&k=mouse&seq=1",
                        headers={"Origin": "https://example.com"}).code == 204
    row = harness.replay("SELECT * FROM chunks WHERE kind='mouse'")[0]
    assert row["payload"] == gz


def test_chunks_of_a_feature_the_site_does_not_record_are_flagged(make_harness):
    h = make_harness(extra='\n[sites.plain]\norigins = ["https://plain.example"]\n')
    r = h.call("POST", "/chunk", body=b"[]", query="site=plain&k=replay&seq=0", headers={"Origin": "https://plain.example"})
    assert r.code == 204
    assert [r["site_ok"] for r in h.replay("SELECT site_ok FROM chunks")] == [0]


def test_chunk_limits(make_harness):
    h = make_harness()
    h.cfg.server["chunk_rate_limit_per_minute"] = 2
    send = lambda addr: h.call("POST", "/chunk", body=b"[]", query="site=example&k=replay&seq=0",  # noqa: E731
                               headers={"Origin": "https://example.com"}, remote_addr=addr).code
    assert [send("198.51.100.20") for _ in range(3)] == [204, 204, 429]
    assert send("198.51.100.21") == 204
    h.cfg.server["chunk_kb_per_minute"] = 4
    big = h.call("POST", "/chunk", body=b"x" * 5000, query="site=example&k=mouse&seq=0",
                 headers={"Origin": "https://example.com"}, remote_addr="198.51.100.22")
    assert big.code == 429
    assert h.events("SELECT n FROM counters WHERE site_id = 'example' AND name = 'chunk_rate_limited'")[0]["n"] == 2


def test_chunk_unknown_kind_and_too_large(make_harness):
    h = make_harness()
    r = h.call("POST", "/chunk", body=b"[]", query="site=example&k=bogus", headers={"Origin": "https://example.com"})
    assert r.code == 400
    assert h.replay("SELECT kind, error FROM chunks")[0] == {"kind": "_invalid", "error": "unknown_kind"}
    h.cfg.server["max_chunk_kb"] = 1
    r = h.call("POST", "/chunk", body=b"x" * 2048, query="site=example&k=replay")
    assert r.code == 413


def test_config_js_and_json(harness):
    r = harness.call("GET", "/config.js", query="site=example")
    assert r.code == 200 and r.headers["content-type"].startswith("application/javascript")
    text = r.body.decode()
    # Hands the configuration to the tracker, or queues it; an element named "tidesight" cannot make it throw.
    assert text.startswith('(typeof window.tidesight=="function"?window.tidesight:window.tidesight=function(){')
    assert '"_config"' in text
    payload = json.loads(text[text.index('"_config",') + len('"_config",'):text.rindex(");")])
    assert payload["site"] == "example" and payload["known"] is True
    assert payload["features"]["pageviews"] is True and payload["features"]["replay"] is True
    assert payload["features"]["copy_paste"] is False and payload["max_kb"] == 64
    assert payload["timezone"] == "Europe/Paris"
    j = harness.call("GET", "/config.json", query="site=example").json()
    assert j == payload
    etag = r.headers["etag"]
    assert harness.call("GET", "/config.js", query="site=example", headers={"If-None-Match": etag}).code == 304
    unknown = harness.call("GET", "/config.json", query="site=zzz").json()
    assert unknown["known"] is False and unknown["features"]["pageviews"] is True


def test_health(harness):
    r = harness.call("GET", "/health")
    assert r.code == 200
    body = r.json()
    assert body["status"] == "ok" and body["storage"] == "sqlite"
    assert harness.data_dir not in r.body.decode()


def test_privacy_hash_and_truncate_ip(make_harness):
    h = make_harness(extra="\n[privacy]\ntruncate_ip = true\n")
    h.post(envelope(), headers={"X-Forwarded-For": "198.51.100.23, 10.0.0.1"})
    req = h.events("SELECT ip, headers FROM raw_requests")[0]
    assert req["ip"] == "203.0.113.0"
    assert json.loads(req["headers"])["x-forwarded-for"] == "198.51.100.0, 10.0.0.0"
    h2 = make_harness(extra="\n[privacy]\nhash_ip = true\n")
    h2.post(envelope())
    ip = h2.events("SELECT ip FROM raw_requests")[0]["ip"]
    assert ip.startswith("h:") and len(ip) == 18


def test_privacy_per_site_override(make_harness):
    h = make_harness(extra="privacy.truncate_ip = true\n")  # appended to [sites.example]
    h.post(envelope())
    assert h.events("SELECT ip FROM raw_requests")[0]["ip"] == "203.0.113.0"


def test_honour_dnt_and_gpc(make_harness):
    h = make_harness(extra="\n[privacy]\nhonour_dnt = true\nhonour_gpc = true\n")
    assert h.post(envelope(), headers={"DNT": "1"}).code == 204
    assert h.post(envelope(), headers={"Sec-GPC": "1"}).code == 204
    assert h.post(envelope()).code == 204
    assert len(h.events("SELECT id FROM raw_requests")) == 1
    counters = {r["name"]: r["n"] for r in h.events("SELECT name, n FROM counters")}
    assert counters == {"dropped_dnt": 1, "dropped_gpc": 1, "hits": 1}
    assert h.call("GET", "/config.json", query="site=example").json()["dnt"] is True


def test_drop_fingerprint(make_harness):
    h = make_harness(extra="\n[privacy]\ndrop_fingerprint = true\n")
    env = envelope(events=[{"t": "pageview"}, {"t": "fp", "h": "abc"}])
    assert h.post(env).code == 204
    assert [r["type"] for r in h.events("SELECT type FROM raw_hits")] == ["pageview"]
    cfg = h.call("GET", "/config.json", query="site=example").json()
    assert cfg["features"]["fingerprint"] is False and cfg["features"]["canvas_fingerprint"] is False


def test_server_cookie(make_harness):
    h = make_harness(extra='server_cookie_domain = ".example.com"\n')
    r = h.post(envelope())
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("ts_svid=visitor00000001.1728380000000; Domain=.example.com; Path=/; Max-Age=")
    assert "SameSite=Lax" in cookie and "Secure" in cookie
    r = h.post(envelope(vid="bad id!"))
    assert "set-cookie" not in r.headers
    r = h.post(envelope(vid="visitor00000001\n"))  # no header splitting
    assert "set-cookie" not in r.headers
    r = h.post(envelope(), headers={"Origin": "https://evil.com"})
    assert "set-cookie" not in r.headers


def test_lone_surrogates_become_replacement_characters(harness):
    # Text cut in the middle of an emoji reaches JSON as a lone surrogate escape, which UTF-8 cannot hold.
    ev = {"t": "event", "id": "ev1", "ts": 1728380012001, "pid": "page0001", "url": "https://example.com/",
          "n": "party \ud83c", "p": {"label": "\udf89 done", "k\ud83c": 1, "ok": "\U0001f389"}}
    r = harness.post(envelope([ev], title="\ud83c"))
    assert r.code == 204
    hit = harness.events("SELECT name, data FROM raw_hits")[0]
    assert hit["name"] == "party �"
    assert json.loads(hit["data"])["p"] == {"label": "� done", "k�": 1, "ok": "\U0001f389"}
    req = harness.events("SELECT envelope, error FROM raw_requests")[0]
    assert json.loads(req["envelope"])["title"] == "�" and req["error"] is None


def test_static_files(harness, tmp_path):
    static = tmp_path / "static"
    (static / "ts").mkdir(parents=True)
    (static / "ts.js").write_text("console.log(1)")
    (static / "ts" / "vitals.js").write_text("x")
    harness.cfg.server["static_dir"] = str(static)
    r = harness.call("GET", "/ts.js")
    assert r.code == 200 and r.body == b"console.log(1)" and "max-age=3600" in r.headers["cache-control"]
    r = harness.call("GET", "/ts/vitals.js", query="v=1")
    assert r.code == 200 and "immutable" in r.headers["cache-control"]
    assert harness.call("GET", "/ts/../config.py").code == 404
    assert harness.call("GET", "/ts/missing.js").code == 404
    assert harness.call("GET", "/robots.txt").body.startswith(b"User-agent")


def test_storage_failure_falls_back_to_spool(harness):
    harness.post(envelope())  # creates the database
    harness.app._store.primary.close()
    events_db = os.path.join(harness.data_dir, "events.db")

    class Boom:
        def __getattr__(self, name):
            import sqlite3
            raise sqlite3.OperationalError("database is locked")

    harness.app._store.primary.events_conn = lambda: Boom()
    r = harness.post(envelope())
    assert r.code == 204
    spool_files = []
    for root, _dirs, files in os.walk(os.path.join(harness.data_dir, "spool")):
        spool_files += [os.path.join(root, f) for f in files]
    assert len(spool_files) == 1
    assert os.path.exists(events_db)


class _Locked:
    def __getattr__(self, name):
        raise sqlite3.OperationalError("database is locked")


def test_rate_limit_still_applies_while_falling_back_to_the_spool(make_harness, monkeypatch):
    monkeypatch.setattr(collect, "now_ms", lambda: 1728380000000)  # one minute window
    h = make_harness(rate=2)
    h.post(envelope())  # creates the database
    h.app._store.primary.events_conn = lambda: _Locked()
    assert [h.post(envelope()).code for _ in range(3)] == [204, 204, 429]


def test_storage_failure_without_fallback_answers_503(make_harness):
    h = make_harness()
    h.cfg.server["spool_fallback"] = False
    assert h.post(envelope()).code == 204
    h.app._store.events_conn = lambda: _Locked()
    r = h.post(envelope())
    assert r.code == 503 and r.headers["retry-after"] == "60"


def test_numbers_too_large_for_a_float_make_a_body_unreadable(harness):
    # Stored, 1e999 would become Infinity: not JSON (SQLite's JSON functions reject the row). Like NaN, refused.
    r = harness.post(None, raw=b'{"v":1,"site":"example","vid":"v1","events":[{"t":"event","n":"x","val":1e999}]}')
    assert r.code == 400
    req = harness.events("SELECT error, site_status FROM raw_requests")[0]
    assert req["error"] == "bad_json: number out of range"
    assert harness.events("SELECT COUNT(*) AS n FROM raw_hits")[0]["n"] == 0
    big = "9" * 400  # a large integer is exact in JSON and in Python: kept
    assert harness.post(None, raw=('{"v":1,"site":"example","vid":"v1","events":[{"t":"event","n":"x","p":{"a":%s}}]}'
                                   % big).encode()).code == 204


def test_hits_are_charged_as_stored(make_harness):
    # 9e15 is stored as 9000000000000000.0: almost four times longer. The allowance counts what is stored.
    h = make_harness()
    h.cfg.server["rate_limit_kb_per_minute"] = 16
    body = '{"v":1,"site":"example","vid":"v1","events":[{"t":"event","n":"x","p":{"a":[%s]}}]}' % ",".join(["9e15"] * 1400)
    assert len(body) < 8000
    assert h.post(None, raw=body.encode()).code == 429


def test_merged_cookie_headers_never_leak_into_a_kept_value(harness):
    # Two Cookie headers merged by the server ("a, b"): the second one's cookies are not Tidesight's.
    harness.post(envelope(), headers={"Cookie": "ts_vid=abc.1, sessionid=SECRET-SESSION; ts_ses=s.1"})
    cookies = json.loads(harness.events("SELECT cookies FROM raw_requests")[0]["cookies"])
    assert cookies["kept"] == {"ts_vid": "abc.1", "ts_ses": "s.1"}


def test_auth_like_headers_are_redacted_by_default(harness):
    harness.post(envelope(), headers={"X-Authorization": "Bearer abc", "Authentication": "Bearer def",
                                      "Cf-Access-Jwt-Assertion": "eyJ", "X-Session-Id": "s1", "Cookie2": "x=1",
                                      "X-Signature": "sig", "Accept-Language": "en"})
    headers = json.loads(harness.events("SELECT headers FROM raw_requests")[0]["headers"])
    for name in ("x-authorization", "authentication", "cf-access-jwt-assertion", "x-session-id", "cookie2",
                 "x-signature"):
        assert headers[name] == "[redacted]", name
    assert headers["accept-language"] == "en"


def test_refuses_to_run_with_its_data_inside_the_web_root(make_harness):
    h = make_harness()
    root = os.path.dirname(h.data_dir)
    r = h.call("POST", "/collect", body=json.dumps(envelope()).encode(),
               headers={"Content-Type": "text/plain", "Origin": "https://example.com", "DOCUMENT_ROOT": root})
    assert r.code == 503
    assert h.call("GET", "/health", headers={"DOCUMENT_ROOT": os.path.join(root, "elsewhere")}).code == 200
    h.cfg.server["allow_data_in_web_root"] = True
    assert h.call("GET", "/health", headers={"DOCUMENT_ROOT": root}).code == 200
