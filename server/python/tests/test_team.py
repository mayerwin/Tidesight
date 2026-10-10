"""Managing users in the dashboard: invitations, password links, roles, removing people, changing one's password."""

from __future__ import annotations

import json

import pytest

from conftest import call
from test_users import WRITE, fresh, run, setup, setup_call  # noqa: F401  (fixtures)
from tidesight import jobs, users
from tidesight.config import ConfigHolder, load
from tidesight.wsgi import App


@pytest.fixture
def team(fresh):  # noqa: F811
    """A Tidesight whose first admin, "owner", was created with the setup code; and owner's session cookie."""
    app, data_dir, path = fresh
    call(app, "GET", "/dashboard/api/me")
    made = setup_call(app, {"code": users.setup_code(data_dir, create=False), "user": "owner",
                            "password": "owner password"})
    return app, data_dir, path, made.headers["set-cookie"].split(";")[0]


def api(app, method, endpoint, cookie, body=None, query=""):
    headers = dict(WRITE, Cookie=cookie) if method != "GET" else {"Cookie": cookie}
    data = json.dumps(body).encode() if body is not None else b""
    return call(app, method, "/dashboard/api/" + endpoint, headers=headers, body=data, query=query)


def link_call(app, body):
    return call(app, "POST", "/dashboard/api/password_link", headers=WRITE, body=json.dumps(body).encode())


def sign_in(app, user, password):
    return call(app, "POST", "/dashboard/api/login", headers=WRITE,
                body=json.dumps({"user": user, "password": password}).encode())


def cookie_of(res):
    return res.headers["set-cookie"].split(";")[0]


def test_roles():
    assert users.role_of("x", {}, [], {}) == "admin"  # no admin named anywhere: everyone, as before roles
    assert users.role_of("x", {}, ["boss"], {}) == "viewer"
    assert users.role_of("boss", {"boss": "h"}, ["boss"], {}) == "admin"
    assert users.role_of("x", {}, ["boss"], {"x": "admin"}) == "admin"
    assert users.role_of("x", {}, [], {"x": "viewer"}) == "viewer"
    assert users.role_of("boss", {"boss": "h"}, [], {"boss": "viewer"}) == "admin"  # the file's users are the file's
    table = {"a": {"hash": "pbkdf2_sha256$x", "role": "viewer"}, "b": {"hash": None, "role": "admin"}}
    assert users.active_admins({}, [], table) == []  # b cannot sign in yet
    assert users.active_admins({"boss": "h"}, [], table) == ["boss"]


def test_admins_invite_people_who_choose_their_password(team):
    app, data_dir, path, owner = team
    invited = api(app, "POST", "users", owner, {"name": "alice", "role": "viewer"})
    assert invited.code == 201 and invited.json()["kind"] == "invite"
    token = invited.json()["token"]
    listed = {u["name"]: u for u in api(app, "GET", "users", owner).json()["users"]}
    assert listed["alice"]["status"] == "invited" and listed["alice"]["role"] == "viewer"
    assert listed["owner"]["you"] and listed["owner"]["role"] == "admin" and listed["owner"]["last_sign_in"]
    assert api(app, "POST", "users", owner, {"name": "alice"}).code == 409
    assert sign_in(app, "alice", "anything at all").code == 401  # no password yet

    assert link_call(app, {"token": token}).json() == {"user": "alice", "kind": "invite"}
    assert link_call(app, {"token": token, "password": "short"}).code == 400
    chosen = link_call(app, {"token": token, "password": "alice password"})
    assert chosen.code == 200 and chosen.json() == {"user": "alice", "admin": False}
    alice = cookie_of(chosen)
    assert api(app, "GET", "me", alice).json()["user"] == "alice"
    assert link_call(app, {"token": token, "password": "another password"}).code == 401  # once only
    assert api(app, "GET", "users", alice).code == 403  # a viewer manages nothing
    assert api(app, "POST", "users", alice, {"name": "mallory", "role": "admin"}).code == 403
    assert api(app, "POST", "user_link", alice, query="name=owner").code == 403

    # Forgotten: an admin makes a password link; the old password works until the link is used.
    reset = api(app, "POST", "user_link", owner, query="name=alice").json()
    assert reset["kind"] == "reset"
    assert sign_in(app, "alice", "alice password").code == 200
    assert link_call(app, {"token": reset["token"], "password": "alice new password"}).code == 200
    assert api(app, "GET", "me", alice).code == 401  # her sessions ended
    assert sign_in(app, "alice", "alice password").code == 401
    again = sign_in(app, "alice", "alice new password")
    assert again.code == 200

    promoted = api(app, "PATCH", "user", owner, {"role": "admin"}, query="name=alice")
    assert promoted.json() == {"name": "alice", "role": "admin"}
    assert api(app, "GET", "me", cookie_of(again)).json()["admin"] is True
    assert api(app, "PATCH", "user", owner, {"role": "viewer"}, query="name=owner").code == 409  # not one's own
    assert api(app, "DELETE", "user", owner, query="name=owner").code == 409  # nor oneself
    assert api(app, "DELETE", "user", owner, query="name=alice").json() == {"deleted": "alice"}
    assert api(app, "GET", "me", cookie_of(again)).code == 401 and "alice" not in users.records(data_dir)
    assert api(app, "DELETE", "user", owner, query="name=nobody").code == 404


