from __future__ import annotations

import json
import math
import tempfile
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import ImportBatch, ImportBatchStatus, TransactionRow


def _strip_like_metachars(s: str) -> str:
    return "".join(ch for ch in s if ch not in "\\%_")


def _ensure_repo_on_path() -> None:
    import sys

    from app.config import repo_root

    r = str(repo_root())
    if r not in sys.path:
        sys.path.insert(0, r)


def _normalize_external_unique_id(value: object, *, row_index: int) -> str:
    """Stable string id from Excel UniqueId (required, non-empty)."""
    try:
        if pd.api.types.is_scalar(value) and not isinstance(value, (str, bytes)) and pd.isna(value):
            raise ValueError(f"Row {row_index}: UniqueId is missing.")
    except (AttributeError, TypeError, ValueError):
        pass
    if value is None:
        raise ValueError(f"Row {row_index}: UniqueId is missing.")
    if isinstance(value, float) and math.isfinite(value) and value == int(value):
        s = str(int(value))
    else:
        s = str(value).strip()
    if not s:
        raise ValueError(f"Row {row_index}: UniqueId is empty.")
    return s


def _filter_importable_transaction_rows(
    db: Session, parsed: list[tuple[int, dict, str]]
) -> tuple[list[tuple[int, dict, str]], int, int]:
    """
    Keep only importable rows.

    Rules:
    - If UniqueId is duplicated inside the file: keep the first occurrence, skip the rest.
    - If UniqueId already exists in DB: skip it (keep the rest of the file).
    Returns (importable_rows, skipped_duplicates_in_file, skipped_existing_in_db).
    """
    if not parsed:
        return [], 0, 0

    # De-dupe within the file (keep first seen row index for each ext id)
    seen: set[str] = set()
    uniq: list[tuple[int, dict, str]] = []
    skipped_dup = 0
    for idx, payload, ext in parsed:
        if ext in seen:
            skipped_dup += 1
            continue
        seen.add(ext)
        uniq.append((idx, payload, ext))

    ids = [ext for _idx, _payload, ext in uniq]
    existing = set(
        str(x)
        for x in db.scalars(
            select(TransactionRow.transaction_external_id).where(TransactionRow.transaction_external_id.in_(ids))
        ).all()
        if x
    )
    if not existing:
        return uniq, skipped_dup, 0

    importable: list[tuple[int, dict, str]] = []
    skipped_existing = 0
    for idx, payload, ext in uniq:
        if ext in existing:
            skipped_existing += 1
            continue
        importable.append((idx, payload, ext))
    return importable, skipped_dup, skipped_existing


