from __future__ import annotations

import math
from typing import Any

from sqlalchemy.orm import Session

from app.models import ScenarioConfig

# Scenario codes stored in JSON and used by scenarios.py registry.
SCENARIO_CODES: tuple[str, ...] = ("D1", "D2", "D3", "W1", "W2", "W3")

THRESHOLD_FIELDS: list[tuple[str, str]] = [
    ("d_amount_min", "Daily: min amount per txn (D1/D2: many cards - one wallet / one Card - multiple wallets)"),
    ("d_total_amount_min", "Daily: min total amount per group (D1/D2)"),
    ("d1_min_txn", "D1 (many cards - one wallet): min txn count per wallet/day"),
    ("d1_min_unique_cards", "D1 (many cards - one wallet): min unique cards per wallet/day"),
    ("d1_risk_min_total_amount", "D1 high risk: min total amount"),
    ("d1_risk_min_expenditure_pct", "D1 high risk: min expenditure percentage"),
    ("d2_min_wallets", "D2 (one Card - multiple wallets): min unique wallets per card/day"),
    ("d2_risk_min_total_amount", "D2 high risk: min total amount"),
    ("d2_risk_min_wallet_expenditure_pct", "D2 high risk: min expenditure percentage per wallet"),
    ("d2_risk_min_wallets_pct", "D2 high risk: min wallets percentage meeting expenditure threshold"),
    ("d3_min_rejected", "D3 (multiple failed transactions): min rejected per wallet/day"),
    ("w1_min_txn", "W1 (many cards - one wallet): min txn count per wallet/week"),
    ("w1_min_unique_cards", "W1 (many cards - one wallet): min unique cards per wallet/week"),
    ("w1_min_total_amount", "W1 (many cards - one wallet): min total amount per wallet/week"),
    ("w2_min_wallets", "W2 (one Card - multiple wallets): min unique wallets per card/week"),
    ("w2_min_txn", "W2 (one Card - multiple wallets): min txn count per card/week"),
    ("w2_min_total_amount", "W2 (one Card - multiple wallets): min total amount per card/week"),
    ("w3_min_rejected", "W3 (multiple failed transactions): min rejected per wallet/week"),
]


def monitor_param_key(scenario_id: str) -> str:
    """Matches ScenarioParams field names: monitor_bank_d1, monitor_bank_w1, …"""
    return f"monitor_bank_{scenario_id.strip().lower()}"


def monitored_banks_normalized(raw: object) -> dict[str, str | None]:
    """Map each scenario code to optional issuer substring (None = no filter)."""
    out: dict[str, str | None] = {c: None for c in SCENARIO_CODES}
    if not isinstance(raw, dict):
        return out
    for c in SCENARIO_CODES:
        if c not in raw:
            continue
        v = raw[c]
        if v is None:
            out[c] = None
        else:
            t = str(v).strip()
            out[c] = t if t else None
    return out


def scenario_enabled_normalized(raw: object) -> dict[str, bool]:
    """
    Map each scenario code to enabled/disabled.

    Missing keys default to True (enabled). Any non-bool truthy value is treated as True.
    """
    out: dict[str, bool] = {c: True for c in SCENARIO_CODES}
    if not isinstance(raw, dict):
        return out
    for c in SCENARIO_CODES:
        if c not in raw:
            continue
        out[c] = bool(raw.get(c))
    return out


def get_threshold_field_keys_for_scenario(scenario_id: str) -> list[str]:
    """Threshold column names that apply to this scenario (may overlap across scenarios)."""
    sid = scenario_id.strip().upper()
    if sid == "D1":
        return [
            "d_amount_min",
            "d_total_amount_min",
            "d1_min_txn",
            "d1_min_unique_cards",
            "d1_risk_min_total_amount",
            "d1_risk_min_expenditure_pct",
        ]
    if sid == "D2":
        return [
            "d_amount_min",
            "d_total_amount_min",
            "d2_min_wallets",
            "d2_risk_min_total_amount",
            "d2_risk_min_wallet_expenditure_pct",
            "d2_risk_min_wallets_pct",
        ]
    if sid == "D3":
        return ["d3_min_rejected"]
    if sid == "W1":
        return ["w1_min_txn", "w1_min_unique_cards", "w1_min_total_amount"]
    if sid == "W2":
        return ["w2_min_wallets", "w2_min_txn", "w2_min_total_amount"]
    if sid == "W3":
        return ["w3_min_rejected"]
    raise ValueError(f"Unknown scenario: {scenario_id!r}")


def get_threshold_fields_for_scenario(scenario_id: str) -> list[tuple[str, str]]:
    allowed = set(get_threshold_field_keys_for_scenario(scenario_id))
    return [(k, lab) for k, lab in THRESHOLD_FIELDS if k in allowed]


