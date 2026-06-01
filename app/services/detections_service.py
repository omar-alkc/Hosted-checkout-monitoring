from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import Select, func, literal_column, select, text
from sqlalchemy.orm import Session, joinedload

from app.constants import (
    ALLOWED_TRANSITIONS,
    DETECTION_METRICS_DISPLAY_ORDER,
    HIDDEN_DETECTION_METRIC_KEYS,
    STATUS_KEYS,
    statuses_for_queue,
)
from app.models import Detection, Note, StatusHistory, TransactionRow, User


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
    queue: str | None = None,
    scenario_id: str | None = None,
    batch_id: int | None = None,
    scope: str | None = None,
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
    elif queue:
        bucket = statuses_for_queue(queue)
        if bucket:
            stmt = stmt.where(Detection.status.in_(tuple(bucket)))
    if scenario_id:
        stmt = stmt.where(Detection.scenario_id == scenario_id)
    if batch_id is not None:
        stmt = stmt.where(Detection.import_batch_id == batch_id)
    if scope:
        sc = scope.strip().lower()
        if sc in {"batch", "rolling"}:
            stmt = stmt.where(Detection.scope_type == sc)
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
        lit = assigned.strip()
        clean = _strip_like_metachars(lit)
        if clean:
            pat = f"%{clean}%"
            stmt = stmt.where(func.coalesce(Detection.assigned_senior, "").ilike(pat))
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


def list_assignee_options(db: Session) -> list[tuple[str, str]]:
    """Return (value, label) for Assigned-to filter; value matches assigned_senior storage (display name)."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    users = db.scalars(
        select(User)
        .where(User.is_active.is_(True))
        .where(User.role.in_(("investigator", "supervisor")))
        .order_by(User.display_name, User.username)
    ).all()
    for u in users:
        dn = (u.display_name or "").strip()
        val = dn if dn else u.username
        key = val.lower()
        if key in seen:
            continue
        seen.add(key)
        label = f"{dn} ({u.username})" if dn and dn != u.username else u.username
        out.append((val, label))
    for row in db.execute(
        select(Detection.assigned_senior)
        .where(Detection.assigned_senior.isnot(None))
        .where(Detection.assigned_senior != "")
        .distinct()
        .order_by(Detection.assigned_senior)
    ).all():
        val = (row[0] or "").strip()
        if not val or val.lower() in seen:
            continue
        seen.add(val.lower())
        out.append((val, val))
    return sorted(out, key=lambda x: x[1].lower())


def count_detections(
    db: Session,
    *,
    status: str | None = None,
    queue: str | None = None,
    scenario_id: str | None = None,
    batch_id: int | None = None,
    scope: str | None = None,
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
        queue=queue,
        scenario_id=scenario_id,
        batch_id=batch_id,
        scope=scope,
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
    queue: str | None = None,
    scenario_id: str | None = None,
    batch_id: int | None = None,
    scope: str | None = None,
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
        queue=queue,
        scenario_id=scenario_id,
        batch_id=batch_id,
        scope=scope,
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


# Count other detections sharing any WalletId / WalletIdsPipe token (same rules as detail page).
_PRIOR_DETECTION_COUNT_SQL = """
(SELECT count(*)::bigint FROM detections d2
 WHERE d2.id != detections.id
 AND EXISTS (
   SELECT 1
   FROM unnest(
     array_remove(
       ARRAY[NULLIF(trim(coalesce(detections.metrics->>'WalletId', '')), '')]
       || COALESCE(
         (
           SELECT array_agg(trim(x))
           FROM unnest(string_to_array(coalesce(detections.metrics->>'WalletIdsPipe', ''), '|')) AS t(x)
           WHERE trim(x) <> ''
         ),
         ARRAY[]::text[]
       ),
       NULL
     )
   ) AS tok(t)
   WHERE tok.t IS NOT NULL
     AND (
       trim(coalesce(d2.metrics->>'WalletId', '')) = tok.t
       OR EXISTS (
         SELECT 1
         FROM unnest(string_to_array(coalesce(d2.metrics->>'WalletIdsPipe', ''), '|')) AS seg(x)
         WHERE trim(seg.x) = tok.t AND trim(seg.x) <> ''
       )
     )
 )
)::int
"""

# Calendar days in current pending_evidence stint (NULL when not in that status).
_PENDING_EVIDENCE_DAYS_SQL = """
CASE WHEN detections.status = 'pending_evidence' THEN
  GREATEST(0, (
    CURRENT_DATE - COALESCE(
      (
        SELECT (sh.created_at AT TIME ZONE 'UTC')::date
        FROM status_history sh
        WHERE sh.detection_id = detections.id
          AND sh.to_status = 'pending_evidence'
        ORDER BY sh.created_at DESC
        LIMIT 1
      ),
      (detections.updated_at AT TIME ZONE 'UTC')::date,
      (detections.created_at AT TIME ZONE 'UTC')::date
    )
  ))::int
