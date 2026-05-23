# Sample import data

Synthetic transaction files for testing imports and scenario runs **without real PII**.

## Quick use

1. Regenerate (optional):
   ```bash
   python scripts/generate_synthetic_import.py
   ```
2. Log in as **supervisor** → **Imports** → upload `synthetic_transactions_demo.xlsx`
3. Open the batch → **Run scenarios** → choose **both** (daily + weekly)
4. Open **Detections** — you should see hits for **D1, D2, D3, W1, W2, W3** (with default thresholds)

## What’s in the demo file

| Pattern | Wallet / card | Scenario |
|---------|---------------|----------|
| 4 cards → 1 wallet (same day) | `964770123456` | D1 |
| 1 card → 3 wallets (same day) | card `424242`/`5555` | D2 |
| 5 failed attempts (same day) | `964770999001` | D3 |
| 10 txns, 4 cards (7-day window) | `964770111222` | W1 |
| 1 card → 5 wallets (7-day window) | card `400000`/`7777` | W2 |
| 10 rejected (7-day window) | `964770888777` | W3 |

Also includes one low-amount noise row and one `PaymentType=CR` row (skipped on import).

All MSISDNs, names, and banks are fictional (`964…` demo numbers, `Demo Bank A/B/C`).

## Regenerate with a fixed date

```bash
python scripts/generate_synthetic_import.py --date 2026-05-20
```

## Custom output path

```bash
python scripts/generate_synthetic_import.py -o sample_data/my_batch.xlsx
```
