from __future__ import annotations

import json
import logging
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import Detection
from app.services.import_service import dataframe_for_batch, dataframe_for_rolling_window, _ensure_repo_on_path
from app.services.serialize import json_safe
from app.services.thresholds_service import SCENARIO_CODES, get_or_create_scenario_config, overrides_dict_from_row, scenario_enabled_normalized

_ensure_repo_on_path()
from wallet_enrichment import (
    apply_linked_row_totals_to_metrics,
    apply_scenario_slice_for_linked_indices,
    enrich_detection_metrics_dataframe,
    enrich_top_card_metrics,
    slice_raw_for_detection_row,
)


def _det_indices_for_row(
    det_row: pd.Series,
    raw: pd.DataFrame,
    key_cols: list[str],
    *,
    scenario_id: str,
    use_transaction_row_ids: bool = False,
) -> list[int]:
    """
    Map a scenario output row back to linked transaction identifiers.

    - Batch runs: link by `_aml_row_index` (stored as TransactionRow.row_index on the detection).
    - Rolling runs: pass ``use_transaction_row_ids=True`` so `_aml_transaction_row_id` (TransactionRow.id)
      is used when present. Batch dataframes from ``dataframe_for_batch`` do not include that column,
      so they always resolve via `_aml_row_index`` even if this flag is true.

    Uses normalized comparisons so detection rows from groupby (mixed dtypes) still match
    `raw` rows from `dataframe_for_batch` (dates as date, timestamps normalized).
    Cash-in scenarios (D1/D2/W1/W2) persist only approved rows; D3/W3 only rejected rows.
    """
    m = slice_raw_for_detection_row(det_row, raw, key_cols)
    if m.empty:
        return []
    m = apply_scenario_slice_for_linked_indices(m, scenario_id)
    if m.empty:
        return []
    if use_transaction_row_ids and "_aml_transaction_row_id" in m.columns:
        ids = m["_aml_transaction_row_id"].dropna().astype(int).unique().tolist()
        return sorted(int(x) for x in ids)
    if "_aml_row_index" in m.columns:
        idx = m["_aml_row_index"].dropna().astype(int).unique().tolist()
        return sorted(int(x) for x in idx)
    return []


def _detection_exists_for_exact_indices(db: Session, *, batch_id: int, raw_idx: list[int]) -> bool:
    """
    True if a detection already exists in this batch with the exact same raw_row_indices.

    Note: raw_row_indices is JSONB; equality is order-sensitive, so callers should pass a
    canonical (sorted) list. _det_indices_for_row already returns sorted indices.
    """
    if not raw_idx:
        return False
    stmt = text(
        """
        SELECT 1
        FROM detections
        WHERE import_batch_id = :b
          AND raw_row_indices = CAST(:idx AS jsonb)
        LIMIT 1
        """
    )
    # Bind JSON explicitly for consistent behavior across drivers.
    return db.execute(stmt, {"b": int(batch_id), "idx": json.dumps(list(raw_idx))}).first() is not None


def _rolling_detection_exists(
    db: Session,
    *,
    period: str,
    scenario_id: str,
    key_field: str,
    key_value: str,
    window_end: str,
) -> bool:
    """
    True if a rolling detection already exists for the same customer and alert day.

    Dedupe key: (scope_type=rolling, period, scenario_id, TxnWeek window-end date, wallet/card).
    ``scope_days`` and bounded ``RollingPeriod*`` are not part of the key — reruns with
    different load windows must not create a second alert for the same MSISDN/card on the same day.
    """
    sid = str(scenario_id or "").strip().upper()
    per = str(period or "").strip().lower()
    kf = str(key_field or "").strip()
    kv = str(key_value or "").strip()
    we = str(window_end or "").strip()
    if not (sid and per and kf and kv and we):
        return False
    if kf not in {"WalletId", "CardId"}:
        return False
    bind: dict[str, object] = {
        "per": per,
        "sid": sid,
        "we": we,
        "kf": kf,
        "kv": kv,
    }
    stmt = text(
        """
        SELECT 1
        FROM detections
        WHERE scope_type = 'rolling'
          AND period = :per
          AND scenario_id = :sid
          AND trim(coalesce(metrics->>'TxnWeek','')) = :we
          AND trim(coalesce(metrics->>:kf,'')) = :kv
        LIMIT 1
        """
    )
    return db.execute(stmt, bind).first() is not None


