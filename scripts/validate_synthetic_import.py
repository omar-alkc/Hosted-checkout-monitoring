"""Quick check that synthetic demo file triggers default scenarios."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from io_utils import add_helper_columns, read_transactions_xlsx
from scenarios import ScenarioParams, daily_d1, daily_d2, daily_d3, weekly_w1, weekly_w2, weekly_w3

path = ROOT / "sample_data" / "synthetic_transactions_demo.xlsx"
df, spec, _ = read_transactions_xlsx(path)
df = add_helper_columns(df, spec)
# Web import keeps PaymentType=DB only
if "PaymentType" in df.columns:
    df = df.loc[df["PaymentType"].astype(str).str.strip().str.upper().eq("DB")].copy()
p = ScenarioParams()
for name, fn in [
    ("D1", daily_d1),
    ("D2", daily_d2),
    ("D3", daily_d3),
    ("W1", weekly_w1),
    ("W2", weekly_w2),
    ("W3", weekly_w3),
]:
    det, _ = fn(df, p)
    print(f"{name}: {len(det)} detection(s)")
