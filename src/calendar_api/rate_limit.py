from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """In-memory sliding-window rate limiter enforced server-side.

    Each key (for example ``"submit:<client ip>"``) keeps the timestamps of
    recent hits inside ``window_seconds``. When the count reaches ``limit``,
    further hits are rejected with a ``retry_after`` hint in seconds.
    State is process-local; a restart clears all windows.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}

    def check(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: int,
        now: float | None = None,
    ) -> tuple[bool, float]:
        """Return ``(allowed, retry_after_seconds)`` for one hit of ``key``."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            cutoff = moment - window_seconds
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= limit:
                return False, max(0.0, hits[0] + window_seconds - moment)
            hits.append(moment)
            return True, 0.0

    def reset(self, key: str | None = None) -> None:
        """Drop recorded hits; tests use this to isolate rate-limit cases."""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)
