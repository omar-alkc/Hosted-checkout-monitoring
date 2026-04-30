from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

from io_utils import (
    add_helper_columns,
    fetch_last_30_days_transactions,
    fetch_wallet_profiles,
    load_city_name_mapping_from_env,
    load_dotenv_if_present,
    read_transactions_xlsx,
    write_scenario_output,
)
from scenarios import DAILY, WEEKLY, params_from_overrides, scenario_defaults
from wallet_enrichment import enrich_detection_metrics_dataframe


SCENARIOS_JSON = Path(__file__).with_name("scenarios.json")


def log(msg: str) -> None:
    """Print progress to the console (stdout), flushed so lines appear immediately."""
    print(msg, flush=True)


def _ask_period(root: tk.Tk) -> str:
    # Default: Daily
    choice = simpledialog.askstring(
        "Period",
        "Choose period to run: daily / weekly / both\n(Default is daily)",
        initialvalue="daily",
        parent=root,
    )
    if not choice:
        return "daily"
    choice = choice.strip().lower()
    if choice not in {"daily", "weekly", "both"}:
        messagebox.showerror("Invalid", "Please enter one of: daily, weekly, both", parent=root)
        return _ask_period(root)
    return choice


def _pick_input_file(root: tk.Tk) -> str:
    path = filedialog.askopenfilename(
        parent=root,
        title="Select input Excel",
        filetypes=[("Excel files", "*.xlsx")],
    )
    if not path:
        raise SystemExit("No input selected.")
    return path


def _pick_output_folder(root: tk.Tk) -> str:
    folder = filedialog.askdirectory(parent=root, title="Select output folder")
    if not folder:
        raise SystemExit("No output folder selected.")
    return folder


def _load_overrides() -> Dict[str, object]:
    if SCENARIOS_JSON.exists():
        try:
            return json.loads(SCENARIOS_JSON.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_overrides(overrides: Dict[str, object]) -> None:
    SCENARIOS_JSON.write_text(json.dumps(overrides, indent=2), encoding="utf-8")


def _edit_thresholds_one_form(root: tk.Tk, defaults: Dict[str, object], current: Dict[str, object]) -> Dict[str, object]:
    """
    Single-form editor using a Toplevel window with all thresholds at once.
    Returns updated overrides dict.
    """
    fields = [
        ("d_amount_min", "Daily: min amount per txn (D1/D2)"),
        ("d_total_amount_min", "Daily: min total amount per group (D1/D2)"),
        ("d1_min_txn", "D1: min txn count per wallet/day"),
        ("d1_min_unique_cards", "D1: min unique cards per wallet/day"),
        ("d2_min_wallets", "D2: min unique wallets per card/day"),
        ("d3_min_rejected", "D3: min rejected per wallet/day"),
        ("w1_min_txn", "W1: min txn count per wallet/week"),
        ("w1_min_unique_cards", "W1: min unique cards per wallet/week"),
        ("w1_min_total_amount", "W1: min total amount per wallet/week"),
        ("w2_min_wallets", "W2: min unique wallets per card/week"),
        ("w2_min_total_amount", "W2: min total amount per card/week"),
        ("w3_min_rejected", "W3: min rejected per wallet/week"),
    ]

    win = tk.Toplevel(root)
    win.title("Scenario thresholds")
    win.grab_set()

    entries: Dict[str, tk.Entry] = {}
    for r, (key, label) in enumerate(fields):
        tk.Label(win, text=label, anchor="w").grid(row=r, column=0, sticky="w", padx=8, pady=4)
        e = tk.Entry(win, width=24)
        e.grid(row=r, column=1, sticky="w", padx=8, pady=4)
        e.insert(0, str(current.get(key, defaults.get(key, ""))))
        entries[key] = e

    save_var = tk.BooleanVar(value=False)
    tk.Checkbutton(win, text="Save to scenarios.json for next runs", variable=save_var).grid(
        row=len(fields), column=0, columnspan=2, sticky="w", padx=8, pady=8
    )

    result: Dict[str, object] = {}
    cancelled = {"v": False}

    def on_ok() -> None:
        updated: Dict[str, object] = {}
        # Validate numeric
        for key, _ in fields:
            val = entries[key].get().strip()
            if val == "":
                continue
            try:
                float(val)
            except ValueError:
                messagebox.showerror("Invalid", f"Invalid numeric value for {key}: {val}", parent=win)
                return
            updated[key] = val
        result.update(updated)
        if save_var.get():
            _save_overrides({**current, **result})
        win.destroy()

    def on_cancel() -> None:
        cancelled["v"] = True
        win.destroy()

    btns = tk.Frame(win)
    btns.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="e", padx=8, pady=8)
    tk.Button(btns, text="Cancel", command=on_cancel).pack(side="right", padx=6)
    tk.Button(btns, text="OK", command=on_ok).pack(side="right")

    win.wait_window()
    if cancelled["v"]:
        return dict(current)
    return {**current, **result}


