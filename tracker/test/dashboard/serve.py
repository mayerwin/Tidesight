"""Demo data and two dashboard servers for the dashboard tests (tracker/playwright.dashboard.config.mjs).

    http://127.0.0.1:8795  the dashboard without signing in (--no-auth)
    http://127.0.0.1:8796  the same data, signing in required: admin / test-pass (an admin), viewer / view-pass
    http://127.0.0.1:8797  a new installation without any user (the first-account form), in .data/setup

The first two share test/dashboard/.data, rebuilt from tools/demo.py at every start.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path[:0] = [os.path.join(ROOT, "server", "python"), os.path.join(ROOT, "tools")]
DATA = os.path.join(HERE, ".data")
SETUP = os.path.join(DATA, "setup")


def main() -> None:
    import demo
    from devserver import serve
    from tidesight.auth import hash_password
    from tidesight.wsgi import create_app

    os.makedirs(DATA, exist_ok=True)
    for name in ("properties.json", "users.json", "update.json", "update-request.json", "web.db", "web.db-wal",
                 "web.db-shm"):  # what the tests change
        try:
            os.remove(os.path.join(DATA, "data", name))
        except OSError:
            pass
    demo.main(["--out", DATA, "--days", "21", "--visitors", "400", "--seed", "3"])
    with open(os.path.join(DATA, "tidesight.toml"), encoding="utf-8") as fh:
        base = fh.read()
    with open(os.path.join(DATA, "auth.toml"), "w", encoding="utf-8") as fh:
        fh.write(base + '\n[dashboard]\nusers = { admin = "%s", viewer = "%s" }\nadmins = ["admin"]\n'
                 % (hash_password("test-pass", iterations=1000), hash_password("view-pass", iterations=1000)))
    shutil.rmtree(SETUP, ignore_errors=True)  # no user, no setup code yet
    os.makedirs(SETUP)
    with open(os.path.join(SETUP, "tidesight.toml"), "w", encoding="utf-8") as fh:
        fh.write('[server]\ndata_dir = "%s"\n' % os.path.join(SETUP, "data").replace("\\", "/"))
    servers = [serve(create_app(os.path.join(DATA, "tidesight.toml"), auth=False), "127.0.0.1", 8795),
               serve(create_app(os.path.join(DATA, "auth.toml")), "127.0.0.1", 8796),
               serve(create_app(os.path.join(SETUP, "tidesight.toml")), "127.0.0.1", 8797)]
    print("dashboard test servers on 8795 (open), 8796 (sign-in) and 8797 (no user yet)", flush=True)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
