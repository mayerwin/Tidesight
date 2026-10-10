"""Dashboard users kept in data_dir/users.json (`jobs.py user`), merged with the configuration file's."""

from __future__ import annotations

import io
import json
import os
import re
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


# -- the first account, created in the dashboard with the setup code ----------------------------------------

CODE_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]{4}(-[0-9A-HJKMNP-TV-Z]{4}){3}\Z")


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A Tidesight without any user, and a fast password hash."""
    from tidesight import auth
    real = auth.hash_password
    monkeypatch.setattr(auth, "hash_password", lambda password, **kw: real(password, iterations=1000))
    monkeypatch.setattr(jobs, "hash_password", lambda password, **kw: real(password, iterations=1000))
    data_dir = str(tmp_path / "data").replace("\\", "/")
    path = tmp_path / "tidesight.toml"
    path.write_text('[server]\ndata_dir = "%s"\npublic_url = "https://stats.example.com/"\n' % data_dir, encoding="utf-8")
    return App(ConfigHolder(str(path), check_interval=0)), data_dir, str(path)


def setup_call(app, body, headers=WRITE):
    return call(app, "POST", "/dashboard/api/setup", headers=headers, body=json.dumps(body).encode())


def test_first_account_needs_the_setup_code(fresh):
    app, data_dir, _ = fresh
    page = call(app, "GET", "/dashboard/")  # the page loads: it offers to create the first account
    assert page.code == 200 and page.headers["content-type"].startswith("text/html")
    assert call(app, "GET", "/dashboard/api/me").json() == {"user": None, "setup": True}
    code = open(users.setup_path(data_dir), encoding="ascii").read().strip()
    assert CODE_RE.match(code)
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(users.setup_path(data_dir)).st_mode) == 0o600
    assert call(app, "GET", "/dashboard/api/sites").json()["setup"] is True  # nothing else answers
    assert call(app, "GET", "/dashboard/api/me").json()["setup"] is True
    assert open(users.setup_path(data_dir), encoding="ascii").read().strip() == code  # the same code every time

    body = {"code": code, "user": "erwin", "password": "a long password"}
    assert setup_call(app, body, headers={"Content-Type": "application/json"}).code == 403  # not from the page
    assert setup_call(app, dict(body, code="ABCD-EFGH-JKMN-PQRS")).code == 401
    assert setup_call(app, dict(body, user="two words")).code == 400
    assert setup_call(app, dict(body, password="short")).code == 400
    assert not os.path.exists(users.path(data_dir))
    # Typed loosely: lower case, spaces instead of dashes, O for 0 and I or L for 1 all pass.
    loose = code.lower().replace("-", " ").replace("0", "o").replace("1", "l")
    created = setup_call(app, dict(body, code=loose))
    assert created.code == 200 and created.json() == {"user": "erwin", "admin": True}
    cookie = created.headers["set-cookie"].split(";")[0]
    assert "Max-Age=" in created.headers["set-cookie"]  # kept signed in unless asked otherwise
    assert list(users.read(data_dir)) == ["erwin"]
    assert not os.path.exists(users.setup_path(data_dir))  # its job is done
    assert call(app, "GET", "/dashboard/api/me", headers={"Cookie": cookie}).json()["user"] == "erwin"
    assert setup_call(app, dict(body, code=code, user="intruder")).code == 409  # never again
    assert call(app, "GET", "/dashboard/api/me").code == 401  # an ordinary sign-in from now on
    signed = call(app, "POST", "/dashboard/api/login", headers=WRITE,
                  body=json.dumps({"user": "erwin", "password": "a long password"}).encode())
    assert signed.code == 200


def test_setup_code_guessing_is_throttled(fresh):
    app, data_dir, _ = fresh
    call(app, "GET", "/dashboard/api/me")
    code = users.setup_code(data_dir, create=False)
    for _ in range(10):
        assert setup_call(app, {"code": "0000-0000-0000-0000", "user": "x", "password": "a long password"}).code == 401
    blocked = setup_call(app, {"code": code, "user": "x", "password": "a long password"})
    assert blocked.code == 429 and users.read(data_dir) == {}


def test_setup_link_and_the_command_line_close_the_form(fresh, monkeypatch, capsys):
    app, data_dir, path = fresh
    jobs.main(["--config", path, "setup-link"])
    printed = capsys.readouterr().out
    code = users.setup_code(data_dir, create=False)
    assert "https://stats.example.com/dashboard/#setup=" + code in printed
    status = jobs.cmd_status(load(path), None)
    assert status["dashboard_users"] == 0 and "setup-code.txt" in status["next_step"]
    run(monkeypatch, capsys, path, "set", "erwin", "--password-stdin", stdin="a long password\n")
    assert not os.path.exists(users.setup_path(data_dir))  # a user exists: the form is closed
    assert setup_call(app, {"code": code, "user": "x", "password": "a long password"}).code == 409
    with pytest.raises(SystemExit) as exc:
        jobs.main(["--config", path, "setup-link"])
    assert "already has users" in str(exc.value)


def test_cron_removes_a_setup_code_left_behind(setup, monkeypatch):
    path, data_dir = setup
    users.setup_code(data_dir)  # e.g. written by a process that had not seen the first user yet
    monkeypatch.setattr("tidesight.enrich.pipeline.run", lambda cfg, max_seconds=None: {})
    jobs.main(["-q", "--config", path, "cron"])
    assert not os.path.exists(users.setup_path(data_dir))


def test_no_setup_when_the_configuration_file_has_users(setup):
    path, data_dir = setup
    app = App(ConfigHolder(path, check_interval=0))
    assert call(app, "GET", "/dashboard/api/me").code == 401
    assert setup_call(app, {"code": "x", "user": "x", "password": "a long password"}).code == 409
    assert not os.path.exists(users.setup_path(data_dir))


# -- from the security review of the first-account form ---------------------------------------------------------

def test_no_dashboard_when_the_data_is_inside_the_web_root(tmp_path, monkeypatch):
    """With the data where the web server hands files out, a setup code written there could be downloaded."""
    root = tmp_path / "www"
    (root / "data").mkdir(parents=True)
    path = root / "tidesight.toml"
    path.write_text('[server]\ndata_dir = "data"\n', encoding="utf-8")
    app = App(ConfigHolder(str(path), check_interval=0))
    env = {"DOCUMENT_ROOT": str(root)}
    for url in ("/dashboard/", "/dashboard/api/me", "/share/x/"):
        assert call(app, "GET", url, headers=env).code == 503
    assert not os.path.exists(users.setup_path(str(root / "data")))


@pytest.mark.parametrize("content", ["", "\0" * 19 + "\n", "----\n", "too-short\n", "IIII-LLLL-OOOO-UUUU\n"])
def test_a_damaged_setup_code_matches_nothing_and_is_replaced(fresh, content):
    app, data_dir, _ = fresh
    os.makedirs(data_dir, exist_ok=True)
    with open(users.setup_path(data_dir), "w", encoding="latin-1") as fh:
        fh.write(content)
    for given in ("", "----", content, "\0" * 16):
        assert setup_call(app, {"code": given, "user": "x", "password": "a long password"}).code in (401, 429)
    assert users.read(data_dir) == {}
    assert call(app, "GET", "/dashboard/api/me").json()["setup"] is True  # a fresh, valid code replaces it
    assert CODE_RE.match(users.setup_code(data_dir, create=False))


def test_setup_never_signs_in_as_a_user_of_the_configuration_file(fresh):
    app, data_dir, path = fresh
    stale = App(ConfigHolder(path, check_interval=3600))  # a process that has not looked at the files for a while
    call(stale, "GET", "/dashboard/api/me")
    code = users.setup_code(data_dir, create=False)
    with open(path, "a", encoding="utf-8") as fh:  # meanwhile, the owner adds a user to the configuration file
        fh.write('\n[dashboard]\nusers = { boss = "%s" }\n' % hash_password("boss-pass", iterations=1000))
    answer = setup_call(stale, {"code": code, "user": "boss", "password": "a long password"})
    assert answer.code == 409 and "set-cookie" not in answer.headers and users.read(data_dir) == {}


def test_other_processes_see_the_first_user_at_once(fresh):
    app, data_dir, path = fresh
    other = App(ConfigHolder(path, check_interval=3600))
    assert call(other, "GET", "/dashboard/api/me").json()["setup"] is True
    code = users.setup_code(data_dir, create=False)
    assert setup_call(app, {"code": code, "user": "erwin", "password": "a long password"}).code == 200
    assert call(other, "GET", "/dashboard/api/me").code == 401  # not the form again after the reload
    assert not os.path.exists(users.setup_path(data_dir))  # and no new code


def test_text_that_is_not_text_is_refused(setup):
    path, _ = setup
    app = App(ConfigHolder(path, check_interval=0))
    for body in (rb'{"user": "boss", "password": "\ud800abc"}', rb'{"user": "\udfff", "password": "x"}'):
        assert call(app, "POST", "/dashboard/api/login", headers=WRITE, body=body).code == 400
    from tidesight.auth import Authenticator, Throttle
    assert Authenticator({"boss": hash_password("x", iterations=1000)}, Throttle()).login("boss", "\ud800") == (None, "denied")


def test_web_db_is_created_once_when_requests_race(tmp_path):
    import threading
    from tidesight.webdb import WebDB
    errors = []

    def reserve(i):
        try:
            WebDB(str(tmp_path / "data")).throttle_reserve("client-%d" % i, 10, 600)
        except Exception as exc:  # e.g. a migration run twice: "table already exists"
            errors.append(exc)

    threads = [threading.Thread(target=reserve, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
