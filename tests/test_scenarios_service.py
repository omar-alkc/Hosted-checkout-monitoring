from __future__ import annotations

import pytest

from app.services.scenarios_service import (
    GROUP_TYPE_LABELS,
    _normalize_thresholds,
    period_display,
)


def test_period_display_weeks():
    assert period_display("week", 2) == "2 weeks"
    assert period_display("day", 1) == "1 day"
    assert period_display("hour", 24) == "24 hours"


def test_normalize_thresholds_many_cards():
    t = _normalize_thresholds(
        "many_cards_one_wallet",
        {"min_txn": "5", "min_unique_cards": "3", "min_amount_per_txn": "1000", "min_total_amount": "5000"},
    )
    assert t["min_txn"] == 5
    assert t["min_total_amount"] == 5000.0


def test_invalid_group_raises():
    with pytest.raises(ValueError, match="Invalid scenario group"):
        _normalize_thresholds("not_a_group", {})


def test_group_type_labels_complete():
    assert len(GROUP_TYPE_LABELS) == 4
    assert "one_card_one_wallet" in GROUP_TYPE_LABELS
