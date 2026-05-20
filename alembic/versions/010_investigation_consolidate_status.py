"""Merge investigation_consolidate into investigator status policy.

Revision ID: 010_inv_consolidate_policy
Revises: 009_scenario_labels_override
Create Date: 2026-05-17

"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "010_inv_consolidate_policy"
down_revision = "009_scenario_labels_override"
branch_labels = None
depends_on = None

_POLICY_ADDITIONS: dict[str, list[str]] = {
    "new": ["investigation_consolidate"],
    "pending_evidence": ["investigation_consolidate"],
    "investigation_consolidate": [
        "false_positive_initial",
        "suspicious_initial",
        "false_positive_final",
        "suspicious_final",
        "pending_evidence",
    ],
}


def upgrade() -> None:
    conn = op.get_bind()
    row = conn.execute(
        sa.text("SELECT allowed_map FROM investigator_status_policy WHERE id = 1")
    ).fetchone()
    if not row or row[0] is None:
        return
    allowed = dict(row[0])
    for from_status, targets in _POLICY_ADDITIONS.items():
        if from_status not in allowed:
            continue
        current = list(allowed[from_status])
        for target in targets:
            if target not in current:
                current.append(target)
        allowed[from_status] = current
    conn.execute(
        sa.text(
            "UPDATE investigator_status_policy SET allowed_map = CAST(:m AS jsonb), "
            "updated_at = now() WHERE id = 1"
        ),
        {"m": json.dumps(allowed)},
    )


def downgrade() -> None:
    conn = op.get_bind()
    row = conn.execute(
        sa.text("SELECT allowed_map FROM investigator_status_policy WHERE id = 1")
    ).fetchone()
    if not row or row[0] is None:
        return
    allowed = dict(row[0])
    for from_status, targets in _POLICY_ADDITIONS.items():
        if from_status not in allowed:
            continue
        allowed[from_status] = [t for t in allowed[from_status] if t not in targets]
        if from_status == "investigation_consolidate":
            del allowed[from_status]
    conn.execute(
        sa.text(
            "UPDATE investigator_status_policy SET allowed_map = CAST(:m AS jsonb), "
            "updated_at = now() WHERE id = 1"
        ),
        {"m": json.dumps(allowed)},
    )
