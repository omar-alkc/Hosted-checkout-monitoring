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
