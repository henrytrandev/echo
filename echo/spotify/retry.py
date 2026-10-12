"""When to retry a failed request, and how long to wait. Pure logic, so it's easy to test."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field

NETWORK_ERROR = 0
"""Pseudo status code for connection errors and timeouts."""


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 0.5
    max_delay: float = 30.0
    """Longest single wait. A 429 asking for more than this means the quota is exhausted
    for a while (Spotify sends reason QUOTA_EXCEEDED), so we give up instead of hanging."""
    random_fn: Callable[[], float] = field(default=random.random, compare=False, repr=False)

    def delay_for(self, attempt: int, status: int, retry_after: str | None = None) -> float | None:
        """Seconds to wait before attempt number `attempt + 1`, or None to stop retrying.

        `attempt` is 1 for the first try. 429s honour Retry-After; 5xx and network
        errors use exponential backoff with full jitter, so many clients that fail
        at once don't all retry at the same moment. Other 4xx errors are the
        caller's fault and are never retried.
        """
        if attempt >= self.max_attempts:
            return None
        if status == 429:
            wait = _parse_retry_after(retry_after)
            if wait is None:
                return self._backoff(attempt)
            return wait if wait <= self.max_delay else None
        if status == NETWORK_ERROR or status >= 500:
            return self._backoff(attempt)
        return None

    def _backoff(self, attempt: int) -> float:
        ceiling = min(self.max_delay, self.base_delay * 2 ** (attempt - 1))
        return self.random_fn() * ceiling


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None  # HTTP-date form; Spotify sends seconds, so fall back to backoff
    return max(seconds, 0.0)
