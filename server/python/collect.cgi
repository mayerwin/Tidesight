#!/usr/bin/env python3
"""CGI fallback entry point, for hosts without Passenger.

Make it executable (chmod 755) and route requests to it, e.g. with
.htaccess rules (see docs/deploy-dreamhost.md). Slower than Passenger:
one process per request.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from wsgiref.handlers import CGIHandler  # noqa: E402

from tidesight.wsgi import application  # noqa: E402

if __name__ == "__main__":
    CGIHandler().run(application)
