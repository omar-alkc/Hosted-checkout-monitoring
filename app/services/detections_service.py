from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import Select, func, select, text
from sqlalchemy.orm import Session, joinedload

from app.constants import ALLOWED_TRANSITIONS, DETECTION_METRICS_DISPLAY_ORDER
from app.models import Detection, Note, StatusHistory, TransactionRow


def _strip_like_metachars(s: str) -> str:
    """Remove LIKE metacharacters from user input used inside %...% patterns."""
    return "".join(ch for ch in s if ch not in "\\%_")


def _normalize_risk_filter(risk: str | None) -> str | None:
    """Return 'high', 'low', or None (no filter)."""
    if not risk:
        return None
    r = risk.strip().lower()
    if r in ("high", "low"):
        return r
    return None


def _detections_stmt(
    db: Session,
    *,
    status: str | None = None,
    scenario_id: str | None = None,
    batch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    assigned: str | None = None,
    detection_id: int | None = None,
    msisdn: str | None = None,
    risk: str | None = None,
) -> Select[tuple[Detection]]:
    stmt: Select[tuple[Detection]] = (
        select(Detection).options(joinedload(Detection.batch)).order_by(Detection.updated_at.desc())
    )
    if status:
        stmt = stmt.where(Detection.status == status)
    if scenario_id:
        stmt = stmt.where(Detection.scenario_id == scenario_id)
    if batch_id is not None:
        stmt = stmt.where(Detection.import_batch_id == batch_id)
    if date_from:
        try:
            d = date.fromisoformat(date_from.strip())
            start = datetime.combine(d, time.min, tzinfo=timezone.utc)
            stmt = stmt.where(Detection.created_at >= start)
        except ValueError:
            pass
    if date_to:
        try:
            d = date.fromisoformat(date_to.strip())
            end = datetime.combine(d + timedelta(days=1), time.min, tzinfo=timezone.utc)
            stmt = stmt.where(Detection.created_at < end)
        except ValueError:
            pass
    if assigned:
        lit = _strip_like_metachars(assigned.strip())
        if lit:
            stmt = stmt.where(Detection.assigned_senior.ilike(f"%{lit}%"))
    if detection_id is not None:
        stmt = stmt.where(Detection.id == detection_id)
    if msisdn:
        raw = msisdn.strip()
        clean = _strip_like_metachars(raw)
        if clean:
            pat = f"%{clean}%"
            stmt = stmt.where(
                text(
                    "(detections.metrics->>'WalletId') ILIKE :ms "
                    "OR (detections.metrics->>'WalletIdsPipe') ILIKE :ms"
                ).bindparams(ms=pat)
            )
    rf = _normalize_risk_filter(risk)
    if rf == "high":
        stmt = stmt.where(Detection.metrics["Risk"].astext == "High")
    elif rf == "low":
        stmt = stmt.where(Detection.metrics["Risk"].astext == "Low")
    return stmt


def count_detections(
    db: Session,
    *,
    status: str | None = None,
    scenario_id: str | None = None,
    batch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    assigned: str | None = None,
    detection_id: int | None = None,
    msisdn: str | None = None,
    risk: str | None = None,
) -> int:
    stmt = _detections_stmt(
        db,
        status=status,
        scenario_id=scenario_id,
        batch_id=batch_id,
        date_from=date_from,
        date_to=date_to,
        assigned=assigned,
        detection_id=detection_id,
        msisdn=msisdn,
        risk=risk,
    )
    # Use count(Detection.id) to keep FROM clause (count(*) can drop FROM under SQLAlchemy 2.0).
    stmt = stmt.with_only_columns(func.count(Detection.id)).order_by(None)
    return int(db.execute(stmt).scalar_one() or 0)


