from __future__ import annotations

import pytest

from app.config import DEV_SESSION_SECRET_DEFAULT, Settings, get_settings
from app.constants import STATUS_KEYS, statuses_for_queue
from app.deps.login_next import sanitize_login_next
from app.services.detections_service import force_set_status
from app.startup_checks import validate_settings


def test_sanitize_login_next_blocks_external_and_traversal():
    assert sanitize_login_next("//evil.example/") == ""
    assert sanitize_login_next("/../admin") == ""
    assert sanitize_login_next("/detections/1/notes") == "/detections/1"
    assert sanitize_login_next("/detections?status=new") == "/detections?status=new"


def test_statuses_for_queue_aliases():
    assert "new" in statuses_for_queue("open")
    assert statuses_for_queue("bogus") is None


def test_startup_rejects_default_secret_without_insecure_dev(monkeypatch):
    monkeypatch.delenv("ALLOW_INSECURE_DEV", raising=False)
    monkeypatch.setenv("ENV", "production")
    settings = Settings(
        database_url="postgresql://user:strongpass@db/aml",
        app_title="t",
        actor_display_name="a",
        max_upload_bytes=1024,
        session_secret=DEV_SESSION_SECRET_DEFAULT,
        secure_cookies=True,
        session_same_site="strict",
        session_idle_timeout_seconds=900,
        session_max_age_seconds=3600,
    )
    with pytest.raises(SystemExit):
        validate_settings(settings)


def test_startup_allows_insecure_dev_flag(monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_DEV", "true")
    monkeypatch.delenv("ENV", raising=False)
    settings = Settings(
        database_url="postgresql://postgres:postgres@127.0.0.1:5432/aml_web",
        app_title="t",
        actor_display_name="a",
        max_upload_bytes=1024,
        session_secret=DEV_SESSION_SECRET_DEFAULT,
        secure_cookies=False,
        session_same_site="lax",
        session_idle_timeout_seconds=900,
        session_max_age_seconds=3600,
    )
    validate_settings(settings)


def test_force_set_status_rejects_invalid_status():
    class _Det:
        status = "new"

    class _Session:
        def get(self, _model, _id):
            return _Det()

        def add(self, _obj):
            pass

        def commit(self):
            pass

        def refresh(self, _det):
            pass

    db = _Session()
    with pytest.raises(ValueError, match="Invalid status"):
        force_set_status(db, detection_id=1, to_status="not_a_real_status", actor_name="sup")


def test_health_endpoint():
    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app())
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_get_settings_has_secure_cookie_fields():
    s = get_settings()
    assert isinstance(s.secure_cookies, bool)
    assert s.session_same_site in {"lax", "strict", "none"}


def test_status_keys_non_empty():
    assert len(STATUS_KEYS) >= 5


def test_status_quick_actions_from_false_positive_initial():
    from app.constants import allowed_targets, status_quick_actions

    allowed = allowed_targets("false_positive_initial")
    quick = status_quick_actions(allowed, from_status="false_positive_initial", limit=3)
    assert [k for k, _ in quick] == [
        "false_positive_final",
        "suspicious_final",
        "pending_evidence",
    ]


def test_status_helper_and_quick_actions_closed_false_positive_final():
    from app.constants import allowed_targets, status_helper_text, status_quick_actions

    allowed = allowed_targets("false_positive_final")
    assert status_quick_actions(allowed, from_status="false_positive_final") == []
    assert status_helper_text("false_positive_final", allowed) == (
        "This detection is closed (False positive (final)). No further triage steps are expected."
    )


def test_status_quick_actions_from_suspicious_final():
    from app.constants import allowed_targets, status_helper_text, status_quick_actions

    allowed = allowed_targets("suspicious_final")
    quick = status_quick_actions(allowed, from_status="suspicious_final", limit=3)
    assert [k for k, _ in quick] == ["wallet_lock", "wallet_ci", "pending_evidence"]
    assert status_helper_text("suspicious_final", allowed) == (
        "From Suspicious (final), typical next steps: Wallet lock, Wallet CI, Pending evidence."
    )


def test_status_quick_actions_from_wallet_lock():
    from app.constants import allowed_targets, status_helper_text, status_quick_actions

    allowed = allowed_targets("wallet_lock")
    quick = status_quick_actions(allowed, from_status="wallet_lock", limit=3)
    assert quick == [("wallet_reactivated", "Wallet Re-activated")]
    assert status_helper_text("wallet_lock", allowed) == (
        "From Wallet lock, typical next steps: Wallet Re-activated."
    )


def test_wallet_reactivated_is_closed_with_no_next_steps():
    from app.constants import (
        CLOSED_DETECTION_STATUSES,
        CLOSED_OUTCOME_NO_NEXT_STEP_STATUSES,
        allowed_targets,
        status_helper_text,
        status_quick_actions,
    )

    assert "wallet_reactivated" in CLOSED_DETECTION_STATUSES
    assert "wallet_reactivated" in CLOSED_OUTCOME_NO_NEXT_STEP_STATUSES
    allowed = allowed_targets("wallet_reactivated")
    assert allowed == set()
    assert status_quick_actions(allowed, from_status="wallet_reactivated") == []
    assert "closed" in status_helper_text("wallet_reactivated", allowed).lower()
