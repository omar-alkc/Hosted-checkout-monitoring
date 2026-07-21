from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from typing import Dict, Tuple

import numpy as np
import pandas as pd

# Flattened payload column from Excel/JSON (see app templates detection_detail).
ISSUER_BANK_COL = "OPP_card.issuer.bank"


def _rolling_window_days(df: pd.DataFrame) -> int:
    """Inclusive rolling lookback length (default 7). Set via ``df.attrs['rolling_window_days']`` on rolling runs."""
    try:
        n = int(df.attrs.get("rolling_window_days", 7))
    except (TypeError, ValueError):
        n = 7
    return max(1, n)


def _approved_amount_subset(amt: pd.Series, approved: pd.Series, wpos: list[int]) -> pd.Series:
    """
    Amounts at window positions where Approved is true.

    Do not use ``amt.iloc[wpos][approved.iloc[wpos]]``: chained indexing can
    mis-align indices and trigger large intermediate allocations on big frames.
    """
    if not wpos:
        return pd.Series(dtype=np.float64)
    a = np.asarray(amt.iloc[wpos], dtype=np.float64)
    ok = np.asarray(approved.iloc[wpos], dtype=bool)
    return pd.Series(a[ok])


def _filter_by_monitored_bank(df: pd.DataFrame, bank_substr: str | None) -> pd.DataFrame:
    """
    Case-insensitive substring match on card issuer bank.

    Supports multiple substrings separated by '|', e.g. 'BankA|BankB'.
    No-op if filter blank or column absent.
    """
    if df.empty or not bank_substr or not str(bank_substr).strip():
        return df
    if ISSUER_BANK_COL not in df.columns:
        return df
    needles = [p.strip().lower() for p in str(bank_substr).split("|")]
    needles = [n for n in needles if n]
    if not needles:
        return df
    col = df[ISSUER_BANK_COL].astype(str).str.lower()
    mask = False
    for n in needles:
        mask = mask | col.str.contains(n, na=False, regex=False)
    return df[mask]


@dataclass(frozen=True)
class ScenarioParams:
    # Daily
    d_amount_min: float = 50000
    d_total_amount_min: float = 500000
    d1_min_txn: int = 3
    d1_min_unique_cards: int = 3
    d1_risk_min_total_amount: float = 0
    d1_risk_min_expenditure_pct: float = 0
    d2_min_wallets: int = 3
    d2_risk_min_total_amount: float = 0
    d2_risk_min_wallet_expenditure_pct: float = 0
    d2_risk_min_wallets_pct: float = 0
    d3_min_rejected: int = 5

    # Weekly
    w1_min_txn: int = 10
    w1_min_unique_cards: int = 3
    w1_min_total_amount: float = 500000
    w2_min_wallets: int = 5
    w2_min_txn: int = 1
    w2_min_total_amount: float = 500000
    w3_min_rejected: int = 10

    # Optional per-scenario issuer substring (matches ISSUER_BANK_COL); None = no filter.
    monitor_bank_d1: str | None = None
    monitor_bank_d2: str | None = None
    monitor_bank_d3: str | None = None
    monitor_bank_w1: str | None = None
    monitor_bank_w2: str | None = None
    monitor_bank_w3: str | None = None


def _filter_amount_ge(df: pd.DataFrame, amount_min: float) -> pd.DataFrame:
    return df[df["Amount"].ge(amount_min)]


def _approved_row_mask(series: pd.Series) -> pd.Series:
    """Strict approved flag — matches cash-in scenario truthiness (avoids object/string surprises)."""

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


def _linked_raw_approved_only(df: pd.DataFrame) -> pd.DataFrame:
    """
    Scenarios driven by approved cash-ins (e.g. W1, W2): linked transaction rows stored on a
    detection must not include failed / not-approved attempts so detection detail matches scenario logic.
    """
    if df.empty or "Approved" not in df.columns:
        return df
    return df.loc[_approved_row_mask(df["Approved"])].copy()


def _detection_base(base_all: pd.DataFrame, txn_filter: str) -> pd.DataFrame:
    tf = (txn_filter or "approved_only").strip().lower()
    if tf == "failed_only":
        if "Rejected" in base_all.columns:
            return base_all.loc[base_all["Rejected"].fillna(False).astype(bool)].copy()
        return base_all.loc[~_approved_row_mask(base_all["Approved"])].copy() if "Approved" in base_all.columns else base_all.iloc[0:0]
    if "Approved" in base_all.columns:
        return base_all.loc[_approved_row_mask(base_all["Approved"])].copy()
    return base_all.copy()


def _link_raw_rows(
    base: pd.DataFrame,
    base_all: pd.DataFrame,
    det: pd.DataFrame,
    keys: list[str],
    txn_filter: str,
) -> pd.DataFrame:
    tf = (txn_filter or "approved_only").strip().lower()
    src = base_all if tf == "both" else base
    return src.merge(det[keys].drop_duplicates(), on=keys, how="inner")


