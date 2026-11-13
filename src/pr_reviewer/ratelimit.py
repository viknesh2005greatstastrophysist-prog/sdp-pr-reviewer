"""Tiny in-memory sliding-window rate limiter, keyed by client address."""

from __future__ import annotations

import time
from collections import defaultdict, deque

MAX_REQUESTS = 20
WINDOW_SECONDS = 60.0
MAX_KEYS = 5000


class RateLimiter:
    def __init__(
        self,
        limit: int = MAX_REQUESTS,
        window: float = WINDOW_SECONDS,
        clock=time.monotonic,
    ):
        self.limit = limit
        self.window = window
        self.clock = clock
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        """Record a request and return False once ``key`` exceeded the limit in the window."""
        now = self.clock()
        if len(self.hits) > MAX_KEYS:  # bound memory under an address-spraying flood
            for stale in list(self.hits):
                self._trim(stale, now)
        queue = self._trim(key, now)
        if len(queue) >= self.limit:
            return False
        self.hits[key].append(now)
        return True

    def _trim(self, key: str, now: float) -> deque[float]:
        queue = self.hits[key]
        while queue and queue[0] <= now - self.window:
            queue.popleft()
        if not queue:
            self.hits.pop(key, None)
            return self.hits[key]
        return queue
