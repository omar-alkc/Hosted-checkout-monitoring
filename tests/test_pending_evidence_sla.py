from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.services.pending_evidence_sla import (
    apply_pending_evidence_auto_escalation,
    calendar_days_since,
    pending_evidence_days_for_status,
)
from app.services.policy_service import set_pending_evidence_max_days


def test_calendar_days_since_same_day_is_zero():
    now = datetime(2026, 6, 1, 15, 0, tzinfo=timezone.utc)
    entered = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    assert calendar_days_since(entered, as_of=now) == 0


def test_calendar_days_since_yesterday_is_one():
    now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    entered = datetime(2026, 6, 1, 23, 59, tzinfo=timezone.utc)
    assert calendar_days_since(entered, as_of=now) == 1


def test_pending_evidence_days_for_status_non_pending_is_none():
    assert pending_evidence_days_for_status("new", datetime.now(timezone.utc)) is None


def test_pending_evidence_days_for_status_pending_returns_days():
    now = datetime(2026, 6, 10, tzinfo=timezone.utc)
    entered = datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert pending_evidence_days_for_status("pending_evidence", entered, as_of=now) == 9


def test_set_pending_evidence_max_days_accepts_zero():
    db = MagicMock()
    row = MagicMock()
    row.pending_evidence_max_days = 10
    db.get.return_value = row
    assert set_pending_evidence_max_days(db, 0) == 0
    assert row.pending_evidence_max_days == 0
    db.commit.assert_called_once()


def test_set_pending_evidence_max_days_rejects_out_of_range():
    db = MagicMock()
    with pytest.raises(ValueError, match="0 and 365"):
        set_pending_evidence_max_days(db, 400)


def test_apply_pending_evidence_auto_escalation_disabled_when_zero():
    db = MagicMock()
    with patch("app.services.pending_evidence_sla.get_pending_evidence_max_days", return_value=0):
        assert apply_pending_evidence_auto_escalation(db) == 0
    db.execute.assert_not_called()


def test_apply_pending_evidence_auto_escalation_calls_change_status():
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = [(42,)]
    db.execute.return_value = result
    with patch("app.services.pending_evidence_sla.get_pending_evidence_max_days", return_value=10):
        with patch("app.services.detections_service.change_status") as mock_change:
            n = apply_pending_evidence_auto_escalation(db)
    assert n == 1
    mock_change.assert_called_once_with(
        db,
        detection_id=42,
        to_status="suspicious_initial",
        actor_name="System (pending evidence SLA)",
    )