def daily_d1(df: pd.DataFrame, p: ScenarioParams, txn_filter: str = "approved_only") -> Tuple[pd.DataFrame, pd.DataFrame]:
    # wallet/day: >= txn count AND >= unique cards, for per-txn amount >= threshold
    base_all = df.copy()
    base_all = _filter_by_monitored_bank(base_all, p.monitor_bank_d1)
    base_all = _filter_amount_ge(base_all, p.d_amount_min)
    base = _detection_base(base_all, txn_filter)
    agg = {
        "TxnCount": ("Amount", "size"),
        "UniqueCards": ("CardId", pd.Series.nunique),
        "TotalAmount": ("Amount", "sum"),
        "AvgAmount": ("Amount", "mean"),
        "MinAmount": ("Amount", "min"),
        "MaxAmount": ("Amount", "max"),
        "CardHolderNamesPipe": (
            "AccountHolder",
            lambda s: "|".join(
                sorted({str(x).strip() for x in s if str(x).strip() and str(x).lower() != "nan"})
            ),
        ),
    }
    if ISSUER_BANK_COL in base.columns:
        agg["UniqueBanks"] = (ISSUER_BANK_COL, pd.Series.nunique)
    grp = (
        base.groupby(["WalletId", "TxnDate"], dropna=False)
        .agg(**agg)
        .reset_index()
    )
    # Count not-approved attempts in the same wallet/day bucket (after bank+amount filters).
    na = (
        base_all.groupby(["WalletId", "TxnDate"], dropna=False)["Approved"]
        .apply(lambda s: int((~s.fillna(False).astype(bool)).sum()))
        .reset_index(name="NotApprovedCount")
    )
    grp = grp.merge(na, on=["WalletId", "TxnDate"], how="left")
    grp["NotApprovedCount"] = grp["NotApprovedCount"].fillna(0).astype(int)
    det = grp[
        (grp["TxnCount"] >= p.d1_min_txn)
        & (grp["UniqueCards"] >= p.d1_min_unique_cards)
        & (grp["TotalAmount"] >= p.d_total_amount_min)
    ].copy()
    if det.empty:
        return det.assign(ScenarioId="D1"), base_all.iloc[0:0].copy()

    # Link only approved rows that fed the aggregation (`base`). `base_all` also has failed /
    # non-approved attempts (used for NotApprovedCount in metrics); those must not appear as
    # "linked transactions" on the detection.
    raw = _link_raw_rows(base, base_all, det, ["WalletId", "TxnDate"], txn_filter)
    det.insert(0, "ScenarioId", "D1")
    return det, raw


def daily_d2(df: pd.DataFrame, p: ScenarioParams, txn_filter: str = "approved_only") -> Tuple[pd.DataFrame, pd.DataFrame]:
    # card/day: >= distinct wallets, for per-txn amount >= threshold
    base_all = df.copy()
    base_all = _filter_by_monitored_bank(base_all, p.monitor_bank_d2)
    base_all = _filter_amount_ge(base_all, p.d_amount_min)
    base = _detection_base(base_all, txn_filter)
    agg = {
        "UniqueWallets": ("WalletId", pd.Series.nunique),
        "TxnCount": ("Amount", "size"),
        "TotalAmount": ("Amount", "sum"),
        "AvgAmount": ("Amount", "mean"),
        "MinAmount": ("Amount", "min"),
        "MaxAmount": ("Amount", "max"),
        "CardHolderNamesPipe": (
            "AccountHolder",
            lambda s: "|".join(
                sorted({str(x).strip() for x in s if str(x).strip() and str(x).lower() != "nan"})
            ),
        ),
    }
    if ISSUER_BANK_COL in base.columns:
        agg["UniqueBanks"] = (ISSUER_BANK_COL, pd.Series.nunique)
    grp = (
        base.groupby(["CardId", "TxnDate"], dropna=False)
        .agg(**agg)
        .reset_index()
    )
    na = (
        base_all.groupby(["CardId", "TxnDate"], dropna=False)["Approved"]
        .apply(lambda s: int((~s.fillna(False).astype(bool)).sum()))
        .reset_index(name="NotApprovedCount")
    )
    grp = grp.merge(na, on=["CardId", "TxnDate"], how="left")
    grp["NotApprovedCount"] = grp["NotApprovedCount"].fillna(0).astype(int)
    det = grp[(grp["UniqueWallets"] >= p.d2_min_wallets) & (grp["TotalAmount"] >= p.d_total_amount_min)].copy()
    if det.empty:
        return det.assign(ScenarioId="D2"), base_all.iloc[0:0].copy()

    # Same as D1: linked rows must match approved-only aggregation input (`base`).
    raw = _link_raw_rows(base, base_all, det, ["CardId", "TxnDate"], txn_filter)
    det.insert(0, "ScenarioId", "D2")
    return det, raw


def daily_d3(df: pd.DataFrame, p: ScenarioParams) -> Tuple[pd.DataFrame, pd.DataFrame]:
    # wallet/day: >= rejected attempts (ReasonCode != 0)
    base = df[df["Rejected"]].copy()
    base = _filter_by_monitored_bank(base, p.monitor_bank_d3)
    agg = {
        "RejectedCount": ("Rejected", "size"),
        "NotApprovedCount": ("Approved", lambda s: int((~s.fillna(False).astype(bool)).sum())),
        "AvgAmount": ("Amount", "mean"),
        "MinAmount": ("Amount", "min"),
        "MaxAmount": ("Amount", "max"),
        "CardHolderNamesPipe": (
            "AccountHolder",
            lambda s: "|".join(
                sorted({str(x).strip() for x in s if str(x).strip() and str(x).lower() != "nan"})
            ),
        ),
    }
    if ISSUER_BANK_COL in base.columns:
        agg["UniqueBanks"] = (ISSUER_BANK_COL, pd.Series.nunique)
    grp = (
        base.groupby(["WalletId", "TxnDate"], dropna=False)
        .agg(**agg)
        .reset_index()
    )
    det = grp[grp["RejectedCount"] >= p.d3_min_rejected].copy()
    if det.empty:
        return det.assign(ScenarioId="D3"), base.iloc[0:0].copy()

    raw = base.merge(det[["WalletId", "TxnDate"]].drop_duplicates(), on=["WalletId", "TxnDate"], how="inner")
    det.insert(0, "ScenarioId", "D3")
    return det, raw