ELSE NULL END
"""


def list_detections_with_previous_count(
    db: Session,
    *,
    status: str | None = None,
    queue: str | None = None,
    scenario_id: str | None = None,
    batch_id: int | None = None,
    scope: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    assigned: str | None = None,
    detection_id: int | None = None,
    msisdn: str | None = None,
    risk: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[tuple[Detection, int, int | None]]:
    """
    Like list_detections, but returns (Detection, previous_detection_count, pending_evidence_days) where:
    - wallet tokens come from metrics WalletId and WalletIdsPipe (same as detail page)
    - "previous" means any other detection sharing at least one token (any age / batch)
    - pending_evidence_days is calendar days in current pending_evidence stint, or None
    """
    prev_count = literal_column(_PRIOR_DETECTION_COUNT_SQL).label("previous_detection_count")
    pending_days = literal_column(_PENDING_EVIDENCE_DAYS_SQL).label("pending_evidence_days")

    stmt = _detections_stmt(
        db,
        status=status,
        queue=queue,
        scenario_id=scenario_id,
        batch_id=batch_id,
        scope=scope,
        date_from=date_from,
        date_to=date_to,
        assigned=assigned,
        detection_id=detection_id,
        msisdn=msisdn,
        risk=risk,
    ).with_only_columns(
        Detection,
        prev_count.label("previous_detection_count"),
        pending_days.label("pending_evidence_days"),
    )

    if offset is not None:
        stmt = stmt.offset(int(offset))
    if limit is not None:
        stmt = stmt.limit(int(limit))

    rows = list(db.execute(stmt).all())
    out: list[tuple[Detection, int, int | None]] = []
    for det, n, pd in rows:
        pending_val: int | None
        if pd is None:
            pending_val = None
        else:
            pending_val = int(pd)
        out.append((det, int(n or 0), pending_val))
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
    if nxt not in STATUS_KEYS:
        raise ValueError(f"Invalid status: {nxt}")
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
    scope = str(getattr(det, "scope_type", "batch") or "batch").strip().lower()
    # Rolling detections store TransactionRow.id in raw_row_indices and have no import batch.
    if scope == "rolling" or det.import_batch_id is None:
        stmt = select(TransactionRow).where(TransactionRow.id.in_(indices)).order_by(TransactionRow.id.asc())
        return list(db.scalars(stmt).all())
    # Batch detections store Excel row_index values scoped to import_batch_id.
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


def _hidden_metric_keys_for_detection(metrics: Mapping[str, Any]) -> frozenset[str]:
    """Per-detection keys to omit from the detail metrics panel (redundant with another field)."""
    hidden = set(HIDDEN_DETECTION_METRIC_KEYS)
    if str(metrics.get("WalletHolderNamesPipe") or "").strip():
        hidden.add("WalletHolderFullName")
    return frozenset(hidden)


def ordered_detection_metric_items(
    metrics: Mapping[str, Any] | None, *, scenario_id: str | None = None
) -> list[tuple[str, Any]]:
    """(key, value) pairs for the detection detail metrics table: canonical order, then leftover keys sorted."""
    m = dict(metrics or {})
    eff_sid = _scenario_id_for_risk_display(m, scenario_id)
    show_risk_block = eff_sid in {"D1", "D2"}

    risk_error = str(m.get("RiskError") or "").strip()
    suppress_risk_display = bool(risk_error)

    hidden_keys = _hidden_metric_keys_for_detection(m)

    seen: set[str] = set()
    out: list[tuple[str, Any]] = []
    for k in DETECTION_METRICS_DISPLAY_ORDER:
        if k in hidden_keys or k == "RiskError":
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
        if k in seen or k in hidden_keys or k == "RiskError":
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
    """Other detections involving any of the given wallet / MSISDN tokens (metrics WalletId or pipe list)."""
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


def bulk_change_status(
    db: Session,
    *,
    detection_ids: list,
    to_status: str,
    actor_name: str,
    supervisor: bool = False,
) -> tuple[int, int]:
    """
    Apply the same target status to each selected detection.
    Supervisors use force_set_status; investigators use workflow + investigator policy.
    """
    from app.services.policy_service import investigator_effective_targets

    applied = 0
    skipped = 0
    nxt = (to_status or "").strip()
    ids = _unique_positive_ids(list(detection_ids))
    if not nxt:
        return 0, len(ids)
    if nxt not in STATUS_KEYS:
        return 0, len(ids)
    for did in dict.fromkeys(ids):
        try:
            if supervisor:
                if force_set_status(db, detection_id=did, to_status=nxt, actor_name=actor_name) is None:
                    skipped += 1
                else:
                    applied += 1
            else:
                det = db.get(Detection, did)
                if det is None:
                    skipped += 1
                    continue
                wf = ALLOWED_TRANSITIONS.get(det.status, set())
                override = investigator_effective_targets(
                    db, from_status=det.status, workflow_targets=wf
                )
                change_status(
                    db,
                    detection_id=did,
                    to_status=nxt,
                    actor_name=actor_name,
                    allowed_targets_override=override,
                )
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
