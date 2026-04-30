from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class ImportBatchStatus(str, enum.Enum):
    uploaded = "uploaded"
    ready = "ready"
    failed = "failed"


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=ImportBatchStatus.uploaded.value)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    transactions: Mapped[list[TransactionRow]] = relationship(back_populates="batch", cascade="all, delete-orphan")
    detections: Mapped[list[Detection]] = relationship(back_populates="batch")


class TransactionRow(Base):
    __tablename__ = "transaction_rows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"), index=True)
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Normalized TransactionId from source file; globally unique when set (see migration partial unique index).
    transaction_external_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    batch: Mapped[ImportBatch] = relationship(back_populates="transactions")


class ScenarioConfig(Base):
    __tablename__ = "scenario_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    d_amount_min: Mapped[float] = mapped_column(Numeric(24, 6), nullable=False)
    d_total_amount_min: Mapped[float] = mapped_column(Numeric(24, 6), nullable=False)
    d1_min_txn: Mapped[int] = mapped_column(Integer, nullable=False)
    d1_min_unique_cards: Mapped[int] = mapped_column(Integer, nullable=False)
    d1_risk_min_total_amount: Mapped[float] = mapped_column(Numeric(24, 6), nullable=False, default=0)
    d1_risk_min_expenditure_pct: Mapped[float] = mapped_column(Numeric(24, 6), nullable=False, default=0)
    d2_min_wallets: Mapped[int] = mapped_column(Integer, nullable=False)
    d2_risk_min_total_amount: Mapped[float] = mapped_column(Numeric(24, 6), nullable=False, default=0)
    d2_risk_min_wallet_expenditure_pct: Mapped[float] = mapped_column(Numeric(24, 6), nullable=False, default=0)
    d2_risk_min_wallets_pct: Mapped[float] = mapped_column(Numeric(24, 6), nullable=False, default=0)
    d3_min_rejected: Mapped[int] = mapped_column(Integer, nullable=False)
    w1_min_txn: Mapped[int] = mapped_column(Integer, nullable=False)
    w1_min_unique_cards: Mapped[int] = mapped_column(Integer, nullable=False)
    w1_min_total_amount: Mapped[float] = mapped_column(Numeric(24, 6), nullable=False)
    w2_min_wallets: Mapped[int] = mapped_column(Integer, nullable=False)
    w2_min_txn: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    w2_min_total_amount: Mapped[float] = mapped_column(Numeric(24, 6), nullable=False)
    w3_min_rejected: Mapped[int] = mapped_column(Integer, nullable=False)
    monitored_banks: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Per-scenario on/off switch. Missing keys default to enabled.
    scenario_enabled: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Detection(Base):
    __tablename__ = "detections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"), index=True)
    scenario_id: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="new", index=True)
    assigned_senior: Mapped[str | None] = mapped_column(String(256), nullable=True)
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    raw_row_indices: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    batch: Mapped[ImportBatch] = relationship(back_populates="detections")
    notes: Mapped[list[Note]] = relationship(
        back_populates="detection", cascade="all, delete-orphan", order_by="Note.created_at"
    )
    status_history: Mapped[list[StatusHistory]] = relationship(
        back_populates="detection", cascade="all, delete-orphan", order_by="StatusHistory.created_at"
    )


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    detection_id: Mapped[int] = mapped_column(ForeignKey("detections.id", ondelete="CASCADE"), index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    author_name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    detection: Mapped[Detection] = relationship(back_populates="notes")


class StatusHistory(Base):
    __tablename__ = "status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    detection_id: Mapped[int] = mapped_column(ForeignKey("detections.id", ondelete="CASCADE"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    to_status: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    detection: Mapped[Detection] = relationship(back_populates="status_history")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    # Fixed roles: admin | supervisor | investigator
    role: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class InvestigatorStatusPolicy(Base):
    """
    Singleton row (id=1): global allowed_map for investigators (maker-checker),
    intersected with ALLOWED_TRANSITIONS in application code.
    """

    __tablename__ = "investigator_status_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    allowed_map: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
