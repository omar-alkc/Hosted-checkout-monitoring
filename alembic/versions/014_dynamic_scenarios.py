"""Dynamic scenarios table and widen detection.scenario_id.

Revision ID: 014_dynamic_scenarios
Revises: 013_pending_evidence_sla
Create Date: 2026-07-06

"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "014_dynamic_scenarios"
down_revision = "013_pending_evidence_sla"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scenarios",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("group_type", sa.String(length=32), nullable=False),
        sa.Column("period_unit", sa.String(length=16), nullable=False, server_default="day"),
        sa.Column("period_value", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("thresholds", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("monitored_bank", sa.String(length=512), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_scenarios_code", "scenarios", ["code"], unique=True)
    op.create_index("ix_scenarios_group_type", "scenarios", ["group_type"], unique=False)

    op.alter_column(
        "detections",
        "scenario_id",
        existing_type=sa.String(length=8),
        type_=sa.String(length=32),
        existing_nullable=False,
    )

    conn = op.get_bind()
    cfg = conn.execute(
        sa.text(
            """
            SELECT d_amount_min, d_total_amount_min,
                   d1_min_txn, d1_min_unique_cards, d1_risk_min_total_amount, d1_risk_min_expenditure_pct,
                   d2_min_wallets, d2_risk_min_total_amount, d2_risk_min_wallet_expenditure_pct, d2_risk_min_wallets_pct,
                   d3_min_rejected,
                   w1_min_txn, w1_min_unique_cards, w1_min_total_amount,
                   w2_min_wallets, w2_min_txn, w2_min_total_amount,
                   w3_min_rejected,
                   monitored_banks, scenario_labels, scenario_enabled
            FROM scenario_config
            ORDER BY id ASC
            LIMIT 1
            """
        )
    ).mappings().first()

    def _bank(code: str) -> str | None:
        if not cfg:
            return None
        raw = cfg.get("monitored_banks") or {}
        if not isinstance(raw, dict):
            return None
        v = raw.get(code)
        if v is None:
            return None
        t = str(v).strip()
        return t if t else None

    def _enabled(code: str) -> bool:
        if not cfg:
            return True
        raw = cfg.get("scenario_enabled") or {}
        if not isinstance(raw, dict):
            return True
        if code not in raw:
            return True
        return bool(raw.get(code))

    def _label(code: str, default: str) -> str:
        if not cfg:
            return default
        raw = cfg.get("scenario_labels") or {}
        if not isinstance(raw, dict):
            return default
        t = str(raw.get(code) or "").strip()
        return t if t else default

    def _num(key: str, default: float) -> float:
        if not cfg or cfg.get(key) is None:
            return default
        return float(cfg[key])

    def _int(key: str, default: int) -> int:
        return int(_num(key, default))

    seeds = [
        {
            "code": "D1",
            "name": _label("D1", "D1: Many cards - One wallet"),
            "group_type": "many_cards_one_wallet",
            "period_unit": "day",
            "period_value": 1,
            "thresholds": {
                "min_txn": _int("d1_min_txn", 3),
                "min_unique_cards": _int("d1_min_unique_cards", 3),
                "min_amount_per_txn": _num("d_amount_min", 50000),
                "min_total_amount": _num("d_total_amount_min", 500000),
                "risk_min_total_amount": _num("d1_risk_min_total_amount", 0),
                "risk_min_expenditure_pct": _num("d1_risk_min_expenditure_pct", 0),
            },
            "monitored_bank": _bank("D1"),
            "enabled": _enabled("D1"),
            "sort_order": 1,
        },
        {
            "code": "D2",
            "name": _label("D2", "D2: One Card - multiple wallets"),
            "group_type": "one_card_many_wallets",
            "period_unit": "day",
            "period_value": 1,
            "thresholds": {
                "min_wallets": _int("d2_min_wallets", 3),
                "min_amount_per_txn": _num("d_amount_min", 50000),
                "min_total_amount": _num("d_total_amount_min", 500000),
                "risk_min_total_amount": _num("d2_risk_min_total_amount", 0),
                "risk_min_wallet_expenditure_pct": _num("d2_risk_min_wallet_expenditure_pct", 0),
                "risk_min_wallets_pct": _num("d2_risk_min_wallets_pct", 0),
            },
            "monitored_bank": _bank("D2"),
            "enabled": _enabled("D2"),
            "sort_order": 2,
        },
        {
            "code": "D3",
            "name": _label("D3", "D3: Multiple failed transactions"),
            "group_type": "multiple_failed",
            "period_unit": "day",
            "period_value": 1,
            "thresholds": {"min_rejected": _int("d3_min_rejected", 5)},
            "monitored_bank": _bank("D3"),
            "enabled": _enabled("D3"),
            "sort_order": 3,
        },
        {
            "code": "W1",
            "name": _label("W1", "W1: Many cards - One wallet"),
            "group_type": "many_cards_one_wallet",
            "period_unit": "week",
            "period_value": 1,
            "thresholds": {
                "min_txn": _int("w1_min_txn", 10),
                "min_unique_cards": _int("w1_min_unique_cards", 3),
                "min_amount_per_txn": 0,
                "min_total_amount": _num("w1_min_total_amount", 500000),
            },
            "monitored_bank": _bank("W1"),
            "enabled": _enabled("W1"),
            "sort_order": 4,
        },
        {
            "code": "W2",
            "name": _label("W2", "W2: One Card - multiple wallets"),
            "group_type": "one_card_many_wallets",
            "period_unit": "week",
            "period_value": 1,
            "thresholds": {
                "min_wallets": _int("w2_min_wallets", 5),
                "min_txn": _int("w2_min_txn", 1),
                "min_amount_per_txn": 0,
                "min_total_amount": _num("w2_min_total_amount", 500000),
            },
            "monitored_bank": _bank("W2"),
            "enabled": _enabled("W2"),
            "sort_order": 5,
        },
        {
            "code": "W3",
            "name": _label("W3", "W3: Multiple failed transactions"),
            "group_type": "multiple_failed",
            "period_unit": "week",
            "period_value": 1,
            "thresholds": {"min_rejected": _int("w3_min_rejected", 10)},
            "monitored_bank": _bank("W3"),
            "enabled": _enabled("W3"),
            "sort_order": 6,
        },
    ]

    for row in seeds:
        conn.execute(
            sa.text(
                """
                INSERT INTO scenarios
                  (code, name, group_type, period_unit, period_value, thresholds,
                   monitored_bank, enabled, sort_order)
                VALUES
                  (:code, :name, :group_type, :period_unit, :period_value, CAST(:thresholds AS jsonb),
                   :monitored_bank, :enabled, :sort_order)
                """
            ),
            {
                **row,
                "thresholds": json.dumps(row["thresholds"]),
                "monitored_bank": row["monitored_bank"],
                "enabled": row["enabled"],
            },
        )


def downgrade() -> None:
    op.alter_column(
        "detections",
        "scenario_id",
        existing_type=sa.String(length=32),
        type_=sa.String(length=8),
        existing_nullable=False,
    )
    op.drop_index("ix_scenarios_group_type", table_name="scenarios")
    op.drop_index("ix_scenarios_code", table_name="scenarios")
    op.drop_table("scenarios")
