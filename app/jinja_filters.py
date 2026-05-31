from __future__ import annotations

from jinja2 import Environment
from markupsafe import Markup, escape


def _amount0_filter(value: object) -> str:
    """Whole-number money style: thousands separators, no decimals."""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return str(value).lower()
    try:
        n = int(round(float(value)))
        return f"{n:,}"
    except (ValueError, TypeError, OverflowError):
        return str(value)


def _short_dt_filter(value: object) -> str:
    """Format as YYYY-MM-DD HH:MM (no sub-second)."""
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError):
            pass
    s = str(value).strip()
    if len(s) >= 16 and s[4] == "-" and s[7] == "-":
        if s[10] == "T":
            return f"{s[:10]} {s[11:16]}"
        if s[10] == " ":
            return s[:16]
    return s


def _is_blank_scalar(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, float):
        import math

        return math.isnan(value) or math.isinf(value)
    try:
        import pandas as pd

        if pd.isna(value):
            return True
    except Exception:
        pass
    s = str(value).strip()
    return not s or s.lower() in {"nan", "none", "<na>", "nat"}


def _yes_no_filter(value: object) -> str:
    """Boolean / true|false strings → Yes / No; blank → empty string."""
    if _is_blank_scalar(value):
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    s = str(value).strip().lower()
    if s in {"true", "1", "yes"}:
        return "Yes"
    if s in {"false", "0", "no"}:
        return "No"
    return str(value).strip()


def _display_cell_filter(value: object) -> str:
    """None / NaN / 'nan' → empty string (use `or '—'` in templates)."""
    if _is_blank_scalar(value):
        return ""
    if isinstance(value, float):
        import math

        if math.isfinite(value) and value == int(value):
            return str(int(value))
        return str(value).strip()
    return str(value).strip()


def _norm_msisdn_token(value: object) -> str:
    if _is_blank_scalar(value):
        return ""
    s = _display_cell_filter(value)
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _format_detection_msisdn_filter(metrics: object, scenario_id: object = "") -> str:
    m = metrics if isinstance(metrics, dict) else {}
    sid = str(scenario_id or "").strip().upper()
    wallet = _norm_msisdn_token(m.get("WalletId"))
    pipe = str(m.get("WalletIdsPipe") or "").strip()
    if sid in {"D2", "W2"} and pipe:
        seen: list[str] = []
        for seg in pipe.split("|"):
            tok = _norm_msisdn_token(seg)
            if tok and tok not in seen:
                seen.append(tok)
        return ", ".join(seen[:4])
    return wallet


def _pipe_segments(value: object) -> list[str]:
    """Split a pipe-delimited metric value into non-empty segments."""
    if _is_blank_scalar(value):
        return []
    return [seg for seg in str(value).split("|") if seg.strip()]


def _name_pipe_chips_filter(value: object) -> Markup:
    """Pipe-separated holder names → yellow chip spans (metrics snapshot)."""
    parts: list[str] = []
    for seg in _pipe_segments(value):
        tok = _display_cell_filter(seg)
        if tok and tok not in parts:
            parts.append(tok)
    if not parts:
        return Markup('<span class="muted">—</span>')
    inner = "".join(f'<span class="metric-name-chip">{escape(tok)}</span>' for tok in parts)
    return Markup(f'<div class="metric-name-chips">{inner}</div>')


def _msisdn_pipe_chips_filter(value: object) -> Markup:
    """Pipe-separated MSISDNs → card-style chips (deduped by normalized number)."""
    parts: list[str] = []
    seen: set[str] = set()
    for seg in _pipe_segments(value):
        tok = _norm_msisdn_token(seg)
        if not tok or tok in seen:
            continue
        seen.add(tok)
        parts.append(tok)
    if not parts:
        return Markup('<span class="muted">—</span>')
    inner = "".join(f'<span class="metric-msisdn-card">{escape(tok)}</span>' for tok in parts)
    return Markup(f'<div class="metric-msisdn-chips">{inner}</div>')


def _city_pipe_chips_filter(value: object) -> Markup:
    """Pipe-separated cities → chips with case-insensitive deduplication."""
    parts: list[str] = []
    seen: set[str] = set()
    for seg in _pipe_segments(value):
        tok = _display_cell_filter(seg)
        if not tok:
            continue
        key = tok.casefold()
        if key in seen:
            continue
        seen.add(key)
        parts.append(tok)
    if not parts:
        return Markup('<span class="muted">—</span>')
    inner = "".join(f'<span class="metric-city-chip">{escape(tok)}</span>' for tok in parts)
    return Markup(f'<div class="metric-city-chips">{inner}</div>')


def register_jinja_filters(env: Environment) -> None:
    env.filters["amount0"] = _amount0_filter
    env.filters["shortdt"] = _short_dt_filter
    env.filters["yes_no"] = _yes_no_filter
    env.filters["display_cell"] = _display_cell_filter
    env.filters["format_detection_msisdn"] = _format_detection_msisdn_filter
    env.filters["name_pipe_chips"] = _name_pipe_chips_filter
    env.filters["msisdn_pipe_chips"] = _msisdn_pipe_chips_filter
    env.filters["city_pipe_chips"] = _city_pipe_chips_filter