def weekly_w1(df: pd.DataFrame, p: ScenarioParams) -> Tuple[pd.DataFrame, pd.DataFrame]:
    # Rolling N-day window (per wallet, default 7): thresholds may be met before N calendar days of history
    # (e.g. after 4 days) — each transaction is evaluated on the window ending that day.
    window_days = _rolling_window_days(df)
    lookback_td = _rolling_lookback_from_df(df)
    lookback = window_days - 1
    base_all = df.copy()
    base_all = _filter_by_monitored_bank(base_all, p.monitor_bank_w1)

    # Rolling window end is each transaction timestamp; we bucket detections by day for stability.
    # `TxnWeek` is repurposed as the window end date (YYYY-MM-DD) for weekly/rolling scenarios.
    if base_all.empty:
        return base_all.iloc[0:0].assign(ScenarioId="W1"), base_all.iloc[0:0].copy()

    # Ensure required columns exist (best-effort; missing will raise KeyError as before).
    base_all = base_all.sort_values(["WalletId", "TxnTimestamp"], kind="mergesort")
    have_bank = ISSUER_BANK_COL in base_all.columns

    det_rows: list[dict] = []
    raw_rows: list[pd.DataFrame] = []

    # Process each wallet independently with a sliding 7-day window.
    for wallet_id, g in base_all.groupby("WalletId", dropna=False, sort=False):
        gg = g.reset_index(drop=False)  # keep original row index for raw extraction
        ts = pd.to_datetime(gg["TxnTimestamp"], errors="coerce")
        amt = pd.to_numeric(gg["Amount"], errors="coerce").fillna(0.0)
        approved = gg["Approved"].fillna(False).astype(bool)
        card = gg["CardId"]
        holder = gg.get("AccountHolder")
        bank = gg.get(ISSUER_BANK_COL) if have_bank else None

        win = deque()  # entries: (pos, ts)
        approved_sum_amt = 0.0
        approved_cards = Counter()
        holders = Counter()
        banks = Counter()

        for pos in range(len(gg)):
            t = ts.iat[pos]
            if pd.isna(t):
                continue

            a = float(amt.iat[pos])
            is_ok = bool(approved.iat[pos])
            c = str(card.iat[pos]).strip()
            if is_ok:
                approved_sum_amt += a
                if c and c.lower() != "nan":
                    approved_cards[c] += 1
                # Holders / banks for metrics: approved cash-ins only (same as TotalAmount / linked rows).
                if holder is not None:
                    h = str(holder.iat[pos]).strip()
                    if h and h.lower() != "nan":
                        holders[h] += 1
                if bank is not None:
                    b = str(bank.iat[pos]).strip()
                    if b and b.lower() != "nan":
                        banks[b] += 1

            win.append((pos, t))

            # Evict outside rolling window ending at t (inclusive).
            if lookback_td >= pd.Timedelta(days=1):
                cutoff = t - lookback_td + pd.Timedelta(days=1)
            else:
                cutoff = t - lookback_td + pd.Timedelta(seconds=1)
            while win and win[0][1] < cutoff:
                old_pos, old_t = win.popleft()
                old_ok = bool(approved.iat[old_pos])
                old_a = float(amt.iat[old_pos])
                old_c = str(card.iat[old_pos]).strip()
                if old_ok:
                    approved_sum_amt -= float(old_a)
                    if old_c and old_c.lower() != "nan":
                        approved_cards[old_c] -= 1
                        if approved_cards[old_c] <= 0:
                            del approved_cards[old_c]
                    if holder is not None:
                        oh = str(holder.iat[old_pos]).strip()
                        if oh and oh.lower() != "nan":
                            holders[oh] -= 1
                            if holders[oh] <= 0:
                                del holders[oh]
                    if bank is not None:
                        ob = str(bank.iat[old_pos]).strip()
                        if ob and ob.lower() != "nan":
                            banks[ob] -= 1
                            if banks[ob] <= 0:
                                del banks[ob]

            txn_count = int(approved.iloc[[x[0] for x in win]].sum()) if win else 0
            not_approved_count = len(win) - txn_count if win else 0
            unique_cards = len(approved_cards)
            total_amount = float(approved_sum_amt)

            if (
                txn_count >= p.w1_min_txn
                and unique_cards >= p.w1_min_unique_cards
                and total_amount >= p.w1_min_total_amount
            ):
                # Aggregate window stats
                wpos = [x[0] for x in win]
                wamt_ok = _approved_amount_subset(amt, approved, wpos)
                end_date = t.date()
                det_rows.append(
                    {
                        "ScenarioId": "W1",
                        "WalletId": wallet_id,
                        "TxnWeek": end_date,  # rolling window end date
                        "TxnCount": int(txn_count),
                        "NotApprovedCount": int(not_approved_count),
                        "UniqueCards": int(unique_cards),
                        "TotalAmount": float(total_amount),
                        "AvgAmount": float(wamt_ok.mean()) if len(wamt_ok) else 0.0,
                        "MinAmount": float(wamt_ok.min()) if len(wamt_ok) else 0.0,
                        "MaxAmount": float(wamt_ok.max()) if len(wamt_ok) else 0.0,
                        "CardHolderNamesPipe": "|".join(sorted(holders.keys())) if holders else "",
                        "UniqueBanks": int(len(banks)) if have_bank else None,
                    }
                )

                raw_win = gg.iloc[wpos].copy()
                raw_win = _linked_raw_approved_only(raw_win)
                raw_win["ScenarioId"] = "W1"
                raw_win["TxnWeek"] = end_date
                raw_rows.append(raw_win)

    det = pd.DataFrame(det_rows)
    raw = pd.concat(raw_rows, ignore_index=True) if raw_rows else base_all.iloc[0:0].copy()

    # Match legacy shape: if no detections, return empty det + empty raw.
    if det.empty:
        return det.assign(ScenarioId="W1"), base_all.iloc[0:0].copy()

    # Drop duplicates that can happen when multiple txns share same day and produce identical windows.
    det = det.drop_duplicates(subset=["WalletId", "TxnWeek"], keep="first").reset_index(drop=True)

    return det, raw


