"""A small per-principal rate limiter for the one route that costs money to call.

``POST /v1/proposals/evaluate`` runs the deterministic engine and may call a paid advisory model
(finding F-20: unbounded free text plus an unauthenticated route is a cost amplifier). The limiter is
a fixed-window counter keyed by tenant and agent, deliberately boring: it is a guard rail, not a
billing system, and a guard rail that needs a datastore is one that fails open when the datastore does.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

__all__ = ["FixedWindowRateLimiter", "RateLimit"]


@dataclass(frozen=True)
class RateLimit:
    """``max_requests`` per ``window_seconds``. Zero requests means the route is closed."""

    max_requests: int = 60
    window_seconds: int = 60

    def __post_init__(self) -> None:
        if self.max_requests < 0 or self.window_seconds <= 0:
            raise ValueError("max_requests must be >= 0 and window_seconds > 0")


class FixedWindowRateLimiter:
    """Thread-safe fixed-window counter. ``allow`` returns False when the window is exhausted."""

    def __init__(self, limit: RateLimit, *, clock: Callable[[], datetime]) -> None:
        self.limit = limit
        self.clock = clock
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[int, int]] = {}

    def allow(self, key: str) -> bool:
        window_seconds = self.limit.window_seconds
        now = int(self.clock().timestamp())
        window = now // window_seconds
        with self._lock:
            current_window, count = self._windows.get(key, (window, 0))
            if current_window != window:
                current_window, count = window, 0
            if count >= self.limit.max_requests:
                self._windows[key] = (current_window, count)
                return False
            self._windows[key] = (current_window, count + 1)
            return True
