from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.constants import OPEN_DETECTION_STATUSES
from app.models import Detection, Scenario
from app.services.import_service import dataframe_for_batch, dataframe_for_rolling_window, _ensure_repo_on_path
from app.services.scenarios_service import (
    get_scenario_by_code,
    group_type_for_scenario_code,
    legacy_group_type_for_code,
    list_enabled_scenarios,
)
from app.services.serialize import json_safe

_ensure_repo_on_path()
from wallet_enrichment import (
    apply_linked_row_totals_to_metrics,
    apply_scenario_slice_for_linked_indices,
    enrich_detection_metrics_dataframe,
    enrich_top_card_metrics,
    slice_raw_for_detection_row,
)


def _group_type_for(sid: str, db: Session | None, scenario: Scenario | None = None) -> str:
    if scenario is not None:
        return scenario.group_type
    if db is not None:
        gt = group_type_for_scenario_code(db, sid)
        if gt:
            return gt
    legacy = legacy_group_type_for_code(sid)
    if legacy:
        return legacy
    return ""


def _det_indices_for_row(
    det_row: pd.Series,
    raw: pd.DataFrame,
    key_cols: list[str],
    *,
    scenario_id: str,
    group_type: str = "",
    transaction_filter: str = "approved_only",
    use_transaction_row_ids: bool = False,
) -> list[int]:
    m = slice_raw_for_detection_row(det_row, raw, key_cols)
    if m.empty:
        return []
    m = apply_scenario_slice_for_linked_indices(
        m, scenario_id, group_type=group_type, transaction_filter=transaction_filter
    )
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
    sid = str(scenario_id or "").strip().upper()
    per = str(period or "").strip().lower()
    kf = str(key_field or "").strip()
    kv = str(key_value or "").strip()
    we = str(window_end or "").strip()
    if not (sid and per and kf and kv and we):
        return False
    if kf not in {"WalletId", "CardId"}:
        return False
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
    return db.execute(stmt, {"per": per, "sid": sid, "we": we, "kf": kf, "kv": kv}).first() is not None


def _rolling_key_field_for_group(group_type: str) -> str:
    gt = (group_type or "").strip()
    if gt in {"one_card_many_wallets"}:
        return "CardId"
    if gt == "one_card_one_wallet":
        return "CardId"
    return "WalletId"


def _rolling_key_field_for_scenario(scenario_id: str) -> str:
    """Legacy helper for tests and rolling dedupe keyed by scenario code."""
    sid = (scenario_id or "").strip().upper()
    if sid == "W2":
        return "CardId"
    legacy = legacy_group_type_for_code(sid)
    if legacy:
        return _rolling_key_field_for_group(legacy)
    return "WalletId"


def _collapse_rolling_det_rows(det: pd.DataFrame, group_type_or_sid: str) -> pd.DataFrame:
    gt = (group_type_or_sid or "").strip()
    if gt.upper() in {"D1", "D2", "D3", "W1", "W2", "W3"}:
        gt = legacy_group_type_for_code(gt.upper()) or gt
    if det is None or det.empty:
        return det
    group_col = "CardId" if gt in {"one_card_many_wallets", "one_card_one_wallet"} else "WalletId"
    if group_col not in det.columns or "TxnWeek" not in det.columns:
        return det
    work = det.copy()
    work["_txn_week_sort"] = pd.to_datetime(work["TxnWeek"], errors="coerce")
    work = work.sort_values("_txn_week_sort", kind="mergesort")
    collapsed = work.groupby(group_col, as_index=False, sort=False).last()
    return collapsed.drop(columns=["_txn_week_sort"], errors="ignore")


def _find_open_rolling_detection(
    db: Session,
    *,
    period: str,
    scenario_id: str,
    key_field: str,
    key_value: str,
) -> Detection | None:
    sid = str(scenario_id or "").strip().upper()
    per = str(period or "").strip().lower()
    kf = str(key_field or "").strip()
    kv = str(key_value or "").strip()
    if not (sid and per and kf and kv):
        return None
    if kf not in {"WalletId", "CardId"}:
        return None
    open_statuses = list(OPEN_DETECTION_STATUSES)
    if not open_statuses:
        return None
    stmt = text(
        """
        SELECT id
        FROM detections
        WHERE scope_type = 'rolling'
          AND period = :per
          AND scenario_id = :sid
          AND status = ANY(:statuses)
          AND trim(coalesce(metrics->>:kf, '')) = :kv
        ORDER BY id DESC
        LIMIT 1
        """
    )
    row = db.execute(
        stmt,
        {"per": per, "sid": sid, "statuses": open_statuses, "kf": kf, "kv": kv},
    ).first()
    if not row:
        return None
    return db.get(Detection, int(row[0]))