def weekly_w2(df: pd.DataFrame, p: ScenarioParams) -> Tuple[pd.DataFrame, pd.DataFrame]:
    # Rolling N-day window (per card): >= distinct wallets AND total amount >= threshold
    lookback_td = _rolling_lookback_from_df(df)
    lookback = _rolling_window_days(df) - 1
    base_all = df.copy()
    base_all = _filter_by_monitored_bank(base_all, p.monitor_bank_w2)
    if base_all.empty:
        return base_all.iloc[0:0].assign(ScenarioId="W2"), base_all.iloc[0:0].copy()

    base_all = base_all.sort_values(["CardId", "TxnTimestamp"], kind="mergesort")
    have_bank = ISSUER_BANK_COL in base_all.columns

    det_rows: list[dict] = []
    raw_rows: list[pd.DataFrame] = []

    for card_id, g in base_all.groupby("CardId", dropna=False, sort=False):
        gg = g.reset_index(drop=False)
        ts = pd.to_datetime(gg["TxnTimestamp"], errors="coerce")
        amt = pd.to_numeric(gg["Amount"], errors="coerce").fillna(0.0)
        approved = gg["Approved"].fillna(False).astype(bool)
        wallet = gg["WalletId"]
        holder = gg.get("AccountHolder")
        bank = gg.get(ISSUER_BANK_COL) if have_bank else None

        win = deque()  # (pos, ts)
        approved_sum_amt = 0.0
        approved_wallets = Counter()
        holders = Counter()
        banks = Counter()

        for pos in range(len(gg)):
            t = ts.iat[pos]
            if pd.isna(t):
                continue

            a = float(amt.iat[pos])
            is_ok = bool(approved.iat[pos])
            w = str(wallet.iat[pos]).strip()
            if is_ok:
                approved_sum_amt += a
                if w and w.lower() != "nan":
                    approved_wallets[w] += 1
                if holder is not None:
                    h = str(holder.iat[pos]).strip()
                    if h and h.lower() != "nan":
                        holders[h] += 1
                if bank is not None:
                    b = str(bank.iat[pos]).strip()
                    if b and b.lower() != "nan":
                        banks[b] += 1

            win.append((pos, t))

            if lookback_td >= pd.Timedelta(days=1):
                cutoff = t - lookback_td + pd.Timedelta(days=1)
            else:
                cutoff = t - lookback_td + pd.Timedelta(seconds=1)
            while win and win[0][1] < cutoff:
                old_pos, old_t = win.popleft()
                old_ok = bool(approved.iat[old_pos])
                old_a = float(amt.iat[old_pos])
                old_w = str(wallet.iat[old_pos]).strip()
                if old_ok:
                    approved_sum_amt -= float(old_a)
                    if old_w and old_w.lower() != "nan":
                        approved_wallets[old_w] -= 1
                        if approved_wallets[old_w] <= 0:
                            del approved_wallets[old_w]
                    if holder is not None:
                        oh = str(holder.iat[old_pos]).strip()
                        if oh and oh.lower() != "nan":
                            holders[oh] -= 1
                            if holders[oh] <= 0:
                                del holders[oh]
                    if bank is not None:
                        ob = str(bank.iat[old_pos]).strip()
                        if ob and ob.lower() != "nan":
                            banks[ob] -= 1
                            if banks[ob] <= 0:
                                del banks[ob]

            txn_count = int(approved.iloc[[x[0] for x in win]].sum()) if win else 0
            not_approved_count = len(win) - txn_count if win else 0
            unique_wallets = len(approved_wallets)
            total_amount = float(approved_sum_amt)

            if (
                unique_wallets >= p.w2_min_wallets
                and txn_count >= p.w2_min_txn
                and total_amount >= p.w2_min_total_amount
            ):
                wpos = [x[0] for x in win]
                wamt_ok = _approved_amount_subset(amt, approved, wpos)
                end_date = t.date()
                det_rows.append(
                    {
                        "ScenarioId": "W2",
                        "CardId": card_id,
                        "TxnWeek": end_date,
                        "UniqueWallets": int(unique_wallets),
                        "TxnCount": int(txn_count),
                        "NotApprovedCount": int(not_approved_count),
                        "TotalAmount": float(total_amount),
                        "AvgAmount": float(wamt_ok.mean()) if len(wamt_ok) else 0.0,
                        "MinAmount": float(wamt_ok.min()) if len(wamt_ok) else 0.0,
                        "MaxAmount": float(wamt_ok.max()) if len(wamt_ok) else 0.0,
                        "CardHolderNamesPipe": "|".join(sorted(holders.keys())) if holders else "",
                        "UniqueBanks": int(len(banks)) if have_bank else None,
                    }
                )
                raw_win = gg.iloc[wpos].copy()
                raw_win = _linked_raw_approved_only(raw_win)
                raw_win["ScenarioId"] = "W2"
                raw_win["TxnWeek"] = end_date
                raw_rows.append(raw_win)

    det = pd.DataFrame(det_rows)
    raw = pd.concat(raw_rows, ignore_index=True) if raw_rows else base_all.iloc[0:0].copy()
    if det.empty:
        return det.assign(ScenarioId="W2"), base_all.iloc[0:0].copy()

    det = det.drop_duplicates(subset=["CardId", "TxnWeek"], keep="first").reset_index(drop=True)
    return det, raw