def get_or_create_scenario_config(db: Session) -> ScenarioConfig:
    row = db.query(ScenarioConfig).order_by(ScenarioConfig.id.asc()).first()
    if row is not None:
        return row
    row = ScenarioConfig(
        d_amount_min=50000,
        d_total_amount_min=500000,
        d1_min_txn=3,
        d1_min_unique_cards=3,
        d1_risk_min_total_amount=0,
        d1_risk_min_expenditure_pct=0,
        d2_min_wallets=3,
        d2_risk_min_total_amount=0,
        d2_risk_min_wallet_expenditure_pct=0,
        d2_risk_min_wallets_pct=0,
        d3_min_rejected=5,
        w1_min_txn=10,
        w1_min_unique_cards=3,
        w1_min_total_amount=500000,
        w2_min_wallets=5,
        w2_min_txn=1,
        w2_min_total_amount=500000,
        w3_min_rejected=10,
        monitored_banks={},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def overrides_dict_from_row(row: ScenarioConfig) -> dict[str, object]:
    banks = monitored_banks_normalized(getattr(row, "monitored_banks", None))
    enabled = scenario_enabled_normalized(getattr(row, "scenario_enabled", None))
    o: dict[str, Any] = {
        "d_amount_min": float(row.d_amount_min),
        "d_total_amount_min": float(row.d_total_amount_min),
        "d1_min_txn": int(row.d1_min_txn),
        "d1_min_unique_cards": int(row.d1_min_unique_cards),
        "d1_risk_min_total_amount": float(getattr(row, "d1_risk_min_total_amount", 0)),
        "d1_risk_min_expenditure_pct": float(getattr(row, "d1_risk_min_expenditure_pct", 0)),
        "d2_min_wallets": int(row.d2_min_wallets),
        "d2_risk_min_total_amount": float(getattr(row, "d2_risk_min_total_amount", 0)),
        "d2_risk_min_wallet_expenditure_pct": float(getattr(row, "d2_risk_min_wallet_expenditure_pct", 0)),
        "d2_risk_min_wallets_pct": float(getattr(row, "d2_risk_min_wallets_pct", 0)),
        "d3_min_rejected": int(row.d3_min_rejected),
        "w1_min_txn": int(row.w1_min_txn),
        "w1_min_unique_cards": int(row.w1_min_unique_cards),
        "w1_min_total_amount": float(row.w1_min_total_amount),
        "w2_min_wallets": int(row.w2_min_wallets),
        "w2_min_txn": int(getattr(row, "w2_min_txn", 1)),
        "w2_min_total_amount": float(row.w2_min_total_amount),
        "w3_min_rejected": int(row.w3_min_rejected),
    }
    for c in SCENARIO_CODES:
        o[monitor_param_key(c)] = banks.get(c)
        o[f"scenario_enabled_{c.lower()}"] = enabled.get(c, True)
    return o


def set_scenario_enabled(db: Session, *, scenario_id: str, enabled: bool) -> ScenarioConfig:
    sid = scenario_id.strip().upper()
    if sid not in SCENARIO_CODES:
        raise ValueError("Invalid scenario id.")
    row = get_or_create_scenario_config(db)
    m = dict(getattr(row, "scenario_enabled", {}) or {}) if isinstance(getattr(row, "scenario_enabled", None), dict) else {}
    m[sid] = bool(enabled)
    row.scenario_enabled = m
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_scenario_config(db: Session, *, values: dict[str, str]) -> ScenarioConfig:
    row = get_or_create_scenario_config(db)
    for key, _label in THRESHOLD_FIELDS:
        if key not in values:
            continue
        raw = str(values[key]).strip()
        if raw == "":
            continue
        v = float(raw)
        if not math.isfinite(v) or v < 0:
            raise ValueError(f"{key} must be a non-negative finite number.")
        if key.endswith("_min_txn") or key.endswith("_min_unique_cards") or key.endswith("_min_wallets") or key.endswith(
            "_min_rejected"
        ):
            setattr(row, key, int(v))
        else:
            setattr(row, key, v)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _apply_threshold_value(row: ScenarioConfig, key: str, raw: str) -> None:
    v = float(raw)
    if not math.isfinite(v) or v < 0:
        raise ValueError(f"{key} must be a non-negative finite number.")
    if key.endswith("_min_txn") or key.endswith("_min_unique_cards") or key.endswith("_min_wallets") or key.endswith(
        "_min_rejected"
    ):
        setattr(row, key, int(v))
    else:
        setattr(row, key, v)


def update_scenario_partial(
    db: Session,
    *,
    scenario_id: str,
    values: dict[str, str],
    monitored_bank: str | None,
) -> ScenarioConfig:
    """Update thresholds that belong to one scenario plus its monitored issuer substring."""
    sid = scenario_id.strip().upper()
    if sid not in SCENARIO_CODES:
        raise ValueError("Invalid scenario id.")
    row = get_or_create_scenario_config(db)
    allowed = set(get_threshold_field_keys_for_scenario(sid))
    for key in allowed:
        if key not in values:
            continue
        raw = str(values[key]).strip()
        if raw == "":
            continue
        _apply_threshold_value(row, key, raw)

    m = dict(row.monitored_banks) if isinstance(row.monitored_banks, dict) else {}
    nb = (monitored_bank or "").strip() or None
    if nb:
        m[sid] = nb
    else:
        m.pop(sid, None)
    row.monitored_banks = m

    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def monitored_bank_for_scenario(row: ScenarioConfig, scenario_id: str) -> str | None:
    sid = scenario_id.strip().upper()
    raw = row.monitored_banks if isinstance(row.monitored_banks, dict) else {}
    v = raw.get(sid)
    if v is None:
        return None
    t = str(v).strip()
    return t if t else None
