from __future__ import annotations

from app.models import Note, User


def _norm(s: str | None) -> str:
    return (s or "").strip().casefold()


def can_modify_note(user: User, note: Note, *, actor_name: str) -> bool:
    if user.role == "supervisor":
        return True
    if user.role != "investigator":
        return False
    author = _norm(note.author_name)
    if not author:
        return False
    actor = _norm(actor_name)
    if actor and author == actor:
        return True
    return author == _norm(user.display_name) or author == _norm(user.username)