def test_an_invitation_can_be_renewed_and_the_old_link_dies(team):
    app, data_dir, path, owner = team
    first = api(app, "POST", "users", owner, {"name": "bob", "role": "admin"}).json()["token"]
    second = api(app, "POST", "user_link", owner, query="name=bob").json()
    assert second["kind"] == "invite"
    assert link_call(app, {"token": first}).code == 401
    assert link_call(app, {"token": second["token"], "password": "bob password"}).json()["admin"] is True


def test_changing_ones_own_password(team):
    app, data_dir, path, owner = team
    assert api(app, "GET", "me", owner).json()["can_change_password"] is True
    other = cookie_of(sign_in(app, "owner", "owner password"))  # another browser
    wrong = api(app, "POST", "password", owner, {"current": "nope", "password": "new owner password"})
    assert wrong.code == 403 and api(app, "GET", "me", owner).code == 200  # still signed in
    assert api(app, "POST", "password", owner, {"current": "owner password", "password": "short"}).code == 400
    changed = api(app, "POST", "password", owner, {"current": "owner password", "password": "new owner password"})
    assert changed.code == 200
    assert api(app, "GET", "me", cookie_of(changed)).json()["user"] == "owner"  # this browser goes on
    assert api(app, "GET", "me", owner).code == 401 and api(app, "GET", "me", other).code == 401  # the others end
    assert sign_in(app, "owner", "new owner password").code == 200


def test_users_of_the_configuration_file_are_changed_there(setup):  # noqa: F811
    path, data_dir = setup
    app = App(ConfigHolder(path, check_interval=0))
    boss = cookie_of(sign_in(app, "boss", "boss-pass"))
    assert api(app, "GET", "me", boss).json()["can_change_password"] is False
    assert api(app, "POST", "password", boss, {"current": "boss-pass", "password": "a new password"}).code == 409
    listed = api(app, "GET", "users", boss).json()["users"]
    assert listed == [dict(listed[0], name="boss", source="config", status="active", role="admin")]
    for method, endpoint, body in (("PATCH", "user", {"role": "viewer"}), ("DELETE", "user", None),
                                   ("POST", "user_link", None)):
        assert api(app, method, endpoint, boss, body, query="name=boss").code == 409
    assert api(app, "POST", "users", boss, {"name": "boss"}).code == 409


def test_link_guessing_is_throttled(team):
    app, data_dir, path, owner = team
    token = api(app, "POST", "users", owner, {"name": "carol"}).json()["token"]
    for _ in range(10):
        assert link_call(app, {"token": "x" * 43}).code == 401
    assert link_call(app, {"token": token}).code == 429


def test_a_link_from_the_command_line(team, monkeypatch, capsys):
    app, data_dir, path, owner = team
    jobs.main(["--config", path, "user", "link", "owner"])
    printed = capsys.readouterr().out
    assert printed.count("https://stats.example.com/dashboard/#password=") == 1
    token = printed.split("#password=")[1].split()[0]
    assert link_call(app, {"token": token, "password": "recovered password"}).code == 200
    run(monkeypatch, capsys, path, "set", "viewer1", "--password-stdin", "--role", "viewer", stdin="a long password\n")
    assert load(path).role("viewer1") == "viewer"
    with pytest.raises(SystemExit) as exc:  # the last admin who can sign in stays
        run(monkeypatch, capsys, path, "remove", "owner")
    assert "last admin" in str(exc.value)


# -- from the security review of these features ----------------------------------------------------------------

