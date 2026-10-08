"""The deployable WSGI application: collector endpoints (and, later, the dashboard API).

Passenger, CGI and the dev server all import `application` from here.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Optional

from .collect import make_app
from .config import Config

_app: Optional[Callable[..., Iterable[bytes]]] = None


def create_app(config_path: Optional[str] = None, config: Optional[Config] = None):
    """Build the full application."""
    return make_app(config_path, config)


def application(environ: Dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
    """Lazily created on the first request, using $TIDESIGHT_CONFIG or a conventional path."""
    global _app
    if _app is None:
        _app = create_app()
    return _app(environ, start_response)
