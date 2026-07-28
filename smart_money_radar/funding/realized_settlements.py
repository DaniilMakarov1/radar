from __future__ import annotations

from typing import Any


TRUST_PRIORITY = {
    "unknown": 0,
    "legacy_default": 1,
    "inferred": 2,
    "adapter_explicit": 3,
    "account_ledger": 4,
}

TRUSTED_REALIZED_SOURCE_PAIRS = {
    ("realized_settlement", "adapter_explicit"),
    ("account_transaction", "account_ledger"),
}


def normalized_trust(value: Any) -> str:
    text = str(value or "unknown").strip().lower()
    return text if text in TRUST_PRIORITY else "unknown"


def settlement_cashflow_trusted(row: dict[str, Any]) -> bool:
    source_type = str(row.get("source_type") or row.get("source") or "").strip().lower()
    source_trust = normalized_trust(
        row.get("source_trust") or row.get("rate_semantics_source")
    )
    return (source_type, source_trust) in TRUSTED_REALIZED_SOURCE_PAIRS


def should_replace_realized_settlement(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any],
) -> bool:
    if existing is None:
        return True
    existing_trust = normalized_trust(
        existing.get("source_trust") or existing.get("rate_semantics_source")
    )
    incoming_trust = normalized_trust(
        incoming.get("source_trust") or incoming.get("rate_semantics_source")
    )
    return TRUST_PRIORITY[incoming_trust] >= TRUST_PRIORITY[existing_trust]
