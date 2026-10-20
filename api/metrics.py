"""Requests per minute for the API heartbeat."""

import threading
import time
from collections import deque
from collections.abc import Callable


class RequestCounter:
    """Counts requests in a rolling window, one bucket per second, so memory stays bounded at any rate."""

    def __init__(self, window_s: int, clock: Callable[[], float] = time.monotonic):
        self._window = window_s
        self._clock = clock
        self._buckets: deque[list[int]] = deque()  # [second, count], oldest first
        self._lock = threading.Lock()

    def record(self) -> None:
        second = int(self._clock())
        with self._lock:
            if self._buckets and self._buckets[-1][0] == second:
                self._buckets[-1][1] += 1
            else:
                self._buckets.append([second, 1])
            self._prune(second)

    def count(self) -> int:
        """Requests in the last `window_s` seconds."""
        with self._lock:
            self._prune(int(self._clock()))
            return sum(n for _, n in self._buckets)

    def _prune(self, now: int) -> None:
        while self._buckets and self._buckets[0][0] <= now - self._window:
            self._buckets.popleft()
