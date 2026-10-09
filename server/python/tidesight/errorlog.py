"""Bounded error logging for the request path (standard library only).

Full details for the first few errors of each minute, then only a count: a flood of failing requests (or a
database locked for a while) must not become a flood of log lines on a shared host's disk. One budget per
process, shared by the collector, the dashboard API and the storage layer.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional


class ErrorLog:
    def __init__(self, per_minute: int = 5, logger: Optional[logging.Logger] = None) -> None:
        self.per_minute = per_minute
        self.log = logger or logging.getLogger("tidesight.collect")
        self._minute = 0
        self._count = 0
        self._lock = threading.Lock()

    def _allowed(self, logger: logging.Logger) -> bool:
        minute = int(time.time() // 60)
        with self._lock:
            if minute != self._minute:
                if self._count > self.per_minute:
                    logger.error("%d more errors in the previous minute (not logged)", self._count - self.per_minute)
                self._minute, self._count = minute, 0
            self._count += 1
            return self._count <= self.per_minute

    def exception(self, msg: str, *args: Any, logger: Optional[logging.Logger] = None) -> None:
        """Log msg with the current exception's traceback, within the budget."""
        logger = logger or self.log
        if self._allowed(logger):
            logger.exception(msg, *args)

    def error(self, msg: str, *args: Any, logger: Optional[logging.Logger] = None) -> None:
        logger = logger or self.log
        if self._allowed(logger):
            logger.error(msg, *args)


ERRORS = ErrorLog()