def weekly_w3(df: pd.DataFrame, p: ScenarioParams) -> Tuple[pd.DataFrame, pd.DataFrame]:
    # Rolling N-day window (per wallet): >= rejected attempts
    lookback_td = _rolling_lookback_from_df(df)
    lookback = _rolling_window_days(df) - 1
    base = df[df["Rejected"]].copy()
    base = _filter_by_monitored_bank(base, p.monitor_bank_w3)
    if base.empty:
        return base.iloc[0:0].assign(ScenarioId="W3"), base.iloc[0:0].copy()

    base = base.sort_values(["WalletId", "TxnTimestamp"], kind="mergesort")
    have_bank = ISSUER_BANK_COL in base.columns

    det_rows: list[dict] = []
    raw_rows: list[pd.DataFrame] = []

    for wallet_id, g in base.groupby("WalletId", dropna=False, sort=False):
        gg = g.reset_index(drop=False)
        ts = pd.to_datetime(gg["TxnTimestamp"], errors="coerce")
        amt = pd.to_numeric(gg["Amount"], errors="coerce").fillna(0.0)
        holder = gg.get("AccountHolder")
        bank = gg.get(ISSUER_BANK_COL) if have_bank else None

        win = deque()  # (pos, ts, amt)
        holders = Counter()
        banks = Counter()

        for pos in range(len(gg)):
            t = ts.iat[pos]
            if pd.isna(t):
                continue

            if holder is not None:
                h = str(holder.iat[pos]).strip()
                if h and h.lower() != "nan":
                    holders[h] += 1

            if bank is not None:
                b = str(bank.iat[pos]).strip()
                if b and b.lower() != "nan":
                    banks[b] += 1

            win.append((pos, t))

            if lookback_td >= pd.Timedelta(days=1):
                cutoff = t - lookback_td + pd.Timedelta(days=1)
            else:
                cutoff = t - lookback_td + pd.Timedelta(seconds=1)
            while win and win[0][1] < cutoff:
                old_pos, old_t = win.popleft()
                if holder is not None:
                    oh = str(holder.iat[old_pos]).strip()
                    if oh and oh.lower() != "nan":
                        holders[oh] -= 1
                        if holders[oh] <= 0:
                            del holders[oh]
                if bank is not None:
                    ob = str(bank.iat[old_pos]).strip()
                    if ob and ob.lower() != "nan":
                        banks[ob] -= 1
                        if banks[ob] <= 0:
                            del banks[ob]

            rejected_count = len(win)
            if rejected_count >= p.w3_min_rejected:
                wpos = [x[0] for x in win]
                wamt = amt.iloc[wpos]
                end_date = t.date()
                det_rows.append(
                    {
                        "ScenarioId": "W3",
                        "WalletId": wallet_id,
                        "TxnWeek": end_date,
                        "RejectedCount": int(rejected_count),
                        "NotApprovedCount": 0,
                        "AvgAmount": float(wamt.mean()) if len(wamt) else 0.0,
                        "MinAmount": float(wamt.min()) if len(wamt) else 0.0,
                        "MaxAmount": float(wamt.max()) if len(wamt) else 0.0,
                        "CardHolderNamesPipe": "|".join(sorted(holders.keys())) if holders else "",
                        "UniqueBanks": int(len(banks)) if have_bank else None,
                    }
                )
                raw_win = gg.iloc[wpos].copy()
                raw_win["ScenarioId"] = "W3"
                raw_win["TxnWeek"] = end_date
                raw_rows.append(raw_win)

    det = pd.DataFrame(det_rows)
    raw = pd.concat(raw_rows, ignore_index=True) if raw_rows else base.iloc[0:0].copy()
    if det.empty:
        return det.assign(ScenarioId="W3"), base.iloc[0:0].copy()

    det = det.drop_duplicates(subset=["WalletId", "TxnWeek"], keep="first").reset_index(drop=True)
    return det, raw


DAILY = {"D1": daily_d1, "D2": daily_d2, "D3": daily_d3}
WEEKLY = {"W1": weekly_w1, "W2": weekly_w2, "W3": weekly_w3}


# ---------------------------------------------------------------------------
# Dynamic scenario runners (group + configurable period)
# ---------------------------------------------------------------------------

GROUP_TYPES = (
    "many_cards_one_wallet",
    "one_card_many_wallets",
    "one_card_one_wallet",
    "multiple_failed",
)


