from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path, *, override: bool = True) -> None:
    """
    Load KEY=VALUE lines from path into os.environ.

    When override is True (default), keys present in the file replace existing environment
    variables. That avoids a stale machine-level DATABASE_URL breaking local Docker Compose.
    """
    if not path.exists():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().lstrip("\ufeff")
            value = value.strip()
            if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            if not key:
                continue
            if override or key not in os.environ:
                os.environ[key] = value
    except OSError:
        return


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    database_url: str
    app_title: str
    demo_mode: bool
    actor_display_name: str
    max_upload_bytes: int
    session_secret: str
    #: Idle timeout: no requests for this long clears the login session (seconds).
    session_idle_timeout_seconds: int
    #: Absolute max lifetime of a login session / signed session cookie (seconds).
    session_max_age_seconds: int


def get_settings() -> Settings:
    """Not cached: always re-reads environment so Alembic/uvicorn pick up .env changes."""
    _load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    db_url = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/aml_web").strip()
    title = os.getenv("APP_TITLE", "Hosted Checkout Monitoring system").strip()
    actor = os.getenv("APP_ACTOR_NAME", "Demo user").strip() or "Demo user"
    # Default 25 MiB cap for Excel uploads (read in chunks; see routers/web imports_upload).
    max_up = _env_int("MAX_UPLOAD_BYTES", 25 * 1024 * 1024)
    if max_up < 1:
        max_up = 25 * 1024 * 1024
    sess = os.getenv("SESSION_SECRET", "").strip() or "dev-insecure-change-me-set-SESSION_SECRET"
    idle_sec = _env_int("SESSION_IDLE_TIMEOUT_SECONDS", 15 * 60)
    if idle_sec < 60:
        idle_sec = 15 * 60
    max_age_sec = _env_int("SESSION_MAX_AGE_SECONDS", 24 * 60 * 60)
    if max_age_sec < 300:
        max_age_sec = 24 * 60 * 60
    return Settings(
        database_url=db_url,
        app_title=title or "Hosted Checkout Monitoring system",
        demo_mode=os.getenv("DEMO_MODE", "true").lower() in {"1", "true", "yes"},
        actor_display_name=actor,
        max_upload_bytes=max_up,
        session_secret=sess,
        session_idle_timeout_seconds=idle_sec,
        session_max_age_seconds=max_age_sec,
    )


def repo_root() -> Path:
    """Repository root (parent of the ``app`` package: project root with ``run.py`` and ``alembic/``)."""
    return Path(__file__).resolve().parents[1]