def parse_upload_to_batch(db: Session, *, filename: str, file_bytes: bytes) -> ImportBatch:
    _ensure_repo_on_path()
    from io_utils import add_helper_columns, read_transactions_csv, read_transactions_xlsx

    tmp_path: Path | None = None
    parsed: list[tuple[int, dict, str]] | None = None
    err: str | None = None
    skipped_dup = 0
    skipped_existing = 0
    skipped_paymenttype = 0
    try:
        ext = str(Path(filename).suffix or "").lower()
        suffix = ".csv" if ext == ".csv" else ".xlsx"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = Path(tmp.name)
        if suffix == ".csv":
            df_raw, spec, _ = read_transactions_csv(tmp_path)
        else:
            df_raw, spec, _ = read_transactions_xlsx(tmp_path)
        df_raw = df_raw.copy()
        df_raw.insert(0, "_aml_row_index", range(len(df_raw)))
        df_helpers = add_helper_columns(df_raw, spec)
        pt_col = str(getattr(spec, "payment_type", "PaymentType") or "PaymentType")
        if pt_col not in df_helpers.columns:
            raise ValueError(
                "Missing required column: PaymentType (required for web imports; only PaymentType=DB rows are stored)."
            )
        before_n = int(len(df_helpers))
        pt_norm = df_helpers[pt_col].astype(str).fillna("").map(lambda s: str(s).strip().upper())
        df_helpers = df_helpers.loc[pt_norm.eq("DB")].copy()
        skipped_paymenttype = max(0, before_n - int(len(df_helpers)))
        if df_helpers.empty:
            raise ValueError("No rows with PaymentType=DB to import.")
        parsed = []
        for rec in df_helpers.to_dict(orient="records"):
            idx = int(rec.pop("_aml_row_index"))
            row = {str(k): v for k, v in rec.items()}
            # Deduplicate/validate on UniqueId (not TransactionId).
            ext = _normalize_external_unique_id(row.get("UniqueId"), row_index=idx)
            payload = _to_postgres_jsonb(row)
            json.dumps(payload, allow_nan=False)
            parsed.append((idx, payload, ext))
        parsed, skipped_dup, skipped_existing = _filter_importable_transaction_rows(db, parsed)
        if not parsed:
            msg = "No new rows to import."
            if skipped_dup:
                msg += f" Skipped {skipped_dup} duplicate UniqueId row(s) inside the file."
            if skipped_existing:
                msg += f" Skipped {skipped_existing} UniqueId row(s) already imported."
            raise ValueError(msg)
    except Exception as e:
        err = str(e)
        parsed = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    batch = ImportBatch(original_filename=filename, row_count=0, status=ImportBatchStatus.uploaded.value)
    if err is not None:
        batch.status = ImportBatchStatus.failed.value
        batch.error_message = err
        batch.row_count = 0
    else:
        batch.status = ImportBatchStatus.ready.value
        warn_parts: list[str] = []
        if skipped_paymenttype:
            warn_parts.append(f"Skipped {skipped_paymenttype} non-DB PaymentType row(s).")
        if skipped_dup:
            warn_parts.append(f"Skipped {skipped_dup} duplicate UniqueId row(s) inside the file.")
        if skipped_existing:
            warn_parts.append(f"Skipped {skipped_existing} UniqueId row(s) already imported.")
        batch.error_message = " ".join(warn_parts) if warn_parts else None
        batch.row_count = len(parsed or [])

    db.add(batch)
    db.flush()
    if parsed:
        for idx, payload, ext in parsed:
            db.add(
                TransactionRow(
                    import_batch_id=batch.id,
                    row_index=idx,
                    payload=payload,
                    transaction_external_id=ext,
                )
            )
    db.commit()
    db.refresh(batch)
    return batch


def _to_postgres_jsonb(obj: object) -> object:
    """
    Recursively convert Excel/pandas values to structures PostgreSQL JSONB accepts
    (strict JSON: no NaN / Infinity tokens).
    """
    if obj is None:
        return None
    try:
        if obj is pd.NA:
            return None
    except (AttributeError, TypeError):
        pass
    try:
        if pd.api.types.is_scalar(obj) and not isinstance(obj, (str, bytes)) and pd.isna(obj):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(obj, dict):
        return {str(k): _to_postgres_jsonb(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_postgres_jsonb(x) for x in obj]
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int,)) and not isinstance(obj, bool):
        return int(obj)
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, str):
        return obj
    if isinstance(obj, Decimal):
        try:
            x = float(obj)
            if math.isnan(x) or math.isinf(x):
                return None
            return x
        except Exception:
            return str(obj)
    try:
        import numpy as np

        if isinstance(obj, np.floating):
            x = float(obj)
            return None if math.isnan(x) or math.isinf(x) else float(x)
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, np.ndarray):
            return _to_postgres_jsonb(obj.tolist())
    except ImportError:
        pass
    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except (ValueError, OSError):
            return None
    if hasattr(obj, "item"):
        try:
            return _to_postgres_jsonb(obj.item())
        except Exception:
            return str(obj)
    return str(obj)


def dataframe_for_batch(db: Session, batch_id: int) -> pd.DataFrame:
    rows = (
        db.query(TransactionRow)
        .filter(TransactionRow.import_batch_id == batch_id)
        .order_by(TransactionRow.row_index.asc())
        .all()
    )
    if not rows:
        return pd.DataFrame()
    data = []
    for r in rows:
        row = dict(r.payload)
        row["_aml_row_index"] = r.row_index
        data.append(row)
    df = pd.DataFrame(data)
    if "TxnTimestamp" in df.columns:
        df["TxnTimestamp"] = pd.to_datetime(df["TxnTimestamp"], errors="coerce")
    if "TxnDate" in df.columns:
        df["TxnDate"] = pd.to_datetime(df["TxnDate"], errors="coerce").dt.date
    if "TxnWeek" in df.columns:
        df["TxnWeek"] = pd.to_datetime(df["TxnWeek"], errors="coerce").dt.date
    for col in ("Approved", "Rejected"):
        if col in df.columns:
            df[col] = df[col].astype(bool)
    return df


