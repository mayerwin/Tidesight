"""Configuration tests: defaults, layering, validation, origins, public config, reloading."""

from __future__ import annotations

import os
import time

import pytest

from tidesight import config as config_mod
from tidesight.config import (FEATURE_DEFAULTS, Config, ConfigError, ConfigHolder, load, loads,
                              normalize_origin)

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def test_defaults_without_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TIDESIGHT_CONFIG", raising=False)
    monkeypatch.setattr(config_mod, "find_config_path", lambda explicit=None: None)
    cfg = load()
    # Never the working directory (under CGI it is the web root): ~/tidesight-data.
    assert cfg.server["storage_mode"] == "sqlite" and cfg.data_dir == os.path.expanduser(os.path.join("~", "tidesight-data"))
    assert cfg.sites == {}
    site = cfg.site_or_default("anything")
    assert site.features == FEATURE_DEFAULTS and site.timezone == "UTC"


def test_example_config_loads():
    cfg = load(os.path.join(REPO, "config", "tidesight.example.toml"))
    assert cfg.warnings == []
    site = cfg.site("example")
    assert site.name == "Example" and site.timezone == "Pacific/Tahiti"
    assert site.origin_allowed("https://www.example.com") and not site.origin_allowed("https://other.com")
    assert site.features["replay"] is False and site.opts["replay"]["sample_rate"] == 0.1
    assert site.opts["replay"]["mask_all_inputs"] is True
    assert cfg.server["journal_mode"] == "wal"


def test_layering_defaults_and_sites():
    cfg = loads("""
[defaults]
timezone = "Europe/Paris"
heartbeat_sec = 15
features.clicks = false
features.replay = true
replay.sample_rate = 0.5

[sites.a]
origins = ["https://a.com"]

[sites.b]
timezone = "UTC"
features.clicks = true
replay.sample_rate = 1.0
replay.mask_all_inputs = false
""")
    a, b = cfg.site("a"), cfg.site("b")
    assert a.timezone == "Europe/Paris" and b.timezone == "UTC"
    assert a.settings["heartbeat_sec"] == 15 and b.settings["heartbeat_sec"] == 15
    assert a.features["clicks"] is False and b.features["clicks"] is True
    assert a.features["replay"] and b.features["replay"]
    assert a.opts["replay"]["sample_rate"] == 0.5 and b.opts["replay"]["sample_rate"] == 1.0
    assert a.opts["replay"]["mask_all_inputs"] is True and b.opts["replay"]["mask_all_inputs"] is False
    assert a.opts["scroll"]["marks"] == [25, 50, 75, 90, 100]
    assert a.opts["forms"] == {"sample_rate": 1.0}


@pytest.mark.parametrize("text,match", [
    ('[server]\nmax_payload_kb = "big"\n', "max_payload_kb"),
    ('[server]\nstorage_mode = "mysql"\n', "storage_mode"),
    ('[sites.a]\nfeatures.clicks = "yes"\n', "features.clicks"),
    ('[sites.a]\nreplay.sample_rate = 2.0\n', "sample_rate"),
    ('[sites.a]\nhash_mode = "sometimes"\n', "hash_mode"),
    ('[sites.a]\norigins = "https://a.com"\n', "origins"),
    ('[sites]\na = 1\n', "sites.a"),
    ('not toml at all = = =', "invalid TOML"),
])
def test_validation_errors(text, match):
    with pytest.raises(ConfigError, match=match):
        loads(text)


def test_unknown_keys_warn():
    cfg = loads('[server]\nfoo = 1\n[sites.a]\nbar = 2\nfeatures.nope = true\n[weird]\nx = 1\n')
    joined = " | ".join(cfg.warnings)
    assert "foo" in joined and "bar" in joined and "nope" in joined and "[weird]" in joined


def test_origin_normalisation_and_wildcards():
    assert normalize_origin("HTTPS://Example.COM:443/") == "https://example.com"
    assert normalize_origin("http://example.com:80") == "http://example.com"
    assert normalize_origin("http://example.com:8080") == "http://example.com:8080"
    cfg = loads('[sites.a]\norigins = ["https://*.example.com", "http://localhost:3000"]\n'
                '[sites.b]\norigins = ["*"]\n[sites.c]\n')
    a = cfg.site("a")
    assert a.origin_allowed("https://www.example.com") and a.origin_allowed("https://a.b.example.com")
    assert not a.origin_allowed("https://example.com.evil.net")
    assert a.origin_allowed("http://localhost:3000") and not a.origin_allowed(None)
    assert cfg.site("b").origin_allowed("https://anything.org")
    assert cfg.site("c").origin_allowed(None)  # no origins listed: anything goes
    assert a.hosts() == ["example.com", "localhost"]


def test_public_config_contains_no_secrets():
    cfg = loads('[server]\nsecret = "s3cr3t"\n[dashboard]\nusers = { admin = "pw-digest-xyz" }\n'
                '[sites.a]\nshare_token = "share-tok-xyz"\nfeatures.replay = true\n')
    pub = cfg.public_site_config("a")
    text = repr(pub)
    assert "s3cr3t" not in text and "pw-digest-xyz" not in text and "share-tok-xyz" not in text
    assert pub["known"] is True and pub["opts"]["replay"]["sample_rate"] == 0.1
    assert "scroll" in pub["opts"] and "forms" not in pub["opts"]  # only non-trivial options are sent
    assert len(pub["cv"]) == 12
    assert cfg.public_site_config("zzz")["known"] is False


def test_generated_secret_is_persistent(tmp_path):
    text = '[server]\ndata_dir = "%s"\n' % str(tmp_path).replace("\\", "/")
    s1 = loads(text).secret
    s2 = loads(text).secret
    assert s1 == s2 and len(s1) == 64
    assert loads(text + 'secret = "fixed"\n').secret == "fixed"


def test_paths_relative_to_config_file(tmp_path):
    path = tmp_path / "conf" / "tidesight.toml"
    path.parent.mkdir()
    path.write_text("[server]\n")
    cfg = load(str(path))
    assert cfg.data_dir == os.path.join(str(tmp_path), "conf", "data")
    path.write_text('[server]\ndata_dir = "d"\nlog_file = "logs/ts.log"\n[enrich]\ngeo_dir = "geo"\n')
    cfg = load(str(path))
    base = os.path.join(str(tmp_path), "conf")
    assert cfg.data_dir == os.path.join(base, "d") and cfg.enrich["geo_dir"] == os.path.join(base, "geo")
    assert cfg.server["log_file"] == os.path.join(base, "logs", "ts.log") and cfg.server["static_dir"] == ""


def test_config_holder_reloads(tmp_path):
    path = tmp_path / "tidesight.toml"
    path.write_text('[sites.a]\ntimezone = "UTC"\n')
    holder = ConfigHolder(str(path), check_interval=0)
    assert holder.get().site("a").timezone == "UTC"
    time.sleep(0.05)
    path.write_text('[sites.a]\ntimezone = "Asia/Tokyo"\n')
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert holder.get().site("a").timezone == "Asia/Tokyo"
    path.write_text("broken = = =")
    os.utime(path, (time.time() + 10, time.time() + 10))
    assert holder.get().site("a").timezone == "Asia/Tokyo"  # keeps the last good config


def test_env_var_locates_config(tmp_path, monkeypatch):
    path = tmp_path / "x.toml"
    path.write_text('[sites.envsite]\n')
    monkeypatch.setenv("TIDESIGHT_CONFIG", str(path))
    assert "envsite" in load().sites
