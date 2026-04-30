from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any


def json_safe(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return obj
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if hasattr(obj, "item"):  # numpy scalar
        try:
            return obj.item()
        except Exception:
            return str(obj)
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(x) for x in obj]
    return str(obj)