def dataframe_for_rolling_window(
    db: Session,
    *,
    days: int,
    as_of_iso: str | None = None,
    period_start_iso: str | None = None,
    period_end_iso: str | None = None,
) -> pd.DataFrame:
    """
    Load transaction_rows across all import batches within a trailing window (best-effort).

    - We filter using ISO-string comparisons on payload timestamps, similar to search_transactions_for_batch().
    - Adds helper columns the same way as dataframe_for_batch(), plus:
        _aml_import_batch_id, _aml_transaction_row_id

    Without period bounds: trailing ``days`` ending at ``now()`` or at ``as_of_iso`` when set.

    With both ``period_start_iso`` and ``period_end_iso`` (timestamptz ISO strings, UTC):
    intersect [period_start, period_end] with the last ``days`` before min(period_end, now()) — i.e.
    ``ts >= greatest(period_start, anchor - days)`` and ``ts <= anchor`` where anchor = least(period_end, now()).
    When ``as_of_iso`` is also set, period mode takes precedence for the WHERE clause.
    """
    try:
        d = int(days)
    except Exception:
        d = 0
    if d <= 0:
        return pd.DataFrame()

    ts_expr = "coalesce(transaction_rows.payload->>'TxnTimestamp', transaction_rows.payload->>'RequestTimestamp', transaction_rows.payload->>'TxnDate', '')"
    # Cast ISO-like strings to timestamptz for comparisons (payload values are written as isoformat strings).
    ts_cast = f"NULLIF({ts_expr}, '')::timestamptz"
    params: dict[str, object] = {"days": d}
    ps = str(period_start_iso or "").strip()
    pe = str(period_end_iso or "").strip()
    if ps and pe:
        params["pstart"] = ps
        params["pend"] = pe
        # Use CAST(... AS timestamptz), not :bind::timestamptz — SQLAlchemy text() treats :name as binds
        # and misparses PostgreSQL's :: cast when it immediately follows a bind name.
        anchor = "least(CAST(:pend AS timestamptz), now())"
        where_sql = (
            f"{ts_cast} >= greatest(CAST(:pstart AS timestamptz), {anchor} - make_interval(days => :days)) "
            f"AND {ts_cast} <= {anchor}"
        )
    elif as_of_iso and str(as_of_iso).strip():
        params["asof"] = str(as_of_iso).strip()
        where_sql = (
            f"{ts_cast} >= (CAST(:asof AS timestamptz) - make_interval(days => :days)) "
            f"AND {ts_cast} <= CAST(:asof AS timestamptz)"
        )
    else:
        where_sql = f"{ts_cast} >= (now() - make_interval(days => :days)) AND {ts_cast} <= now()"

    ids_stmt = text(
        f"""
        SELECT id
        FROM transaction_rows
        WHERE {where_sql}
        ORDER BY id ASC
        """
    )
    ids = [int(r[0]) for r in db.execute(ids_stmt, params).all()]
    if not ids:
        return pd.DataFrame()
    rows = db.query(TransactionRow).filter(TransactionRow.id.in_(ids)).all()
    by_id = {r.id: r for r in rows}
    ordered = [by_id[i] for i in ids if i in by_id]
    data: list[dict] = []
    for r in ordered:
        row = dict(r.payload)
        row["_aml_row_index"] = r.row_index
        row["_aml_import_batch_id"] = r.import_batch_id
        row["_aml_transaction_row_id"] = r.id
        data.append(row)
    df = pd.DataFrame(data)
    if "TxnTimestamp" in df.columns:
        df["TxnTimestamp"] = pd.to_datetime(df["TxnTimestamp"], errors="coerce")
    if "TxnDate" in df.columns:
        df["TxnDate"] = pd.to_datetime(df["TxnDate"], errors="coerce").dt.date
    if "TxnWeek" in df.columns:
        df["TxnWeek"] = pd.to_datetime(df["TxnWeek"], errors="coerce").dt.date
    for col in ("Approved", "Rejected"):
        if col in df.columns:
            df[col] = df[col].astype(bool)
    return df


