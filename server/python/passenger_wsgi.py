"""Passenger entry point (Dreamhost and other Passenger hosts).

Place this directory as the Passenger app root (or symlink this file there)
and set TIDESIGHT_CONFIG, or put the config at ~/tidesight/tidesight.toml.
See docs/deploy-dreamhost.md.
"""

import os
import sys

# Optional: run under a specific interpreter (e.g. a newer Python built with pyenv).
INTERP = os.environ.get("TIDESIGHT_PYTHON", "")
if INTERP and os.path.realpath(sys.executable) != os.path.realpath(INTERP) and os.path.exists(INTERP):
    os.execl(INTERP, INTERP, *sys.argv)

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from tidesight.wsgi import application  # noqa: E402,F401
