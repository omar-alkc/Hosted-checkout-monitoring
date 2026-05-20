"""Add scenario label overrides to scenario_config.

Revision ID: 009_scenario_labels_override
Revises: 008_rolling_detection_scope
Create Date: 2026-05-08

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "009_scenario_labels_override"
down_revision = "008_rolling_detection_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scenario_config",
        sa.Column(
            "scenario_labels",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("scenario_config", "scenario_labels")

