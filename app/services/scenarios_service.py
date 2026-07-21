from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.constants import SCENARIO_LABELS
from app.models import Scenario, ScenarioGroupType, ScenarioPeriodUnit

GROUP_TYPE_LABELS: dict[str, str] = {
    ScenarioGroupType.many_cards_one_wallet.value: "Many cards → one wallet",
    ScenarioGroupType.one_card_many_wallets.value: "One card → many wallets",
    ScenarioGroupType.one_card_one_wallet.value: "One card → one wallet",
    ScenarioGroupType.multiple_failed.value: "Multiple failed attempts",
}

PERIOD_UNIT_LABELS: dict[str, str] = {
    ScenarioPeriodUnit.hour.value: "Hourly",
    ScenarioPeriodUnit.day.value: "Daily",
    ScenarioPeriodUnit.week.value: "Weekly",
    ScenarioPeriodUnit.month.value: "Monthly",
}

TRANSACTION_FILTER_LABELS: dict[str, str] = {
    "approved_only": "Approved only",
    "failed_only": "Failed only",
    "both": "Both (approved + failed)",
}

VALID_TRANSACTION_FILTERS = frozenset(TRANSACTION_FILTER_LABELS.keys())

GROUP_CODE_PREFIX: dict[str, str] = {
    ScenarioGroupType.many_cards_one_wallet.value: "MCOW",
    ScenarioGroupType.one_card_many_wallets.value: "OCMW",
    ScenarioGroupType.one_card_one_wallet.value: "OCOW",
    ScenarioGroupType.multiple_failed.value: "MF",
}

# Threshold field definitions: (json_key, label, field_type)
THRESHOLD_SCHEMA: dict[str, list[tuple[str, str, str]]] = {
    ScenarioGroupType.many_cards_one_wallet.value: [
        ("min_txn", "Min transaction count", "int"),
        ("min_unique_cards", "Min unique cards", "int"),
        ("min_amount_per_txn", "Min amount per transaction", "float"),
        ("min_total_amount", "Min total amount", "float"),
        ("risk_min_total_amount", "High risk: min total amount", "float"),
        ("risk_min_expenditure_pct", "High risk: min expenditure %", "float"),
    ],
    ScenarioGroupType.one_card_many_wallets.value: [
        ("min_wallets", "Min unique wallets", "int"),
        ("min_txn", "Min transaction count", "int"),
        ("min_amount_per_txn", "Min amount per transaction", "float"),
        ("min_total_amount", "Min total amount", "float"),
        ("risk_min_total_amount", "High risk: min total amount", "float"),
        ("risk_min_wallet_expenditure_pct", "High risk: min wallet expenditure %", "float"),
        ("risk_min_wallets_pct", "High risk: min wallets %", "float"),
    ],
    ScenarioGroupType.one_card_one_wallet.value: [
        ("min_txn", "Min transaction count", "int"),
        ("min_amount_per_txn", "Min amount per transaction", "float"),
        ("min_total_amount", "Min total amount", "float"),
    ],
    ScenarioGroupType.multiple_failed.value: [
        ("min_rejected", "Min rejected attempts", "int"),
    ],
}

DEFAULT_THRESHOLDS: dict[str, dict[str, float | int]] = {
    ScenarioGroupType.many_cards_one_wallet.value: {
        "min_txn": 3,
        "min_unique_cards": 3,
        "min_amount_per_txn": 50000,
        "min_total_amount": 500000,
        "risk_min_total_amount": 0,
        "risk_min_expenditure_pct": 0,
    },
    ScenarioGroupType.one_card_many_wallets.value: {
        "min_wallets": 3,
        "min_txn": 1,
        "min_amount_per_txn": 50000,
        "min_total_amount": 500000,
        "risk_min_total_amount": 0,
        "risk_min_wallet_expenditure_pct": 0,
        "risk_min_wallets_pct": 0,
    },
    ScenarioGroupType.one_card_one_wallet.value: {
        "min_txn": 3,
        "min_amount_per_txn": 50000,
        "min_total_amount": 500000,
    },
    ScenarioGroupType.multiple_failed.value: {
        "min_rejected": 5,
    },
}

