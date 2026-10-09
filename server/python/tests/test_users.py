"""Dashboard users kept in data_dir/users.json (`jobs.py user`), merged with the configuration file's."""

from __future__ import annotations

import io
import json
import os
import stat
import sys

import pytest

from conftest import call
from tidesight import jobs, users
from tidesight.auth import hash_password
from tidesight.config import ConfigHolder, load
from tidesight.wsgi import App

WRITE = {"X-Tidesight": "1", "Origin": "https://127.0.0.1", "Content-Type": "application/json"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """A configuration file with one user of its own, and a fast password hash for the tests."""
    data_dir = str(tmp_path / "data").replace("\\", "/")
    path = tmp_path / "tidesight.toml"
    path.write_text('[server]\ndata_dir = "%s"\npublic_url = "https://stats.example.com/"\n\n'
                    '[dashboard]\nusers = { boss = "%s" }\n' % (data_dir, hash_password("boss-pass", iterations=1000)),
                    encoding="utf-8")
    monkeypatch.setattr(jobs, "hash_password", lambda password: hash_password(password, iterations=1000))
    return str(path), data_dir


def run(monkeypatch, capsys, path, *argv, stdin=""):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = jobs.main(["--config", path, "user", *argv])
    return code, capsys.readouterr().out


def test_set_list_remove(setup, monkeypatch, capsys):
    path, data_dir = setup
    code, out = run(monkeypatch, capsys, path, "set", "erwin", "--password-stdin", stdin="a long password\n")
    assert code == 0 and json.loads(out)["password"] == "set"
    assert json.loads(out)["sign_in"] == "https://stats.example.com/dashboard/"
    stored = users.read(data_dir)
    assert list(stored) == ["erwin"] and stored["erwin"].startswith("pbkdf2_sha256$")
    assert "a long password" not in open(users.path(data_dir), encoding="utf-8").read()
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(users.path(data_dir)).st_mode) == 0o600
    cfg = load(path)
    assert set(cfg.dashboard["users"]) == {"boss", "erwin"} and set(cfg.file_users) == {"boss"}

    _, out = run(monkeypatch, capsys, path, "list")
    assert json.loads(out)["users"] == [{"user": "boss", "where": "configuration file", "admin": True},
                                        {"user": "erwin", "where": "users.json", "admin": True}]
    _, out = run(monkeypatch, capsys, path, "set", "erwin", "--password-stdin", stdin="another password\n")
    assert json.loads(out)["password"].startswith("changed")
    run(monkeypatch, capsys, path, "remove", "erwin")
    assert users.read(data_dir) == {} and set(load(path).dashboard["users"]) == {"boss"}


@pytest.mark.parametrize("argv,stdin,message", [
    (["set", "boss", "--password-stdin"], "a long password\n", "configuration file"),  # the file's users win
    (["remove", "boss"], "", "configuration file"),
    (["remove", "nobody"], "", "no user"),
    (["set", "erwin", "--password-stdin"], "short\n", "at least 8"),
    (["set", ".hidden", "--password-stdin"], "a long password\n", "user name"),
    (["set", "two words", "--password-stdin"], "a long password\n", "user name"),
    (["set", "ok\n", "--password-stdin"], "a long password\n", "user name"),
    (["set"], "", "Name the user"),
])
def test_refusals_change_nothing(setup, monkeypatch, capsys, argv, stdin, message):
    path, data_dir = setup
    with pytest.raises(SystemExit) as exc:
        run(monkeypatch, capsys, path, *argv, stdin=stdin)
    assert message in str(exc.value)
    assert not os.path.exists(users.path(data_dir)) or users.read(data_dir) == {}


def test_web_app_signs_in_users_from_the_file_and_follows_changes(setup, monkeypatch, capsys):
    path, data_dir = setup
    app = App(ConfigHolder(path, check_interval=0))
    login = lambda user, pw: call(app, "POST", "/dashboard/api/login", headers=WRITE,  # noqa: E731
                                  body=json.dumps({"user": user, "password": pw}).encode())
    assert login("erwin", "a long password").code == 401
    run(monkeypatch, capsys, path, "set", "erwin", "--password-stdin", stdin="a long password\n")
    first = login("erwin", "a long password")
    assert first.code == 200
    cookie = first.headers["set-cookie"].split(";")[0]
    me = call(app, "GET", "/dashboard/api/me", headers={"Cookie": cookie})
    assert me.code == 200 and me.json()["user"] == "erwin"
    run(monkeypatch, capsys, path, "set", "erwin", "--password-stdin", stdin="a new password\n")
    assert call(app, "GET", "/dashboard/api/me", headers={"Cookie": cookie}).code == 401  # its sessions ended
    assert login("boss", "boss-pass").code == 200


def test_unreadable_users_file_keeps_the_previous_users(setup, monkeypatch, capsys):
    path, data_dir = setup
    run(monkeypatch, capsys, path, "set", "erwin", "--password-stdin", stdin="a long password\n")
    holder = ConfigHolder(path, check_interval=0)
    assert "erwin" in holder.get().dashboard["users"]
    with open(users.path(data_dir), "w", encoding="utf-8") as fh:
        fh.write("{ half written")
    os.utime(users.path(data_dir), (1, 1))
    cfg = holder.get()
    assert "erwin" in cfg.dashboard["users"]  # the previous configuration stays
    assert load(path).users_error and "erwin" not in load(path).dashboard["users"]
    with pytest.raises(SystemExit) as exc:  # and nothing is written over a file that cannot be read
        run(monkeypatch, capsys, path, "set", "other", "--password-stdin", stdin="a long password\n")
    assert "could not be read" in str(exc.value)
    assert open(users.path(data_dir), encoding="utf-8").read() == "{ half written"


def test_no_users_page_says_how_to_add_one(tmp_path):
    path = tmp_path / "tidesight.toml"
    path.write_text('[server]\ndata_dir = "%s"\n' % str(tmp_path / "data").replace("\\", "/"), encoding="utf-8")
    app = App(ConfigHolder(str(path)))
    page = call(app, "GET", "/dashboard/")
    assert page.code == 403 and b"jobs user set NAME" in page.body
    api = call(app, "GET", "/dashboard/api/sites")
    assert api.code == 403 and api.json()["setup"] is True
