"""
Re-run MariaDB wallet profile enrichment and D1/D2 external risk lookup for persisted detections.

Used when the external DB was temporarily unavailable during scenario runs.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Detection
from app.services.import_service import dataframe_for_batch, _ensure_repo_on_path
from app.services.serialize import json_safe
from app.services.thresholds_service import get_or_create_scenario_config, overrides_dict_from_row
from app.services.scenario_run import _d1_d2_detection_risk_metrics

_ensure_repo_on_path()

from scenarios import params_from_overrides, scenario_defaults  # noqa: E402
from wallet_enrichment import (  # noqa: E402
    apply_linked_row_totals_to_metrics,
    enrich_detection_metrics_dataframe,
    enrich_top_card_metrics,
)

_KEY_COLS_MAP: dict[str, list[str]] = {
    "D1": ["WalletId", "TxnDate"],
    "D2": ["CardId", "TxnDate"],
    "D3": ["WalletId", "TxnDate"],
    "W1": ["WalletId", "TxnWeek"],
    "W2": ["CardId", "TxnWeek"],
    "W3": ["WalletId", "TxnWeek"],
}

_RISK_METRIC_KEYS = (
    "Risk",
    "RiskError",
    "RiskObservedExpenditurePct",
    "RiskObservedExpenditureAmount",
    "RiskObservedWalletsPct",
    "RiskObservedMatchedWalletCount",
    "RiskObservedWalletCount",
    "RiskObservedMaxWalletExpenditurePct",
)


def _nonempty_str(x: object) -> bool:
    return bool(str(x or "").strip())


def _enrichment_outcome_ok(metrics_dict: dict[str, Any], *, sid: str) -> bool:
    """
    True after a full recompute if external enrichment succeeded:
    - When metrics include wallet id(s), holder name(s) must be present.
    - D1/D2 must have Risk High/Low with no RiskError.
    """
    has_wallet_ids = _nonempty_str(metrics_dict.get("WalletId")) or _nonempty_str(metrics_dict.get("WalletIdsPipe"))
    if has_wallet_ids:
        holder_ok = _nonempty_str(metrics_dict.get("WalletHolderFullName")) or _nonempty_str(
            metrics_dict.get("WalletHolderNamesPipe")
        )
        if not holder_ok:
            return False
    if sid in {"D1", "D2"}:
        if _nonempty_str(metrics_dict.get("RiskError")):
            return False
        if not _nonempty_str(metrics_dict.get("Risk")):
            return False
    return True


def _enrichment_failure_detail(metrics_dict: dict[str, Any], *, sid: str) -> str:
    parts: list[str] = []
    has_wallet_ids = _nonempty_str(metrics_dict.get("WalletId")) or _nonempty_str(metrics_dict.get("WalletIdsPipe"))
    if has_wallet_ids:
        holder_ok = _nonempty_str(metrics_dict.get("WalletHolderFullName")) or _nonempty_str(
            metrics_dict.get("WalletHolderNamesPipe")
        )
        if not holder_ok:
            parts.append("wallet holder still empty")
    if sid in {"D1", "D2"}:
        if _nonempty_str(metrics_dict.get("RiskError")):
            parts.append(f"risk: {str(metrics_dict.get('RiskError'))[:120]}")
        elif not _nonempty_str(metrics_dict.get("Risk")):
            parts.append("risk still missing")
    return "; ".join(parts) if parts else "enrichment incomplete"


def _coerce_detection_metrics_frame(md: dict[str, Any]) -> pd.DataFrame:
    """Build a single-row scenario frame from JSON metrics; align date columns with dataframe_for_batch."""
    det_df = pd.DataFrame([md])
    if "TxnDate" in det_df.columns:
        det_df["TxnDate"] = pd.to_datetime(det_df["TxnDate"], errors="coerce").dt.date
    if "TxnWeek" in det_df.columns:
        det_df["TxnWeek"] = pd.to_datetime(det_df["TxnWeek"], errors="coerce").dt.date
    return det_df


def retry_wallet_and_risk_enrichment(db: Session) -> dict[str, Any]:
    """
    Recompute wallet holder fields and D1/D2 risk for every detection that has linked transaction rows.

    Returns dict: updated, failed, skipped_ok (no linked rows), errors (list, capped).
    """
    log = logging.getLogger(__name__)
    cfg_row = get_or_create_scenario_config(db)
    overrides = overrides_dict_from_row(cfg_row)
    params = params_from_overrides({**scenario_defaults(), **overrides})

    city_mapping: dict[str, str] = {}
    try:
        from io_utils import load_city_name_mapping_from_env

        city_mapping = dict(load_city_name_mapping_from_env())
    except Exception:
        pass

    all_dets = list(db.scalars(select(Detection).order_by(Detection.import_batch_id.asc(), Detection.id.asc())).all())
    # Process all detections with linked rows so a full retry matches scenario count (not only "incomplete" rows).
    candidates = [d for d in all_dets if list(d.raw_row_indices or [])]
    skipped_ok = len(all_dets) - len(candidates)

    by_batch: dict[int, list[Detection]] = {}
    for d in candidates:
        by_batch.setdefault(d.import_batch_id, []).append(d)

    updated = 0
    failed = 0
    errors: list[str] = []
    max_err_samples = 20

    for batch_id, batch_dets in by_batch.items():
        df = dataframe_for_batch(db, batch_id)
        if df.empty:
            for d in batch_dets:
                failed += 1
                if len(errors) < max_err_samples:
                    errors.append(f"#{d.id}: import batch {batch_id} has no transaction rows")
            continue

        all_wallets: set[str] = set()
        if "WalletId" in df.columns:
            for x in df["WalletId"].astype(str).tolist():
                t = x.strip()
                if t and t.lower() != "nan":
                    all_wallets.add(t)

        wallet_profiles_df: pd.DataFrame | None
        try:
            if not all_wallets:
                wallet_profiles_df = pd.DataFrame()
            else:
                from io_utils import fetch_wallet_profiles

                wallet_profiles_df = fetch_wallet_profiles(sorted(all_wallets))
        except Exception as e:
            log.warning("Wallet profile fetch failed for batch %s: %s", batch_id, e)
            wallet_profiles_df = pd.DataFrame()

        for d in batch_dets:
            raw_idx = list(d.raw_row_indices or [])
            if not raw_idx:
                failed += 1
                if len(errors) < max_err_samples:
                    errors.append(f"#{d.id}: no linked row indices")
                continue

            sid = str(d.scenario_id or "").strip().upper()
            md = dict(d.metrics or {})
            if md.get("ScenarioId") is None:
                md["ScenarioId"] = sid

            try:
                det_df = _coerce_detection_metrics_frame(md)
                key_cols = _KEY_COLS_MAP.get(sid, [])

                det_df = enrich_detection_metrics_dataframe(det_df, wallet_profiles_df, city_mapping)
                det_df = enrich_top_card_metrics(det_df, df, key_cols)

                base_series = det_df.iloc[0]
                metrics_dict = base_series.to_dict()
                metrics_dict = apply_linked_row_totals_to_metrics(df, raw_idx, metrics_dict)

                if sid in {"D1", "D2"}:
                    for rk in _RISK_METRIC_KEYS:
                        metrics_dict.pop(rk, None)
                    metrics_dict.update(_d1_d2_detection_risk_metrics(base_series, df, sid, params))

                resolved = _enrichment_outcome_ok(metrics_dict, sid=sid)

                d.metrics = json_safe(metrics_dict)
                db.add(d)
                db.commit()
                if resolved:
                    updated += 1
                else:
                    failed += 1
                    if len(errors) < max_err_samples:
                        errors.append(f"#{d.id}: {_enrichment_failure_detail(metrics_dict, sid=sid)}")
            except Exception as e:
                db.rollback()
                failed += 1
                log.exception("Enrichment retry failed for detection %s", d.id)
                if len(errors) < max_err_samples:
                    errors.append(f"#{d.id}: {e}")

    return {"updated": updated, "failed": failed, "skipped_ok": skipped_ok, "errors": errors}
