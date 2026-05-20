from __future__ import annotations

import time
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps.auth import get_optional_user, require_user
from app.deps.login_next import sanitize_login_next
from app.deps.login_rate_limit import login_allowed, record_failed_login
from app.models import User
from app.services.users_service import authenticate, change_password
from app.template_ctx import template_ctx

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    db: Session = Depends(get_db),
    next: str | None = Query(None),
    error: str | None = Query(None),
):
    u = get_optional_user(request, db)
    if u is not None:
        return _redirect_after_login(u, next)
    return templates.TemplateResponse(
        request,
        "login.html",
        template_ctx(
            request,
            current_user=None,
            login_error=(error or "").strip() or None,
            next_url=(next or "").strip() or None,
        ),
    )


def _redirect_after_login(user: User, next_raw: str | None) -> RedirectResponse:
    n = sanitize_login_next(next_raw)
    if n:
        loc = n
    elif user.role == "admin":
        loc = "/admin/users"
    else:
        loc = "/detections"
    return RedirectResponse(url=loc, status_code=303)


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Form(...),
    password: str = Form(""),
    next: str = Form(""),
):
    if not login_allowed(request):
        q = {"error": "Too many login attempts. Try again later."}
        nx = (next or "").strip()
        if nx:
            q["next"] = nx
        return RedirectResponse(url="/login?" + urlencode(q), status_code=303)
    u = authenticate(db, username=username, password=password)
    if u is None:
        record_failed_login(request)
        q: dict[str, str] = {"error": "Invalid username or password."}
        nx = (next or "").strip()
        if nx:
            q["next"] = nx
        return RedirectResponse(url="/login?" + urlencode(q), status_code=303)
    now = time.time()
    request.session["user_id"] = u.id
    request.session["login_at"] = now
    request.session["last_activity"] = now
    return _redirect_after_login(u, next)


@router.post("/logout", response_class=HTMLResponse)
def logout(request: Request):
    try:
        request.session.clear()
    except AssertionError:
        pass
    return RedirectResponse(url="/login?notice=" + quote("logged_out"), status_code=303)


@router.get("/account/password", response_class=HTMLResponse)
def password_form(request: Request, user: User = Depends(require_user), saved: str | None = Query(None)):
    return templates.TemplateResponse(
        request,
        "change_password.html",
        template_ctx(
            request,
            current_user=user,
            password_saved=(saved or "").strip() == "1",
        ),
    )


@router.post("/account/password", response_class=HTMLResponse)
def password_submit(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
    old_password: str = Form(""),
    new_password: str = Form(""),
    new_password2: str = Form(""),
):
    try:
        if (new_password or "") != (new_password2 or ""):
            raise ValueError("New password fields do not match.")
        change_password(db, user=user, old_password=old_password, new_password=new_password)
    except ValueError as e:
        return templates.TemplateResponse(
            request,
            "change_password.html",
            template_ctx(request, current_user=user, password_error=str(e)),
        )
    return RedirectResponse(url="/account/password?saved=1", status_code=303)
