from __future__ import annotations

from app.models import Detection


def wallet_msisdns_from_detection(det: Detection) -> list[str]:
    metrics = dict(det.metrics or {})
    wallets: list[str] = []
    w = str(metrics.get("WalletId") or "").strip()
    if w:
        wallets.append(w)
    for seg in str(metrics.get("WalletIdsPipe") or "").split("|"):
        s = seg.strip()
        if s and s.lower() != "nan":
            wallets.append(s)
    return sorted(set(wallets))


def default_card_id(det: Detection) -> str:
    metrics = dict(det.metrics or {})
    return str(metrics.get("CardId") or metrics.get("TopCardId") or "").strip()
