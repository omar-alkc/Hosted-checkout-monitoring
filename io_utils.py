from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd


def _norm_header(s: object) -> str:
    if s is None:
        return ""
    s = str(s)
    # collapse whitespace + strip; keep original casing irrelevant for matching
    return " ".join(s.replace("\t", " ").split()).strip()


def _build_normalized_column_map(columns: Iterable[object]) -> Dict[str, str]:
    """
    Returns mapping {normalized_lower: original_column_name}.
    If duplicates normalize to same key, last wins (we also track collisions in validation).
    """
    m: Dict[str, str] = {}
    for c in columns:
        norm = _norm_header(c).lower()
        if not norm:
            continue
        m[norm] = str(c)
        compact = norm.replace(" ", "")
        if compact and compact != norm:
            # e.g. "transaction id" -> also match resolve("transactionid")
            m.setdefault(compact, str(c))
    return m


@dataclass(frozen=True)
class ColumnSpec:
    request_timestamp: str = "RequestTimestamp"
    mobile: str = "Mobile"
    bin: str = "Bin"
    acct_last4: str = "AccountNumberLast4"
    credit: str = "Credit"
    reason_code: str = "ReasonCode"
    result: str = "Result"
    account_holder: str = "AccountHolder"
    transaction_id: str = "TransactionId"

    def required(self) -> List[str]:
        return [
            self.request_timestamp,
            self.mobile,
            self.bin,
            self.acct_last4,
            self.credit,
            self.reason_code,
            self.transaction_id,
        ]

    def optional(self) -> List[str]:
        return [self.result, self.account_holder]


def read_transactions_xlsx(path: str | Path) -> Tuple[pd.DataFrame, ColumnSpec, Dict[str, str]]:
    """
    Reads input Excel and returns:
    - dataframe with original columns untouched
    - ColumnSpec with resolved *actual* column names (post-normalization matching)
    - mapping of normalized_lower -> actual column name
    """
    path = Path(path)
    df = pd.read_excel(path, engine="openpyxl")

    norm_map = _build_normalized_column_map(df.columns)
    spec = ColumnSpec()

    def resolve(expected: str) -> Optional[str]:
        return norm_map.get(_norm_header(expected).lower())

    missing: List[str] = []
    resolved: Dict[str, str] = {}
    for col in spec.required():
        actual = resolve(col)
        if actual is None:
            missing.append(col)
        else:
            resolved[col] = actual

    if missing:
        detected = ", ".join([str(c) for c in df.columns.tolist()])
        raise ValueError(
            "Missing required columns: "
            + ", ".join(missing)
            + "\nDetected columns: "
            + detected
        )

    resolved_spec = ColumnSpec(
        request_timestamp=resolved[spec.request_timestamp],
        mobile=resolved[spec.mobile],
        bin=resolved[spec.bin],
        acct_last4=resolved[spec.acct_last4],
        credit=resolved[spec.credit],
        reason_code=resolved[spec.reason_code],
        result=resolve(spec.result) or spec.result,
        account_holder=resolve(spec.account_holder) or spec.account_holder,
        transaction_id=resolved[spec.transaction_id],
    )
    return df, resolved_spec, norm_map