def test_user_set_keeps_a_role(setup, monkeypatch, capsys):  # noqa: F811
    path, data_dir = setup
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('admins = ["boss"]\n')  # under [dashboard]: users of users.json without a role are viewers
    users.update(data_dir, lambda table: table.setdefault("erwin", "pbkdf2_sha256$1000$c2FsdA$aGFzaA"))  # format 1
    assert load(path).role("erwin") == "viewer"
    run(monkeypatch, capsys, path, "set", "erwin", "--password-stdin", stdin="a new long password\n")
    assert load(path).role("erwin") == "viewer"  # a new password, not a promotion
    run(monkeypatch, capsys, path, "set", "newbie", "--password-stdin", stdin="a long password\n")
    assert load(path).role("newbie") == "viewer"
    run(monkeypatch, capsys, path, "set", "erwin", "--password-stdin", "--role", "admin", stdin="a long password\n")
    assert load(path).role("erwin") == "admin"


def test_another_process_sees_a_demotion_at_once(team):
    app, data_dir, path, owner = team
    other = App(ConfigHolder(path, check_interval=3600))  # a second process, with its configuration in memory
    token = api(app, "POST", "users", owner, {"name": "alice", "role": "admin"}).json()["token"]
    alice = cookie_of(link_call(app, {"token": token, "password": "alice password"}))
    assert api(other, "GET", "me", alice).json()["admin"] is True
    assert api(app, "PATCH", "user", owner, {"role": "viewer"}, query="name=alice").json()["role"] == "viewer"
    assert api(other, "GET", "me", alice).json()["admin"] is False
    assert api(other, "POST", "users", alice, {"name": "mallory", "role": "admin"}).code == 403
    assert "mallory" not in users.records(data_dir)


def test_a_password_change_voids_older_links(team, monkeypatch, capsys):
    app, data_dir, path, owner = team
    token = api(app, "POST", "users", owner, {"name": "alice"}).json()["token"]
    link_call(app, {"token": token, "password": "alice password"})
    leaked = api(app, "POST", "user_link", owner, query="name=alice").json()["token"]
    alice = cookie_of(sign_in(app, "alice", "alice password"))
    assert api(app, "POST", "password", alice, {"current": "alice password", "password": "alice changed it"}).code == 200
    assert link_call(app, {"token": leaked}).code == 401  # her own change voided it
    leaked = api(app, "POST", "user_link", owner, query="name=alice").json()["token"]
    run(monkeypatch, capsys, path, "set", "alice", "--password-stdin", stdin="set on the server\n")
    assert link_call(app, {"token": leaked}).code == 401  # so did one set on the server


def test_names_of_the_configuration_files_admins(team):
    app, data_dir, path, owner = team
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('\n[dashboard]\nadmins = ["owner", "chief"]\n')
    app = App(ConfigHolder(path, check_interval=0))
    token = api(app, "POST", "users", owner, {"name": "chief", "role": "admin"}).json()["token"]
    assert api(app, "POST", "users", owner, {"name": "deputy", "role": "viewer"}).code == 201
    assert api(app, "POST", "users", owner, {"name": "chief2", "role": "viewer"}).code == 201
    assert api(app, "PATCH", "user", owner, {"role": "viewer"}, query="name=chief").code == 409
    listed = {u["name"]: u for u in api(app, "GET", "users", owner).json()["users"]}
    assert listed["chief"]["role_locked"] is True and listed["deputy"]["role_locked"] is False
    assert link_call(app, {"token": token, "password": "chief password"}).json()["admin"] is True


def test_bad_input_is_refused_cleanly(team):
    app, data_dir, path, owner = team
    assert api(app, "POST", "user_link", owner, {"name": ["owner"]}).code == 400
    assert api(app, "POST", "user_link", owner, {"name": {"a": 1}}).code == 400


def test_passwords_only_over_https(team):
    app, data_dir, path, owner = team
    plain = dict(WRITE, Origin="http://stats.example.com", Host="stats.example.com")
    for endpoint, body in (("login", {"user": "owner", "password": "owner password"}),
                           ("password_link", {"token": "x" * 43}), ("setup", {"code": "x"})):
        answer = call(app, "POST", "/dashboard/api/" + endpoint, headers=plain, body=json.dumps(body).encode(),
                      scheme="http")
        assert answer.code == 403 and "HTTPS" in answer.json()["error"], endpoint
    page = call(app, "GET", "/dashboard/")
    assert page.headers["strict-transport-security"] == "max-age=31536000"
