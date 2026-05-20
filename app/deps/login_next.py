"""Safe post-login redirect targets (avoid POST-only paths in ?next=)."""

from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import Request

# POST-only sub-routes under a detection detail page → redirect back to detail after login.
_DETECTION_POST_SUB = re.compile(
    r"^/detections/(\d+)/(notes(?:/\d+)?(?:/edit|/delete)?|status)\s*$",
    re.IGNORECASE,
)


def sanitize_login_next(path: str | None) -> str:
    """Return a GET-safe relative path for ?next=, or empty string if invalid."""
    n = (path or "").strip()
    if not n.startswith("/") or n.startswith("//") or ".." in n:
        return ""
    base = n.split("?", 1)[0]
    m = _DETECTION_POST_SUB.match(base)
    if m:
        return f"/detections/{m.group(1)}"
    return n


def login_next_from_request(request: Request) -> str:
    path = request.url.path
    if request.method.upper() == "POST":
        path = sanitize_login_next(path) or path
    elif request.url.query:
        path = f"{path}?{request.url.query}"
    return sanitize_login_next(path) or path


def login_url_with_next(request: Request) -> str:
    nxt = login_next_from_request(request)
    return f"/login?next={quote(nxt, safe='')}"
