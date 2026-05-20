"""Validate security-sensitive configuration before serving traffic."""

from __future__ import annotations

import os
import sys

from app.config import DEV_SESSION_SECRET_DEFAULT, Settings

_DEFAULT_DB_MARKERS = (
    "postgresql://postgres:postgres@",
    ":postgres@",
    "/postgres:",
)


def _is_production_env() -> bool:
    env = (os.getenv("ENV") or os.getenv("APP_ENV") or "").strip().lower()
    return env in {"production", "prod"}


def _allow_insecure_dev() -> bool:
    raw = (os.getenv("ALLOW_INSECURE_DEV") or "").strip().lower()
    if " #" in raw:
        raw = raw.split(" #", 1)[0].strip().lower()
    return raw in {"1", "true", "yes"}


def validate_settings(settings: Settings) -> None:
    """
    Refuse startup when production-like config uses known-insecure defaults.

    Local development: set ALLOW_INSECURE_DEV=true to keep default session secret / DB URL.
    """
    prod = _is_production_env()
    insecure_dev = _allow_insecure_dev()
    errors: list[str] = []

    secret = (settings.session_secret or "").strip()
    if not secret or secret == DEV_SESSION_SECRET_DEFAULT:
        if prod or not insecure_dev:
            errors.append(
                "SESSION_SECRET is missing or uses the repository default. "
                "Generate one: python -c \"import secrets; print(secrets.token_urlsafe(32))\" "
                "For local-only dev, set ALLOW_INSECURE_DEV=true in .env."
            )
    elif len(secret) < 32:
        errors.append("SESSION_SECRET must be at least 32 characters.")

    db = (settings.database_url or "").lower()
    if any(m in db for m in _DEFAULT_DB_MARKERS) and "postgres:postgres" in db.replace("%40", "@"):
        if prod:
            errors.append(
                "DATABASE_URL uses default postgres:postgres credentials. "
                "Set a strong password before production deployment."
            )

    if errors:
        msg = "Startup blocked — insecure configuration:\n" + "\n".join(f"  - {e}" for e in errors)
        print(msg, file=sys.stderr)
        raise SystemExit(1)
