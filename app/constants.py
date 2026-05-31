from __future__ import annotations

# Primary display order for detection detail "Metrics snapshot". Keys missing from a detection
# are skipped; any other keys are appended alphabetically at the end.
DETECTION_METRICS_DISPLAY_ORDER: tuple[str, ...] = (
    "CardHolderNamesPipe",
    "WalletHolderFullName",
    "WalletHolderNamesPipe",
    "WalletId",
    "WalletIdsPipe",
    "TopCardHolderName",
    "TopCardId",
    "TopCardTotalAmount",
    "MinAmount",
    "MaxAmount",
    "AvgAmount",
    "TotalAmount",
    "TxnCount",
    "UniqueCards",
    "UniqueWallets",
    "UniqueBanks",
    "Risk",
    "RiskObservedExpenditurePct",
    "RiskObservedExpenditureAmount",
    "RiskObservedWalletsPct",
    "RiskObservedMatchedWalletCount",
    "RiskObservedWalletCount",
    "RiskObservedMaxWalletExpenditurePct",
    "NotApprovedCount",
    "TxnDate",
    "TxnWeek",
)

# Stored on metrics but not shown on detection detail (redundant with another key).
# WalletHolderFullName is also hidden per-detection when WalletHolderNamesPipe is present.
HIDDEN_DETECTION_METRIC_KEYS: frozenset[str] = frozenset({"WalletHolderName"})

# Short codes stored on Detection.scenario_id; shown in UI with these titles.
SCENARIO_LABELS: dict[str, str] = {
    "D1": "D1: Many cards - One wallet",
    "D2": "D2: One Card - multiple wallets",
    "D3": "D3: Multiple failed transactions",
    "W1": "W1: Many cards - One wallet",
    "W2": "W2: One Card - multiple wallets",
    "W3": "W3: Multiple failed transactions",
}

# Detection metrics JSON keys → table row labels on the detail page.
METRIC_KEY_LABELS: dict[str, str] = {
    "ScenarioId": "Scenario name",
    "WalletId": "MSISDN (wallet)",
    "WalletIdsPipe": "MSISDNs",
    "CardHolderNamesPipe": "Card Holder Names",
    "WalletHolderFullName": "Wallet holder full name",
    "WalletCityName": "Wallet residency city",
    "WalletHolderNamesPipe": "Wallet holder names",
    "WalletCityNamesPipe": "Wallet residency cities",
    "TopCardId": "Top card ID",
    "TopCardTotalAmount": "Top card total amount",
    "TopCardHolderName": "Top card holder name(s)",
    "CardId": "Card id",
    "TotalAmount": "Total amount",
    "TxnCount": "Transaction count",
    "UniqueCards": "Unique cards",
    "UniqueWallets": "Unique wallets",
    "MinAmount": "Min amount",
    "MaxAmount": "Max amount",
    "AvgAmount": "Avg amount",
    "UniqueBanks": "Unique banks",
    "RejectedCount": "Rejected count",
    "NotApprovedCount": "Not approved transactions",
    "Risk": "Risk",
    "RiskObservedExpenditurePct": "Observed debit percentage",
    "RiskObservedExpenditureAmount": "Observed debit amount",
    "RiskObservedWalletsPct": "Observed wallets percentage",
    "RiskObservedMatchedWalletCount": "Observed matched wallet count",
    "RiskObservedWalletCount": "Observed wallet count",
    "RiskObservedMaxWalletExpenditurePct": "Observed max wallet debit percentage",
    "TxnDate": "Date & time",
    "TxnWeek": "Rolling window end (date)",
    "PreviousAlerts": "Previous alerts",
}

STATUS_LABELS: dict[str, str] = {
    "test": "Test",
    "new": "New",
    "false_positive_initial": "False positive (initial)",
    "suspicious_initial": "Suspicious (initial)",
    "false_positive_final": "False positive (final)",
    "suspicious_final": "Suspicious (final)",
    "wallet_lock": "Wallet lock",
    "wallet_ci": "Wallet CI",
    "wallet_reactivated": "Wallet Re-activated",
    "pending_evidence": "Pending evidence",
    "investigation_consolidate": "Investigation Consolidate",
}

