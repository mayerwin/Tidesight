#!/usr/bin/env python3
"""CGI fallback entry point, for hosts without Passenger.

Copy it into the web directory, make it executable (chmod 755) and route requests to it with
the .htaccess rules in docs/deploy-dreamhost.md. One process per request: slower than Passenger.
It finds the code in ~/tidesight/server/python (or $TIDESIGHT_CODE, or next to this file) and
the configuration in ~/tidesight/tidesight.toml (or $TIDESIGHT_CONFIG).
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.environ.get("TIDESIGHT_CODE", "")
if not CODE:
    CODE = HERE if os.path.isdir(os.path.join(HERE, "tidesight")) else os.path.expanduser("~/tidesight/server/python")
sys.path.insert(0, CODE)

from wsgiref.handlers import CGIHandler  # noqa: E402

from tidesight.wsgi import application  # noqa: E402

if __name__ == "__main__":
    CGIHandler().run(application)
