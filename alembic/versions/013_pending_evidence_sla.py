"""Add pending_evidence_max_days to investigator_status_policy.

Revision ID: 013_pending_evidence_sla
Revises: 012_wallet_reactivated
Create Date: 2026-06-01

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "013_pending_evidence_sla"
down_revision = "012_wallet_reactivated"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "investigator_status_policy",
        sa.Column(
            "pending_evidence_max_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("10"),
        ),
    )
    op.execute(
        """
        INSERT INTO investigator_status_policy (id, allowed_map, pending_evidence_max_days)
        SELECT 1, '{}'::jsonb, 10
        WHERE NOT EXISTS (SELECT 1 FROM investigator_status_policy WHERE id = 1)
        """
    )


def downgrade() -> None:
    op.drop_column("investigator_status_policy", "pending_evidence_max_days")
