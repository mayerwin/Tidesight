#!/usr/bin/env python3
"""FastCGI entry point: Tidesight in a long-lived process, on hosts without Passenger (Dreamhost and most shared
hosts run *.fcgi files with Apache's mod_fcgid).

Copy it into the web directory, make it executable (chmod 755) and send every request to it with the .htaccess
of server/python/web.htaccess (docs/deploy-dreamhost.md). One process then answers many requests; run as a plain
CGI script instead, it answers one. It finds the code beside it (app/server/python), in ~/tidesight/server/python,
or in $TIDESIGHT_CODE, and the configuration beside it (tidesight.toml), in ~/tidesight/tidesight.toml, or in
$TIDESIGHT_CONFIG. To use another Python, change the first line.
After an update of the code, running processes finish their request and make way for fresh ones by themselves.
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

from tidesight.fcgi import run  # noqa: E402


def load():
    from tidesight.wsgi import application
    return application


if __name__ == "__main__":
    run(load, watch=[os.path.abspath(__file__)])
