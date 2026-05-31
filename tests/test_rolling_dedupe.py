from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.models import Detection
from app.services.scenario_run import (
    _collapse_rolling_det_rows,
    _find_open_rolling_detection,
    _refresh_open_rolling_detection,
    _rolling_key_field_for_scenario,
)


def test_rolling_key_field_for_scenario():
    assert _rolling_key_field_for_scenario("W1") == "WalletId"
    assert _rolling_key_field_for_scenario("W3") == "WalletId"
    assert _rolling_key_field_for_scenario("W2") == "CardId"


def test_collapse_rolling_det_rows_keeps_latest_txnweek_per_wallet():
    det = pd.DataFrame(
        [
            {"WalletId": "964770001111", "TxnWeek": date(2026, 5, 25), "TxnCount": 10},
            {"WalletId": "964770001111", "TxnWeek": date(2026, 5, 27), "TxnCount": 12},
            {"WalletId": "964770001111", "TxnWeek": date(2026, 5, 29), "TxnCount": 14},
            {"WalletId": "964770002222", "TxnWeek": date(2026, 5, 26), "TxnCount": 8},
        ]
    )
    out = _collapse_rolling_det_rows(det, "W1")
    assert len(out) == 2
    w1 = out[out["WalletId"] == "964770001111"].iloc[0]
    assert w1["TxnWeek"] == date(2026, 5, 29)
    assert int(w1["TxnCount"]) == 14


def test_collapse_rolling_det_rows_groups_by_card_for_w2():
    det = pd.DataFrame(
        [
            {"CardId": "4111111111111111", "TxnWeek": date(2026, 5, 24), "TxnCount": 1},
            {"CardId": "4111111111111111", "TxnWeek": date(2026, 5, 28), "TxnCount": 3},
        ]
    )
    out = _collapse_rolling_det_rows(det, "W2")
    assert len(out) == 1
    assert out.iloc[0]["TxnWeek"] == date(2026, 5, 28)


def test_refresh_open_rolling_detection_merges_indices_and_metrics():
    det = Detection(
        import_batch_id=None,
        scope_type="rolling",
        scope_days=7,
        scope_as_of=datetime(2026, 5, 27, tzinfo=timezone.utc),
        scenario_id="W1",
        period="weekly",
        status="new",
        metrics={"WalletId": "964770001111", "TxnWeek": "2026-05-27", "TxnCount": 10},
        raw_row_indices=[1, 2, 3],
    )
    as_of = datetime(2026, 5, 29, tzinfo=timezone.utc)
    _refresh_open_rolling_detection(
        det,
        metrics_dict={
            "WalletId": "964770001111",
            "TxnWeek": "2026-05-29",
            "TxnCount": 14,
            "RollingWindowDays": 7,
        },
        raw_idx=[3, 4, 5],
        scope_days=7,
        as_of=as_of,
    )
    assert det.raw_row_indices == [1, 2, 3, 4, 5]
    assert det.metrics["TxnWeek"] == "2026-05-29"
    assert det.metrics["TxnCount"] == 14
    assert det.scope_as_of == as_of


def test_find_open_rolling_detection_returns_detection():
    db = MagicMock()
    db.execute.return_value.first.return_value = (42,)
    existing = Detection(id=42, status="new")
    db.get.return_value = existing

    found = _find_open_rolling_detection(
        db,
        period="weekly",
        scenario_id="W1",
        key_field="WalletId",
        key_value="964770001111",
    )
    assert found is existing
    db.get.assert_called_once_with(Detection, 42)


def test_find_open_rolling_detection_returns_none_when_missing():
    db = MagicMock()
    db.execute.return_value.first.return_value = None
    assert (
        _find_open_rolling_detection(
            db,
            period="weekly",
            scenario_id="W1",
            key_field="WalletId",
            key_value="964770001111",
        )
        is None
    )


def test_find_open_rolling_detection_rejects_invalid_key_field():
    db = MagicMock()
    assert (
        _find_open_rolling_detection(
            db,
            period="weekly",
            scenario_id="W1",
            key_field="NotAKey",
            key_value="x",
        )
        is None
    )
    db.execute.assert_not_called()


@pytest.mark.parametrize(
    "status",
    ["false_positive_final", "suspicious_final", "wallet_lock"],
)
def test_open_statuses_excluded_from_closed_detection_refresh_path(status: str):
    """Closed statuses are not in OPEN_DETECTION_STATUSES; find_open should not match them via SQL filter."""
    from app.constants import OPEN_DETECTION_STATUSES

    assert status not in OPEN_DETECTION_STATUSES
