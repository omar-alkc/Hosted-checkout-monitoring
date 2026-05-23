"""
Generate synthetic transaction Excel for web import testing.

Creates patterns that should hit default scenario thresholds (migration 001 seeds):
  D1: many cards -> one wallet (same day)
  D2: one card -> many wallets (same day)
  D3: many rejected attempts (same day)
  W1/W2/W3: rolling 7-day window patterns

Web import requires: UniqueId, PaymentType=DB, Result=ACK + ReasonCode=0 for approved rows.

Usage (repo root):
  python scripts/generate_synthetic_import.py
  python scripts/generate_synthetic_import.py -o sample_data/my_test.xlsx
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# Default scenario thresholds (scenario_config seed values).
AMOUNT_MIN = 50_000
DAILY_TOTAL_MIN = 500_000
D1_MIN_TXN = 3
D1_MIN_CARDS = 3
D2_MIN_WALLETS = 3
D3_MIN_REJECTED = 5
W1_MIN_TXN = 10
W1_MIN_CARDS = 3
W1_MIN_TOTAL = 500_000
W2_MIN_WALLETS = 5
W2_MIN_TOTAL = 500_000
W3_MIN_REJECTED = 10

COLUMNS = [
    "UniqueId",
    "RequestTimestamp",
    "Mobile",
    "Bin",
    "AccountNumberLast4",
    "Credit",
    "ReasonCode",
    "Result",
    "TransactionId",
    "PaymentType",
    "AccountHolder",
    "OPP_card.issuer.bank",
]


def _ts(day: datetime, hour: int, minute: int = 0) -> str:
    return day.replace(hour=hour, minute=minute, second=0).strftime("%Y-%m-%d %H:%M:%S")


def _approved_row(
    *,
    uid: str,
    when: datetime,
    mobile: str,
    bin_: str,
    last4: str,
    amount: float,
    holder: str,
    bank: str,
    txn_suffix: str,
    hour: int,
) -> dict:
    return {
        "UniqueId": uid,
        "RequestTimestamp": _ts(when, hour),
        "Mobile": mobile,
        "Bin": bin_,
        "AccountNumberLast4": last4,
        "Credit": amount,
        "ReasonCode": 0,
        "Result": "ACK",
        "TransactionId": f"TXN-{txn_suffix}",
        "PaymentType": "DB",
        "AccountHolder": holder,
        "OPP_card.issuer.bank": bank,
    }


def _rejected_row(
    *,
    uid: str,
    when: datetime,
    mobile: str,
    bin_: str,
    last4: str,
    amount: float,
    reason: int,
    holder: str,
    bank: str,
    txn_suffix: str,
    hour: int,
) -> dict:
    return {
        "UniqueId": uid,
        "RequestTimestamp": _ts(when, hour),
        "Mobile": mobile,
        "Bin": bin_,
        "AccountNumberLast4": last4,
        "Credit": amount,
        "ReasonCode": reason,
        "Result": "NAK",
        "TransactionId": f"TXN-{txn_suffix}",
        "PaymentType": "DB",
        "AccountHolder": holder,
        "OPP_card.issuer.bank": bank,
    }


def build_rows(*, anchor: datetime) -> list[dict]:
    """Build synthetic rows; anchor is the main 'daily scenario' date."""
    rows: list[dict] = []
    week_start = anchor - timedelta(days=6)

    # --- D1: 4 cards -> wallet 964770123456 on anchor day (total 600k) ---
    d1_wallet = "964770123456"
    d1_amount = DAILY_TOTAL_MIN // D1_MIN_TXN + AMOUNT_MIN  # 216666 -> above min per txn
    for i, last4 in enumerate(["1001", "1002", "1003", "1004"], start=1):
        rows.append(
            _approved_row(
                uid=f"SYN-D1-{i:03d}",
                when=anchor,
                mobile=d1_wallet,
                bin_="411111",
                last4=last4,
                amount=float(d1_amount),
                holder=f"D1 Holder {i}",
                bank="Demo Bank A",
                txn_suffix=f"D1-{i}",
                hour=9 + i,
            )
        )

    # --- D2: one card -> 3 wallets on anchor day ---
    d2_bin, d2_last4 = "424242", "5555"
    d2_amount = DAILY_TOTAL_MIN // D2_MIN_WALLETS + 1
    for i, mobile in enumerate(["964770200001", "964770200002", "964770200003"], start=1):
        rows.append(
            _approved_row(
                uid=f"SYN-D2-{i:03d}",
                when=anchor,
                mobile=mobile,
                bin_=d2_bin,
                last4=d2_last4,
                amount=float(d2_amount),
                holder="D2 Shared Card Holder",
                bank="Demo Bank B",
                txn_suffix=f"D2-{i}",
                hour=11 + i,
            )
        )

    # --- D3: 5 rejected attempts -> wallet 964770999001 on anchor day ---
    d3_wallet = "964770999001"
    for i in range(1, D3_MIN_REJECTED + 1):
        rows.append(
            _rejected_row(
                uid=f"SYN-D3-{i:03d}",
                when=anchor,
                mobile=d3_wallet,
                bin_="510510",
                last4=f"30{i:02d}",
                amount=10_000.0,
                reason=51,
                holder=f"D3 Failed Holder {i}",
                bank="Demo Bank C",
                txn_suffix=f"D3-{i}",
                hour=14 + i,
            )
        )

    # --- W1: 10 approved txns, 4 cards, same day (fits in one rolling window) ---
    w1_wallet = "964770111222"
    w1_per_txn = 55_000  # 10 * 55k = 550k > w1_min_total
    w1_cards = [("411111", "2001"), ("411111", "2002"), ("411111", "2003"), ("411111", "2004")]
    for i in range(W1_MIN_TXN):
        bin_, last4 = w1_cards[i % len(w1_cards)]
        rows.append(
            _approved_row(
                uid=f"SYN-W1-{i+1:03d}",
                when=anchor,
                mobile=w1_wallet,
                bin_=bin_,
                last4=last4,
                amount=float(w1_per_txn),
                holder=f"W1 Holder {i+1}",
                bank="Demo Bank A",
                txn_suffix=f"W1-{i+1}",
                hour=8 + (i % 10),
            )
        )

    # --- W2: one card -> 5 wallets across the week ---
    w2_bin, w2_last4 = "400000", "7777"
    w2_per = W2_MIN_TOTAL // W2_MIN_WALLETS
    for i in range(W2_MIN_WALLETS):
        day = week_start + timedelta(days=i)
        rows.append(
            _approved_row(
                uid=f"SYN-W2-{i+1:03d}",
                when=day,
                mobile=f"96477030000{i+1}",
                bin_=w2_bin,
                last4=w2_last4,
                amount=float(w2_per),
                holder="W2 Single Card Holder",
                bank="Demo Bank B",
                txn_suffix=f"W2-{i+1}",
                hour=10 + i,
            )
        )

    # --- W3: 10 rejected -> wallet 964770888777 on anchor day (rolling window) ---
    w3_wallet = "964770888777"
    for i in range(W3_MIN_REJECTED):
        rows.append(
            _rejected_row(
                uid=f"SYN-W3-{i+1:03d}",
                when=anchor,
                mobile=w3_wallet,
                bin_="510510",
                last4="9001",
                amount=5_000.0,
                reason=5,
                holder="W3 Repeat Failures",
                bank="Demo Bank C",
                txn_suffix=f"W3-{i+1}",
                hour=14 + (i % 8),
            )
        )

    # --- Background noise (should not trigger daily volume scenarios) ---
    rows.append(
        _approved_row(
            uid="SYN-NOISE-001",
            when=anchor,
            mobile="964770000000",
            bin_="411111",
            last4="9999",
            amount=1_000.0,
            holder="Low Amount Noise",
            bank="Demo Bank A",
            txn_suffix="NOISE-1",
            hour=7,
        )
    )
    rows.append(
        {
            "UniqueId": "SYN-SKIP-CR-001",
            "RequestTimestamp": _ts(anchor, 6, 30),
            "Mobile": "964770000001",
            "Bin": "411111",
            "AccountNumberLast4": "9998",
            "Credit": 999_999.0,
            "ReasonCode": 0,
            "Result": "ACK",
            "TransactionId": "TXN-SKIP-CR",
            "PaymentType": "CR",
            "AccountHolder": "Skipped Credit Row",
            "OPP_card.issuer.bank": "Demo Bank A",
        }
    )

    return rows


def generate(path: Path, *, anchor: datetime | None = None) -> Path:
    anchor = anchor or datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    rows = build_rows(anchor=anchor)
    df = pd.DataFrame(rows, columns=COLUMNS)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(path, index=False, engine="openpyxl")
    return path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    default_out = repo_root / "sample_data" / "synthetic_transactions_demo.xlsx"

    p = argparse.ArgumentParser(description="Generate synthetic import Excel for scenario testing.")
    p.add_argument("-o", "--output", type=Path, default=default_out, help="Output .xlsx path")
    p.add_argument(
        "--date",
        default=None,
        help="Anchor date YYYY-MM-DD for daily scenarios (default: today)",
    )
    args = p.parse_args()

    anchor = None
    if args.date:
        anchor = datetime.strptime(args.date, "%Y-%m-%d")

    out = generate(args.output.resolve(), anchor=anchor)
    print(f"Wrote {out} ({len(pd.read_excel(out))} rows)")
    print("Import as supervisor -> Imports, then run scenarios (daily + weekly).")
    print("Expected hits with default thresholds: D1, D2, D3, W1, W2, W3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
