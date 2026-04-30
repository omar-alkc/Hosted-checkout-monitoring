from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User
from app.services.auth_service import hash_password, verify_password

VALID_ROLES = frozenset({"admin", "supervisor", "investigator"})


def normalize_username(raw: str) -> str:
    return str(raw or "").strip().lower()


def list_users(db: Session) -> list[User]:
    return list(db.scalars(select(User).order_by(User.username.asc())).all())


def get_by_username(db: Session, username: str) -> User | None:
    u = normalize_username(username)
    if not u:
        return None
    return db.scalars(select(User).where(User.username == u)).first()


def authenticate(db: Session, *, username: str, password: str) -> User | None:
    u = get_by_username(db, username)
    if u is None or not u.is_active:
        return None
    if not verify_password(password, u.password_hash):
        return None
    return u


def create_user(
    db: Session,
    *,
    username: str,
    password: str,
    display_name: str,
    role: str,
) -> User:
    un = normalize_username(username)
    if not un:
        raise ValueError("Username is required.")
    if len(un) > 128:
        raise ValueError("Username is too long.")
    rl = str(role or "").strip().lower()
    if rl not in VALID_ROLES:
        raise ValueError("Invalid role.")
    if get_by_username(db, un) is not None:
        raise ValueError("That username is already taken.")
    plain = str(password or "")
    if len(plain) < 8:
        raise ValueError("Password must be at least 8 characters.")
    user = User(
        username=un,
        password_hash=hash_password(plain),
        display_name=(display_name or "").strip() or un,
        role=rl,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def set_role(db: Session, *, user_id: int, role: str) -> User | None:
    rl = str(role or "").strip().lower()
    if rl not in VALID_ROLES:
        raise ValueError("Invalid role.")
    u = db.get(User, user_id)
    if u is None:
        return None
    u.role = rl
    db.commit()
    db.refresh(u)
    return u


def set_active(db: Session, *, user_id: int, is_active: bool) -> User | None:
    u = db.get(User, user_id)
    if u is None:
        return None
    u.is_active = is_active
    db.commit()
    db.refresh(u)
    return u


def set_display_name(db: Session, *, user_id: int, display_name: str) -> User | None:
    u = db.get(User, user_id)
    if u is None:
        return None
    dn = (display_name or "").strip()
    if len(dn) > 256:
        raise ValueError("Display name is too long.")
    u.display_name = dn if dn else u.username
    db.commit()
    db.refresh(u)
    return u


def admin_set_password(db: Session, *, user_id: int, new_password: str) -> User | None:
    nw = str(new_password or "")
    if len(nw) < 8:
        raise ValueError("Password must be at least 8 characters.")
    u = db.get(User, user_id)
    if u is None:
        return None
    u.password_hash = hash_password(nw)
    db.commit()
    db.refresh(u)
    return u


def change_password(
    db: Session, *, user: User, old_password: str, new_password: str
) -> None:
    if not verify_password(old_password, user.password_hash):
        raise ValueError("Current password is incorrect.")
    nw = str(new_password or "")
    if len(nw) < 8:
        raise ValueError("New password must be at least 8 characters.")
    user.password_hash = hash_password(nw)
    db.commit()
    db.refresh(user)