STATUS_KEYS = tuple(STATUS_LABELS.keys())

OPEN_DETECTION_STATUSES: frozenset[str] = frozenset(
    {"new", "false_positive_initial", "suspicious_initial", "pending_evidence"}
)
INITIAL_REVIEW_STATUSES: frozenset[str] = frozenset({"false_positive_initial", "suspicious_initial"})
TEST_DETECTION_STATUSES: frozenset[str] = frozenset({"test"})
CLOSED_DETECTION_STATUSES: frozenset[str] = frozenset(
    k for k in STATUS_KEYS if k not in OPEN_DETECTION_STATUSES and k not in TEST_DETECTION_STATUSES
)

# Closed outcomes with no investigator triage path — suppress quick actions and "typical next steps".
CLOSED_OUTCOME_NO_NEXT_STEP_STATUSES: frozenset[str] = frozenset(
    {"false_positive_final", "wallet_reactivated"}
)

_QUEUE_ALIASES: dict[str, frozenset[str]] = {
    "open": OPEN_DETECTION_STATUSES,
    "closed": CLOSED_DETECTION_STATUSES,
    "initial": INITIAL_REVIEW_STATUSES,
    "test": TEST_DETECTION_STATUSES,
}


def statuses_for_queue(queue: str | None) -> frozenset[str] | None:
    if not queue:
        return None
    return _QUEUE_ALIASES.get(queue.strip().lower())

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "test": {"new"},
    # "test" lets supervisors mark live alerts (e.g. rolling W1) for sandbox triage; bulk status uses this graph.
    "new": {
        "false_positive_initial",
        "suspicious_initial",
        "pending_evidence",
        "investigation_consolidate",
        "test",
    },
    "false_positive_initial": {
        "false_positive_final",
        "suspicious_final",
        "pending_evidence",
        "investigation_consolidate",
        "wallet_lock",
        "wallet_ci",
    },
    "suspicious_initial": {
        "false_positive_final",
        "suspicious_final",
        "pending_evidence",
        "investigation_consolidate",
        "wallet_lock",
        "wallet_ci",
    },
    "false_positive_final": {"wallet_lock", "wallet_ci", "pending_evidence", "investigation_consolidate"},
    "suspicious_final": {"wallet_lock", "wallet_ci", "pending_evidence", "investigation_consolidate"},
    "wallet_lock": {"wallet_reactivated"},
    "wallet_ci": {"wallet_reactivated"},
    "wallet_reactivated": set(),
    "pending_evidence": {
        "new",
        "false_positive_initial",
        "suspicious_initial",
        "investigation_consolidate",
    },
    "investigation_consolidate": {
        "false_positive_initial",
        "suspicious_initial",
        "false_positive_final",
        "suspicious_final",
        "pending_evidence",
        "wallet_lock",
        "wallet_ci",
        "wallet_reactivated",
    },
}


def allowed_targets(from_status: str) -> set[str]:
    return ALLOWED_TRANSITIONS.get(from_status, set())


# UI grouping for status dropdowns (optgroup labels).
STATUS_GROUP_LABELS: dict[str, str] = {
    "new": "Intake",
    "test": "Sandbox",
    "false_positive_initial": "Initial review",
    "suspicious_initial": "Initial review",
    "false_positive_final": "Final outcomes",
    "suspicious_final": "Final outcomes",
    "pending_evidence": "Escalation",
    "investigation_consolidate": "Escalation",
    "wallet_lock": "Terminal actions",
    "wallet_ci": "Terminal actions",
    "wallet_reactivated": "Closed outcomes",
}

STATUS_GROUP_ORDER: tuple[str, ...] = (
    "Intake",
    "Initial review",
    "Final outcomes",
    "Escalation",
    "Terminal actions",
    "Closed outcomes",
    "Sandbox",
    "Other",
)