def _enrich_wallet_pipe(det: pd.DataFrame, raw: pd.DataFrame, keys: list[str], pipe_col: str) -> pd.DataFrame:
    if det.empty or raw.empty or len(keys) < 2:
        return det
    g1, g2 = keys[0], keys[1]
    if g1 not in raw.columns or g2 not in raw.columns:
        return det
    wallet_pipe = (
        raw.groupby([g1, g2])["WalletId"]
        .apply(lambda s: "|".join(sorted({str(x).strip() for x in s if str(x).strip()})))
        .reset_index(name=pipe_col)
    )
    return det.merge(wallet_pipe, on=[g1, g2], how="left")


def _d1_d2_detection_risk_metrics(det_row: pd.Series, raw: pd.DataFrame, sid: str, params: Any) -> dict[str, object]:
    sid = str(sid or "").strip().upper()
    if sid not in {"D1", "D2"} or raw is None or raw.empty:
        return {}

    try:
        from io_utils import fetch_post_card_debit_transactions
    except Exception as e:
        logging.getLogger(__name__).warning("Risk enrichment unavailable: %s", e)
        return {"RiskError": str(e)}

    subset = raw.copy()
    if sid == "D1" and "WalletId" in det_row.index and "WalletId" in subset.columns:
        subset = subset[subset["WalletId"] == det_row["WalletId"]]
    if sid == "D2" and "CardId" in det_row.index and "CardId" in subset.columns:
        subset = subset[subset["CardId"] == det_row["CardId"]]
    if "TxnDate" in det_row.index and "TxnDate" in subset.columns:
        subset = subset[subset["TxnDate"] == det_row["TxnDate"]]
    if subset.empty:
        return {}

    approved = subset["Approved"].fillna(False).astype(bool) if "Approved" in subset.columns else pd.Series(False, index=subset.index)
    approved_subset = subset[approved] if len(subset) else subset.iloc[0:0].copy()

    wallet_info: list[tuple[str, str, float]] = []
    if sid == "D1":
        wallet = str(det_row.get("WalletId", "") or "").strip()
        if wallet and not approved_subset.empty:
            start_ts = pd.to_datetime(approved_subset["TxnTimestamp"], errors="coerce").min()
            if pd.notna(start_ts):
                wallet_amount = float(pd.to_numeric(approved_subset["Amount"], errors="coerce").fillna(0).sum())
                wallet_info.append((wallet, start_ts.strftime("%Y-%m-%d %H:%M:%S"), wallet_amount))
    else:
        for wallet, g in approved_subset.groupby("WalletId", dropna=False):
            w = str(wallet or "").strip()
            if not w or w.lower() == "nan":
                continue
            start_ts = pd.to_datetime(g["TxnTimestamp"], errors="coerce").min()
            if pd.isna(start_ts):
                continue
            wallet_amount = float(pd.to_numeric(g["Amount"], errors="coerce").fillna(0).sum())
            wallet_info.append((w, start_ts.strftime("%Y-%m-%d %H:%M:%S"), wallet_amount))

    if not wallet_info:
        return {}

    try:
        post_df = fetch_post_card_debit_transactions([(w, s) for w, s, _a in wallet_info])
    except Exception as e:
        logging.getLogger(__name__).warning("Risk lookup failed for %s: %s", sid, e)
        return {"RiskError": str(e)}

    total_amount = float(det_row.get("TotalAmount", 0) or 0)
    if sid == "D1":
        wallet, _start_ts, wallet_amount = wallet_info[0]
        debit_amt = 0.0
        if not post_df.empty:
            debit_amt = float(
                pd.to_numeric(
                    post_df.loc[post_df["query_wallet"].astype(str).str.strip() == wallet, "transactionAmount"],
                    errors="coerce",
                )
                .fillna(0)
                .sum()
            )
        observed_pct = (debit_amt / wallet_amount * 100.0) if wallet_amount > 0 else 0.0
        is_high = (
            total_amount >= float(getattr(params, "d1_risk_min_total_amount", 0) or 0)
            and observed_pct >= float(getattr(params, "d1_risk_min_expenditure_pct", 0) or 0)
        )
        return {
            "Risk": "High" if is_high else "Low",
            "RiskObservedExpenditurePct": round(observed_pct, 2),
            "RiskObservedExpenditureAmount": round(debit_amt, 2),
        }

    wallet_threshold = float(getattr(params, "d2_risk_min_wallet_expenditure_pct", 0) or 0)
    wallets_pct_threshold = float(getattr(params, "d2_risk_min_wallets_pct", 0) or 0)
    matched_wallets = 0
    wallet_count = 0
    observed_wallet_pcts: list[float] = []
    for wallet, _start_ts, wallet_amount in wallet_info:
        wallet_count += 1
        debit_amt = 0.0
        if not post_df.empty:
            debit_amt = float(
                pd.to_numeric(
                    post_df.loc[post_df["query_wallet"].astype(str).str.strip() == wallet, "transactionAmount"],
                    errors="coerce",
                )
                .fillna(0)
                .sum()
            )
        observed_pct = (debit_amt / wallet_amount * 100.0) if wallet_amount > 0 else 0.0
        observed_wallet_pcts.append(observed_pct)
        if observed_pct >= wallet_threshold:
            matched_wallets += 1

    observed_wallets_pct = (matched_wallets / wallet_count * 100.0) if wallet_count > 0 else 0.0
    is_high = (
        total_amount >= float(getattr(params, "d2_risk_min_total_amount", 0) or 0)
        and observed_wallets_pct >= wallets_pct_threshold
    )
    return {
        "Risk": "High" if is_high else "Low",
        "RiskObservedWalletsPct": round(observed_wallets_pct, 2),
        "RiskObservedMatchedWalletCount": int(matched_wallets),
        "RiskObservedWalletCount": int(wallet_count),
        "RiskObservedMaxWalletExpenditurePct": round(max(observed_wallet_pcts) if observed_wallet_pcts else 0.0, 2),
    }


