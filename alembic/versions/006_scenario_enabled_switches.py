"""add scenario_enabled switches

Revision ID: 006_scenario_enabled_switches
Revises: 005_w2_min_txn
Create Date: 2026-04-15

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "006_scenario_enabled_switches"
down_revision = "005_w2_min_txn"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scenario_config",
        sa.Column(
            "scenario_enabled",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("scenario_config", "scenario_enabled", server_default=None)


def downgrade() -> None:
    op.drop_column("scenario_config", "scenario_enabled")

