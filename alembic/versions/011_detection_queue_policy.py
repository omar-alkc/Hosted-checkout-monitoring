"""Ensure investigator policy allows investigation_consolidate reopen paths.

Revision ID: 011_detection_queue_policy
Revises: 010_inv_consolidate_policy
Create Date: 2026-05-17

"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "011_detection_queue_policy"
down_revision = "010_inv_consolidate_policy"
branch_labels = None
depends_on = None

_POLICY_ADDITIONS: dict[str, list[str]] = {
    "false_positive_initial": ["investigation_consolidate"],
    "suspicious_initial": ["investigation_consolidate"],
    "false_positive_final": ["investigation_consolidate"],
    "suspicious_final": ["investigation_consolidate"],
    "investigation_consolidate": [
        "false_positive_initial",
        "suspicious_initial",
        "false_positive_final",
        "suspicious_final",
        "pending_evidence",
        "wallet_lock",
        "wallet_ci",
    ],
}


def _merge_policy(allowed: dict) -> dict:
    for from_status, targets in _POLICY_ADDITIONS.items():
        if from_status not in allowed:
            continue
        current = list(allowed[from_status])
        for target in targets:
            if target not in current:
                current.append(target)
        allowed[from_status] = current
    return allowed


def upgrade() -> None:
    conn = op.get_bind()
    row = conn.execute(
        sa.text("SELECT allowed_map FROM investigator_status_policy WHERE id = 1")
    ).fetchone()
    if not row or row[0] is None:
        return
    allowed = _merge_policy(dict(row[0]))
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
    conn.execute(
        sa.text(
            "UPDATE investigator_status_policy SET allowed_map = CAST(:m AS jsonb), "
            "updated_at = now() WHERE id = 1"
        ),
        {"m": json.dumps(allowed)},
    )