def _refresh_open_rolling_detection(
    det: Detection,
    *,
    metrics_dict: dict[str, Any],
    raw_idx: list[int],
    scope_days: int,
    as_of: datetime,
) -> None:
    existing = [int(x) for x in (det.raw_row_indices or [])]
    merged = sorted(set(existing) | {int(x) for x in raw_idx})
    det.raw_row_indices = merged
    det.metrics = json_safe(metrics_dict)
    det.scope_days = int(scope_days)
    det.scope_as_of = as_of


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


def _d1_d2_detection_risk_metrics(
    det_row: pd.Series,
    raw: pd.DataFrame,
    group_type: str,
    thresholds: dict,
) -> dict[str, object]:
    gt = (group_type or "").strip()
    if gt not in {"many_cards_one_wallet", "one_card_many_wallets"} or raw is None or raw.empty:
        return {}

    try:
        from io_utils import fetch_post_card_debit_transactions
    except Exception as e:
        logging.getLogger(__name__).warning("Risk enrichment unavailable: %s", e)
        return {"RiskError": str(e)}

    subset = raw.copy()
    if gt == "many_cards_one_wallet" and "WalletId" in det_row.index and "WalletId" in subset.columns:
        subset = subset[subset["WalletId"] == det_row["WalletId"]]
    if gt == "one_card_many_wallets" and "CardId" in det_row.index and "CardId" in subset.columns:
        subset = subset[subset["CardId"] == det_row["CardId"]]
    if "TxnDate" in det_row.index and "TxnDate" in subset.columns:
        subset = subset[subset["TxnDate"] == det_row["TxnDate"]]
    elif "TxnWeek" in det_row.index and "TxnWeek" in subset.columns:
        tw = pd.to_datetime(det_row["TxnWeek"], errors="coerce")
        if pd.notna(tw):
            subset = subset[pd.to_datetime(subset["TxnWeek"], errors="coerce").dt.normalize() == tw.normalize()]
    if subset.empty:
        return {}

    approved = subset["Approved"].fillna(False).astype(bool) if "Approved" in subset.columns else pd.Series(False, index=subset.index)
    approved_subset = subset[approved] if len(subset) else subset.iloc[0:0].copy()

    wallet_info: list[tuple[str, str, float]] = []
    if gt == "many_cards_one_wallet":
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
        logging.getLogger(__name__).warning("Risk lookup failed for %s: %s", gt, e)
        return {"RiskError": str(e)}

    total_amount = float(det_row.get("TotalAmount", 0) or 0)
    t = thresholds or {}
    if gt == "many_cards_one_wallet":
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
            total_amount >= float(t.get("risk_min_total_amount", 0) or 0)
            and observed_pct >= float(t.get("risk_min_expenditure_pct", 0) or 0)
        )
        return {
            "Risk": "High" if is_high else "Low",
            "RiskObservedExpenditurePct": round(observed_pct, 2),
            "RiskObservedExpenditureAmount": round(debit_amt, 2),
        }

    wallet_threshold = float(t.get("risk_min_wallet_expenditure_pct", 0) or 0)
    wallets_pct_threshold = float(t.get("risk_min_wallets_pct", 0) or 0)
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
        total_amount >= float(t.get("risk_min_total_amount", 0) or 0)
        and observed_wallets_pct >= wallets_pct_threshold
    )
    return {
        "Risk": "High" if is_high else "Low",
        "RiskObservedWalletsPct": round(observed_wallets_pct, 2),
        "RiskObservedMatchedWalletCount": int(matched_wallets),
        "RiskObservedWalletCount": int(wallet_count),
        "RiskObservedMaxWalletExpenditurePct": round(max(observed_wallet_pcts) if observed_wallet_pcts else 0.0, 2),
    }


def _period_name_for_scenario(scenario: Scenario) -> str:
    u = (scenario.period_unit or "day").strip().lower()
    if u == "hour":
        return "hourly"
    if u == "day":
        return "daily"
    if u == "week":
        return "weekly"
    if u == "month":
        return "monthly"
    return u