RISK_THRESHOLD_KEYS: dict[str, set[str]] = {
    ScenarioGroupType.many_cards_one_wallet.value: {"risk_min_total_amount", "risk_min_expenditure_pct"},
    ScenarioGroupType.one_card_many_wallets.value: {
        "risk_min_total_amount",
        "risk_min_wallet_expenditure_pct",
        "risk_min_wallets_pct",
    },
}


def _valid_group_type(group_type: str) -> str:
    gt = (group_type or "").strip()
    if gt not in GROUP_TYPE_LABELS:
        raise ValueError("Invalid scenario group type.")
    return gt


def _valid_transaction_filter(value: str) -> str:
    v = (value or "approved_only").strip().lower()
    if v not in VALID_TRANSACTION_FILTERS:
        raise ValueError("Invalid transaction filter.")
    return v


def default_transaction_filter_for_group(group_type: str) -> str:
    gt = _valid_group_type(group_type)
    if gt == ScenarioGroupType.multiple_failed.value:
        return "failed_only"
    return "approved_only"


def _valid_period_unit(unit: str) -> str:
    u = (unit or "").strip().lower()
    if u not in PERIOD_UNIT_LABELS:
        raise ValueError("Invalid monitoring period unit.")
    return u


def list_active_scenarios(db: Session) -> list[Scenario]:
    return (
        db.query(Scenario)
        .filter(Scenario.deleted_at.is_(None))
        .order_by(Scenario.sort_order.asc(), Scenario.id.asc())
        .all()
    )


def list_enabled_scenarios(db: Session) -> list[Scenario]:
    return [s for s in list_active_scenarios(db) if s.enabled]


def get_scenario_by_code(db: Session, code: str) -> Scenario | None:
    c = (code or "").strip().upper()
    if not c:
        return None
    return (
        db.query(Scenario)
        .filter(Scenario.code == c, Scenario.deleted_at.is_(None))
        .first()
    )


def scenario_label_map(db: Session) -> dict[str, str]:
    """Map scenario code → display name."""
    out = dict(SCENARIO_LABELS)
    for s in list_active_scenarios(db):
        out[s.code.upper()] = s.name
    return out


def scenario_codes(db: Session) -> tuple[str, ...]:
    return tuple(s.code.upper() for s in list_active_scenarios(db))


def period_display(unit: str, value: int) -> str:
    u = _valid_period_unit(unit)
    v = max(1, int(value))
    label = PERIOD_UNIT_LABELS[u].lower()
    if v == 1:
        if u == "hour":
            return "1 hour"
        if u == "day":
            return "1 day"
        if u == "week":
            return "1 week"
        return "1 month"
    unit_word = {"hour": "hours", "day": "days", "week": "weeks", "month": "months"}[u]
    return f"{v} {unit_word}"


def _next_code(db: Session, group_type: str) -> str:
    gt = _valid_group_type(group_type)
    prefix = GROUP_CODE_PREFIX[gt]
    existing = (
        db.query(Scenario.code)
        .filter(Scenario.code.like(f"{prefix}-%"))
        .all()
    )
    max_n = 0
    pat = re.compile(rf"^{re.escape(prefix)}-(\d+)$", re.I)
    for (code,) in existing:
        m = pat.match(str(code or "").strip())
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"{prefix}-{max_n + 1:03d}"


def _normalize_thresholds(group_type: str, raw: dict[str, Any] | None) -> dict[str, float | int]:
    gt = _valid_group_type(group_type)
    base = dict(DEFAULT_THRESHOLDS.get(gt, {}))
    if not isinstance(raw, dict):
        return base
    schema_keys = {k for k, _, _ in THRESHOLD_SCHEMA.get(gt, [])}
    for key in schema_keys:
        if key not in raw:
            continue
        val = raw[key]
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        _, _, ftype = next(x for x in THRESHOLD_SCHEMA[gt] if x[0] == key)
        num = float(val)
        if not math.isfinite(num) or num < 0:
            raise ValueError(f"{key} must be a non-negative finite number.")
        base[key] = int(num) if ftype == "int" else num
    return base


def _parse_form_thresholds(group_type: str, form: dict[str, str]) -> dict[str, float | int]:
    gt = _valid_group_type(group_type)
    raw: dict[str, Any] = {}
    for key, _, _ in THRESHOLD_SCHEMA.get(gt, []):
        if key in form:
            raw[key] = form[key]
    return _normalize_thresholds(gt, raw)


