from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.constants import SCENARIO_LABELS
from app.services.detections_service import list_detections


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
    scenario_id: str | None = None,
    batch_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    assigned: str | None = None,
    detection_id: int | None = None,
    msisdn: str | None = None,
    risk: str | None = None,
) -> tuple[bytes, str]:
    """Return (xlsx_bytes, suggested_filename)."""
    dets = list_detections(
        db,
        status=status,
        scenario_id=scenario_id,
        batch_id=batch_id,
        date_from=date_from,
        date_to=date_to,
        assigned=assigned,
        detection_id=detection_id,
        msisdn=msisdn,
        risk=risk,
    )

    now = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    fname = f"aml_detections_{now}.xlsx"

    det_rows: list[dict[str, Any]] = []
    for d in dets:
        m = dict(d.metrics or {})
        row: dict[str, Any] = {
            "id": d.id,
            "scenario_id": d.scenario_id,
            "scenario_name": SCENARIO_LABELS.get(str(d.scenario_id).strip().upper(), d.scenario_id),
            "period": d.period,
            "status": d.status,
            "assigned_senior": d.assigned_senior or "",
            "import_batch_id": d.import_batch_id,
            "created_at": d.created_at,
            "updated_at": d.updated_at,
        }
        for k, v in m.items():
            row[f"metric_{k}"] = v
        det_rows.append({k: _excel_scalar(v) for k, v in row.items()})

    df_det = pd.DataFrame(det_rows)
    if dets:
        df_sum = (
            df_det.groupby(["scenario_id", "status", "period"], dropna=False)
            .size()
            .reset_index(name="count")
            .sort_values(["scenario_id", "status", "period"])
        )
        total = int(len(dets))
    else:
        df_sum = pd.DataFrame(columns=["scenario_id", "status", "period", "count"])
        total = 0

    meta = pd.DataFrame(
        [
            {"key": "exported_at_utc", "value": datetime.now(timezone.utc).isoformat()},
            {"key": "detection_row_count", "value": total},
            {"key": "filter_status", "value": status or ""},
            {"key": "filter_scenario_id", "value": scenario_id or ""},
            {"key": "filter_batch_id", "value": batch_id if batch_id is not None else ""},
            {"key": "filter_date_from", "value": date_from or ""},
            {"key": "filter_date_to", "value": date_to or ""},
            {"key": "filter_assigned", "value": assigned or ""},
            {"key": "filter_detection_id", "value": detection_id if detection_id is not None else ""},
            {"key": "filter_msisdn", "value": msisdn or ""},
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
