"""The demo data (tools/demo.py), the read-only demo mode ([dashboard] demo) and the website's live-demo data."""

from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

from conftest import call
from tidesight.config import load
from tidesight.wsgi import create_app

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO, "tools"))

import build_site  # noqa: E402
import demo  # noqa: E402

WRITE = {"X-Tidesight": "1", "Origin": "https://demo.example.org", "Content-Type": "application/json",
         "Host": "demo.example.org"}


@pytest.fixture(scope="module")
def made(tmp_path_factory):
    """A small demo: three properties, a few days, the team, read-only."""
    out = str(tmp_path_factory.mktemp("demo"))
    assert demo.main(["--out", out, "--days", "4", "--visitors", "40", "--seed", "5", "--demo",
                      "--public-url", "https://stats.example.com/"]) == 0
    return out


def get(app, endpoint, query="", host="demo.example.org"):
    return call(app, "GET", "/dashboard/api/" + endpoint, headers={"Host": host}, query=query)


def test_three_properties_and_their_visitors(made):
    cfg = load(os.path.join(made, "tidesight.toml"))
    assert sorted(cfg.sites) == ["demo", "harbor", "journal"] and all(cfg.site(s).managed for s in cfg.sites)
    assert cfg.dashboard["demo"] is True and cfg.server["public_url"] == "https://stats.example.com/"
    conn = sqlite3.connect(os.path.join(made, "data", "derived.db"))
    per_site = dict(conn.execute("SELECT site_id, COUNT(*) FROM sessions WHERE is_bot = 0 GROUP BY site_id").fetchall())
    assert set(per_site) == {"demo", "harbor", "journal"} and min(per_site.values()) > 5
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE is_bot = 1").fetchone()[0] > 0  # crawlers, flagged
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE uid IS NOT NULL").fetchone()[0] > 0  # signed-in people
    conn.close()
    with open(os.path.join(made, "data", "users.json"), encoding="utf-8") as fh:
        assert {"maya", "noah", "lea", "sam", "ines"} <= set(json.load(fh)["users"])


def test_the_demo_is_open_and_read_only(made):
    app = create_app(os.path.join(made, "tidesight.toml"))  # sign-in on: the demo setting opens it
    me = get(app, "me").json()
    assert me["demo"] is True and me["admin"] is True and me["auth"] == "none" and not me["can_sign_out"]
    assert get(app, "sites").code == 200 and get(app, "properties").code == 200 and get(app, "users").code == 200
    assert get(app, "update").code == 404  # never offered an update
    live = get(app, "realtime", "site=demo").json()
    assert live["active"] > 0  # the last half hour has visitors
    for method, endpoint, body in (("POST", "properties", {"domains": ["x.example"]}), ("POST", "login", {"user": "maya"}),
                                   ("POST", "segments", {"name": "x", "filters": []}), ("DELETE", "update", {}),
                                   ("PATCH", "user", {"role": "admin"}), ("POST", "users", {"name": "eve"})):
        res = call(app, method, "/dashboard/api/" + endpoint, headers=WRITE, body=json.dumps(body).encode())
        assert res.code == 403 and "read-only demo" in res.json()["error"], (method, endpoint)
    page = call(app, "GET", "/dashboard/", headers={"Host": "demo.example.org"})
    assert page.code == 200


def test_without_the_demo_setting_nothing_is_open(made, tmp_path):
    path = tmp_path / "tidesight.toml"
    with open(os.path.join(made, "tidesight.toml"), encoding="utf-8") as fh:
        text = fh.read().replace("demo = true", "demo = false").replace('data_dir = "data"', 'data_dir = "%s"'
                                                                         % os.path.join(made, "data").replace("\\", "/"))
    path.write_text(text, encoding="utf-8")
    app = create_app(str(path))
    assert get(app, "me").code == 401 and get(app, "properties").code == 401


def test_live_visits_arrive_page_by_page(made):
    cfg = load(os.path.join(made, "tidesight.toml"))
    conn = sqlite3.connect(os.path.join(made, "data", "events.db"))
    before = conn.execute("SELECT COUNT(*) FROM raw_hits").fetchone()[0]
    assert demo.live(cfg, minutes=5, seed=9) > 0
    added = conn.execute("SELECT received_at, client_ts FROM raw_hits WHERE id > (SELECT MAX(id) FROM raw_hits) - ?",
                         (conn.execute("SELECT COUNT(*) FROM raw_hits").fetchone()[0] - before,)).fetchall()
    conn.close()
    assert added
    assert all(r[0] >= (r[1] or 0) for r in added)  # sent when they happened, never before


def test_the_website_demo_data_is_slimmed_without_changing_reports(made, tmp_path):
    import shutil
    work = str(tmp_path / "slim")
    shutil.copytree(made, work)
    app = create_app(os.path.join(work, "tidesight.toml"))
    before = get(app, "overview", "site=demo&period=30d").json()["totals"]
    build_site.slim(os.path.join(work, "data"), keep_days=0)  # everything counts as old here
    app = create_app(os.path.join(work, "tidesight.toml"))
    assert get(app, "overview", "site=demo&period=30d").json()["totals"] == before
    conn = sqlite3.connect(os.path.join(work, "data", "derived.db"))
    assert conn.execute("SELECT COUNT(*) FROM hits WHERE type IN ('vital', 'perf', 'scroll')").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM vitals").fetchone()[0] > 0  # the Performance report keeps its data
    conn.close()
