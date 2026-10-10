"""Updates: the daily look for a newer release, and installing one in place (updates.py)."""

from __future__ import annotations

import io
import json
import os
import tarfile

import pytest

from test_team import api, cookie_of, link_call, team  # noqa: F401  (fixtures)
from test_users import fresh  # noqa: F401
from tidesight import __repository__, __version__, updates
from tidesight.config import loads


@pytest.fixture
def cfg(tmp_path):
    data = str(tmp_path / "data").replace("\\", "/")
    os.makedirs(data)
    return loads('[server]\ndata_dir = "%s"\n' % data, str(tmp_path / "tidesight.toml"))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def code_tree(root, version, fcgi="#!/usr/bin/env python3\n# entry, old\n", htaccess="RewriteEngine On\n# old\n",
              extra=None):
    write(os.path.join(root, "server", "python", "tidesight", "__init__.py"), '__version__ = "%s"\n' % version)
    write(os.path.join(root, "server", "python", "tidesight", "jobs.py"), "X = 1\n")
    write(os.path.join(root, "server", "python", "tidesight.fcgi"), fcgi)
    write(os.path.join(root, "server", "python", "web.htaccess"), htaccess)
    write(os.path.join(root, "README.md"), "Tidesight %s\n" % version)
    for rel, text in (extra or {}).items():
        write(os.path.join(root, *rel.split("/")), text)


def release(tmp_path, version, members=None, **kw):
    """A download function serving a release archive as GitHub makes it (one top folder)."""
    src = tmp_path / ("release-src-" + version)
    code_tree(str(src), version, fcgi="#!/usr/bin/env python3\n# entry, new\n", htaccess="RewriteEngine On\n# new\n", **kw)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(str(src), arcname="Tidesight-%s" % version)
        for name, data in (members or {}).items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    asked = []

    def download(url, dest, limit):
        asked.append(url)
        with open(dest, "wb") as fh:
            fh.write(buf.getvalue())

    download.asked = asked
    return download


def test_versions_and_the_daily_look(cfg):
    assert updates.parse_version("v1.10.2") == (1, 10, 2) and updates.parse_version("1.2") is None
    assert updates.due(cfg)
    st = updates.check(cfg, head=lambda url: (302, __repository__ + "/releases/tag/v99.0.0"))
    assert st["latest"] == "99.0.0" and st["available"] is True and st["current"] == __version__
    assert not updates.due(cfg)
    st = updates.check(cfg, head=lambda url: (302, __repository__ + "/releases"))  # a repository without releases
    assert st["latest"] is None and st["available"] is False and "no release" in st["error"]
    st = updates.check(cfg, head=lambda url: (_ for _ in ()).throw(OSError("offline")))
    assert "offline" in st["error"]
    st = updates.check(cfg, head=lambda url: (302, __repository__ + "/releases/tag/v" + __version__))
    assert st["available"] is False  # the running version


def test_install_swaps_the_code_and_keeps_what_the_release_lacks(cfg, tmp_path):
    app, web = str(tmp_path / "site" / "app"), str(tmp_path / "site")
    code_tree(app, "0.1.0", extra={"tidesight.toml": "[server]\n", "notes.txt": "mine\n"})
    write(os.path.join(web, "tidesight.fcgi"), "#!/usr/bin/env python3\n# entry, old\n")  # an untouched copy
    write(os.path.join(web, ".htaccess"), "RewriteEngine On\n# edited by the owner\n")
    download = release(tmp_path, "9.9.9")
    done = updates.install(cfg, "9.9.9", code_dir=app, download=download, web_dirs=[web])
    assert download.asked == [__repository__ + "/archive/refs/tags/v9.9.9.tar.gz"]
    assert done["to"] == "9.9.9" and updates._announced_version(app) == "9.9.9"
    assert updates._announced_version(app + ".previous") == "0.1.0"
    assert open(os.path.join(app, "tidesight.toml")).read() == "[server]\n"  # carried over
    assert open(os.path.join(app, "notes.txt")).read() == "mine\n"
    assert open(os.path.join(web, "tidesight.fcgi")).read().endswith("# entry, new\n")  # refreshed
    assert open(os.path.join(web, ".htaccess")).read().endswith("# edited by the owner\n")  # left alone, and said
    assert any("edited by hand" in n for n in done["notes"])
    assert not [n for n in os.listdir(os.path.dirname(app)) if ".update-" in n]  # no leftovers

    rolled = updates.rollback(cfg, code_dir=app)
    assert (rolled["from"], rolled["to"]) == ("9.9.9", "0.1.0") and updates._announced_version(app) == "0.1.0"
    assert updates.state(cfg)["last_update"]["rollback"] is True
    assert updates._announced_version(app + ".previous") == "9.9.9"