def add_helper_columns(df: pd.DataFrame, spec: ColumnSpec) -> pd.DataFrame:
    """
    Adds computed helper columns:
    - WalletId
    - CardId
    - Amount
    - Rejected (bool)
    - TxnTimestamp (datetime)
    - TxnDate (date)
    - TxnWeek (week period starting Monday)
    """
    out = df.copy()

    out["TxnTimestamp"] = pd.to_datetime(out[spec.request_timestamp], errors="coerce")
    if out["TxnTimestamp"].isna().all():
        raise ValueError(f"Could not parse any timestamps from column '{spec.request_timestamp}'.")

    out["TxnDate"] = out["TxnTimestamp"].dt.date
    # Week start (Monday): normalize to calendar week start.
    # pandas Period("W-MON") has week *ending* Monday; its start_time is Tuesday, which is not what we want.
    ts = out["TxnTimestamp"]
    out["TxnWeek"] = (ts - pd.to_timedelta(ts.dt.weekday, unit="D")).dt.date

    out["WalletId"] = out[spec.mobile].astype(str).str.strip()
    out["CardId"] = (
        out[spec.bin].astype(str).str.strip().fillna("")
        + out[spec.acct_last4].astype(str).str.strip().fillna("")
    )
    out["Amount"] = pd.to_numeric(out[spec.credit], errors="coerce").fillna(0)

    reason = pd.to_numeric(out[spec.reason_code], errors="coerce").fillna(0)
    out["Rejected"] = reason.ne(0)

    # Best-effort Result normalization (some inputs may not include this column)
    has_result = spec.result in out.columns
    if has_result:
        out["Result"] = out[spec.result].astype(str).fillna("").str.strip()
    else:
        out["Result"] = ""

    # Approved: Result == "ACK" (case-insensitive) AND ReasonCode == 0.
    # If the Result column is absent, Result is empty → never ACK → no approved rows.
    out["Approved"] = out["Result"].astype(str).str.upper().eq("ACK") & reason.eq(0)

    # Optional columns (best effort)
    if spec.account_holder in out.columns:
        out["AccountHolder"] = out[spec.account_holder].astype(str).fillna("").str.strip()
    else:
        out["AccountHolder"] = ""

    return out


def load_dotenv_if_present(env_path: str | Path | None = None) -> bool:
    """
    Minimal .env loader (no external dependency).
    - Reads KEY=VALUE lines
    - Does NOT treat '#' inside the value as a comment (important for passwords)
    - Only sets variables that are not already present in the process environment
    """
    if env_path is None:
        env_path = Path(__file__).with_name(".env")
    env_path = Path(env_path)

    if not env_path.exists():
        return False

    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        return False

    return True


def _guess_code_and_name_columns(df_map: pd.DataFrame) -> Tuple[str, str]:
    import re

    code_like = re.compile(r"^[A-Za-z]{1,5}\d{1,5}$")

    col_scores = {}
    for col in df_map.columns:
        series = df_map[col].dropna().astype(str).str.strip()
        if len(series) == 0:
            col_scores[col] = {"code_matches": 0, "non_null": 0}
            continue
        code_matches = int(series.map(lambda x: 1 if code_like.match(x) else 0).sum())
        col_scores[col] = {"code_matches": code_matches, "non_null": len(series)}

    code_col = None
    best_code_score = -1
    for col, sc in col_scores.items():
        if sc["code_matches"] > best_code_score:
            best_code_score = sc["code_matches"]
            code_col = col

    name_col = None
    best_name_score = -1
    for col in df_map.columns:
        if col == code_col:
            continue
        series = df_map[col].dropna().astype(str).str.strip()
        if len(series) == 0:
            continue
        non_code = int(series.map(lambda x: 1 if (x != "" and not code_like.match(x)) else 0).sum())
        if non_code > best_name_score:
            best_name_score = non_code
            name_col = col

    if code_col is None:
        code_col = df_map.columns[0]
    if name_col is None:
        name_col = df_map.columns[1] if len(df_map.columns) > 1 else df_map.columns[0]

    return str(code_col), str(name_col)


def load_city_name_mapping_from_env() -> Dict[str, str]:
    """
    Load city mapping from GOV_MAPPING_PATH (Excel).
    Returns dict {city_code: city_name}. If missing, returns empty dict.
    """
    raw_path = os.environ.get("GOV_MAPPING_PATH", "").strip()
    if not raw_path:
        return {}

    path = Path(raw_path)
    if not path.is_absolute():
        path = (Path(__file__).parent / path).resolve()
    if not path.exists():
        return {}

    df_map = pd.read_excel(path, sheet_name=0, engine="openpyxl")
    if df_map is None or len(df_map) == 0:
        return {}

    code_col, name_col = _guess_code_and_name_columns(df_map)
    df_map[code_col] = df_map[code_col].astype(str).str.strip()
    df_map[name_col] = df_map[name_col].astype(str).str.strip()
    df_map = df_map[df_map[code_col].notna() & (df_map[code_col].astype(str).str.strip() != "")]

    mapping: Dict[str, str] = {}
    for _, row in df_map[[code_col, name_col]].iterrows():
        code = str(row[code_col]).strip()
        name = str(row[name_col]).strip()
        if code == "" or code.lower() == "nan":
            continue
        mapping[code] = name
    return mapping


