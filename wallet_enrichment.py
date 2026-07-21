from __future__ import annotations

import math
from app.constants import GOVERNORATE_CODE_MAP

from typing import Any, Dict, Mapping

import pandas as pd


def account_holder_names_pipe(rows: pd.DataFrame) -> str:
    """Distinct AccountHolder values on rows, sorted and pipe-separated (same style as CardHolderNamesPipe)."""
    if rows.empty or "AccountHolder" not in rows.columns:
        return ""
    names: set[str] = set()
    for h in rows["AccountHolder"]:
        if pd.isna(h):
            continue
        s = str(h).strip()
        if not s or s.lower() == "nan":
            continue
        names.add(s)
    return "|".join(sorted(names))


def normalize_wallet_card_id(x: object) -> str:
    """Stable string id for WalletId / CardId matching (Excel float / nan safe)."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return ""
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        try:
            xf = float(x)
            if math.isfinite(xf) and xf == int(xf):
                return str(int(xf))
        except (TypeError, ValueError):
            pass
    s = str(x).strip()
    if s.lower() == "nan":
        return ""
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def slice_raw_for_detection_row(det_row: pd.Series, raw: pd.DataFrame, key_cols: list[str]) -> pd.DataFrame:
    """
    Rows from scenario `raw` that belong to one detection row (same keys as index resolution).

    Uses the same normalization as Excel row index mapping (dates, wallet/card ids).
    """
    if raw is None or raw.empty or "_aml_row_index" not in raw.columns:
        return pd.DataFrame()
    if not key_cols:
        return pd.DataFrame()

    for c in key_cols:
        if c not in det_row.index or c not in raw.columns:
            return pd.DataFrame()

    m = raw
    for c in key_cols:
        dv = det_row[c]
        if c in ("TxnDate", "TxnWeek"):
            dt_series = pd.to_datetime(m[c], errors="coerce").dt.normalize()
            dt_val = pd.to_datetime(dv, errors="coerce")
            if pd.isna(dt_val):
                return pd.DataFrame()
            dt_norm = pd.Timestamp(dt_val).normalize()
            m = m[dt_series == dt_norm]
        elif c in ("WalletId", "CardId"):
            ms = m[c].map(normalize_wallet_card_id)
            dv_s = normalize_wallet_card_id(dv)
            m = m[ms == dv_s]
        else:
            m = m[m[c] == dv]
        if m.empty:
            return pd.DataFrame()

    return m


def enrich_detection_metrics_dataframe(
    det: pd.DataFrame,
    wallet_profiles_df: pd.DataFrame | None,
    city_name_mapping: Mapping[str, str] | dict | None = None,
) -> pd.DataFrame:
    """
    Add wallet holder name and city columns to a scenario detections DataFrame,
    using MariaDB profile rows (msisdn, Fullname, city) from fetch_wallet_profiles
    (actors_clean1_clone.extra13 as Fullname).
    """
    if det is None or det.empty:
        return det

    wallet_ids: list[str] = []
    if "WalletId" in det.columns:
        wallet_ids = det["WalletId"].astype(str).tolist()
    elif "WalletIdsPipe" in det.columns:
        for s in det["WalletIdsPipe"].astype(str).tolist():
            wallet_ids.extend([x for x in str(s).split("|") if x.strip()])
    wallet_ids = sorted({w.strip() for w in wallet_ids if w and w.strip()})

    det = det.copy()

    if not wallet_ids or wallet_profiles_df is None or len(wallet_profiles_df) == 0:
        det["WalletHolderFullName"] = ""
        return det

    prof = wallet_profiles_df.copy()

    def _resolve_city(raw_city: str) -> str:
        code = str(raw_city or "").strip()
        if not code or code.lower() == "nan":
            return ""
        if code in GOVERNORATE_CODE_MAP:
            return GOVERNORATE_CODE_MAP[code]
        if city_name_mapping and code in city_name_mapping:
            return str(city_name_mapping[code])
        return code

    prof["CityName"] = prof["city"].map(_resolve_city)
    prof["GovernorateName"] = prof["city"].map(lambda c: GOVERNORATE_CODE_MAP.get(str(c or "").strip(), ""))

    name_map: Dict[str, str] = dict(zip(prof["msisdn"].astype(str), prof["Fullname"].astype(str)))
    city_map: Dict[str, str] = dict(zip(prof["msisdn"].astype(str), prof["CityName"].astype(str)))
    gov_map: Dict[str, str] = dict(zip(prof["msisdn"].astype(str), prof["GovernorateName"].astype(str)))

    if "WalletId" in det.columns:
        det["WalletHolderFullName"] = det["WalletId"].astype(str).map(name_map).fillna("")
        det["WalletCityName"] = det["WalletId"].astype(str).map(city_map).fillna("")
        det["WalletGovernorate"] = det["WalletId"].astype(str).map(gov_map).fillna("")
        det["WalletGovernorate"] = det["WalletGovernorate"].where(
            det["WalletGovernorate"].astype(str).str.strip() != "",
            det["WalletCityName"],
        )
    elif "WalletIdsPipe" in det.columns:

        def map_pipe(s: str, m: Dict[str, str]) -> str:
            parts = [x.strip() for x in str(s).split("|") if x.strip()]
            return "|".join([m.get(p, "") for p in parts])

        det["WalletHolderNamesPipe"] = det["WalletIdsPipe"].apply(lambda s: map_pipe(str(s), name_map))
        det["WalletCityNamesPipe"] = det["WalletIdsPipe"].apply(lambda s: map_pipe(str(s), city_map))
        det["WalletGovernoratesPipe"] = det["WalletIdsPipe"].apply(lambda s: map_pipe(str(s), gov_map))
        det["WalletHolderFullName"] = det["WalletHolderNamesPipe"].fillna("")
    else:
        det["WalletHolderFullName"] = ""

    return det


def _approved_row_mask(series: pd.Series) -> pd.Series:
    """Same semantics as scenarios._approved_row_mask (keep wallet_enrichment import-free of scenarios)."""

    def _one(x: object) -> bool:
        if pd.isna(x):
            return False
        if isinstance(x, bool):
            return x
        if isinstance(x, (int, float)) and not isinstance(x, bool):
            try:
                return float(x) != 0.0
            except (TypeError, ValueError):
                return False
        s = str(x).strip().lower()
        return s in {"true", "1", "yes", "t", "y"}

    return series.map(_one).fillna(False).astype(bool)


def apply_linked_row_totals_to_metrics(
    df: pd.DataFrame,
    raw_indices: list[int],
    metrics_dict: dict[str, Any],
) -> dict[str, Any]:
    """
    Align TotalAmount with the sum of Amount on persisted linked rows (matches detection detail footer).

    TopCardTotalAmount and TopCardHolderName follow linked rows only (holders can be pipe-separated).
    """
    out = dict(metrics_dict)
    if df is None or df.empty or not raw_indices or "_aml_row_index" not in df.columns:
        return out
    idx_set = {int(x) for x in raw_indices}
    sub = df.loc[df["_aml_row_index"].isin(idx_set)]
    if sub.empty:
        return out
    amt_all = pd.to_numeric(sub["Amount"], errors="coerce").fillna(0.0)
    out["TotalAmount"] = float(amt_all.sum())

    top_raw = out.get("TopCardId")
    top_s = normalize_wallet_card_id(top_raw) if top_raw is not None else ""
    if top_s and "CardId" in sub.columns:
        mask = sub["CardId"].map(normalize_wallet_card_id) == top_s
        card_rows = sub.loc[mask]
        out["TopCardTotalAmount"] = float(
            pd.to_numeric(card_rows["Amount"], errors="coerce").fillna(0.0).sum()
        )
        out["TopCardHolderName"] = account_holder_names_pipe(card_rows)
    return out


def apply_scenario_slice_for_linked_indices(
    m: pd.DataFrame,
    scenario_id: str,
    *,
    group_type: str = "",
    transaction_filter: str = "approved_only",
) -> pd.DataFrame:
    """
    After key-matching `raw` to one detection row, restrict to rows that belong in *linked transactions*
    for that scenario (approved-only cash-in rules vs rejected-only failure rules).
    """
    if m.empty:
        return m
    tf = (transaction_filter or "approved_only").strip().lower()
    gt = (group_type or "").strip()
    sid = (scenario_id or "").strip().upper()
    if not gt:
        if sid in {"D1", "W1"}:
            gt = "many_cards_one_wallet"
        elif sid in {"D2", "W2"}:
            gt = "one_card_many_wallets"
        elif sid in {"D3", "W3"}:
            gt = "multiple_failed"

    if tf == "both":
        return m.copy()
    if tf == "failed_only" or gt == "multiple_failed":
        if "Rejected" in m.columns:
            rj = m["Rejected"].fillna(False).astype(bool)
            return m.loc[rj].copy()
        if "Approved" in m.columns:
            return m.loc[~_approved_row_mask(m["Approved"])].copy()
        return m.iloc[0:0]

    if gt in {"many_cards_one_wallet", "one_card_many_wallets", "one_card_one_wallet"}:
        if "Approved" not in m.columns:
            return m
        ok = _approved_row_mask(m["Approved"])
        return m.loc[ok].copy()
    if gt == "multiple_failed":
        if "Rejected" not in m.columns:
            return m
        rj = m["Rejected"].fillna(False).astype(bool)
        return m.loc[rj].copy()
    # Legacy fallback by scenario code
    if sid in {"D1", "D2", "W1", "W2"}:
        if "Approved" not in m.columns:
            return m
        ok = _approved_row_mask(m["Approved"])
        return m.loc[ok].copy()
    if sid in {"D3", "W3"}:
        if "Rejected" not in m.columns:
            return m
        rj = m["Rejected"].fillna(False).astype(bool)
        return m.loc[rj].copy()
    return m


def _linked_rows_for_top_card_totals(sub: pd.DataFrame) -> pd.DataFrame:
    """
    Rows whose Amount should contribute to Top card totals — aligned with detection TotalAmount.

    Scenarios compute TotalAmount from approved cash-ins (D1/D2/W1/W2) or from rejected rows only
    (D3/W3). We must never sum failed/unapproved attempts into top-card totals when TotalAmount is
    approved-only; that caused TopCardTotalAmount to exceed TotalAmount.
    """
    if sub.empty:
        return sub
    if "Approved" not in sub.columns:
        return sub
    ap = _approved_row_mask(sub["Approved"])
    if ap.any():
        return sub.loc[ap]
    if "Rejected" in sub.columns:
        rj = sub["Rejected"].fillna(False).astype(bool)
        if rj.any():
            return sub.loc[rj]
    return sub.iloc[0:0]


def enrich_top_card_metrics(det: pd.DataFrame, raw: pd.DataFrame | None, key_cols: list[str]) -> pd.DataFrame:
    """
    Per detection row: CardId with highest sum of Amount in the linked raw slice, plus holder name.

    Uses the same monetary row set as TotalAmount in scenarios (approved-only when any approved
    transactions exist in the slice; otherwise rejected-only buckets for failure scenarios).
    """
    if det is None or det.empty:
        return det

    det = det.copy()
    ids: list[str] = []
    totals: list[float | None] = []
    holders: list[str] = []

    for _, row in det.iterrows():
        sub = slice_raw_for_detection_row(row, raw if raw is not None else pd.DataFrame(), key_cols)
        scen = str(row["ScenarioId"]).strip() if "ScenarioId" in row.index and pd.notna(row.get("ScenarioId")) else ""
        sub = apply_scenario_slice_for_linked_indices(sub, scen)
        if sub.empty or "CardId" not in sub.columns:
            ids.append("")
            totals.append(None)
            holders.append("")
            continue

        work = _linked_rows_for_top_card_totals(sub)

        amt = pd.to_numeric(work["Amount"], errors="coerce").fillna(0.0)
        cid = work["CardId"].map(normalize_wallet_card_id)
        work = work.assign(_amt=amt, _cid=cid)
        work = work[work["_cid"] != ""]
        if "_aml_row_index" in work.columns:
            work = work.drop_duplicates(subset=["_aml_row_index"], keep="first")
        if work.empty:
            ids.append("")
            totals.append(None)
            holders.append("")
            continue

        # One sum per normalized CardId — amount for Top card row is that group's total only (not TotalAmount across cards).
        by_card = work.groupby("_cid", dropna=False, sort=False)["_amt"].sum()
        winner_cid = by_card.idxmax()
        top_total = float(by_card.loc[winner_cid])
        top_label = str(winner_cid).strip()

        card_rows = work[work["_cid"] == winner_cid]
        holder = account_holder_names_pipe(card_rows)

        ids.append(top_label)
        totals.append(top_total)
        holders.append(holder)

    det["TopCardId"] = ids
    det["TopCardTotalAmount"] = totals
    det["TopCardHolderName"] = holders
    return det
