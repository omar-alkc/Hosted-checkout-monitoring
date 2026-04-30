"""initial schema

Revision ID: 001_initial
Revises:
Create Date: 2026-04-11

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "import_batches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "scenario_config",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("d_amount_min", sa.Numeric(24, 6), nullable=False),
        sa.Column("d_total_amount_min", sa.Numeric(24, 6), nullable=False),
        sa.Column("d1_min_txn", sa.Integer(), nullable=False),
        sa.Column("d1_min_unique_cards", sa.Integer(), nullable=False),
        sa.Column("d2_min_wallets", sa.Integer(), nullable=False),
        sa.Column("d3_min_rejected", sa.Integer(), nullable=False),
        sa.Column("w1_min_txn", sa.Integer(), nullable=False),
        sa.Column("w1_min_unique_cards", sa.Integer(), nullable=False),
        sa.Column("w1_min_total_amount", sa.Numeric(24, 6), nullable=False),
        sa.Column("w2_min_wallets", sa.Integer(), nullable=False),
        sa.Column("w2_min_total_amount", sa.Numeric(24, 6), nullable=False),
        sa.Column("w3_min_rejected", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.execute(
        sa.text(
            """
            INSERT INTO scenario_config (
              d_amount_min, d_total_amount_min, d1_min_txn, d1_min_unique_cards, d2_min_wallets, d3_min_rejected,
              w1_min_txn, w1_min_unique_cards, w1_min_total_amount, w2_min_wallets, w2_min_total_amount, w3_min_rejected
            ) VALUES (
              50000, 500000, 3, 3, 3, 5,
              10, 3, 500000, 5, 500000, 10
            )
            """
        )
    )

    op.create_table(
        "transaction_rows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("import_batch_id", sa.Integer(), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["import_batch_id"], ["import_batches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transaction_rows_import_batch_id", "transaction_rows", ["import_batch_id"], unique=False)

    op.create_table(
        "detections",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("import_batch_id", sa.Integer(), nullable=False),
        sa.Column("scenario_id", sa.String(length=8), nullable=False),
        sa.Column("period", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("assigned_senior", sa.String(length=256), nullable=True),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "raw_row_indices",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["import_batch_id"], ["import_batches.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_detections_import_batch_id", "detections", ["import_batch_id"], unique=False)
    op.create_index("ix_detections_scenario_id", "detections", ["scenario_id"], unique=False)
    op.create_index("ix_detections_status", "detections", ["status"], unique=False)

    op.create_table(
        "notes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("detection_id", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author_name", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["detection_id"], ["detections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notes_detection_id", "notes", ["detection_id"], unique=False)

    op.create_table(
        "status_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("detection_id", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(length=64), nullable=True),
        sa.Column("to_status", sa.String(length=64), nullable=False),
        sa.Column("actor_name", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["detection_id"], ["detections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_status_history_detection_id", "status_history", ["detection_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_status_history_detection_id", table_name="status_history")
    op.drop_table("status_history")
    op.drop_index("ix_notes_detection_id", table_name="notes")
    op.drop_table("notes")
    op.drop_index("ix_detections_status", table_name="detections")
    op.drop_index("ix_detections_scenario_id", table_name="detections")
    op.drop_index("ix_detections_import_batch_id", table_name="detections")
    op.drop_table("detections")
    op.drop_index("ix_transaction_rows_import_batch_id", table_name="transaction_rows")
    op.drop_table("transaction_rows")
    op.drop_table("scenario_config")
    op.drop_table("import_batches")
