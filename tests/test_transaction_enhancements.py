from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from app.services.detection_tx_table import hosted_row_after_detection, paginate_rows, sort_wallet_tx_rows
from app.services.minitrans_window import (
    _coerce_utc_ts,
    compute_minitrans_window,
    resolve_detection_anchor,
)
from app.services.scenarios_service import (
    TRANSACTION_FILTER_LABELS,
    _valid_transaction_filter,
    default_transaction_filter_for_group,
)
from app.services.transactions_export import build_transactions_export_workbook
from wallet_enrichment import apply_scenario_slice_for_linked_indices


def test_valid_transaction_filter():
    assert _valid_transaction_filter("approved_only") == "approved_only"
    assert _valid_transaction_filter("both") == "both"
    with pytest.raises(ValueError, match="Invalid transaction filter"):
        _valid_transaction_filter("invalid")


def test_default_transaction_filter_by_group():
    assert default_transaction_filter_for_group("multiple_failed") == "failed_only"
    assert default_transaction_filter_for_group("many_cards_one_wallet") == "approved_only"


def test_transaction_filter_labels_complete():
    assert set(TRANSACTION_FILTER_LABELS.keys()) == {"approved_only", "failed_only", "both"}


def test_resolve_detection_anchor_mixed_tz():
    metrics = {"TxnDate": "2026-01-10"}
    payloads = [{"TxnTimestamp": "2026-01-15T12:00:00+00:00"}]
    created = datetime(2026, 1, 14, tzinfo=timezone.utc)
    anchor = resolve_detection_anchor(metrics, payloads, created)
    assert anchor is not None
    assert anchor == pd.Timestamp("2026-01-15T12:00:00+00:00")


def test_compute_minitrans_window_naive_anchor_normalized():
    anchor = pd.Timestamp("2026-01-15 12:00:00")  # naive
    dt_from, dt_to = compute_minitrans_window(anchor, before_preset="last_week", include_after=True)
    assert dt_from is not None
    assert dt_to is not None


def test_coerce_utc_ts_compare_naive_and_aware():
    naive = _coerce_utc_ts("2026-01-16 10:00:00")
    aware = _coerce_utc_ts("2026-01-15T12:00:00+00:00")
    assert naive is not None and aware is not None
    assert naive > aware


def test_compute_minitrans_window_last_week():
    anchor = pd.Timestamp("2026-01-15 12:00:00", tz=timezone.utc)
    dt_from, dt_to = compute_minitrans_window(anchor, before_preset="last_week", include_after=False)
    assert dt_from is not None
    assert dt_to is not None
    assert dt_from < dt_to


def test_compute_minitrans_window_include_after_extends_to_now():
    anchor = pd.Timestamp("2026-01-15 12:00:00", tz=timezone.utc)
    dt_from, dt_to = compute_minitrans_window(anchor, before_preset="none", include_after=True)
    assert dt_from is not None
    assert dt_to is not None
    assert dt_to >= datetime.now(timezone.utc).replace(microsecond=0) - pd.Timedelta(minutes=1)


def test_apply_scenario_slice_both_keeps_all_rows():
    m = pd.DataFrame(
        {
            "Approved": [True, False],
            "Rejected": [False, True],
            "Amount": [100, 50],
        }
    )
    out = apply_scenario_slice_for_linked_indices(
        m, "D1", group_type="many_cards_one_wallet", transaction_filter="both"
    )
    assert len(out) == 2


def test_apply_scenario_slice_failed_only():
    m = pd.DataFrame(
        {
            "Approved": [True, False],
            "Rejected": [False, True],
            "Amount": [100, 50],
        }
    )
    out = apply_scenario_slice_for_linked_indices(
        m, "D1", group_type="many_cards_one_wallet", transaction_filter="failed_only"
    )
    assert len(out) == 1
    assert out.iloc[0]["Amount"] == 50


def test_hosted_row_after_detection():
    anchor = "2026-07-06T11:30:00+00:00"
    assert hosted_row_after_detection({"TxnTimestamp": "2026-07-06T12:00:00+00:00"}, anchor) is True
    assert hosted_row_after_detection({"TxnTimestamp": "2026-07-06T10:00:00+00:00"}, anchor) is False


def test_paginate_rows():
    rows, total, page, pages = paginate_rows(list(range(25)), page=2, per_page=10)
    assert total == 25
    assert page == 2
    assert pages == 3
    assert len(rows) == 10
    assert rows[0] == 10


def test_sort_wallet_tx_rows_amount():
    data = [
        {"transactionAmount": 100, "timestamp": "2026-01-01"},
        {"transactionAmount": 500, "timestamp": "2026-01-02"},
    ]
    out = sort_wallet_tx_rows(data, sort_by="amount", sort_dir="desc")
    assert out[0]["transactionAmount"] == 500


def test_export_workbook_single_sheet(monkeypatch):
    class FakeRow:
        def __init__(self):
            self.import_batch_id = 1
            self.row_index = 1
            self.payload = {
                "WalletId": "9647000000001",
                "CardId": "4111111111111111",
                "Amount": 1000,
                "Approved": True,
                "TxnTimestamp": "2026-01-01T10:00:00",
            }

    monkeypatch.setattr(
        "app.services.transactions_export.search_transactions_for_batch",
        lambda *a, **k: ([FakeRow()], 1),
    )
    data, name = build_transactions_export_workbook(None)
    assert name.endswith(".xlsx")
    assert len(data) > 100

    import io

    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["Transactions"]