@pytest.mark.parametrize("problem", ["git", "version", "path", "syntax", "link"])
def test_a_failed_install_changes_nothing(cfg, tmp_path, problem):
    app = str(tmp_path / "app")
    code_tree(app, "0.1.0")
    kw = {}
    if problem == "git":
        os.makedirs(os.path.join(app, ".git"))
    if problem == "path":
        kw["members"] = {"Tidesight-9.9.9/../escaped.txt": b"x"}
    if problem == "syntax":
        kw["extra"] = {"server/python/tidesight/broken.py": "def (:\n"}
    download = release(tmp_path, "9.9.8" if problem == "version" else "9.9.9", **kw)
    if problem == "link":
        def download(url, dest, limit):  # noqa: F811  an archive with a symbolic link out of the folder
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                info = tarfile.TarInfo("Tidesight-9.9.9/server/python/tidesight/__init__.py")
                data = b'__version__ = "9.9.9"\n'
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
                link = tarfile.TarInfo("Tidesight-9.9.9/evil")
                link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
                tar.addfile(link)
            open(dest, "wb").write(buf.getvalue())
    if problem == "link":
        updates.install(cfg, "9.9.9", code_dir=app, download=download)  # the link is skipped, never followed
        assert not os.path.lexists(os.path.join(app, "evil"))
        return
    with pytest.raises(updates.UpdateError):
        updates.install(cfg, "9.9.9", code_dir=app, download=download)
    assert updates._announced_version(app) == "0.1.0" and not os.path.exists(app + ".previous")
    assert not os.path.exists(str(tmp_path / "escaped.txt"))
    assert not [n for n in os.listdir(str(tmp_path)) if ".update-" in n]


def test_a_requested_update_runs_once(cfg, tmp_path, monkeypatch):
    app = str(tmp_path / "app")
    code_tree(app, "0.1.0")
    monkeypatch.setattr(updates, "CODE_DIR", app)  # (the repository running the tests is a git checkout)
    updates.check(cfg, head=lambda url: (302, __repository__ + "/releases/tag/v9.9.9"))
    with pytest.raises(updates.UpdateError):
        updates.request(cfg, "9.9.8", "owner")  # only the latest
    updates.request(cfg, "9.9.9", "owner")
    assert updates.state(cfg)["request"]["requested_by"] == "owner"
    done = updates.run_requested(cfg, download=release(tmp_path, "9.9.9"), code_dir=app)
    assert done["to"] == "9.9.9" and updates.state(cfg)["request"] is None
    assert updates.state(cfg)["last_update"]["requested_by"] == "owner"
    assert updates.run_requested(cfg, download=release(tmp_path, "9.9.9"), code_dir=app) is None  # spent
    updates.check(cfg, head=lambda url: (302, __repository__ + "/releases/tag/v9.9.10"))
    updates.request(cfg, "9.9.10", "owner")
    failed = updates.run_requested(cfg, download=release(tmp_path, "9.9.9"), code_dir=app)  # announces 9.9.9
    assert "announces" in failed["error"] and updates.state(cfg)["last_update"]["error"]
    assert updates._announced_version(app) == "9.9.9"


def test_admins_ask_for_an_update_in_the_dashboard(team, tmp_path, monkeypatch):
    app, data_dir, path, owner = team
    monkeypatch.setattr(updates, "CODE_DIR", str(tmp_path / "app"))  # not the git checkout running the tests
    from tidesight.config import load
    cfg = load(path)
    st = api(app, "GET", "update", owner).json()
    assert st["current"] == __version__ and st["available"] is False and st["repository"] == __repository__
    updates.check(cfg, head=lambda url: (302, __repository__ + "/releases/tag/v99.0.0"))
    assert api(app, "GET", "update", owner).json()["available"] is True
    assert api(app, "POST", "update", owner, {"version": "98.0.0"}).code == 409
    asked = api(app, "POST", "update", owner, {"version": "99.0.0"})
    assert asked.code == 201 and asked.json()["request"]["version"] == "99.0.0"
    assert api(app, "DELETE", "update", owner).json()["request"] is None
    token = api(app, "POST", "users", owner, {"name": "viewer1", "role": "viewer"}).json()["token"]
    viewer = cookie_of(link_call(app, {"token": token, "password": "viewer password"}))
    assert api(app, "GET", "update", viewer).code == 403 and api(app, "POST", "update", viewer, {"version": "99.0.0"}).code == 403
    os.makedirs(str(tmp_path / "app" / ".git"))
    refused = api(app, "POST", "update", owner, {"version": "99.0.0"})
    assert refused.code == 409 and "git pull" in refused.json()["error"]  # a git checkout is left to git


