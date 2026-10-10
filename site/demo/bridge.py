"""The live demo's bridge, run by Pyodide in a web worker (site/demo/worker.js).

The dashboard's API calls are answered by Tidesight's own web app, on the demo data that tools/build_site.py made
with tools/demo.py ([dashboard] demo = true: everything can be seen, nothing can be changed). While the page is
open, tick() adds a visit now and then, so that the Realtime page has visitors arriving.
"""

import io
import json
import sys

sys.path.insert(0, "/app")

import demo  # noqa: E402  (tools/demo.py)
from tidesight.config import load  # noqa: E402
from tidesight.wsgi import create_app  # noqa: E402

CONFIG = "/demo/tidesight.toml"
app = create_app(CONFIG)
cfg = load(CONFIG)
demo.live(cfg, minutes=30, scale=0.35)  # the last half hour: Realtime has visitors from the start


def handle(method, path, query, headers, body):
    """One request to the web app: (status, reason, [[header, value]], body bytes)."""
    raw = body.encode("utf-8") if body else b""
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query, "SERVER_NAME": "demo",
               "SERVER_PORT": "443", "SERVER_PROTOCOL": "HTTP/1.1", "HTTP_HOST": "demo", "REMOTE_ADDR": "127.0.0.1",
               "wsgi.url_scheme": "https", "wsgi.input": io.BytesIO(raw), "wsgi.errors": sys.stderr,
               "wsgi.version": (1, 0), "wsgi.multithread": False, "wsgi.multiprocess": False, "wsgi.run_once": False,
               "CONTENT_LENGTH": str(len(raw)) if raw else "", "HTTP_SEC_FETCH_SITE": "same-origin"}
    for name, value in json.loads(headers or "{}").items():
        key = name.upper().replace("-", "_")
        environ[key if key in ("CONTENT_TYPE", "CONTENT_LENGTH") else "HTTP_" + key] = str(value)
    got = {}
    out = b"".join(app(environ, lambda status, hdrs, exc_info=None: got.update(status=status, headers=hdrs)))
    code, _, reason = got["status"].partition(" ")
    return int(code), reason, [list(h) for h in got["headers"]], out


def tick():
    """Now and then, someone new arrives (a visit every couple of minutes on each website)."""
    return demo.live(cfg, minutes=1, scale=0.12)
