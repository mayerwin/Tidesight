"""Helpers executed in child processes by the concurrency tests (must be importable)."""

from __future__ import annotations

import io
import json
from typing import Any, Dict, List


def collector_worker(config_text: str, config_path: str, worker: int, requests: int, out_q: Any) -> None:
    """Post `requests` batches through a fresh Collector, as one Passenger process would."""
    from wsgiref.util import setup_testing_defaults

    from tidesight.collect import Collector
    from tidesight.config import ConfigHolder, loads

    app = Collector(ConfigHolder(config=loads(config_text, config_path)))
    codes: List[int] = []
    for i in range(requests):
        env = {"v": 1, "site": "example", "vid": "visitor%08d" % worker, "sid": "session%08d" % worker,
               "events": [{"t": "event", "id": "w%d-r%d-e%d" % (worker, i, j), "n": "x"} for j in range(3)]}
        body = json.dumps(env).encode()
        environ: Dict[str, Any] = {}
        setup_testing_defaults(environ)
        environ.update({"REQUEST_METHOD": "POST", "PATH_INFO": "/collect", "wsgi.input": io.BytesIO(body),
                        "CONTENT_LENGTH": str(len(body)), "CONTENT_TYPE": "text/plain",
                        "HTTP_ORIGIN": "https://example.com", "REMOTE_ADDR": "10.0.%d.%d" % (worker, i % 250)})
        status: Dict[str, str] = {}

        def start_response(s: str, h: Any, e: Any = None) -> None:
            status["s"] = s

        b"".join(app(environ, start_response))
        codes.append(int(status["s"].split()[0]))
    app._store.close()
    out_q.put((worker, codes))