def test_one_update_at_a_time(cfg, tmp_path, monkeypatch):
    from tidesight import jobs
    from tidesight.enrich.db import JobLock
    app = str(tmp_path / "app")
    code_tree(app, "0.1.0")
    monkeypatch.setattr(updates, "CODE_DIR", app)
    updates.check(cfg, head=lambda url: (302, __repository__ + "/releases/tag/v9.9.9"))
    updates.request(cfg, "9.9.9", "owner")
    with JobLock(cfg.data_dir, "update"):  # another run is updating: the request waits for the next run
        assert updates.run_requested(cfg, download=release(tmp_path, "9.9.9"), code_dir=app) is None
        with pytest.raises(updates.UpdateError):
            updates.install_now(cfg, "9.9.9", download=release(tmp_path, "9.9.9"), code_dir=app)
        with pytest.raises(updates.UpdateError):
            updates.rollback(cfg, code_dir=app)
    assert updates.state(cfg)["request"]["version"] == "9.9.9"
    failed = updates.install_now(cfg, "9.9.9", download=release(tmp_path, "9.9.8"), code_dir=app)
    assert "announces" in failed["error"] and updates.state(cfg)["request"] is None  # the command line replaces it
    assert updates.state(cfg)["last_update"]["error"] == failed["error"]

    path = str(tmp_path / "tidesight.toml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('[server]\ndata_dir = "%s"\n' % cfg.data_dir.replace("\\", "/"))
    with pytest.raises(SystemExit, match="No release to install: no release published"):
        jobs.main(["-q", "--config", path, "update", "--install"])  # (the stand-in GitHub has no release)


def test_an_interrupted_install_is_said_and_not_tried_again(cfg, tmp_path, monkeypatch):
    app = str(tmp_path / "app")
    code_tree(app, "0.1.0")
    monkeypatch.setattr(updates, "CODE_DIR", app)
    os.makedirs(str(tmp_path / ".app.update-dead" / "code"))  # left by a run that was stopped half-way
    updates.check(cfg, head=lambda url: (302, __repository__ + "/releases/tag/v9.9.9"))
    updates.request(cfg, "9.9.9", "owner")
    seen = {}

    def download(url, dest, limit):  # while it downloads, the request says the installation started
        seen["request"] = updates.state(cfg)["request"]
        release(tmp_path, "9.9.9")(url, dest, limit)

    assert updates.run_requested(cfg, download=download, code_dir=app)["to"] == "9.9.9"
    assert seen["request"]["started_at"] >= seen["request"]["requested_at"]
    with pytest.raises(updates.UpdateError):
        updates.request(cfg, "9.9.8", "owner")
    assert not os.path.exists(str(tmp_path / ".app.update-dead"))
    assert updates.state(cfg)["request"] is None

    updates.check(cfg, head=lambda url: (302, __repository__ + "/releases/tag/v9.9.10"))
    updates.request(cfg, "9.9.10", "owner")
    started = dict(updates.state(cfg)["request"], started_at=1)  # the process installing it was killed
    updates._write(updates._path(cfg, updates.REQUEST_FILE), started)
    with pytest.raises(updates.UpdateError, match="being installed"):
        updates.request(cfg, "9.9.10", "owner")
    done = updates.run_requested(cfg, download=release(tmp_path, "9.9.10"), code_dir=app)
    assert "interrupted" in done["error"] and updates.state(cfg)["request"] is None
    assert updates._announced_version(app) == "9.9.9"  # not tried again


def test_an_update_being_installed_cannot_be_withdrawn(team, tmp_path, monkeypatch):
    app, data_dir, path, owner = team
    monkeypatch.setattr(updates, "CODE_DIR", str(tmp_path / "app"))
    from tidesight.config import load
    cfg = load(path)
    updates.check(cfg, head=lambda url: (302, __repository__ + "/releases/tag/v99.0.0"))
    assert api(app, "POST", "update", owner, {"version": "99.0.0"}).code == 201
    updates._write(updates._path(cfg, updates.REQUEST_FILE), dict(updates.state(cfg)["request"], started_at=1))
    refused = api(app, "DELETE", "update", owner)
    assert refused.code == 409 and "being installed" in refused.json()["error"]
    assert api(app, "POST", "update", owner, {"version": "99.0.0"}).code == 409
    assert api(app, "GET", "update", owner).json()["request"]["started_at"] == 1
