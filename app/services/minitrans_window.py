from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

BEFORE_PRESETS = ("none", "yesterday", "last_week", "last_month", "custom")


def _coerce_utc_ts(value: object) -> pd.Timestamp | None:
    """Parse a value to a UTC-aware Timestamp (safe to compare with other candidates)."""
    t = pd.to_datetime(value, errors="coerce", utc=True)
    if t is None or pd.isna(t):
        return None
    if isinstance(t, pd.DatetimeIndex):
        if t.empty:
            return None
        t = t[0]
    ts = pd.Timestamp(t)
    if ts.tzinfo is None:
        return ts.tz_localize(timezone.utc)
    return ts.tz_convert(timezone.utc)


def resolve_detection_anchor(
    metrics: dict,
    linked_payloads: list[dict],
    created_at: datetime | None,
) -> pd.Timestamp | None:
    """Best-effort detection anchor timestamp for minitrans / highlighting."""
    candidates: list[pd.Timestamp] = []
    for p in linked_payloads:
        ts = p.get("TxnTimestamp") or p.get("RequestTimestamp") or p.get("TxnDate")
        if ts is not None:
            t = _coerce_utc_ts(ts)
            if t is not None:
                candidates.append(t)
    for key in ("TxnDate", "TxnWeek"):
        if metrics.get(key):
            t = _coerce_utc_ts(metrics[key])
            if t is not None:
                candidates.append(t)
    if created_at is not None:
        t = _coerce_utc_ts(created_at)
        if t is not None:
            candidates.append(t)
    if not candidates:
        return None
    return max(candidates)


def compute_minitrans_window(
    anchor: pd.Timestamp,
    *,
    before_preset: str = "last_week",
    custom_from: datetime | None = None,
    custom_to: datetime | None = None,
    include_after: bool = False,
) -> tuple[datetime | None, datetime | None]:
    """
    Compute (dt_from, dt_to) for minitrans query.

    before_preset: none | yesterday | last_week | last_month | custom
    include_after: extend dt_to from anchor to now
    """
    anchor = _coerce_utc_ts(anchor)
    if anchor is None or pd.isna(anchor):
        return None, None

    now = pd.Timestamp.now(tz=timezone.utc)
    preset = (before_preset or "last_week").strip().lower()
    dt_from: pd.Timestamp | None = None
    dt_to: pd.Timestamp = anchor

    if preset == "yesterday":
        dt_from = anchor - pd.Timedelta(days=1)
    elif preset == "last_week":
        dt_from = anchor - pd.Timedelta(days=7)
    elif preset == "last_month":
        dt_from = anchor - pd.Timedelta(days=30)
    elif preset == "custom":
        if custom_from is not None:
            dt_from = _coerce_utc_ts(custom_from)
        if custom_to is not None:
            dt_to = _coerce_utc_ts(custom_to) or dt_to
    # preset "none" leaves dt_from None until include_after

    if include_after:
        dt_to = max(dt_to, now) if pd.notna(dt_to) else now
        if dt_from is None:
            dt_from = anchor

    if dt_from is not None and pd.notna(dt_from):
        dt_from = _coerce_utc_ts(dt_from)
    if dt_to is not None and pd.notna(dt_to):
        dt_to = _coerce_utc_ts(dt_to) or dt_to

    if dt_from is not None and pd.notna(dt_from) and dt_to is not None and pd.notna(dt_to) and dt_from > dt_to:
        dt_from, dt_to = dt_to, dt_from

    def _py(ts: pd.Timestamp | None) -> datetime | None:
        if ts is None or pd.isna(ts):
            return None
        norm = _coerce_utc_ts(ts)
        return norm.to_pydatetime() if norm is not None else None

    return _py(dt_from), _py(dt_to if include_after or preset != "none" else anchor)
