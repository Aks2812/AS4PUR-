"""
Login brute-force protection (CLAUDE.md Section 5).

In-memory, deliberately: this app's whole background-job/token-handling
design (CLAUDE.md Section 3, Section 11) already requires running as a
single process with no external queue or multi-worker setup, so in-memory
state here carries no new architectural cost. The one real trade-off is
that a process restart clears every lockout - acceptable, since this
guards against an attacker hammering the login form, not against the
app's own operator restarting the service.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict

from ..config import settings


class LoginRateLimiter:
    def __init__(self, max_attempts: int, window_minutes: int) -> None:
        self._max_attempts = max_attempts
        self._window_seconds = window_minutes * 60
        self._attempts: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    @staticmethod
    def _key(ip: str, username: str) -> tuple[str, str]:
        return (ip, username.strip().lower())

    def is_locked_out(self, ip: str, username: str) -> bool:
        with self._lock:
            key = self._key(ip, username)
            now = time.monotonic()
            recent = [t for t in self._attempts[key] if now - t < self._window_seconds]
            self._attempts[key] = recent
            return len(recent) >= self._max_attempts

    def record_failure(self, ip: str, username: str) -> None:
        with self._lock:
            self._attempts[self._key(ip, username)].append(time.monotonic())

    def record_success(self, ip: str, username: str) -> None:
        with self._lock:
            self._attempts.pop(self._key(ip, username), None)


login_rate_limiter = LoginRateLimiter(
    max_attempts=settings.login_rate_limit_attempts,
    window_minutes=settings.login_rate_limit_window_minutes,
)
