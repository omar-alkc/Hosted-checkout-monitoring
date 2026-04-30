from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from typing import Dict, Tuple

import pandas as pd

# Flattened payload column from Excel/JSON (see app templates detection_detail).
ISSUER_BANK_COL = "OPP_card.issuer.bank"


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


def daily_d1(df: pd.DataFrame, p: ScenarioParams) -> Tuple[pd.DataFrame, pd.DataFrame]:
    # wallet/day: >= txn count AND >= unique cards, for per-txn amount >= threshold
    base_all = df.copy()
    base_all = _filter_by_monitored_bank(base_all, p.monitor_bank_d1)
    base_all = _filter_amount_ge(base_all, p.d_amount_min)
    base = base_all[base_all["Approved"]].copy()
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
    raw = base.merge(det[["WalletId", "TxnDate"]].drop_duplicates(), on=["WalletId", "TxnDate"], how="inner")
    det.insert(0, "ScenarioId", "D1")
    return det, raw


def daily_d2(df: pd.DataFrame, p: ScenarioParams) -> Tuple[pd.DataFrame, pd.DataFrame]:
    # card/day: >= distinct wallets, for per-txn amount >= threshold
    base_all = df.copy()
    base_all = _filter_by_monitored_bank(base_all, p.monitor_bank_d2)
    base_all = _filter_amount_ge(base_all, p.d_amount_min)
    base = base_all[base_all["Approved"]].copy()
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
    raw = base.merge(det[["CardId", "TxnDate"]].drop_duplicates(), on=["CardId", "TxnDate"], how="inner")
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
    # Rolling 7-day window (per wallet): >= txn count AND >= unique cards AND total amount >= threshold
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

            # Evict anything older than 7 days (rolling window is [t-6d, t], inclusive by day).
            cutoff = t - pd.Timedelta(days=6)
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
                wamt_ok = amt.iloc[wpos][approved.iloc[wpos]]
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
    # Rolling 7-day window (per card): >= distinct wallets AND total amount >= threshold
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

            cutoff = t - pd.Timedelta(days=6)
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
                wamt_ok = amt.iloc[wpos][approved.iloc[wpos]]
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
    # Rolling 7-day window (per wallet): >= rejected attempts
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

            cutoff = t - pd.Timedelta(days=6)
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

