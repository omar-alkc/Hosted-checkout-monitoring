"""Allow wallet_reactivated after wallet_lock / wallet_ci in investigator policy.

Revision ID: 012_wallet_reactivated
Revises: 011_detection_queue_policy
Create Date: 2026-05-23

"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "012_wallet_reactivated"
down_revision = "011_detection_queue_policy"
branch_labels = None
depends_on = None

_POLICY_ADDITIONS: dict[str, list[str]] = {
    "wallet_lock": ["wallet_reactivated"],
    "wallet_ci": ["wallet_reactivated"],
}


def _merge_policy(allowed: dict) -> dict:
    for from_status, targets in _POLICY_ADDITIONS.items():
        current = list(allowed.get(from_status, []))
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
        if not allowed[from_status]:
            del allowed[from_status]
    conn.execute(
        sa.text(
            "UPDATE investigator_status_policy SET allowed_map = CAST(:m AS jsonb), "
            "updated_at = now() WHERE id = 1"
        ),
        {"m": json.dumps(allowed)},
    )
