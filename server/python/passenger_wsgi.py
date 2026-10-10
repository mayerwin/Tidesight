"""Passenger entry point (Dreamhost and other Passenger hosts).

Copy this file into the Passenger app root (e.g. ~/stats.example.com/passenger_wsgi.py).
It finds the Tidesight code beside it (app/server/python), in ~/tidesight/server/python, or in
$TIDESIGHT_CODE, and the configuration beside it (tidesight.toml), in ~/tidesight/tidesight.toml,
or in $TIDESIGHT_CONFIG. See docs/deploy-dreamhost.md.
"""

import os
import sys

# Optional: run under another interpreter (e.g. a newer Python built with pyenv).
# Set TIDESIGHT_PYTHON, or write the interpreter's path in a file named .tidesight-python next to this one.
HERE = os.path.dirname(os.path.abspath(__file__))
INTERP = os.environ.get("TIDESIGHT_PYTHON", "")
if not INTERP and os.path.exists(os.path.join(HERE, ".tidesight-python")):
    with open(os.path.join(HERE, ".tidesight-python")) as fh:
        INTERP = os.path.expanduser(fh.read().strip())
if INTERP and os.path.realpath(sys.executable) != os.path.realpath(INTERP) and os.path.exists(INTERP):
    os.execl(INTERP, INTERP, *sys.argv)

CODE = os.environ.get("TIDESIGHT_CODE", "")
if not CODE:
    for CODE in (HERE, os.path.join(HERE, "app", "server", "python"), os.path.expanduser("~/tidesight/server/python")):
        if os.path.isdir(os.path.join(CODE, "tidesight")):  # next to this file, in app/ beside it, or in ~/tidesight
            break
if CODE not in sys.path:
    sys.path.insert(0, CODE)

from tidesight.wsgi import application  # noqa: E402,F401