def list_detections(
    db: Session,
    *,
    status: str | None = None,
    scenario_id: str | None = None,
    batch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    assigned: str | None = None,
    detection_id: int | None = None,
    msisdn: str | None = None,
    risk: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[Detection]:
    stmt = _detections_stmt(
        db,
        status=status,
        scenario_id=scenario_id,
        batch_id=batch_id,
        date_from=date_from,
        date_to=date_to,
        assigned=assigned,
        detection_id=detection_id,
        msisdn=msisdn,
        risk=risk,
    )
    if offset is not None:
        stmt = stmt.offset(int(offset))
    if limit is not None:
        stmt = stmt.limit(int(limit))
    return list(db.scalars(stmt).all())


def list_detections_with_previous_count(
    db: Session,
    *,
    status: str | None = None,
    scenario_id: str | None = None,
    batch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    assigned: str | None = None,
    detection_id: int | None = None,
    msisdn: str | None = None,
    risk: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[tuple[Detection, int]]:
    """
    Like list_detections, but returns (Detection, previous_detection_count) where:
    - "customer" is the primary MSISDN in metrics WalletId
    - "previous" means created_at < this detection's created_at
    """
    d2 = Detection.__table__.alias("d2")
    wallet1 = func.trim(func.coalesce(Detection.metrics["WalletId"].astext, ""))
    wallet2 = func.trim(func.coalesce(d2.c.metrics["WalletId"].astext, ""))
    prev_count = (
        select(func.count(d2.c.id))
        .where(d2.c.id != Detection.id)
        .where(wallet2 != "")
        .where(wallet2 == wallet1)
        .where(d2.c.created_at < Detection.created_at)
        .scalar_subquery()
    )

    stmt = _detections_stmt(
        db,
        status=status,
        scenario_id=scenario_id,
        batch_id=batch_id,
        date_from=date_from,
        date_to=date_to,
        assigned=assigned,
        detection_id=detection_id,
        msisdn=msisdn,
        risk=risk,
    ).with_only_columns(Detection, prev_count.label("previous_detection_count"))

    if offset is not None:
        stmt = stmt.offset(int(offset))
    if limit is not None:
        stmt = stmt.limit(int(limit))

    rows = list(db.execute(stmt).all())
    out: list[tuple[Detection, int]] = []
    for det, n in rows:
        out.append((det, int(n or 0)))
    return out


_STATUSES_ASSIGN_SENIOR_FROM = frozenset({"new", "pending_evidence"})
_INITIAL_OUTCOME_STATUSES = frozenset({"false_positive_initial", "suspicious_initial"})


def change_status(
    db: Session,
    *,
    detection_id: int,
    to_status: str,
    actor_name: str,
    allowed_targets_override: set[str] | None = None,
) -> Detection | None:
    det = db.get(Detection, detection_id)
    if det is None:
        raise ValueError("Detection not found.")
    cur = det.status
    workflow = ALLOWED_TRANSITIONS.get(cur, set())
    if allowed_targets_override is not None:
        allowed = workflow & allowed_targets_override
    else:
        allowed = workflow
    if to_status not in allowed:
        raise ValueError(f"Transition not allowed: {cur} -> {to_status}")
    actor_clean = (actor_name or "").strip() or "Unknown"
    det.status = to_status
    if cur in _STATUSES_ASSIGN_SENIOR_FROM and to_status in _INITIAL_OUTCOME_STATUSES:
        det.assigned_senior = actor_clean
    db.add(
        StatusHistory(
            detection_id=detection_id,
            from_status=cur,
            to_status=to_status,
            actor_name=actor_clean,
        )
    )
    db.commit()
    db.refresh(det)
    return det


def force_set_status(db: Session, *, detection_id: int, to_status: str, actor_name: str) -> Detection | None:
    """
    Supervisor-only operation: set status to any value (must be non-empty), bypassing ALLOWED_TRANSITIONS.
    Still records status history.
    """
    det = db.get(Detection, detection_id)
    if det is None:
        return None
    nxt = (to_status or "").strip()
    if not nxt:
        raise ValueError("Status cannot be empty.")
    cur = det.status
    actor_clean = (actor_name or "").strip() or "Unknown"
    det.status = nxt
    db.add(
        StatusHistory(
            detection_id=detection_id,
            from_status=cur,
            to_status=nxt,
            actor_name=actor_clean,
        )
    )
    db.commit()
    db.refresh(det)
    return det


def add_note(db: Session, *, detection_id: int, body: str, author_name: str) -> Note | None:
    det = db.get(Detection, detection_id)
    if det is None:
        return None
    text = (body or "").strip()
    if not text:
        raise ValueError("Note text cannot be empty.")
    note = Note(detection_id=detection_id, body=text, author_name=author_name or "")
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


def get_note(db: Session, *, detection_id: int, note_id: int) -> Note | None:
    note = db.get(Note, note_id)
    if note is None:
        return None
    if int(note.detection_id) != int(detection_id):
        return None
    return note


def update_note(db: Session, *, detection_id: int, note_id: int, body: str) -> Note | None:
    note = get_note(db, detection_id=detection_id, note_id=note_id)
    if note is None:
        return None
    text = (body or "").strip()
    if not text:
        raise ValueError("Note text cannot be empty.")
    note.body = text
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


def delete_note(db: Session, *, detection_id: int, note_id: int) -> bool:
    note = get_note(db, detection_id=detection_id, note_id=note_id)
    if note is None:
        return False
    db.delete(note)
    db.commit()
    return True


def transactions_for_detection(db: Session, det: Detection) -> list[TransactionRow]:
    indices = list(det.raw_row_indices or [])
    if not indices:
        return []
    stmt = (
        select(TransactionRow)
        .where(TransactionRow.import_batch_id == det.import_batch_id)
        .where(TransactionRow.row_index.in_(indices))
        .order_by(TransactionRow.row_index.asc())
    )
    return list(db.scalars(stmt).all())


def _scenario_id_for_risk_display(metrics: Mapping[str, Any] | None, scenario_id: str | None) -> str:
    m = dict(metrics or {})
    return str(scenario_id or m.get("ScenarioId") or "").strip().upper()


def _skip_risk_metric_key(k: str, *, show_risk_block: bool) -> bool:
    if show_risk_block:
        return False
    if k == "Risk" or k.startswith("RiskObserved"):
        return True
    return False


def ordered_detection_metric_items(
    metrics: Mapping[str, Any] | None, *, scenario_id: str | None = None
) -> list[tuple[str, Any]]:
    """(key, value) pairs for the detection detail metrics table: canonical order, then leftover keys sorted."""
    m = dict(metrics or {})
    eff_sid = _scenario_id_for_risk_display(m, scenario_id)
    show_risk_block = eff_sid in {"D1", "D2"}

    risk_error = str(m.get("RiskError") or "").strip()
    suppress_risk_display = bool(risk_error)

    seen: set[str] = set()
    out: list[tuple[str, Any]] = []
    for k in DETECTION_METRICS_DISPLAY_ORDER:
        if k == "RiskError":
            continue
        if _skip_risk_metric_key(k, show_risk_block=show_risk_block):
            continue
        if k == "Risk":
            if "Risk" not in m:
                continue
            out.append(("Risk", "" if suppress_risk_display else m["Risk"]))
            seen.add("Risk")
            continue
        if k in m:
            out.append((k, m[k]))
            seen.add(k)
    for k in sorted(m.keys()):
        if k in seen or k == "RiskError":
            continue
        if _skip_risk_metric_key(k, show_risk_block=show_risk_block):
            continue
        out.append((k, m[k]))
        seen.add(k)
    return out


def wallet_tokens_for_prior_lookup(det: Detection) -> list[str]:
    """MSISDN / wallet values from detection metrics (single WalletId and/or WalletIdsPipe)."""
    m = det.metrics or {}
    out: list[str] = []
    w = m.get("WalletId")
    if w is not None and str(w).strip():
        out.append(str(w).strip())
    pip = m.get("WalletIdsPipe")
    if pip is not None and str(pip).strip():
        for p in str(pip).split("|"):
            p = p.strip()
            if p and p not in out:
                out.append(p)
    return out


def prior_detections_for_wallet_tokens(
    db: Session, *, detection_id: int, wallet_tokens: list[str], limit: int = 50
) -> list[tuple[int, datetime, str, str]]:
    """Older detections involving any of the given wallet / MSISDN tokens (metrics WalletId or pipe list)."""
    tokens = list(dict.fromkeys(t.strip() for t in wallet_tokens if t and str(t).strip()))
    if not tokens:
        return []
    stmt = text(
        """
        SELECT id, created_at, scenario_id, status
        FROM detections
        WHERE id != :cur
          AND (
            trim(coalesce(metrics->>'WalletId','')) = ANY(:wallets)
            OR EXISTS (
              SELECT 1
              FROM unnest(string_to_array(coalesce(metrics->>'WalletIdsPipe',''), '|')) AS seg(x)
              WHERE trim(x) = ANY(:wallets) AND trim(x) <> ''
            )
          )
        ORDER BY created_at DESC
        LIMIT :lim
        """
    )
    rows = db.execute(stmt, {"cur": detection_id, "wallets": tokens, "lim": limit}).all()
    return [(int(r[0]), r[1], str(r[2] or ""), str(r[3] or "")) for r in rows]


def _unique_positive_ids(raw_ids: list) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for x in raw_ids:
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if v > 0 and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def bulk_change_status(db: Session, *, detection_ids: list, to_status: str, actor_name: str) -> tuple[int, int]:
    """Apply the same target status per id where the transition is allowed. Commits once per successful change_status."""
    applied = 0
    skipped = 0
    for did in dict.fromkeys(_unique_positive_ids(list(detection_ids))):
        try:
            change_status(db, detection_id=did, to_status=to_status, actor_name=actor_name)
            applied += 1
        except ValueError:
            skipped += 1
    return applied, skipped


def delete_test_detections(db: Session, *, detection_ids: list) -> tuple[int, int]:
    """
    Delete detections by id if and only if their status is 'test'.
    Returns (deleted, skipped).
    """
    ids = _unique_positive_ids(list(detection_ids))
    if not ids:
        return 0, 0
    rows = list(db.scalars(select(Detection).where(Detection.id.in_(ids))).all())
    deleted = 0
    skipped = 0
    for d in rows:
        if d.status == "test":
            db.delete(d)
            deleted += 1
        else:
            skipped += 1
    db.commit()
    return deleted, skipped
