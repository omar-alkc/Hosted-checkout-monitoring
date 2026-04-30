"""add w2_min_txn threshold

Revision ID: 005_w2_min_txn
Revises: 004_users_and_investigator_policy
Create Date: 2026-04-15

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "005_w2_min_txn"
down_revision = "004_users_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scenario_config", sa.Column("w2_min_txn", sa.Integer(), nullable=False, server_default="1"))
    # Remove server default after backfill; app always sets an explicit value.
    op.alter_column("scenario_config", "w2_min_txn", server_default=None)


def downgrade() -> None:
    op.drop_column("scenario_config", "w2_min_txn")

