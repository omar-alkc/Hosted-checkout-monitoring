"""Add rolling detection scope fields.

Revision ID: 008_rolling_detection_scope
Revises: 007_d1_d2_high_risk_thresholds
Create Date: 2026-05-08

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "008_rolling_detection_scope"
down_revision = "007_d1_d2_high_risk_thresholds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("detections", "import_batch_id", existing_type=sa.Integer(), nullable=True)
    op.add_column(
        "detections",
        sa.Column("scope_type", sa.String(length=16), nullable=False, server_default=sa.text("'batch'")),
    )
    op.add_column("detections", sa.Column("scope_days", sa.Integer(), nullable=True))
    op.add_column("detections", sa.Column("scope_as_of", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_detections_scope_type", "detections", ["scope_type"], unique=False)
    # Keep default for new rows; existing inserts from application set explicit values where needed.


def downgrade() -> None:
    op.drop_index("ix_detections_scope_type", table_name="detections")
    op.drop_column("detections", "scope_as_of")
    op.drop_column("detections", "scope_days")
    op.drop_column("detections", "scope_type")
    op.alter_column("detections", "import_batch_id", existing_type=sa.Integer(), nullable=False)