def _run_period(
    df_helpers,
    period: str,
    output_folder: str,
    params,
    wallet_profiles_df,
    city_name_mapping,
) -> List[Tuple[str, Path, int, int]]:
    results: List[Tuple[str, Path, int, int]] = []
    out_dir = Path(output_folder)
    log(f"  [{period}] Output folder: {out_dir}")

    if period == "daily":
        for sid, fn in DAILY.items():
            log(f"  [{period}] Running scenario {sid} ...")
            det, raw = fn(df_helpers, params)
            # For card-based scenarios, attach wallet list pipe for enrichment + last_30_days
            if sid in {"D2"} and not raw.empty:
                wallet_pipe = (
                    raw.groupby(["CardId", "TxnDate"])["WalletId"]
                    .apply(lambda s: "|".join(sorted({str(x).strip() for x in s if str(x).strip()})))
                    .reset_index(name="WalletIdsPipe")
                )
                det = det.merge(wallet_pipe, on=["CardId", "TxnDate"], how="left")

            det = enrich_detection_metrics_dataframe(det, wallet_profiles_df, city_name_mapping)
            # last 30 days: wallet IDs depend on scenario type
            wallet_ids_30 = det["WalletId"].astype(str).tolist() if "WalletId" in det.columns else []
            if sid in {"D2"} and "WalletIdsPipe" in det.columns:
                wallet_ids_30 = []
                for s in det["WalletIdsPipe"].astype(str).tolist():
                    wallet_ids_30.extend([x for x in str(s).split("|") if x.strip()])
            if wallet_ids_30:
                log(f"    Fetching last_30_days for {len(set(wallet_ids_30))} wallet MSISDN(s) ...")
            last_30 = fetch_last_30_days_transactions(wallet_ids_30) if wallet_ids_30 else None
            out_path = out_dir / f"Scenario_{sid}_daily.xlsx"
            write_scenario_output(out_path, det, raw, last_30_days=last_30)
            log(f"    Wrote {out_path.name} (detections={len(det)}, raw_rows={len(raw)}, last_30_rows={len(last_30) if last_30 is not None else 0})")
            results.append((sid, out_path, len(det), len(raw)))
    elif period == "weekly":
        for sid, fn in WEEKLY.items():
            log(f"  [{period}] Running scenario {sid} ...")
            det, raw = fn(df_helpers, params)
            if sid in {"W2"} and not raw.empty:
                wallet_pipe = (
                    raw.groupby(["CardId", "TxnWeek"])["WalletId"]
                    .apply(lambda s: "|".join(sorted({str(x).strip() for x in s if str(x).strip()})))
                    .reset_index(name="WalletIdsPipe")
                )
                det = det.merge(wallet_pipe, on=["CardId", "TxnWeek"], how="left")

            det = enrich_detection_metrics_dataframe(det, wallet_profiles_df, city_name_mapping)
            wallet_ids_30 = det["WalletId"].astype(str).tolist() if "WalletId" in det.columns else []
            if sid in {"W2"} and "WalletIdsPipe" in det.columns:
                wallet_ids_30 = []
                for s in det["WalletIdsPipe"].astype(str).tolist():
                    wallet_ids_30.extend([x for x in str(s).split("|") if x.strip()])
            if wallet_ids_30:
                log(f"    Fetching last_30_days for {len(set(wallet_ids_30))} wallet MSISDN(s) ...")
            last_30 = fetch_last_30_days_transactions(wallet_ids_30) if wallet_ids_30 else None
            out_path = out_dir / f"Scenario_{sid}_weekly.xlsx"
            write_scenario_output(out_path, det, raw, last_30_days=last_30)
            log(f"    Wrote {out_path.name} (detections={len(det)}, raw_rows={len(raw)}, last_30_rows={len(last_30) if last_30 is not None else 0})")
            results.append((sid, out_path, len(det), len(raw)))
    else:
        raise ValueError(period)
    return results


