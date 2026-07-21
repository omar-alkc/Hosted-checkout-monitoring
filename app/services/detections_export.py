from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.services.detections_service import list_detections_with_previous_count
from app.services.thresholds_service import scenario_label_map


def _excel_scalar(v: Any) -> Any:
    """openpyxl cannot write timezone-aware datetimes; normalize for export."""
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            return v.astimezone(timezone.utc).replace(tzinfo=None)
        return v
    if isinstance(v, pd.Timestamp):
        ts = v
        if ts.tz is not None:
            ts = ts.tz_convert("UTC").tz_localize(None)
        return ts.to_pydatetime()
    return v


def build_detections_export_workbook(
    db: Session,
    *,
    status: str | None = None,
    queue: str | None = None,
    scenario_id: str | None = None,
    batch_id: int | None = None,
    scope: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    assigned: str | None = None,
    detection_id: int | None = None,
    msisdn: str | None = None,
    card_id: str | None = None,
    risk: str | None = None,
) -> tuple[bytes, str]:
    """Return (xlsx_bytes, suggested_filename)."""
    det_rows_export = list_detections_with_previous_count(
        db,
        status=status,
        queue=queue,
        scenario_id=scenario_id,
        batch_id=batch_id,
        scope=scope,
        date_from=date_from,
        date_to=date_to,
        assigned=assigned,
        detection_id=detection_id,
        msisdn=msisdn,
        card_id=card_id,
        risk=risk,
    )

    now = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    fname = f"aml_detections_{now}.xlsx"

    det_rows: list[dict[str, Any]] = []
    labels = scenario_label_map(db)
    for d, _prev, pe_days in det_rows_export:
        m = dict(d.metrics or {})
        row: dict[str, Any] = {
            "id": d.id,
            "scenario_id": d.scenario_id,
            "scenario_name": labels.get(str(d.scenario_id).strip().upper(), d.scenario_id),
            "period": d.period,
            "status": d.status,
            "pending_evidence_days": pe_days if pe_days is not None else "",
            "assigned_senior": d.assigned_senior or "",
            "import_batch_id": d.import_batch_id,
            "created_at": d.created_at,
            "updated_at": d.updated_at,
        }
        for k, v in m.items():
            row[f"metric_{k}"] = v
        det_rows.append({k: _excel_scalar(v) for k, v in row.items()})

    df_det = pd.DataFrame(det_rows)
    if det_rows_export:
        df_sum = (
            df_det.groupby(["scenario_id", "status", "period"], dropna=False)
            .size()
            .reset_index(name="count")
            .sort_values(["scenario_id", "status", "period"])
        )
        total = int(len(det_rows_export))
    else:
        df_sum = pd.DataFrame(columns=["scenario_id", "status", "period", "count"])
        total = 0

    meta = pd.DataFrame(
        [
            {"key": "exported_at_utc", "value": datetime.now(timezone.utc).isoformat()},
            {"key": "detection_row_count", "value": total},
            {"key": "filter_status", "value": status or ""},
            {"key": "filter_queue", "value": queue or ""},
            {"key": "filter_scenario_id", "value": scenario_id or ""},
            {"key": "filter_batch_id", "value": batch_id if batch_id is not None else ""},
            {"key": "filter_scope", "value": scope or ""},
            {"key": "filter_date_from", "value": date_from or ""},
            {"key": "filter_date_to", "value": date_to or ""},
            {"key": "filter_assigned", "value": assigned or ""},
            {"key": "filter_detection_id", "value": detection_id if detection_id is not None else ""},
            {"key": "filter_msisdn", "value": msisdn or ""},
            {"key": "filter_card_id", "value": card_id or ""},
            {"key": "filter_risk", "value": risk or ""},
        ]
    )

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        meta.to_excel(writer, sheet_name="Export_info", index=False)
        df_sum.to_excel(writer, sheet_name="Summary", index=False)
        df_det.to_excel(writer, sheet_name="Detections", index=False)
    buf.seek(0)
    return buf.getvalue(), fname
