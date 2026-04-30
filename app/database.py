from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def engine():
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=engine(), autoflush=False, autocommit=False, expire_on_commit=False)
    return _SessionLocal


def get_db() -> Generator[Session, None, None]:
    sf = session_factory()
    db = sf()
    try:
        yield db
    finally:
        db.close()
