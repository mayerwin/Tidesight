#!/usr/bin/env python3
"""FastCGI entry point: Tidesight in a long-lived process, on hosts without Passenger (Dreamhost and most shared
hosts run *.fcgi files with Apache's mod_fcgid).

Copy it into the web directory, make it executable (chmod 755) and route requests to it with the .htaccess rules
in docs/deploy-dreamhost.md. One process then answers many requests; run as a plain CGI script instead, it answers
one. It finds the code in ~/tidesight/server/python (or $TIDESIGHT_CODE, or next to this file) and the
configuration in ~/tidesight/tidesight.toml (or $TIDESIGHT_CONFIG). To use another Python, change the first line.
After an update of the code, running processes finish their request and make way for fresh ones by themselves.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.environ.get("TIDESIGHT_CODE", "")
if not CODE:
    CODE = HERE if os.path.isdir(os.path.join(HERE, "tidesight")) else os.path.expanduser("~/tidesight/server/python")
sys.path.insert(0, CODE)

from tidesight.fcgi import run  # noqa: E402


def load():
    from tidesight.wsgi import application
    return application


if __name__ == "__main__":
    run(load, watch=[os.path.abspath(__file__)])
