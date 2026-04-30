"""scenario_config.monitored_banks JSONB

Revision ID: 003_scenario_monitored_banks
Revises: 002_transaction_external_id
Create Date: 2026-04-11

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "003_scenario_monitored_banks"
down_revision = "002_transaction_external_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scenario_config",
        sa.Column(
            "monitored_banks",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("scenario_config", "monitored_banks")