def search_transactions_for_batch(
    db: Session,
    *,
    batch_id: int | None = None,
    unique_id: str | None = None,
    msisdn: str | None = None,
    card_id: str | None = None,
    account_holder: str | None = None,
    bank: str | None = None,
    amount_min: str | None = None,
    amount_max: str | None = None,
    dt_from: str | None = None,
    dt_to: str | None = None,
    approved: str | None = None,
    exclude_row_indices: list[int] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[TransactionRow], int]:
    """
    Filter transaction_rows for one batch or across all batches using JSONB payload fields.
    Returns (rows, total_count).
    """
    params: dict[str, object] = {}
    where: list[str] = []
    if batch_id is not None:
        params["bid"] = int(batch_id)
        where.append("transaction_rows.import_batch_id = :bid")

    def add_like(field_expr: str, value: str, key: str) -> None:
        v = _strip_like_metachars(value.strip())
        if not v:
            return
        params[key] = f"%{v}%"
        where.append(f"{field_expr} ILIKE :{key}")

    if unique_id:
        v = _strip_like_metachars(unique_id.strip())
        if v:
            params["uid"] = f"%{v}%"
            where.append(
                "(transaction_rows.transaction_external_id ILIKE :uid "
                "OR transaction_rows.payload->>'UniqueId' ILIKE :uid)"
            )
    if msisdn:
        add_like("(transaction_rows.payload->>'WalletId')", msisdn, "ms")
    if card_id:
        add_like("(transaction_rows.payload->>'CardId')", card_id, "cid")
    if account_holder:
        add_like("(transaction_rows.payload->>'AccountHolder')", account_holder, "ah")
    if bank:
        add_like("(transaction_rows.payload->>'OPP_card.issuer.bank')", bank, "bank")

    def add_amount(bound: str, key: str) -> None:
        try:
            v = float(str(bound).strip())
        except ValueError:
            return
        params[key] = v
        where.append(f"NULLIF(transaction_rows.payload->>'Amount','')::numeric >= :{key}" if key == "amin" else f"NULLIF(transaction_rows.payload->>'Amount','')::numeric <= :{key}")

    if amount_min:
        add_amount(amount_min, "amin")
    if amount_max:
        add_amount(amount_max, "amax")

    # Date/time compares as ISO strings (best effort) using coalesce(TxnTimestamp, RequestTimestamp, TxnDate)
    ts_expr = "coalesce(transaction_rows.payload->>'TxnTimestamp', transaction_rows.payload->>'RequestTimestamp', transaction_rows.payload->>'TxnDate', '')"
    if dt_from and str(dt_from).strip():
        params["dtf"] = str(dt_from).strip()
        where.append(f"{ts_expr} >= :dtf")
    if dt_to and str(dt_to).strip():
        params["dtt"] = str(dt_to).strip()
        where.append(f"{ts_expr} <= :dtt")

    if approved:
        a = approved.strip().lower()
        if a in {"true", "false"}:
            params["appr"] = a
            where.append("lower(coalesce(transaction_rows.payload->>'Approved','')) = :appr")

    if exclude_row_indices:
        ex = []
        for x in exclude_row_indices:
            try:
                v = int(x)
            except (TypeError, ValueError):
                continue
            if v > 0:
                ex.append(v)
        if ex:
            params["excl"] = ex
            where.append("NOT (transaction_rows.row_index = ANY(:excl))")

    where_sql = " AND ".join(where) if where else "TRUE"
    count_stmt = text(f"SELECT count(*) FROM transaction_rows WHERE {where_sql}")
    total = int(db.execute(count_stmt, params).scalar_one() or 0)

    # Order by timestamp string desc, then row_index desc.
    params2 = {**params, "lim": int(limit), "off": int(offset)}
    rows_stmt = text(
        f"""
        SELECT id
        FROM transaction_rows
        WHERE {where_sql}
        ORDER BY {ts_expr} DESC, row_index DESC
        LIMIT :lim OFFSET :off
        """
    )
    ids = [int(r[0]) for r in db.execute(rows_stmt, params2).all()]
    if not ids:
        return [], total
    out = db.query(TransactionRow).filter(TransactionRow.id.in_(ids)).all()
    by_id = {r.id: r for r in out}
    ordered = [by_id[i] for i in ids if i in by_id]
    return ordered, total