def period_lookback(unit: str, value: int) -> pd.Timedelta:
    u = (unit or "day").strip().lower()
    v = max(1, int(value))
    if u == "hour":
        return pd.Timedelta(hours=v)
    if u == "day":
        return pd.Timedelta(days=v)
    if u == "week":
        return pd.Timedelta(days=v * 7)
    if u == "month":
        return pd.Timedelta(days=v * 30)
    return pd.Timedelta(days=v)


def uses_calendar_day_bucket(unit: str, value: int) -> bool:
    return (unit or "").strip().lower() == "day" and int(value) == 1


def _params_from_thresholds(thresholds: dict, monitored_bank: str | None) -> ScenarioParams:
    """Build ScenarioParams from dynamic scenario thresholds JSON."""
    t = thresholds or {}
    base = ScenarioParams()
    return ScenarioParams(
        d_amount_min=float(t.get("min_amount_per_txn", base.d_amount_min)),
        d_total_amount_min=float(t.get("min_total_amount", base.d_total_amount_min)),
        d1_min_txn=int(t.get("min_txn", base.d1_min_txn)),
        d1_min_unique_cards=int(t.get("min_unique_cards", base.d1_min_unique_cards)),
        d1_risk_min_total_amount=float(t.get("risk_min_total_amount", base.d1_risk_min_total_amount)),
        d1_risk_min_expenditure_pct=float(t.get("risk_min_expenditure_pct", base.d1_risk_min_expenditure_pct)),
        d2_min_wallets=int(t.get("min_wallets", base.d2_min_wallets)),
        d2_risk_min_total_amount=float(t.get("risk_min_total_amount", base.d2_risk_min_total_amount)),
        d2_risk_min_wallet_expenditure_pct=float(
            t.get("risk_min_wallet_expenditure_pct", base.d2_risk_min_wallet_expenditure_pct)
        ),
        d2_risk_min_wallets_pct=float(t.get("risk_min_wallets_pct", base.d2_risk_min_wallets_pct)),
        d3_min_rejected=int(t.get("min_rejected", base.d3_min_rejected)),
        w1_min_txn=int(t.get("min_txn", base.w1_min_txn)),
        w1_min_unique_cards=int(t.get("min_unique_cards", base.w1_min_unique_cards)),
        w1_min_total_amount=float(t.get("min_total_amount", base.w1_min_total_amount)),
        w2_min_wallets=int(t.get("min_wallets", base.w2_min_wallets)),
        w2_min_txn=int(t.get("min_txn", base.w2_min_txn)),
        w2_min_total_amount=float(t.get("min_total_amount", base.w2_min_total_amount)),
        w3_min_rejected=int(t.get("min_rejected", base.w3_min_rejected)),
        monitor_bank_d1=monitored_bank,
        monitor_bank_d2=monitored_bank,
        monitor_bank_d3=monitored_bank,
        monitor_bank_w1=monitored_bank,
        monitor_bank_w2=monitored_bank,
        monitor_bank_w3=monitored_bank,
    )


def _set_scenario_id(det: pd.DataFrame, code: str) -> pd.DataFrame:
    if det is None or det.empty:
        return det
    det = det.copy()
    det["ScenarioId"] = code
    return det


def _rolling_lookback_from_df(df: pd.DataFrame) -> pd.Timedelta:
    lb = df.attrs.get("rolling_lookback")
    if lb is not None:
        try:
            return pd.Timedelta(lb)
        except (TypeError, ValueError):
            pass
    days = _rolling_window_days(df)
    return pd.Timedelta(days=max(1, days))


def _prepare_rolling_df(df: pd.DataFrame, period_unit: str, period_value: int) -> pd.DataFrame:
    work = df.copy()
    work.attrs["rolling_lookback"] = period_lookback(period_unit, period_value)
    # Legacy weekly runners also read rolling_window_days (inclusive day count).
    lb = work.attrs["rolling_lookback"]
    if isinstance(lb, pd.Timedelta):
        work.attrs["rolling_window_days"] = max(1, int(lb / pd.Timedelta(days=1)))
    return work


