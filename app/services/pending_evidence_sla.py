from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.policy_service import get_pending_evidence_max_days

_PENDING_EVIDENCE_STATUS = "pending_evidence"
_ESCALATION_TARGET = "suspicious_initial"
_SYSTEM_ACTOR = "System (pending evidence SLA)"

_OVERDUE_DETECTION_IDS_SQL = """
SELECT d.id
FROM detections d
WHERE d.status = 'pending_evidence'
  AND (
    CURRENT_DATE - COALESCE(
      (
        SELECT (sh.created_at AT TIME ZONE 'UTC')::date
        FROM status_history sh
        WHERE sh.detection_id = d.id
          AND sh.to_status = 'pending_evidence'
        ORDER BY sh.created_at DESC
        LIMIT 1
      ),
      (d.updated_at AT TIME ZONE 'UTC')::date,
      (d.created_at AT TIME ZONE 'UTC')::date
    )
  ) >= :max_days
ORDER BY d.id ASC
"""


def calendar_days_since(entered_at: datetime, *, as_of: datetime | None = None) -> int:
    """UTC calendar days elapsed since entered_at (same day => 0)."""
    ref = as_of or datetime.now(timezone.utc)
    if entered_at.tzinfo is None:
        entered_at = entered_at.replace(tzinfo=timezone.utc)
    else:
        entered_at = entered_at.astimezone(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    else:
        ref = ref.astimezone(timezone.utc)
    return max(0, (ref.date() - entered_at.date()).days)


def pending_evidence_days_for_status(
    status: str,
    entered_at: datetime | None,
    *,
    as_of: datetime | None = None,
) -> int | None:
    if status != _PENDING_EVIDENCE_STATUS:
        return None
    if entered_at is None:
        return 0
    return calendar_days_since(entered_at, as_of=as_of)


def apply_pending_evidence_auto_escalation(db: Session) -> int:
    """
    Move overdue pending_evidence detections to suspicious_initial.
    Returns count escalated. No-op when max_days is 0.
    """
    max_days = get_pending_evidence_max_days(db)
    if max_days <= 0:
        return 0
    rows = db.execute(text(_OVERDUE_DETECTION_IDS_SQL), {"max_days": int(max_days)}).all()
    from app.services.detections_service import change_status

    escalated = 0
    for row in rows:
        det_id = int(row[0])
        try:
            change_status(
                db,
                detection_id=det_id,
                to_status=_ESCALATION_TARGET,
                actor_name=_SYSTEM_ACTOR,
            )
            escalated += 1
        except ValueError:
            continue
    return escalated