def main() -> None:
    log("=" * 60)
    log("Card cash-in monitoring — starting")
    log("=" * 60)

    root = tk.Tk()
    root.withdraw()

    # Load local .env automatically (so DB env vars are picked up).
    env_loaded = load_dotenv_if_present()
    log(f"[1/8] Environment: {'loaded .env from script folder' if env_loaded else 'no .env file (using system env)'}")
    city_name_mapping = load_city_name_mapping_from_env()
    log(f"[2/8] City mapping (GOV_MAPPING_PATH): {len(city_name_mapping)} code(s) loaded")

    log("[3/8] GUI: choose period (daily / weekly / both). Default dialog: daily.")
    period = _ask_period(root)
    log(f"       Selected period: {period}")

    log("[4/8] GUI: select input Excel file ...")
    input_path = _pick_input_file(root)
    log(f"       Input file: {input_path}")

    try:
        log("[5/8] Reading Excel and validating columns ...")
        df_raw, spec, _ = read_transactions_xlsx(input_path)
        log(f"       Loaded {len(df_raw)} row(s).")
    except Exception as e:
        messagebox.showerror("Input error", str(e), parent=root)
        raise

    # Show detected columns (helps "inspect input columns" at runtime)
    messagebox.showinfo(
        "Detected columns",
        "Loaded successfully.\n\nDetected columns:\n" + "\n".join([f"- {c}" for c in df_raw.columns.tolist()]),
        parent=root,
    )

    log("       Building helper columns (WalletId, CardId, Amount, dates, ...) ...")
    df_helpers = add_helper_columns(df_raw, spec)
    log(f"       Helper dataframe ready: {len(df_helpers)} row(s).")

    defaults = scenario_defaults()
    overrides = _load_overrides()
    log("[6/8] GUI: review/edit scenario thresholds (single form) ...")
    overrides = _edit_thresholds_one_form(root, defaults, overrides)
    params = params_from_overrides({**defaults, **overrides})
    log("       Thresholds applied.")

    log("[7/8] GUI: select output folder ...")
    output_folder = _pick_output_folder(root)
    log(f"       Output folder: {output_folder}")

    # Wallet enrichment base dataset (actors_clean1_clone)
    # Build MSISDN set based on the entire dataset to avoid repeated DB hits per scenario.
    all_wallets = sorted({str(x).strip() for x in df_helpers["WalletId"].astype(str).tolist() if str(x).strip()})
    log(f"[8/8] Fetching wallet profiles from DB for {len(all_wallets)} unique wallet MSISDN(s) ...")
    try:
        wallet_profiles_df = fetch_wallet_profiles(all_wallets)
        log(f"       Received {len(wallet_profiles_df)} profile row(s) from actors_clean1_clone.")
    except Exception as e:
        wallet_profiles_df = None
        log(f"       WARNING: wallet profile fetch failed: {e}")
        messagebox.showwarning(
            "DB lookup disabled",
            "Could not fetch wallet profiles from DB.\n\n"
            f"{e}\n\n"
            "Detections will still be generated, but wallet name/city and last_30_days may be empty.",
            parent=root,
        )

    summaries: List[str] = []
    log("")
    log("Running scenarios and writing workbooks ...")
    if period in {"daily", "both"}:
        log("--- Daily scenarios ---")
        for sid, out_path, det_n, raw_n in _run_period(
            df_helpers, "daily", output_folder, params, wallet_profiles_df, city_name_mapping
        ):
            summaries.append(f"{sid} (daily): detections={det_n}, raw_rows={raw_n} -> {out_path.name}")
    if period in {"weekly", "both"}:
        log("--- Weekly scenarios ---")
        for sid, out_path, det_n, raw_n in _run_period(
            df_helpers, "weekly", output_folder, params, wallet_profiles_df, city_name_mapping
        ):
            summaries.append(f"{sid} (weekly): detections={det_n}, raw_rows={raw_n} -> {out_path.name}")

    log("")
    log("Finished. Summary:")
    for line in summaries:
        log(f"  {line}")
    log("=" * 60)

    messagebox.showinfo("Done", "Wrote outputs:\n\n" + "\n".join(summaries), parent=root)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        # Silent exit for cancel actions
        if str(e):
            print(str(e))
        sys.exit(0)