def run_scenarios_for_batch(db: Session, *, batch_id: int, period: str) -> dict[str, Any]:
    """
    period: daily | weekly | both
    Deletes existing detections for the batch, then inserts new rows from scenario engine.
    """
    _ensure_repo_on_path()
    from scenarios import DAILY, WEEKLY, params_from_overrides, scenario_defaults

    df = dataframe_for_batch(db, batch_id)
    if df.empty:
        return {"ok": False, "error": "No transactions for this import batch."}

    city_mapping: dict[str, str] = {}
    try:
        from io_utils import load_city_name_mapping_from_env

        city_mapping = dict(load_city_name_mapping_from_env())
    except Exception:
        pass

    cfg_row = get_or_create_scenario_config(db)
    enabled_map = scenario_enabled_normalized(getattr(cfg_row, "scenario_enabled", None))
    overrides = overrides_dict_from_row(cfg_row)
    params = params_from_overrides({**scenario_defaults(), **overrides})

    db.query(Detection).filter(Detection.import_batch_id == batch_id).delete(synchronize_session=False)

    created = 0
    # Within a batch, do not create two detections with identical transaction sets.
    seen_idx_keys: set[tuple[int, ...]] = set()
    periods = []
    if period in {"daily", "both"}:
        periods.append(("daily", DAILY))
    if period in {"weekly", "both"}:
        periods.append(("weekly", WEEKLY))
    if not periods:
        return {"ok": False, "error": "Invalid period. Use daily, weekly, or both."}

    # First pass: run scenarios, collect wallets involved in detections (and connected wallets
    # from the linked raw slices). We'll fetch wallet profiles once, then enrich detections.
    results: list[tuple[str, str, pd.DataFrame, pd.DataFrame, list[str]]] = []
    wallets_to_enrich: set[str] = set()
    key_cols_map = {
        "D1": ["WalletId", "TxnDate"],
        "D2": ["CardId", "TxnDate"],
        "D3": ["WalletId", "TxnDate"],
        "W1": ["WalletId", "TxnWeek"],
        "W2": ["CardId", "TxnWeek"],
        "W3": ["WalletId", "TxnWeek"],
    }

    def _add_wallet_token(x: object) -> None:
        s = str(x or "").strip()
        if s and s.lower() != "nan":
            wallets_to_enrich.add(s)

    for p_name, registry in periods:
        for sid, fn in registry.items():
            if sid in SCENARIO_CODES and not bool(enabled_map.get(sid, True)):
                continue
            det, raw = fn(df, params)
            if det is None or det.empty:
                continue

            if sid in {"D2"} and raw is not None and not raw.empty:
                det = _enrich_wallet_pipe(det, raw, ["CardId", "TxnDate"], "WalletIdsPipe")
            if sid in {"W2"} and raw is not None and not raw.empty:
                det = _enrich_wallet_pipe(det, raw, ["CardId", "TxnWeek"], "WalletIdsPipe")

            key_cols = key_cols_map.get(sid, [])
            det = enrich_top_card_metrics(det, raw, key_cols)

            # Seed wallets from detections + any linked raw rows (connected wallets).
            if "WalletId" in det.columns:
                for x in det["WalletId"].tolist():
                    _add_wallet_token(x)
            if "WalletIdsPipe" in det.columns:
                for pip in det["WalletIdsPipe"].tolist():
                    for seg in str(pip or "").split("|"):
                        _add_wallet_token(seg)
            if raw is not None and not raw.empty and "WalletId" in raw.columns:
                for x in raw["WalletId"].tolist():
                    _add_wallet_token(x)

            results.append((p_name, sid, det, raw, key_cols))

    wallet_profiles_df = None
    if wallets_to_enrich:
        try:
            from io_utils import fetch_wallet_profiles

            wallet_profiles_df = fetch_wallet_profiles(sorted(wallets_to_enrich))
        except Exception as e:
            logging.getLogger(__name__).warning("Wallet profile fetch failed: %s", e)

    # Second pass: enrich and persist.
    for p_name, sid, det, raw, key_cols in results:
        det = enrich_detection_metrics_dataframe(det, wallet_profiles_df, city_mapping)
        for _, det_row in det.iterrows():
            scen = str(det_row["ScenarioId"]) if "ScenarioId" in det_row.index else sid
            raw_idx = _det_indices_for_row(det_row, raw, key_cols, scenario_id=scen)
            idx_key = tuple(sorted(int(x) for x in raw_idx))
            if not idx_key:
                continue
            if idx_key in seen_idx_keys:
                continue
            metrics_dict = det_row.to_dict()
            metrics_dict = apply_linked_row_totals_to_metrics(df, raw_idx, metrics_dict)
            if scen in {"D1", "D2"}:
                metrics_dict.update(_d1_d2_detection_risk_metrics(det_row, raw, scen, params))
            metrics = json_safe(metrics_dict)
            seen_idx_keys.add(idx_key)
            d = Detection(
                import_batch_id=batch_id,
                scope_type="batch",
                scenario_id=scen,
                period=p_name,
                status="new",
                metrics=metrics,
                raw_row_indices=raw_idx,
            )
            db.add(d)
            created += 1

    db.commit()
    return {"ok": True, "detections_created": created}


