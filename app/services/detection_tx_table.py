from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.minitrans_window import _coerce_utc_ts

HOSTED_SORT_KEYS = frozenset(
    {"batch", "row_index", "msisdn", "card_id", "account_holder", "bank", "amount", "timestamp", "approved"}
)
WALLET_SORT_KEYS = frozenset(
    {"timestamp", "transaction_id", "amount", "credited_msisdn", "debited_msisdn", "type", "description"}
)


def normalize_sort_dir(value: str | None, *, default: str = "desc") -> str:
    d = (value or default).strip().lower()
    return d if d in {"asc", "desc"} else default


def normalize_hosted_sort(value: str | None) -> str:
    key = (value or "timestamp").strip().lower()
    return key if key in HOSTED_SORT_KEYS else "timestamp"


def normalize_wallet_sort(value: str | None) -> str:
    key = (value or "timestamp").strip().lower()
    return key if key in WALLET_SORT_KEYS else "timestamp"


def hosted_row_after_detection(payload: dict, anchor: object | None) -> bool:
    anchor_ts = _coerce_utc_ts(anchor)
    if anchor_ts is None:
        return False
    ts = _coerce_utc_ts(
        payload.get("TxnTimestamp") or payload.get("RequestTimestamp") or payload.get("TxnDate")
    )
    return bool(ts is not None and ts > anchor_ts)


def _wallet_row_sort_value(row: dict[str, Any], sort_by: str) -> Any:
    if sort_by == "timestamp":
        ts = _coerce_utc_ts(row.get("timestamp"))
        if ts is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        return ts.to_pydatetime()
    if sort_by == "amount":
        try:
            return float(row.get("transactionAmount") or 0)
        except (TypeError, ValueError):
            return 0.0
    field_map = {
        "transaction_id": "transactionId",
        "credited_msisdn": "creditedMSISDN",
        "debited_msisdn": "debitedMSISDN",
        "type": "transactionType",
        "description": "transactionDescription",
    }
    raw = row.get(field_map.get(sort_by, sort_by))
    return str(raw or "").lower()


def sort_wallet_tx_rows(
    rows: list[dict[str, Any]],
    *,
    sort_by: str,
    sort_dir: str,
) -> list[dict[str, Any]]:
    reverse = normalize_sort_dir(sort_dir) == "desc"
    return sorted(rows, key=lambda r: _wallet_row_sort_value(r, sort_by), reverse=reverse)


def paginate_rows(rows: list[Any], *, page: int, per_page: int) -> tuple[list[Any], int, int, int]:
    total = len(rows)
    pages = max(1, (total + per_page - 1) // per_page)
    p = max(1, min(int(page), pages))
    start = (p - 1) * per_page
    return rows[start : start + per_page], total, p, pages
