"""transaction_rows.transaction_external_id unique

Revision ID: 002_transaction_external_id
Revises: 001_initial
Create Date: 2026-04-11

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "002_transaction_external_id"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "transaction_rows",
        sa.Column("transaction_external_id", sa.String(length=256), nullable=True),
    )
    op.create_index(
        "uq_transaction_rows_transaction_external_id",
        "transaction_rows",
        ["transaction_external_id"],
        unique=True,
        postgresql_where=sa.text("transaction_external_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_transaction_rows_transaction_external_id", table_name="transaction_rows")
    op.drop_column("transaction_rows", "transaction_external_id")