def run_single_scenario_for_batch(
    db: Session,
    *,
    batch_id: int,
    scenario_id: str,
    status: str,
) -> dict[str, Any]:
    """
    Run ONE scenario (D1..W3) against a batch and insert detections with the given status.
    Does NOT delete existing detections for the batch.
    """
    _ensure_repo_on_path()
    from scenarios import DAILY, WEEKLY, params_from_overrides, scenario_defaults

    sid = scenario_id.strip().upper()
    if sid not in set(DAILY.keys()) | set(WEEKLY.keys()):
        return {"ok": False, "error": "Unknown scenario id."}

    df = dataframe_for_batch(db, batch_id)
    if df.empty:
        return {"ok": False, "error": "No transactions for this import batch."}

    wallet_profiles_df = None

    city_mapping: dict[str, str] = {}
    try:
        from io_utils import load_city_name_mapping_from_env

        city_mapping = dict(load_city_name_mapping_from_env())
    except Exception:
        pass

    cfg_row = get_or_create_scenario_config(db)
    enabled_map = scenario_enabled_normalized(getattr(cfg_row, "scenario_enabled", None))
    if sid in SCENARIO_CODES and not bool(enabled_map.get(sid, True)):
        return {"ok": False, "error": f"Scenario {sid} is disabled in Scenario Manager."}
    overrides = overrides_dict_from_row(cfg_row)
    params = params_from_overrides({**scenario_defaults(), **overrides})

    if sid in DAILY:
        fn = DAILY[sid]
        period_name = "daily"
    else:
        fn = WEEKLY[sid]
        period_name = "weekly"

    det, raw = fn(df, params)
    if sid in {"D2"} and raw is not None and not raw.empty:
        det = _enrich_wallet_pipe(det, raw, ["CardId", "TxnDate"], "WalletIdsPipe")
    if sid in {"W2"} and raw is not None and not raw.empty:
        det = _enrich_wallet_pipe(det, raw, ["CardId", "TxnWeek"], "WalletIdsPipe")
    if det is None or det.empty:
        return {"ok": True, "detections_created": 0}

    key_cols_map = {
        "D1": ["WalletId", "TxnDate"],
        "D2": ["CardId", "TxnDate"],
        "D3": ["WalletId", "TxnDate"],
        "W1": ["WalletId", "TxnWeek"],
        "W2": ["CardId", "TxnWeek"],
        "W3": ["WalletId", "TxnWeek"],
    }
    key_cols = key_cols_map.get(sid, [])

    det = enrich_top_card_metrics(det, raw, key_cols)

    # Targeted wallet enrichment: only wallets in det and linked raw.
    wallets_to_enrich: set[str] = set()
    if "WalletId" in det.columns:
        for x in det["WalletId"].tolist():
            s = str(x or "").strip()
            if s and s.lower() != "nan":
                wallets_to_enrich.add(s)
    if "WalletIdsPipe" in det.columns:
        for pip in det["WalletIdsPipe"].tolist():
            for seg in str(pip or "").split("|"):
                s = str(seg or "").strip()
                if s and s.lower() != "nan":
                    wallets_to_enrich.add(s)
    if raw is not None and not raw.empty and "WalletId" in raw.columns:
        for x in raw["WalletId"].tolist():
            s = str(x or "").strip()
            if s and s.lower() != "nan":
                wallets_to_enrich.add(s)
    if wallets_to_enrich:
        try:
            from io_utils import fetch_wallet_profiles

            wallet_profiles_df = fetch_wallet_profiles(sorted(wallets_to_enrich))
        except Exception as e:
            logging.getLogger(__name__).warning("Wallet profile fetch failed: %s", e)

    det = enrich_detection_metrics_dataframe(det, wallet_profiles_df, city_mapping)

    created = 0
    for _, det_row in det.iterrows():
        raw_idx = (
            _det_indices_for_row(det_row, raw, key_cols, scenario_id=sid) if raw is not None else []
        )
        # Same as run_scenarios_for_batch: never persist a detection without resolved row indices.
        if not raw_idx:
            continue
        metrics_dict = det_row.to_dict()
        metrics_dict = apply_linked_row_totals_to_metrics(df, raw_idx, metrics_dict)
        if sid in {"D1", "D2"}:
            metrics_dict.update(_d1_d2_detection_risk_metrics(det_row, raw, sid, params))
        metrics = json_safe(metrics_dict)
        # Avoid inserting exact duplicates (same transaction set) within the batch.
        if _detection_exists_for_exact_indices(db, batch_id=batch_id, raw_idx=raw_idx):
            continue
        d = Detection(
            import_batch_id=batch_id,
            scope_type="batch",
            scenario_id=sid,
            period=period_name,
            status=status,
            metrics=metrics,
            raw_row_indices=raw_idx,
        )
        db.add(d)
        created += 1
    db.commit()
    return {"ok": True, "detections_created": created}


