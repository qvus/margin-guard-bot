"""Bounded-memory sliding-window rate limiter."""

from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from threading import Lock


class SlidingWindowRateLimiter:
    """Thread-safe sliding-window rate limiter with LRU key eviction."""

    def __init__(
        self,
        *,
        max_tracked_keys: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_tracked_keys < 1:
            raise ValueError("max_tracked_keys must be positive")
        self._max_tracked_keys = max_tracked_keys
        self._clock = clock
        self._lock = Lock()
        self._buckets: OrderedDict[str, deque[float]] = OrderedDict()

    def check(
        self, key: str, *, max_requests: int, window_seconds: float
    ) -> tuple[bool, float]:
        """Record an attempt if permitted; return ``(allowed, retry_after_seconds)``."""
        if max_requests < 1 or window_seconds <= 0:
            raise ValueError("max_requests and window_seconds must be positive")

        now = self._clock()
        cutoff = now - window_seconds

        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is not None:
                self._buckets.move_to_end(key)
                while bucket and bucket[0] <= cutoff:
                    bucket.popleft()
            else:
                if len(self._buckets) >= self._max_tracked_keys:
                    self._buckets.popitem(last=False)
                bucket = deque()
                self._buckets[key] = bucket

            if len(bucket) >= max_requests:
                retry_after = max(0.1, bucket[0] + window_seconds - now)
                return False, float(math.ceil(retry_after * 10) / 10)

            bucket.append(now)
            return True, 0.0
