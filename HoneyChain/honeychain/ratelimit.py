"""
honeychain.ratelimit
====================
Minimal in-process sliding-window rate limiter (final audit).

Used for the unauthenticated public verification endpoint (a flood could
otherwise inflate the Phase 10 scan signals and load the database) and for
login attempts per client address (the Phase 3 throttle is per email only,
so spraying many emails from one address was unlimited).

Deliberate limits, documented rather than hidden:
* In-process: with several worker processes each keeps its own window.
* Keyed on ``request.remote_addr``: behind a reverse proxy that is the proxy
  address unless the deployment configures a trusted proxy fix.
* The key table is bounded (MAX_KEYS); the least-recently-used key is
  evicted, so rotating addresses cannot exhaust memory.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque


class RateLimiter:
    MAX_KEYS = 10_000

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = int(limit)
        self.window = float(window_seconds)
        self._hits: "OrderedDict[str, deque[float]]" = OrderedDict()
        self._lock = threading.Lock()

    def hit(self, key: str, now: float | None = None) -> tuple[bool, int]:
        """Record one attempt. Returns ``(allowed, retry_after_seconds)``."""
        now = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                bucket = deque()
                self._hits[key] = bucket
                if len(self._hits) > self.MAX_KEYS:
                    self._hits.popitem(last=False)
            else:
                self._hits.move_to_end(key)
            while bucket and now - bucket[0] >= self.window:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False, max(1, int(self.window - (now - bucket[0])) + 1)
            bucket.append(now)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_limiters: list[RateLimiter] = []


def limiter(limit: int, window_seconds: float) -> RateLimiter:
    instance = RateLimiter(limit, window_seconds)
    _limiters.append(instance)
    return instance


def reset_all() -> None:
    """Clear every limiter (used by the test harness between tests)."""
    for instance in _limiters:
        instance.reset()
