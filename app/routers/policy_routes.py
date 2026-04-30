from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from sqlalchemy.orm import Session

from app.constants import ALLOWED_TRANSITIONS, STATUS_KEYS, STATUS_LABELS
from app.database import get_db
from app.deps.auth import require_supervisor
from app.models import User
from app.services.policy_service import get_allowed_map, set_allowed_map
from app.template_ctx import template_ctx

router = APIRouter(prefix="/supervisor", tags=["policy"])

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _workflow_edge_pairs() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for fs, tos in ALLOWED_TRANSITIONS.items():
        for ts in sorted(tos):
            pairs.append((fs, ts))
    return pairs


@router.get("/investigator-policy", response_class=HTMLResponse)
def investigator_policy_get(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
    saved: str | None = Query(None),
):
    policy = get_allowed_map(db)
    pairs = _workflow_edge_pairs()
    selected = {f"{a}|{b}" for a, tos in policy.items() for b in tos}
    return templates.TemplateResponse(
        request,
        "investigator_policy.html",
        template_ctx(
            request,
            current_user=user,
            workflow_pairs=pairs,
            status_labels=STATUS_LABELS,
            policy_selected=selected,
            policy_saved=(saved or "").strip() == "1",
        ),
    )


@router.post("/investigator-policy", response_class=HTMLResponse)
async def investigator_policy_post(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_supervisor),
):
    form = await request.form()
    edge = form.getlist("edge")
    m: dict[str, list[str]] = {}
    for e in edge:
        parts = str(e).split("|", 1)
        if len(parts) != 2:
            continue
        fs, ts = parts[0].strip(), parts[1].strip()
        if fs not in STATUS_KEYS:
            continue
        if ts not in ALLOWED_TRANSITIONS.get(fs, set()):
            continue
        m.setdefault(fs, []).append(ts)
    try:
        set_allowed_map(db, m)
    except Exception as ex:
        return RedirectResponse(url="/supervisor/investigator-policy?error=" + quote(str(ex)), status_code=303)
    return RedirectResponse(url="/supervisor/investigator-policy?saved=1", status_code=303)
