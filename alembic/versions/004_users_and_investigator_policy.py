"""users table + global investigator_status_policy JSONB

Revision ID: 004_users_policy
Revises: 003_scenario_monitored_banks
Create Date: 2026-04-14

"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "004_users_policy"
down_revision = "003_scenario_monitored_banks"
branch_labels = None
depends_on = None


_DEFAULT_POLICY = {
    "new": ["false_positive_initial", "suspicious_initial", "pending_evidence", "investigation_consolidate"],
    "pending_evidence": ["new", "false_positive_initial", "suspicious_initial", "investigation_consolidate"],
    "investigation_consolidate": [
        "false_positive_initial",
        "suspicious_initial",
        "false_positive_final",
        "suspicious_final",
        "pending_evidence",
    ],
}


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )
    op.create_index(op.f("ix_users_username"), "users", ["username"], unique=False)
    op.create_index(op.f("ix_users_role"), "users", ["role"], unique=False)

    op.create_table(
        "investigator_status_policy",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "allowed_map",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id = 1", name="ck_investigator_status_policy_singleton"),
    )
    op.execute(
        sa.text(
            "INSERT INTO investigator_status_policy (id, allowed_map) VALUES (1, CAST(:m AS jsonb))"
        ).bindparams(m=json.dumps(_DEFAULT_POLICY))
    )


def downgrade() -> None:
    op.drop_table("investigator_status_policy")
    op.drop_index(op.f("ix_users_role"), table_name="users")
    op.drop_index(op.f("ix_users_username"), table_name="users")
    op.drop_table("users")
