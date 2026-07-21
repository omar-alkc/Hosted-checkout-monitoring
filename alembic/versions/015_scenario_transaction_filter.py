"""Add transaction_filter to scenarios.

Revision ID: 015_scenario_transaction_filter
Revises: 014_dynamic_scenarios
Create Date: 2026-07-06

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "015_scenario_transaction_filter"
down_revision = "014_dynamic_scenarios"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scenarios",
        sa.Column(
            "transaction_filter",
            sa.String(length=32),
            nullable=False,
            server_default="approved_only",
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE scenarios
            SET transaction_filter = 'failed_only'
            WHERE group_type = 'multiple_failed'
            """
        )
    )


def downgrade() -> None:
    op.drop_column("scenarios", "transaction_filter")