def one_card_one_wallet_calendar(
    df: pd.DataFrame,
    thresholds: dict,
    monitored_bank: str | None,
    scenario_code: str,
    txn_filter: str = "approved_only",
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """One card → one wallet: approved cash-ins grouped by card+wallet+day."""
    amount_min = float(thresholds.get("min_amount_per_txn", 0))
    total_min = float(thresholds.get("min_total_amount", 0))
    min_txn = int(thresholds.get("min_txn", 1))

    base_all = df.copy()
    base_all = _filter_by_monitored_bank(base_all, monitored_bank)
    base_all = _filter_amount_ge(base_all, amount_min)
    base = _detection_base(base_all, txn_filter)
    agg = {
        "TxnCount": ("Amount", "size"),
        "TotalAmount": ("Amount", "sum"),
        "AvgAmount": ("Amount", "mean"),
        "MinAmount": ("Amount", "min"),
        "MaxAmount": ("Amount", "max"),
        "CardHolderNamesPipe": (
            "AccountHolder",
            lambda s: "|".join(
                sorted({str(x).strip() for x in s if str(x).strip() and str(x).lower() != "nan"})
            ),
        ),
    }
    if ISSUER_BANK_COL in base.columns:
        agg["UniqueBanks"] = (ISSUER_BANK_COL, pd.Series.nunique)
    grp = (
        base.groupby(["CardId", "WalletId", "TxnDate"], dropna=False)
        .agg(**agg)
        .reset_index()
    )
    na = (
        base_all.groupby(["CardId", "WalletId", "TxnDate"], dropna=False)["Approved"]
        .apply(lambda s: int((~s.fillna(False).astype(bool)).sum()))
        .reset_index(name="NotApprovedCount")
    )
    grp = grp.merge(na, on=["CardId", "WalletId", "TxnDate"], how="left")
    grp["NotApprovedCount"] = grp["NotApprovedCount"].fillna(0).astype(int)
    det = grp[(grp["TxnCount"] >= min_txn) & (grp["TotalAmount"] >= total_min)].copy()
    if det.empty:
        return det.assign(ScenarioId=scenario_code), base_all.iloc[0:0].copy()
    raw = _link_raw_rows(base, base_all, det, ["CardId", "WalletId", "TxnDate"], txn_filter)
    det.insert(0, "ScenarioId", scenario_code)
    return det, raw


def one_card_one_wallet_rolling(
    df: pd.DataFrame,
    thresholds: dict,
    monitored_bank: str | None,
    scenario_code: str,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Rolling window variant for one card → one wallet."""
    amount_min = float(thresholds.get("min_amount_per_txn", 0))
    total_min = float(thresholds.get("min_total_amount", 0))
    min_txn = int(thresholds.get("min_txn", 1))
    lookback = _rolling_lookback_from_df(df)

    base_all = df.copy()
    base_all = _filter_by_monitored_bank(base_all, monitored_bank)
    base_all = _filter_amount_ge(base_all, amount_min)
    if base_all.empty:
        return base_all.iloc[0:0].assign(ScenarioId=scenario_code), base_all.iloc[0:0].copy()

    base_all = base_all.sort_values(["CardId", "WalletId", "TxnTimestamp"], kind="mergesort")
    det_rows: list[dict] = []
    raw_rows: list[pd.DataFrame] = []

    for (card_id, wallet_id), g in base_all.groupby(["CardId", "WalletId"], dropna=False, sort=False):
        gg = g.reset_index(drop=False)
        ts = pd.to_datetime(gg["TxnTimestamp"], errors="coerce")
        amt = pd.to_numeric(gg["Amount"], errors="coerce").fillna(0.0)
        approved = gg["Approved"].fillna(False).astype(bool)
        win = deque()

        for pos in range(len(gg)):
            t = ts.iat[pos]
            if pd.isna(t):
                continue
            win.append((pos, t))
            cutoff = t - lookback
            while win and win[0][1] < cutoff:
                win.popleft()

            wpos = [x[0] for x in win]
            ok_mask = approved.iloc[wpos]
            txn_count = int(ok_mask.sum())
            if txn_count < min_txn:
                continue
            wamt_ok = _approved_amount_subset(amt, approved, wpos)
            total_amount = float(wamt_ok.sum()) if len(wamt_ok) else 0.0
            if total_amount < total_min:
                continue
            end_date = t.date()
            det_rows.append(
                {
                    "ScenarioId": scenario_code,
                    "CardId": card_id,
                    "WalletId": wallet_id,
                    "TxnWeek": end_date,
                    "TxnCount": txn_count,
                    "TotalAmount": total_amount,
                    "AvgAmount": float(wamt_ok.mean()) if len(wamt_ok) else 0.0,
                    "MinAmount": float(wamt_ok.min()) if len(wamt_ok) else 0.0,
                    "MaxAmount": float(wamt_ok.max()) if len(wamt_ok) else 0.0,
                    "NotApprovedCount": int(len(wpos) - txn_count),
                }
            )
            raw_win = gg.iloc[wpos].copy()
            raw_win = _linked_raw_approved_only(raw_win)
            raw_win["ScenarioId"] = scenario_code
            raw_win["TxnWeek"] = end_date
            raw_rows.append(raw_win)

    det = pd.DataFrame(det_rows)
    raw = pd.concat(raw_rows, ignore_index=True) if raw_rows else base_all.iloc[0:0].copy()
    if det.empty:
        return det.assign(ScenarioId=scenario_code), base_all.iloc[0:0].copy()
    det = det.drop_duplicates(subset=["CardId", "WalletId", "TxnWeek"], keep="first").reset_index(drop=True)
    return det, raw


def run_dynamic_scenario(
    df: pd.DataFrame,
    *,
    code: str,
    group_type: str,
    period_unit: str,
    period_value: int,
    thresholds: dict,
    monitored_bank: str | None = None,
    transaction_filter: str = "approved_only",
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Run one configured scenario against a transaction dataframe.

    Returns (detections_df, linked_raw_df) with ScenarioId set to ``code``.
    """
    gt = (group_type or "").strip()
    pu = (period_unit or "day").strip().lower()
    pv = max(1, int(period_value))
    tf = (transaction_filter or "approved_only").strip().lower()
    if gt == "multiple_failed":
        tf = "failed_only"
    p = _params_from_thresholds(thresholds, monitored_bank)

    if gt == "many_cards_one_wallet":
        if tf == "failed_only":
            p_fail = ScenarioParams(
                **{**p.__dict__, "d3_min_rejected": int(thresholds.get("min_txn", p.d1_min_txn)), "monitor_bank_d3": monitored_bank}
            )
            if uses_calendar_day_bucket(pu, pv):
                det, raw = daily_d3(df, p_fail)
            else:
                work = _prepare_rolling_df(df, pu, pv)
                p_fail_w = ScenarioParams(**{**p.__dict__, "w3_min_rejected": int(thresholds.get("min_txn", p.w1_min_txn)), "monitor_bank_w3": monitored_bank})
                det, raw = weekly_w3(work, p_fail_w)
        elif uses_calendar_day_bucket(pu, pv):
            det, raw = daily_d1(df, p, tf)
        else:
            work = _prepare_rolling_df(df, pu, pv)
            det, raw = weekly_w1(work, p)
        return _set_scenario_id(det, code), raw

    if gt == "one_card_many_wallets":
        if tf == "failed_only":
            p_fail = ScenarioParams(
                **{**p.__dict__, "d3_min_rejected": int(thresholds.get("min_txn", 1)), "monitor_bank_d3": monitored_bank}
            )
            if uses_calendar_day_bucket(pu, pv):
                det, raw = daily_d3(df, p_fail)
            else:
                work = _prepare_rolling_df(df, pu, pv)
                p_fail_w = ScenarioParams(**{**p.__dict__, "w3_min_rejected": int(thresholds.get("min_txn", 1)), "monitor_bank_w3": monitored_bank})
                det, raw = weekly_w3(work, p_fail_w)
        elif uses_calendar_day_bucket(pu, pv):
            det, raw = daily_d2(df, p, tf)
        else:
            work = _prepare_rolling_df(df, pu, pv)
            det, raw = weekly_w2(work, p)
        return _set_scenario_id(det, code), raw

    if gt == "multiple_failed":
        if uses_calendar_day_bucket(pu, pv):
            det, raw = daily_d3(df, p)
        else:
            work = _prepare_rolling_df(df, pu, pv)
            det, raw = weekly_w3(work, p)
        return _set_scenario_id(det, code), raw

    if gt == "one_card_one_wallet":
        if uses_calendar_day_bucket(pu, pv):
            return one_card_one_wallet_calendar(df, thresholds, monitored_bank, code, tf)
        work = _prepare_rolling_df(df, pu, pv)
        return one_card_one_wallet_rolling(work, thresholds, monitored_bank, code)

    raise ValueError(f"Unknown scenario group type: {group_type!r}")


def key_cols_for_scenario(group_type: str, period_unit: str, period_value: int) -> list[str]:
    """Columns used to link detection rows back to raw transactions."""
    gt = (group_type or "").strip()
    calendar = uses_calendar_day_bucket(period_unit, period_value)
    end_col = "TxnDate" if calendar else "TxnWeek"
    if gt == "one_card_many_wallets":
        return ["CardId", end_col]
    if gt == "one_card_one_wallet":
        return ["CardId", "WalletId", end_col]
    return ["WalletId", end_col]


def scenario_defaults() -> Dict[str, object]:
    p = ScenarioParams()
    return {
        # Daily
        "d_amount_min": p.d_amount_min,
        "d_total_amount_min": p.d_total_amount_min,
        "d1_min_txn": p.d1_min_txn,
        "d1_min_unique_cards": p.d1_min_unique_cards,
        "d1_risk_min_total_amount": p.d1_risk_min_total_amount,
        "d1_risk_min_expenditure_pct": p.d1_risk_min_expenditure_pct,
        "d2_min_wallets": p.d2_min_wallets,
        "d2_risk_min_total_amount": p.d2_risk_min_total_amount,
        "d2_risk_min_wallet_expenditure_pct": p.d2_risk_min_wallet_expenditure_pct,
        "d2_risk_min_wallets_pct": p.d2_risk_min_wallets_pct,
        "d3_min_rejected": p.d3_min_rejected,
        # Weekly
        "w1_min_txn": p.w1_min_txn,
        "w1_min_unique_cards": p.w1_min_unique_cards,
        "w1_min_total_amount": p.w1_min_total_amount,
        "w2_min_wallets": p.w2_min_wallets,
        "w2_min_total_amount": p.w2_min_total_amount,
        "w3_min_rejected": p.w3_min_rejected,
        "monitor_bank_d1": p.monitor_bank_d1,
        "monitor_bank_d2": p.monitor_bank_d2,
        "monitor_bank_d3": p.monitor_bank_d3,
        "monitor_bank_w1": p.monitor_bank_w1,
        "monitor_bank_w2": p.monitor_bank_w2,
        "monitor_bank_w3": p.monitor_bank_w3,
    }


def params_from_overrides(overrides: Dict[str, object]) -> ScenarioParams:
    base = ScenarioParams()
    kwargs = {k: overrides.get(k, getattr(base, k)) for k in base.__dataclass_fields__.keys()}
    # Cast ints / floats / optional issuer strings
    for k in list(kwargs.keys()):
        if k.startswith("monitor_bank_"):
            v = kwargs[k]
            if v is None:
                kwargs[k] = None
            else:
                try:
                    if isinstance(v, float) and pd.isna(v):
                        kwargs[k] = None
                        continue
                except (TypeError, ValueError):
                    pass
                t = str(v).strip()
                kwargs[k] = t if t else None
            continue
        if k.endswith("_min_txn") or k.endswith("_min_unique_cards") or k.endswith("_min_wallets") or k.endswith(
            "_min_rejected"
        ):
            kwargs[k] = int(float(kwargs[k]))
        else:
            kwargs[k] = float(kwargs[k])
    return ScenarioParams(**kwargs)

