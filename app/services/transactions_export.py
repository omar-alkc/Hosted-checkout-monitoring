from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.services.import_service import search_transactions_for_batch

EXPORT_MAX_ROWS = 50_000


def _excel_scalar(v: Any) -> Any:
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            return v.astimezone(timezone.utc).replace(tzinfo=None)
        return v
    if isinstance(v, pd.Timestamp):
        ts = v
        if ts.tz is not None:
            ts = ts.tz_convert("UTC").tzlocalize(None)
        return ts.to_pydatetime()
    return v


def build_transactions_export_workbook(
    db: Session,
    *,
    batch_id: int | None = None,
    unique_id: str | None = None,
    msisdn: str | None = None,
    card_id: str | None = None,
    account_holder: str | None = None,
    bank: str | None = None,
    amount_min: str | None = None,
    amount_max: str | None = None,
    dt_from: str | None = None,
    dt_to: str | None = None,
    approved: str | None = None,
) -> tuple[bytes, str]:
    rows, total = search_transactions_for_batch(
        db,
        batch_id=batch_id,
        unique_id=unique_id,
        msisdn=msisdn,
        card_id=card_id,
        account_holder=account_holder,
        bank=bank,
        amount_min=amount_min,
        amount_max=amount_max,
        dt_from=dt_from,
        dt_to=dt_to,
        approved=approved,
        limit=EXPORT_MAX_ROWS,
        offset=0,
    )

    export_rows: list[dict[str, Any]] = []
    for row in rows:
        p = dict(row.payload or {})
        export_rows.append(
            {
                "import_batch_id": row.import_batch_id,
                "row_index": row.row_index,
                "unique_id": p.get("UniqueId") or p.get("unique_id") or "",
                "msisdn": p.get("WalletId") or p.get("Mobile") or "",
                "card_id": p.get("CardId") or "",
                "account_holder": p.get("AccountHolder") or "",
                "bank": p.get("OPP_card.issuer.bank") or "",
                "amount": p.get("Amount"),
                "timestamp": p.get("TxnTimestamp") or p.get("RequestTimestamp") or p.get("TxnDate"),
                "approved": p.get("Approved"),
                "transaction_id": p.get("TransactionId") or "",
            }
        )

    df = pd.DataFrame(export_rows)
    for col in df.columns:
        df[col] = df[col].map(_excel_scalar)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Transactions", index=False)

    now = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return buf.getvalue(), f"aml_transactions_{now}.xlsx"
