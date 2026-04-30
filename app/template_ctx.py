from __future__ import annotations

from fastapi import Request

from app.constants import METRIC_KEY_LABELS, SCENARIO_LABELS, STATUS_LABELS
from app.models import User


def operator_display_name(request: Request, current_user: User | None) -> str:
    if current_user is not None:
        dn = (current_user.display_name or "").strip()
        if dn:
            return dn
        return current_user.username
    u = getattr(request.state, "user_display_name", None)
    if u is not None and str(u).strip():
        return str(u).strip()
    for header in ("X-Remote-User", "Remote-User", "X-User-Name"):
        h = request.headers.get(header)
        if h and str(h).strip():
            raw = str(h).strip()
            return raw.split("@", 1)[0] if "@" in raw else raw
    from app.config import get_settings

    return get_settings().actor_display_name


def template_ctx(request: Request, *, current_user: User | None = None, **extra: object) -> dict[str, object]:
    from app.config import get_settings

    s = get_settings()
    base: dict[str, object] = {
        "request": request,
        "settings": s,
        "status_labels": STATUS_LABELS,
        "scenario_labels": SCENARIO_LABELS,
        "metric_key_labels": METRIC_KEY_LABELS,
        "current_user": current_user,
        "operator_display_name": operator_display_name(request, current_user),
    }
    return {**base, **extra}
