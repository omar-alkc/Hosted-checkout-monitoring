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
    "wallet_lock": set(),
    "wallet_ci": set(),
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
    },
}


def allowed_targets(from_status: str) -> set[str]:
    return ALLOWED_TRANSITIONS.get(from_status, set())