def run_scenarios_for_rolling(
    db: Session,
    *,
    days: int,
    period: str = "weekly",
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """
    Run rolling scenarios across all imported transactions within the trailing window.

    Rolling detections:
    - import_batch_id is NULL
    - scope_type='rolling', scope_days=<days>, scope_as_of=now()
    - raw_row_indices stores TransactionRow.id values (not row_index)

    Optional ``date_from`` / ``date_to`` (YYYY-MM-DD): inclusive outer period. The same ``days``
    rolling window is anchored at min(period end, now()); only transactions inside both the
    period and that window are loaded. Metrics then include ``RollingPeriodStart`` /
    ``RollingPeriodEnd`` for dedupe.
    """
    _ensure_repo_on_path()
    from datetime import date, datetime, time, timezone

    from scenarios import WEEKLY, params_from_overrides, scenario_defaults

    eff_period = (period or "").strip().lower()
    if eff_period not in {"weekly"}:
        return {"ok": False, "error": "Invalid period for rolling. Use weekly."}
    if int(days) <= 0:
        return {"ok": False, "error": "days must be > 0"}

    df_from = str(date_from or "").strip()
    df_to = str(date_to or "").strip()
    period_start_iso: str | None = None
    period_end_iso: str | None = None
    rolling_p_start: str | None = None
    rolling_p_end: str | None = None
    if (df_from and not df_to) or (df_to and not df_from):
        return {"ok": False, "error": "Provide both period start and end dates, or leave both empty."}
    if df_from and df_to:
        try:
            d0 = date.fromisoformat(df_from)
            d1 = date.fromisoformat(df_to)
        except ValueError:
            return {"ok": False, "error": "Invalid period date format. Use YYYY-MM-DD."}
        if d0 > d1:
            return {"ok": False, "error": "Period start date must be on or before end date."}
        if (d1 - d0).days + 1 > 365:
            return {"ok": False, "error": "Period cannot exceed 365 days."}
        period_start_iso = datetime.combine(d0, time.min, tzinfo=timezone.utc).isoformat()
        period_end_iso = datetime.combine(
            d1, time.max.replace(microsecond=999999), tzinfo=timezone.utc
        ).isoformat()
        rolling_p_start = d0.isoformat()
        rolling_p_end = d1.isoformat()

    if period_start_iso and period_end_iso:
        df = dataframe_for_rolling_window(
            db,
            days=int(days),
            period_start_iso=period_start_iso,
            period_end_iso=period_end_iso,
        )
    else:
        df = dataframe_for_rolling_window(db, days=int(days))

    if df.empty:
        return {"ok": False, "error": "No transactions in rolling window."}

    df.attrs["rolling_window_days"] = int(days)

    city_mapping: dict[str, str] = {}
    try:
        from io_utils import load_city_name_mapping_from_env

        city_mapping = dict(load_city_name_mapping_from_env())
    except Exception:
        pass

    cfg_row = get_or_create_scenario_config(db)
    enabled_map = scenario_enabled_normalized(getattr(cfg_row, "scenario_enabled", None))
    overrides = overrides_dict_from_row(cfg_row)
    params = params_from_overrides({**scenario_defaults(), **overrides})

    # Append-only: do NOT delete old rolling detections.

    results: list[tuple[str, str, pd.DataFrame, pd.DataFrame, list[str]]] = []
    wallets_to_enrich: set[str] = set()
    key_cols_map = {
        "W1": ["WalletId", "TxnWeek"],
        "W2": ["CardId", "TxnWeek"],
        "W3": ["WalletId", "TxnWeek"],
    }

    def _add_wallet_token(x: object) -> None:
        s = str(x or "").strip()
        if s and s.lower() != "nan":
            wallets_to_enrich.add(s)

    for sid, fn in WEEKLY.items():
        if sid in SCENARIO_CODES and not bool(enabled_map.get(sid, True)):
            continue
        det, raw = fn(df, params)
        if det is None or det.empty:
            continue
        if sid in {"W2"} and raw is not None and not raw.empty:
            det = _enrich_wallet_pipe(det, raw, ["CardId", "TxnWeek"], "WalletIdsPipe")
        key_cols = key_cols_map.get(sid, [])
        det = enrich_top_card_metrics(det, raw, key_cols)
        if "WalletId" in det.columns:
            for x in det["WalletId"].tolist():
                _add_wallet_token(x)
        if "WalletIdsPipe" in det.columns:
            for pip in det["WalletIdsPipe"].tolist():
                for seg in str(pip or "").split("|"):
                    _add_wallet_token(seg)
        if raw is not None and not raw.empty and "WalletId" in raw.columns:
            for x in raw["WalletId"].tolist():
                _add_wallet_token(x)
        results.append(("weekly", sid, det, raw, key_cols))

    wallet_profiles_df = None
    if wallets_to_enrich:
        try:
            from io_utils import fetch_wallet_profiles

            wallet_profiles_df = fetch_wallet_profiles(sorted(wallets_to_enrich))
        except Exception as e:
            logging.getLogger(__name__).warning("Wallet profile fetch failed: %s", e)

    created = 0
    seen_idx_keys: set[tuple[int, ...]] = set()
    as_of = datetime.now(timezone.utc)
    for p_name, sid, det, raw, key_cols in results:
        det = enrich_detection_metrics_dataframe(det, wallet_profiles_df, city_mapping)
        for _, det_row in det.iterrows():
            scen = str(det_row["ScenarioId"]) if "ScenarioId" in det_row.index else sid
            raw_idx = _det_indices_for_row(
                det_row, raw, key_cols, scenario_id=scen, use_transaction_row_ids=True
            )
            idx_key = tuple(sorted(int(x) for x in raw_idx))
            if not idx_key:
                continue
            if idx_key in seen_idx_keys:
                continue
            metrics_dict = det_row.to_dict()
            metrics_dict["RollingWindowDays"] = int(days)
            if rolling_p_start and rolling_p_end:
                metrics_dict["RollingPeriodStart"] = rolling_p_start
                metrics_dict["RollingPeriodEnd"] = rolling_p_end
            # Note: for rolling detections, apply_linked_row_totals_to_metrics uses df+raw_idx, but raw_idx is
            # TransactionRow.id, not _aml_row_index; so we skip the linked-total adjustment for rolling.
            metrics = json_safe(metrics_dict)
            # Avoid duplicates across runs by checking if this logical rolling detection already exists.
            window_end = str(metrics_dict.get("TxnWeek") or "").strip()
            if scen in {"W1", "W3"}:
                key_field = "WalletId"
            elif scen == "W2":
                key_field = "CardId"
            else:
                key_field = "WalletId"
            key_value = str(metrics_dict.get(key_field) or "").strip()
            if _rolling_detection_exists(
                db,
                period=p_name,
                scenario_id=scen,
                key_field=key_field,
                key_value=key_value,
                window_end=window_end,
            ):
                continue
            seen_idx_keys.add(idx_key)
            d = Detection(
                import_batch_id=None,
                scope_type="rolling",
                scope_days=int(days),
                scope_as_of=as_of,
                scenario_id=scen,
                period=p_name,
                status="new",
                metrics=metrics,
                raw_row_indices=raw_idx,
            )
            db.add(d)
            created += 1

    db.commit()
    return {"ok": True, "detections_created": created}


def metrics_row(det: Detection) -> dict[str, object]:
    return dict(det.metrics or {})
