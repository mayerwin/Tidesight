#!/usr/bin/env python3
"""CGI fallback entry point, for hosts without Passenger.

Copy it into the web directory, make it executable (chmod 755) and route requests to it with
the .htaccess rules in docs/deploy-dreamhost.md. One process per request: slower than Passenger.
It finds the code beside it (app/server/python), in ~/tidesight/server/python, or in $TIDESIGHT_CODE,
and the configuration beside it (tidesight.toml), in ~/tidesight/tidesight.toml, or in $TIDESIGHT_CONFIG.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.environ.get("TIDESIGHT_CODE", "")
if not CODE:
    for CODE in (HERE, os.path.join(HERE, "app", "server", "python"), os.path.expanduser("~/tidesight/server/python")):
        if os.path.isdir(os.path.join(CODE, "tidesight")):  # next to this file, in app/ beside it, or in ~/tidesight
            break
sys.path.insert(0, CODE)

from wsgiref.handlers import CGIHandler  # noqa: E402

from tidesight.wsgi import application  # noqa: E402

if __name__ == "__main__":
    CGIHandler().run(application)