def _rolling_scope_days(scenario: Scenario) -> int:
    from scenarios import period_lookback

    lb = period_lookback(scenario.period_unit, scenario.period_value)
    return max(1, int(lb / pd.Timedelta(days=1)))


def _run_scenario_on_df(
    df: pd.DataFrame,
    scenario: Scenario,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    from scenarios import key_cols_for_scenario, run_dynamic_scenario

    det, raw = run_dynamic_scenario(
        df,
        code=scenario.code,
        group_type=scenario.group_type,
        period_unit=scenario.period_unit,
        period_value=scenario.period_value,
        thresholds=dict(scenario.thresholds or {}),
        monitored_bank=scenario.monitored_bank,
        transaction_filter=scenario.transaction_filter,
    )
    if det is None or det.empty:
        return det, raw, []

    gt = scenario.group_type
    key_cols = key_cols_for_scenario(scenario.group_type, scenario.period_unit, scenario.period_value)

    if gt == "one_card_many_wallets" and raw is not None and not raw.empty:
        end_col = key_cols[1]
        det = _enrich_wallet_pipe(det, raw, ["CardId", end_col], "WalletIdsPipe")

    det = enrich_top_card_metrics(det, raw, key_cols)
    return det, raw, key_cols


def _collect_wallets(det: pd.DataFrame, raw: pd.DataFrame | None) -> set[str]:
    wallets: set[str] = set()

    def _add(x: object) -> None:
        s = str(x or "").strip()
        if s and s.lower() != "nan":
            wallets.add(s)

    if "WalletId" in det.columns:
        for x in det["WalletId"].tolist():
            _add(x)
    if "WalletIdsPipe" in det.columns:
        for pip in det["WalletIdsPipe"].tolist():
            for seg in str(pip or "").split("|"):
                _add(seg)
    if raw is not None and not raw.empty and "WalletId" in raw.columns:
        for x in raw["WalletId"].tolist():
            _add(x)
    return wallets


def _city_mapping() -> dict[str, str]:
    try:
        from io_utils import load_city_name_mapping_from_env

        return dict(load_city_name_mapping_from_env())
    except Exception:
        return {}


def run_scenarios_for_batch(db: Session, *, batch_id: int, period: str | None = None) -> dict[str, Any]:
    """Run all enabled scenarios against one import batch."""
    _ensure_repo_on_path()

    df = dataframe_for_batch(db, batch_id)
    if df.empty:
        return {"ok": False, "error": "No transactions for this import batch."}

    scenarios = list_enabled_scenarios(db)
    if not scenarios:
        return {"ok": False, "error": "No enabled scenarios configured."}

    if period and period not in {"daily", "weekly", "both", "all"}:
        return {"ok": False, "error": "Invalid period. Use daily, weekly, both, or all."}

    period_filter = (period or "all").strip().lower()
    if period_filter == "both":
        period_filter = "all"

    city_mapping = _city_mapping()
    db.query(Detection).filter(Detection.import_batch_id == batch_id).delete(synchronize_session=False)

    results: list[tuple[Scenario, pd.DataFrame, pd.DataFrame, list[str]]] = []
    wallets_to_enrich: set[str] = set()

    for scenario in scenarios:
        pname = _period_name_for_scenario(scenario)
        if period_filter != "all" and pname not in {period_filter, period_filter.rstrip("ly") + "ly"}:
            if period_filter == "daily" and pname != "daily":
                continue
            if period_filter == "weekly" and pname != "weekly":
                continue

        det, raw, key_cols = _run_scenario_on_df(df, scenario)
        if det is None or det.empty:
            continue
        wallets_to_enrich |= _collect_wallets(det, raw)
        results.append((scenario, det, raw, key_cols))

    wallet_profiles_df = None
    if wallets_to_enrich:
        try:
            from io_utils import fetch_wallet_profiles

            wallet_profiles_df = fetch_wallet_profiles(sorted(wallets_to_enrich))
        except Exception as e:
            logging.getLogger(__name__).warning("Wallet profile fetch failed: %s", e)

    created = 0
    seen_idx_keys: set[tuple[int, ...]] = set()
    for scenario, det, raw, key_cols in results:
        det = enrich_detection_metrics_dataframe(det, wallet_profiles_df, city_mapping)
        gt = scenario.group_type
        for _, det_row in det.iterrows():
            scen = str(det_row["ScenarioId"]) if "ScenarioId" in det_row.index else scenario.code
            raw_idx = _det_indices_for_row(
                det_row,
                raw,
                key_cols,
                scenario_id=scen,
                group_type=gt,
                transaction_filter=scenario.transaction_filter,
            )
            idx_key = tuple(sorted(int(x) for x in raw_idx))
            if not idx_key or idx_key in seen_idx_keys:
                continue
            metrics_dict = det_row.to_dict()
            metrics_dict["GroupType"] = gt
            metrics_dict["PeriodUnit"] = scenario.period_unit
            metrics_dict["PeriodValue"] = scenario.period_value
            metrics_dict = apply_linked_row_totals_to_metrics(df, raw_idx, metrics_dict)
            if gt in {"many_cards_one_wallet", "one_card_many_wallets"}:
                metrics_dict.update(
                    _d1_d2_detection_risk_metrics(det_row, raw, gt, dict(scenario.thresholds or {}))
                )
            metrics = json_safe(metrics_dict)
            seen_idx_keys.add(idx_key)
            db.add(
                Detection(
                    import_batch_id=batch_id,
                    scope_type="batch",
                    scenario_id=scen,
                    period=_period_name_for_scenario(scenario),
                    status="new",
                    metrics=metrics,
                    raw_row_indices=raw_idx,
                )
            )
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
    sid = scenario_id.strip().upper()
    scenario = get_scenario_by_code(db, sid)
    if scenario is None:
        return {"ok": False, "error": "Unknown scenario id."}
    if not scenario.enabled:
        return {"ok": False, "error": f"Scenario {sid} is disabled in Scenario Manager."}

    df = dataframe_for_batch(db, batch_id)
    if df.empty:
        return {"ok": False, "error": "No transactions for this import batch."}

    city_mapping = _city_mapping()
    det, raw, key_cols = _run_scenario_on_df(df, scenario)
    if det is None or det.empty:
        return {"ok": True, "detections_created": 0}

    wallets_to_enrich = _collect_wallets(det, raw)
    wallet_profiles_df = None
    if wallets_to_enrich:
        try:
            from io_utils import fetch_wallet_profiles

            wallet_profiles_df = fetch_wallet_profiles(sorted(wallets_to_enrich))
        except Exception as e:
            logging.getLogger(__name__).warning("Wallet profile fetch failed: %s", e)

    det = enrich_detection_metrics_dataframe(det, wallet_profiles_df, city_mapping)
    gt = scenario.group_type
    created = 0
    for _, det_row in det.iterrows():
        raw_idx = _det_indices_for_row(
            det_row,
            raw,
            key_cols,
            scenario_id=sid,
            group_type=gt,
            transaction_filter=scenario.transaction_filter,
        )
        if not raw_idx:
            continue
        metrics_dict = det_row.to_dict()
        metrics_dict["GroupType"] = gt
        metrics_dict["PeriodUnit"] = scenario.period_unit
        metrics_dict["PeriodValue"] = scenario.period_value
        metrics_dict = apply_linked_row_totals_to_metrics(df, raw_idx, metrics_dict)
        if gt in {"many_cards_one_wallet", "one_card_many_wallets"}:
            metrics_dict.update(_d1_d2_detection_risk_metrics(det_row, raw, gt, dict(scenario.thresholds or {})))
        metrics = json_safe(metrics_dict)
        if _detection_exists_for_exact_indices(db, batch_id=batch_id, raw_idx=raw_idx):
            continue
        db.add(
            Detection(
                import_batch_id=batch_id,
                scope_type="batch",
                scenario_id=sid,
                period=_period_name_for_scenario(scenario),
                status=status,
                metrics=metrics,
                raw_row_indices=raw_idx,
            )
        )
        created += 1
    db.commit()
    return {"ok": True, "detections_created": created}


def run_scenarios_for_rolling(
    db: Session,
    *,
    days: int | None = None,
    period: str = "weekly",
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    from datetime import date, datetime, time, timezone

    from scenarios import period_lookback

    scenarios = list_enabled_scenarios(db)
    if not scenarios:
        return {"ok": False, "error": "No enabled scenarios configured."}

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

    max_scope_days = int(days) if days and int(days) > 0 else 0
    if max_scope_days <= 0:
        for sc in scenarios:
            max_scope_days = max(max_scope_days, _rolling_scope_days(sc))
    if max_scope_days <= 0:
        max_scope_days = 7

    if period_start_iso and period_end_iso:
        df = dataframe_for_rolling_window(
            db,
            days=max_scope_days,
            period_start_iso=period_start_iso,
            period_end_iso=period_end_iso,
        )
    else:
        df = dataframe_for_rolling_window(db, days=max_scope_days)

    if df.empty:
        return {"ok": False, "error": "No transactions in rolling window."}

    city_mapping = _city_mapping()
    results: list[tuple[Scenario, pd.DataFrame, pd.DataFrame, list[str]]] = []
    wallets_to_enrich: set[str] = set()

    for scenario in scenarios:
        work = df.copy()
        lb = period_lookback(scenario.period_unit, scenario.period_value)
        work.attrs["rolling_lookback"] = lb
        work.attrs["rolling_window_days"] = max(1, int(lb / pd.Timedelta(days=1)))
        det, raw, key_cols = _run_scenario_on_df(work, scenario)
        if det is None or det.empty:
            continue
        wallets_to_enrich |= _collect_wallets(det, raw)
        results.append((scenario, det, raw, key_cols))

    wallet_profiles_df = None
    if wallets_to_enrich:
        try:
            from io_utils import fetch_wallet_profiles

            wallet_profiles_df = fetch_wallet_profiles(sorted(wallets_to_enrich))
        except Exception as e:
            logging.getLogger(__name__).warning("Wallet profile fetch failed: %s", e)

    created = 0
    refreshed = 0
    seen_idx_keys: set[tuple[int, ...]] = set()
    as_of = datetime.now(timezone.utc)

    for scenario, det, raw, key_cols in results:
        det = enrich_detection_metrics_dataframe(det, wallet_profiles_df, city_mapping)
        det = _collapse_rolling_det_rows(det, scenario.group_type)
        gt = scenario.group_type
        pname = _period_name_for_scenario(scenario)
        scope_days = _rolling_scope_days(scenario)

        for _, det_row in det.iterrows():
            scen = str(det_row["ScenarioId"]) if "ScenarioId" in det_row.index else scenario.code
            raw_idx = _det_indices_for_row(
                det_row,
                raw,
                key_cols,
                scenario_id=scen,
                group_type=gt,
                transaction_filter=scenario.transaction_filter,
                use_transaction_row_ids=True,
            )
            idx_key = tuple(sorted(int(x) for x in raw_idx))
            if not idx_key or idx_key in seen_idx_keys:
                continue
            metrics_dict = det_row.to_dict()
            metrics_dict["GroupType"] = gt
            metrics_dict["PeriodUnit"] = scenario.period_unit
            metrics_dict["PeriodValue"] = scenario.period_value
            metrics_dict["RollingWindowDays"] = scope_days
            if rolling_p_start and rolling_p_end:
                metrics_dict["RollingPeriodStart"] = rolling_p_start
                metrics_dict["RollingPeriodEnd"] = rolling_p_end
            metrics = json_safe(metrics_dict)
            window_end = str(metrics_dict.get("TxnWeek") or "").strip()
            key_field = _rolling_key_field_for_group(gt)
            key_value = str(metrics_dict.get(key_field) or "").strip()
            existing = _find_open_rolling_detection(
                db, period=pname, scenario_id=scen, key_field=key_field, key_value=key_value
            )
            if existing:
                _refresh_open_rolling_detection(
                    existing,
                    metrics_dict=metrics_dict,
                    raw_idx=raw_idx,
                    scope_days=scope_days,
                    as_of=as_of,
                )
                refreshed += 1
                seen_idx_keys.add(idx_key)
                continue
            if _rolling_detection_exists(
                db,
                period=pname,
                scenario_id=scen,
                key_field=key_field,
                key_value=key_value,
                window_end=window_end,
            ):
                continue
            seen_idx_keys.add(idx_key)
            db.add(
                Detection(
                    import_batch_id=None,
                    scope_type="rolling",
                    scope_days=scope_days,
                    scope_as_of=as_of,
                    scenario_id=scen,
                    period=pname,
                    status="new",
                    metrics=metrics,
                    raw_row_indices=raw_idx,
                )
            )
            created += 1

    db.commit()
    return {"ok": True, "detections_created": created, "detections_refreshed": refreshed}


def metrics_row(det: Detection) -> dict[str, object]:
    return dict(det.metrics or {})
