from __future__ import annotations

import time
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import User


def _clear_login_session(request: Request) -> None:
    try:
        sess = request.session
    except AssertionError:
        return
    for key in ("user_id", "login_at", "last_activity"):
        sess.pop(key, None)


def _session_uid(request: Request) -> int | None:
    try:
        sess = request.session
    except AssertionError:  # SessionMiddleware not installed
        return None
    uid = sess.get("user_id")
    if uid is None:
        return None
    try:
        uid_int = int(uid)
    except (TypeError, ValueError):
        return None

    settings = get_settings()
    now = time.time()
    login_at = sess.get("login_at")
    last_activity = sess.get("last_activity")
    if login_at is None or last_activity is None:
        _clear_login_session(request)
        return None
    try:
        login_at_f = float(login_at)
        last_f = float(last_activity)
    except (TypeError, ValueError):
        _clear_login_session(request)
        return None

    if now - last_f > settings.session_idle_timeout_seconds:
        _clear_login_session(request)
        return None
    if now - login_at_f > settings.session_max_age_seconds:
        _clear_login_session(request)
        return None

    sess["last_activity"] = now
    return uid_int


def get_optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = _session_uid(request)
    if uid is None:
        return None
    u = db.get(User, uid)
    if u is None or not u.is_active:
        return None
    return u


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    u = get_optional_user(request, db)
    if u is None:
        next_url = request.url.path
        if request.url.query:
            next_url += "?" + request.url.query
        raise HTTPException(
            status_code=303,
            headers={"Location": f"/login?next={quote(next_url, safe='')}"},
            detail="login_required",
        )
    return u


def require_roles(*roles: str):
    """Dependency factory: current user must have one of the given roles."""

    def _inner(user: User = Depends(require_user)) -> User:
        if user.role not in roles:
            loc = "/detections?error=" + quote("You do not have access to that page.")
            if user.role == "admin":
                loc = "/admin/users?error=" + quote("You do not have access to that page.")
            raise HTTPException(status_code=303, headers={"Location": loc}, detail="forbidden")
        return user

    return _inner


def require_supervisor_or_investigator(user: User = Depends(require_user)) -> User:
    """Operational case work (detections, detection detail): supervisors and investigators."""
    if user.role == "admin":
        raise HTTPException(
            status_code=303,
            headers={
                "Location": "/admin/users?error="
                + quote("Administrators manage users only. Sign in as a supervisor or investigator.")
            },
            detail="forbidden",
        )
    if user.role not in ("supervisor", "investigator"):
        raise HTTPException(
            status_code=303,
            headers={"Location": "/login?error=" + quote("Invalid session.")},
            detail="forbidden",
        )
    return user


def require_supervisor(user: User = Depends(require_user)) -> User:
    """Imports, scenarios, export, bulk actions, investigator policy (not admins)."""
    if user.role != "supervisor":
        loc = "/detections?error=" + quote("Supervisor access required.")
        if user.role == "admin":
            loc = "/admin/users?error=" + quote("Supervisors run imports and scenarios. Administrators manage users only.")
        raise HTTPException(status_code=303, headers={"Location": loc}, detail="forbidden")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != "admin":
        raise HTTPException(
            status_code=303,
            headers={"Location": "/detections?error=" + quote("Administrator access required.")},
            detail="forbidden",
        )
    return user
