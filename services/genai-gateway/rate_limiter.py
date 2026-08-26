"""
Token-bucket rate limiter for the Secure GenAI Gateway.

Each caller (identified by caller_id string) gets an independent bucket.
A bursty caller cannot exhaust quota for other callers. State is stored in a
dict protected by a single lock, so the structure is thread-safe at the cost
of a tiny contention window during bucket lookup/creation.

WHY TOKEN BUCKET AND NOT A FIXED WINDOW COUNTER:
    A fixed window counter allows a "boundary burst" exploit: send 60 requests
    at 23:59:59 and another 60 at 00:00:01 -- 120 requests in 2 seconds, which
    violates the spirit of 60 req/min. A token bucket refills continuously at a
    constant rate, so the burst limit is enforced across any arbitrary window.
"""

import threading
import time


class _Bucket:
    """A single token bucket for one caller."""

    def __init__(self, rate: float, capacity: float) -> None:
        self._rate = rate              # tokens added per second
        self._capacity = capacity      # ceiling; bucket never overflows above this
        self._tokens = float(capacity) # starts full -- first request always passes
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, n: float = 1.0) -> bool:
        """
        Attempt to consume n tokens. Returns True if allowed, False if over-limit.

        Refill happens on every call based on elapsed wall-clock time. This means
        no background thread is needed -- the refill is lazy and exact.
        """
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last = now
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    @property
    def available(self) -> float:
        """Read current token count (informational; do not act on this value)."""
        with self._lock:
            now = time.monotonic()
            return min(self._capacity, self._tokens + (now - self._last) * self._rate)


class RateLimiter:
    """
    Per-caller rate limiter.

    One RateLimiter instance lives at module level in gateway.py. New callers
    get a fresh full bucket on first contact -- no pre-registration needed.

    Args:
        rate_per_minute: sustained allowed calls per minute per caller.
        burst: max calls allowed in an instant (token bucket capacity).
               burst=10 means a caller who has been idle for 10+ seconds can
               fire 10 requests back-to-back before being throttled.
    """

    def __init__(self, rate_per_minute: float = 60.0, burst: float = 10.0) -> None:
        self._rate = rate_per_minute / 60.0   # tokens per second
        self._burst = burst
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, caller_id: str) -> bool:
        """Return True if the caller is within quota."""
        with self._lock:
            if caller_id not in self._buckets:
                self._buckets[caller_id] = _Bucket(self._rate, self._burst)
            bucket = self._buckets[caller_id]
        return bucket.consume()

    def available(self, caller_id: str) -> float:
        """Return available tokens for a caller (informational)."""
        with self._lock:
            if caller_id not in self._buckets:
                return self._burst
            bucket = self._buckets[caller_id]
        return bucket.available
