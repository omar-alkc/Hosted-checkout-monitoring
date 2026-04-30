from __future__ import annotations

from sqlalchemy.orm import Session

from app.constants import ALLOWED_TRANSITIONS, STATUS_KEYS
from app.models import InvestigatorStatusPolicy

_POLICY_SINGLETON_ID = 1


def _validate_allowed_map(raw: dict) -> dict[str, list[str]]:
    """Ensure keys/values are known statuses and targets are legal workflow edges."""
    valid_from = set(STATUS_KEYS)
    out: dict[str, list[str]] = {}
    for k, v in raw.items():
        fk = str(k).strip()
        if fk not in valid_from:
            continue
        if not isinstance(v, list):
            continue
        legal = ALLOWED_TRANSITIONS.get(fk, set())
        tos: list[str] = []
        for item in v:
            ts = str(item).strip()
            if ts in legal and ts not in tos:
                tos.append(ts)
        out[fk] = tos
    return out


def get_allowed_map(db: Session) -> dict[str, list[str]]:
    row = db.get(InvestigatorStatusPolicy, _POLICY_SINGLETON_ID)
    if row is None or not row.allowed_map:
        return {}
    return _validate_allowed_map(dict(row.allowed_map))


def set_allowed_map(db: Session, raw: dict) -> dict[str, list[str]]:
    cleaned = _validate_allowed_map(raw)
    row = db.get(InvestigatorStatusPolicy, _POLICY_SINGLETON_ID)
    if row is None:
        row = InvestigatorStatusPolicy(id=_POLICY_SINGLETON_ID, allowed_map=cleaned)
        db.add(row)
    else:
        row.allowed_map = cleaned
    db.commit()
    db.refresh(row)
    return cleaned


def investigator_effective_targets(db: Session, *, from_status: str, workflow_targets: set[str]) -> set[str]:
    m = get_allowed_map(db)
    allowed = set(m.get(from_status, []))
    return workflow_targets & allowed
