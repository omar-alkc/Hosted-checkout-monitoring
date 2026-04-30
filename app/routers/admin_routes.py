from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps.auth import require_admin
from app.models import User
from app.services.users_service import (
    admin_set_password,
    create_user,
    list_users,
    set_active,
    set_display_name,
    set_role,
)
from app.template_ctx import template_ctx

router = APIRouter(prefix="/admin", tags=["admin"])

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

ROLE_OPTIONS = ("admin", "supervisor", "investigator")


@router.get("/users", response_class=HTMLResponse)
def users_list(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    rows = list_users(db)
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        template_ctx(
            request,
            current_user=user,
            users=rows,
            role_options=ROLE_OPTIONS,
        ),
    )


@router.post("/users/create", response_class=HTMLResponse)
def users_create(
    request: Request,
    db: Session = Depends(get_db),
    admin_user: User = Depends(require_admin),
    username: str = Form(""),
    password: str = Form(""),
    display_name: str = Form(""),
    role: str = Form("investigator"),
):
    try:
        create_user(
            db,
            username=username,
            password=password,
            display_name=display_name,
            role=role,
        )
    except ValueError as e:
        return RedirectResponse(url="/admin/users?error=" + quote(str(e)), status_code=303)
    return RedirectResponse(url="/admin/users?notice=user_created", status_code=303)


@router.post("/users/{user_id}/role", response_class=HTMLResponse)
def users_set_role(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
    admin_user: User = Depends(require_admin),
    role: str = Form(...),
):
    if user_id == admin_user.id:
        return RedirectResponse(
            url="/admin/users?error=" + quote("You cannot change your own role while logged in."),
            status_code=303,
        )
    try:
        u = set_role(db, user_id=user_id, role=role)
    except ValueError as e:
        return RedirectResponse(url="/admin/users?error=" + quote(str(e)), status_code=303)
    if u is None:
        return RedirectResponse(url="/admin/users?error=" + quote("User not found."), status_code=303)
    return RedirectResponse(url="/admin/users?notice=" + quote("Role updated."), status_code=303)


@router.post("/users/{user_id}/active", response_class=HTMLResponse)
def users_set_active(
    user_id: int,
    db: Session = Depends(get_db),
    admin_user: User = Depends(require_admin),
    active: str = Form("0"),
):
    if user_id == admin_user.id:
        return RedirectResponse(
            url="/admin/users?error=" + quote("You cannot disable your own account."),
            status_code=303,
        )
    u = set_active(db, user_id=user_id, is_active=active in {"1", "true", "on", "yes"})
    if u is None:
        return RedirectResponse(url="/admin/users?error=" + quote("User not found."), status_code=303)
    return RedirectResponse(url="/admin/users?notice=" + quote("User updated."), status_code=303)


@router.get("/users/{user_id}/display-name", response_class=HTMLResponse)
def user_display_name_form(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
    admin_user: User = Depends(require_admin),
):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "admin_user_display_name.html",
        template_ctx(request, current_user=admin_user, target=target),
    )


@router.post("/users/{user_id}/display-name", response_class=HTMLResponse)
def user_display_name_submit(
    user_id: int,
    db: Session = Depends(get_db),
    admin_user: User = Depends(require_admin),
    display_name: str = Form(""),
):
    try:
        u = set_display_name(db, user_id=user_id, display_name=display_name)
    except ValueError as e:
        return RedirectResponse(url="/admin/users?error=" + quote(str(e)), status_code=303)
    if u is None:
        return RedirectResponse(url="/admin/users?error=" + quote("User not found."), status_code=303)
    return RedirectResponse(
        url="/admin/users?notice=" + quote("Display name updated for " + u.username + "."),
        status_code=303,
    )


@router.get("/users/{user_id}/password", response_class=HTMLResponse)
def user_password_form(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
    admin_user: User = Depends(require_admin),
):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "admin_user_password.html",
        template_ctx(request, current_user=admin_user, target=target),
    )


@router.post("/users/{user_id}/password", response_class=HTMLResponse)
def user_password_submit(
    user_id: int,
    db: Session = Depends(get_db),
    admin_user: User = Depends(require_admin),
    new_password: str = Form(""),
    new_password2: str = Form(""),
):
    if (new_password or "") != (new_password2 or ""):
        return RedirectResponse(
            url=f"/admin/users/{user_id}/password?error=" + quote("New password fields do not match."),
            status_code=303,
        )
    try:
        u = admin_set_password(db, user_id=user_id, new_password=new_password)
    except ValueError as e:
        return RedirectResponse(
            url=f"/admin/users/{user_id}/password?error=" + quote(str(e)),
            status_code=303,
        )
    if u is None:
        return RedirectResponse(url="/admin/users?error=" + quote("User not found."), status_code=303)
    return RedirectResponse(
        url="/admin/users?notice=" + quote("Password updated for " + u.username + "."),
        status_code=303,
    )
