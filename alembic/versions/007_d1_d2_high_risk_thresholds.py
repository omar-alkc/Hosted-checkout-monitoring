"""add d1 d2 high risk thresholds

Revision ID: 007_d1_d2_high_risk_thresholds
Revises: 006_scenario_enabled_switches
Create Date: 2026-04-16

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "007_d1_d2_high_risk_thresholds"
down_revision = "006_scenario_enabled_switches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scenario_config",
        sa.Column("d1_risk_min_total_amount", sa.Numeric(24, 6), nullable=False, server_default="0"),
    )
    op.add_column(
        "scenario_config",
        sa.Column("d1_risk_min_expenditure_pct", sa.Numeric(24, 6), nullable=False, server_default="0"),
    )
    op.add_column(
        "scenario_config",
        sa.Column("d2_risk_min_total_amount", sa.Numeric(24, 6), nullable=False, server_default="0"),
    )
    op.add_column(
        "scenario_config",
        sa.Column("d2_risk_min_wallet_expenditure_pct", sa.Numeric(24, 6), nullable=False, server_default="0"),
    )
    op.add_column(
        "scenario_config",
        sa.Column("d2_risk_min_wallets_pct", sa.Numeric(24, 6), nullable=False, server_default="0"),
    )

    op.alter_column("scenario_config", "d1_risk_min_total_amount", server_default=None)
    op.alter_column("scenario_config", "d1_risk_min_expenditure_pct", server_default=None)
    op.alter_column("scenario_config", "d2_risk_min_total_amount", server_default=None)
    op.alter_column("scenario_config", "d2_risk_min_wallet_expenditure_pct", server_default=None)
    op.alter_column("scenario_config", "d2_risk_min_wallets_pct", server_default=None)


def downgrade() -> None:
    op.drop_column("scenario_config", "d2_risk_min_wallets_pct")
    op.drop_column("scenario_config", "d2_risk_min_wallet_expenditure_pct")
    op.drop_column("scenario_config", "d2_risk_min_total_amount")
    op.drop_column("scenario_config", "d1_risk_min_expenditure_pct")
    op.drop_column("scenario_config", "d1_risk_min_total_amount")