# Shown in metrics summary strip on detection detail.
DETECTION_METRICS_SUMMARY_KEYS: tuple[str, ...] = (
    "WalletId",
    "Risk",
    "TotalAmount",
    "TxnCount",
    "UniqueCards",
    "UniqueWallets",
)

# Investigator bulk status: common workflow targets (supervisors use all STATUS_KEYS).
INVESTIGATOR_BULK_STATUS_KEYS: tuple[str, ...] = (
    "false_positive_initial",
    "suspicious_initial",
    "false_positive_final",
    "suspicious_final",
    "pending_evidence",
    "investigation_consolidate",
)

# Quick-action chips on detection detail (first N allowed targets in this order).
STATUS_QUICK_ACTION_ORDER: tuple[str, ...] = (
    "false_positive_initial",
    "suspicious_initial",
    "pending_evidence",
    "investigation_consolidate",
    "false_positive_final",
    "suspicious_final",
    "wallet_lock",
    "wallet_ci",
    "wallet_reactivated",
    "new",
    "test",
)

# Preferred quick-action order from specific current statuses (overrides global order).
STATUS_QUICK_ACTION_BY_FROM: dict[str, tuple[str, ...]] = {
    "false_positive_initial": (
        "false_positive_final",
        "suspicious_final",
        "pending_evidence",
    ),
    "suspicious_initial": (
        "suspicious_final",
        "false_positive_final",
        "pending_evidence",
    ),
    "suspicious_final": (
        "wallet_lock",
        "wallet_ci",
        "pending_evidence",
    ),
    "wallet_lock": ("wallet_reactivated",),
    "wallet_ci": ("wallet_reactivated",),
}


def _quick_action_order(from_status: str | None) -> tuple[str, ...]:
    if from_status and from_status in STATUS_QUICK_ACTION_BY_FROM:
        return STATUS_QUICK_ACTION_BY_FROM[from_status]
    return STATUS_QUICK_ACTION_ORDER


def status_select_groups(
    allowed: set[str] | frozenset[str] | list[str],
) -> list[tuple[str, list[tuple[str, str]]]]:
    """Build optgroup data: [(group_label, [(status_key, label), ...]), ...]."""
    allowed_set = set(allowed)
    buckets: dict[str, list[tuple[str, str]]] = {}
    for key in sorted(allowed_set, key=lambda k: STATUS_KEYS.index(k) if k in STATUS_KEYS else 999):
        group = STATUS_GROUP_LABELS.get(key, "Other")
        buckets.setdefault(group, []).append((key, STATUS_LABELS.get(key, key)))
    ordered: list[tuple[str, list[tuple[str, str]]]] = []
    seen: set[str] = set()
    for label in STATUS_GROUP_ORDER:
        if label in buckets:
            ordered.append((label, buckets[label]))
            seen.add(label)
    for label, items in buckets.items():
        if label not in seen:
            ordered.append((label, items))
    return ordered


def status_quick_actions(
    allowed: set[str] | frozenset[str] | list[str],
    *,
    from_status: str | None = None,
    limit: int = 3,
) -> list[tuple[str, str]]:
    if from_status in CLOSED_OUTCOME_NO_NEXT_STEP_STATUSES:
        return []
    allowed_set = set(allowed)
    out: list[tuple[str, str]] = []
    for key in _quick_action_order(from_status):
        if key in allowed_set:
            out.append((key, STATUS_LABELS.get(key, key)))
        if len(out) >= limit:
            break
    return out


def status_helper_text(from_status: str, allowed: set[str] | frozenset[str] | list[str]) -> str:
    """One-line guidance for the status change form."""
    from_label = STATUS_LABELS.get(from_status, from_status)
    if from_status in CLOSED_OUTCOME_NO_NEXT_STEP_STATUSES:
        return f"This detection is closed ({from_label}). No further triage steps are expected."
    allowed_set = set(allowed)
    quick = status_quick_actions(allowed_set, from_status=from_status, limit=3)
    if not quick:
        return f"Status {from_label} has no further transitions."
    names = ", ".join(label for _k, label in quick)
    return f"From {from_label}, typical next steps: {names}."