def create_scenario(
    db: Session,
    *,
    name: str,
    group_type: str,
    period_unit: str,
    period_value: int,
    thresholds: dict[str, Any] | None = None,
    monitored_bank: str | None = None,
    enabled: bool = True,
    transaction_filter: str | None = None,
    code: str | None = None,
) -> Scenario:
    gt = _valid_group_type(group_type)
    pu = _valid_period_unit(period_unit)
    pv = int(period_value)
    if pv <= 0:
        raise ValueError("Period value must be positive.")
    nm = (name or "").strip()
    if not nm:
        raise ValueError("Scenario name is required.")
    c = (code or _next_code(db, gt)).strip().upper()
    if get_scenario_by_code(db, c) is not None:
        raise ValueError(f"Scenario code {c} already exists.")
    max_sort = db.query(Scenario.sort_order).filter(Scenario.deleted_at.is_(None)).order_by(Scenario.sort_order.desc()).first()
    sort_order = int(max_sort[0]) + 1 if max_sort else 1
    row = Scenario(
        code=c,
        name=nm,
        group_type=gt,
        period_unit=pu,
        period_value=pv,
        thresholds=_normalize_thresholds(gt, thresholds),
        monitored_bank=(monitored_bank or "").strip() or None,
        transaction_filter=_valid_transaction_filter(transaction_filter or default_transaction_filter_for_group(gt)),
        enabled=bool(enabled),
        sort_order=sort_order,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_scenario(
    db: Session,
    scenario: Scenario,
    *,
    name: str | None = None,
    period_unit: str | None = None,
    period_value: int | None = None,
    thresholds: dict[str, Any] | None = None,
    monitored_bank: str | None = None,
    enabled: bool | None = None,
    transaction_filter: str | None = None,
) -> Scenario:
    if name is not None:
        nm = name.strip()
        if not nm:
            raise ValueError("Scenario name is required.")
        scenario.name = nm
    if period_unit is not None:
        scenario.period_unit = _valid_period_unit(period_unit)
    if period_value is not None:
        pv = int(period_value)
        if pv <= 0:
            raise ValueError("Period value must be positive.")
        scenario.period_value = pv
    if thresholds is not None:
        scenario.thresholds = _normalize_thresholds(scenario.group_type, thresholds)
    if monitored_bank is not None:
        scenario.monitored_bank = monitored_bank.strip() or None
    if transaction_filter is not None:
        scenario.transaction_filter = _valid_transaction_filter(transaction_filter)
    if enabled is not None:
        scenario.enabled = bool(enabled)
    db.add(scenario)
    db.commit()
    db.refresh(scenario)
    return scenario


def soft_delete_scenario(db: Session, scenario: Scenario) -> None:
    scenario.deleted_at = datetime.now(timezone.utc)
    scenario.enabled = False
    db.add(scenario)
    db.commit()


def threshold_fields_for_group(group_type: str) -> list[tuple[str, str]]:
    gt = _valid_group_type(group_type)
    risk_keys = RISK_THRESHOLD_KEYS.get(gt, set())
    return [(k, lab) for k, lab, _ in THRESHOLD_SCHEMA[gt] if k not in risk_keys]


def risk_threshold_fields_for_group(group_type: str) -> list[tuple[str, str]]:
    gt = _valid_group_type(group_type)
    risk_keys = RISK_THRESHOLD_KEYS.get(gt, set())
    return [(k, lab) for k, lab, _ in THRESHOLD_SCHEMA[gt] if k in risk_keys]


def group_type_for_scenario_code(db: Session, code: str) -> str | None:
    s = get_scenario_by_code(db, code)
    return s.group_type if s else None


def legacy_group_type_for_code(code: str) -> str | None:
    """Fallback for detections created before dynamic scenarios."""
    c = (code or "").strip().upper()
    if c in {"D1", "W1"}:
        return ScenarioGroupType.many_cards_one_wallet.value
    if c in {"D2", "W2"}:
        return ScenarioGroupType.one_card_many_wallets.value
    if c in {"D3", "W3"}:
        return ScenarioGroupType.multiple_failed.value
    return None