def _require_env(name: str) -> str:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        raise ValueError(f"Missing required environment variable: {name}")
    return str(v).strip()


def _minitrans_connect_params() -> tuple[str, int, str, str, str]:
    """Connection settings for MariaDB/MySQL enrichment (wallet + minitrans tables)."""
    return (
        _require_env("MINITRANS_HOST"),
        int(_require_env("MINITRANS_PORT")),
        _require_env("MINITRANS_USER"),
        _require_env("MINITRANS_PASSWORD"),
        _require_env("MINITRANS_DATABASE"),
    )


def fetch_wallet_profiles(msisdns: Sequence[str], chunk_size: int = 1000) -> pd.DataFrame:
    """
    Fetch wallet profiles:
      select msisdn, extra13 as Fullname, city
      from actors_clean1_clone
      where msisdn in (...)
    """
    try:
        import pymysql
    except ImportError as e:
        raise ImportError("pymysql is required for DB lookup. Install it in your Python environment.") from e

    host, port, user, password, database = _minitrans_connect_params()

    msisdns = sorted({str(m).strip() for m in msisdns if m is not None and str(m).strip() and str(m).lower() != "nan"})
    if not msisdns:
        return pd.DataFrame(columns=["msisdn", "Fullname", "city"])

    rows: List[Tuple[str, str, str]] = []
    conn = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.Cursor,
        autocommit=True,
    )
    n_chunks = (len(msisdns) + chunk_size - 1) // chunk_size
    try:
        with conn.cursor() as cur:
            for ci, i in enumerate(range(0, len(msisdns), chunk_size), start=1):
                chunk = msisdns[i : i + chunk_size]
                print(
                    f"  [DB] actors_clean1_clone: chunk {ci}/{n_chunks} ({len(chunk)} MSISDNs) ...",
                    flush=True,
                )
                placeholders = ",".join(["%s"] * len(chunk))
                sql = (
                    "select msisdn, extra13 as Fullname, city "
                    "from actors_clean1_clone "
                    f"where msisdn in ({placeholders})"
                )
                cur.execute(sql, tuple(chunk))
                for msisdn, fullname, city in cur.fetchall():
                    rows.append((str(msisdn), "" if fullname is None else str(fullname), "" if city is None else str(city)))
    finally:
        conn.close()

    return pd.DataFrame(rows, columns=["msisdn", "Fullname", "city"])


