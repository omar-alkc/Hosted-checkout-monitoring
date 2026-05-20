"""Simple in-memory rate limit for login attempts (per client IP)."""

from __future__ import annotations

import time
from collections import defaultdict

_WINDOW_SECONDS = 15 * 60
_MAX_ATTEMPTS = 10
_buckets: dict[str, list[float]] = defaultdict(list)


def _client_key(request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def login_allowed(request) -> bool:
    key = _client_key(request)
    now = time.time()
    cutoff = now - _WINDOW_SECONDS
    attempts = [t for t in _buckets[key] if t > cutoff]
    _buckets[key] = attempts
    return len(attempts) < _MAX_ATTEMPTS


def record_failed_login(request) -> None:
    _buckets[_client_key(request)].append(time.time())