def fetch_last_30_days_transactions(msisdns: Sequence[str], chunk_size: int = 1000) -> pd.DataFrame:
    """
    Fetch last 30 days transactions from minitrans_clone for any transaction where
    creditedMSISDN or debitedMSISDN matches the provided msisdns.
    """
    try:
        import pymysql
    except ImportError as e:
        raise ImportError("pymysql is required for DB lookup. Install it in your Python environment.") from e

    host, port, user, password, database = _minitrans_connect_params()

    msisdns = sorted({str(m).strip() for m in msisdns if m is not None and str(m).strip() and str(m).lower() != "nan"})
    if not msisdns:
        return pd.DataFrame(
            columns=[
                "timestamp",
                "transactionId",
                "transactionAmount",
                "creditedMSISDN",
                "debitedMSISDN",
                "transactionType",
                "transactionDescription",
                "AdditionalParameter",
            ]
        )

    cols = [
        "timestamp",
        "transactionId",
        "transactionAmount",
        "creditedMSISDN",
        "debitedMSISDN",
        "transactionType",
        "transactionDescription",
        "AdditionalParameter",
    ]

    out_rows: List[Tuple] = []
    conn = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.Cursor,
        autocommit=True,
    )
    n_chunks = (len(msisdns) + chunk_size - 1) // chunk_size
    try:
        with conn.cursor() as cur:
            for ci, i in enumerate(range(0, len(msisdns), chunk_size), start=1):
                chunk = msisdns[i : i + chunk_size]
                print(
                    f"  [DB] minitrans_clone (last 30 days): chunk {ci}/{n_chunks} ({len(chunk)} MSISDNs) ...",
                    flush=True,
                )
                placeholders = ",".join(["%s"] * len(chunk))
                sql = (
                    "select timestamp, transactionId, transactionAmount, creditedMSISDN, debitedMSISDN, "
                    "transactionType, transactionDescription, AdditionalParameter "
                    "from minitrans_clone "
                    "where timestamp >= CURDATE() - INTERVAL 30 DAY "
                    "  AND timestamp < CURDATE() "
                    f"  and (creditedMSISDN in ({placeholders}) or debitedMSISDN in ({placeholders}))"
                )
                cur.execute(sql, tuple(chunk + chunk))
                out_rows.extend(cur.fetchall())
    finally:
        conn.close()

    return pd.DataFrame(out_rows, columns=cols)


def fetch_post_card_debit_transactions(
    wallet_starts: Sequence[tuple[str, str]],
) -> pd.DataFrame:
    """
    Fetch post-card debit transactions from minitrans_clone for each wallet from its
    corresponding lower-bound timestamp.

    Returns rows with an added `query_wallet` column so callers can group them back to
    the originating monitored MSISDN.
    """
    try:
        import pymysql
    except ImportError as e:
        raise ImportError("pymysql is required for DB lookup. Install it in your Python environment.") from e

    host, port, user, password, database = _minitrans_connect_params()

    pairs: list[tuple[str, str]] = []
    for wallet, start_ts in wallet_starts:
        w = str(wallet or "").strip()
        s = str(start_ts or "").strip()
        if not w or not s or w.lower() == "nan" or s.lower() == "nan":
            continue
        pairs.append((w, s))
    if not pairs:
        return pd.DataFrame(
            columns=[
                "query_wallet",
                "timestamp",
                "transactionId",
                "transactionAmount",
                "creditedMSISDN",
                "debitedMSISDN",
                "transactionType",
                "transactionDescription",
                "AdditionalParameter",
            ]
        )

    cols = [
        "query_wallet",
        "timestamp",
        "transactionId",
        "transactionAmount",
        "creditedMSISDN",
        "debitedMSISDN",
        "transactionType",
        "transactionDescription",
        "AdditionalParameter",
    ]

    out_rows: List[Tuple] = []
    conn = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.Cursor,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            for wallet, start_ts in pairs:
                sql = (
                    "select %s as query_wallet, timestamp, transactionId, transactionAmount, "
                    "creditedMSISDN, debitedMSISDN, transactionType, transactionDescription, AdditionalParameter "
                    "from minitrans_clone "
                    "where debitedMSISDN = %s and timestamp >= %s"
                )
                cur.execute(sql, (wallet, wallet, start_ts))
                out_rows.extend(cur.fetchall())
    finally:
        conn.close()

    df = pd.DataFrame(out_rows, columns=cols)
    if not df.empty and "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    return df


def write_scenario_output(
    output_path: str | Path,
    detections: pd.DataFrame,
    raw_data: pd.DataFrame,
    last_30_days: Optional[pd.DataFrame] = None,
) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        detections.to_excel(writer, sheet_name="detections", index=False)
        raw_data.to_excel(writer, sheet_name="raw_data", index=False)
        if last_30_days is not None:
            last_30_days.to_excel(writer, sheet_name="last_30_days", index=False)

