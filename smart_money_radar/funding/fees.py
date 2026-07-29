from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime
from typing import Any

from smart_money_radar.funding.models import (
    DEFAULT_MAKER_FEE_RATES,
    DEFAULT_TAKER_FEE_RATES,
)

VERIFIED_VIP1_FEE_PROFILES = {
    "binance": {
        "tier": "VIP 1",
        "maker": 0.00016,
        "taker": 0.00040,
        "requirement": "Binance Futures VIP 1; verify account fee page before trading.",
        "source_url": "https://www.binance.com/en/fee/futureFee",
    },
    "bitget": {
        "tier": "VIP 1",
        "maker": 0.00016,
        "taker": 0.00040,
        "requirement": "Bitget futures VIP 1; published threshold is high-volume futures tier.",
        "source_url": "https://www.bitget.com/support/articles/12560603795929",
    },
    "bybit": {
        "tier": "VIP 1",
        "maker": 0.00018,
        "taker": 0.00040,
        "requirement": "Bybit derivatives VIP 1; actual regional account fee must be checked.",
        "source_url": "https://www.bybit.com/en/help-center/article/Benefits-of-the-VIP-Program",
    },
}

TRUSTED_FEE_SOURCE_KINDS = {
    "account_api",
    "account_fee_endpoint",
    "official_account_fee_endpoint",
    "official_public_fee_endpoint",
    "public_fee_endpoint",
    "configured_trusted_fee",
    "trusted_config",
    "versioned_trusted_config",
    "account_override",
}

TRUSTED_FEE_SOURCES = {
    "account_override",
    "official_account_fee_endpoint",
    "official_public_fee_endpoint",
    "venue_public_tier",
    "configured_trusted_fee",
    "verified_vip1_public_fee_schedule",
    "versioned_trusted_config",
}

UNTRUSTED_FEE_SOURCES = {
    "",
    "unknown",
    "unverified",
    "public_default",
    "fee_model_missing",
    "venue_market_fee",
}

TRUSTED_FEE_STATUSES = {
    "VERIFIED",
    "TRUSTED",
    "CONFIGURED_TRUSTED",
    "OFFICIAL",
}


def funding_fee_rate(
    market: dict[str, Any],
    liquidity_role: str = "taker",
) -> float:
    role = "maker" if liquidity_role == "maker" else "taker"
    venue = str(market.get("venue") or "").lower()
    override = fee_override(venue, role)
    if override is not None:
        return override
    field = f"{role}_fee_rate"
    raw = market.get(field)
    defaults = (
        DEFAULT_MAKER_FEE_RATES if role == "maker" else DEFAULT_TAKER_FEE_RATES
    )
    try:
        value = float(raw) if raw is not None else defaults.get(venue, 0.0006)
    except (TypeError, ValueError):
        value = defaults.get(venue, 0.0006)
    # Maker rebates are valid and should remain negative; taker fees cannot be.
    return max(-0.001, value) if role == "maker" else max(0.0, value)


def funding_fee_source(market: dict[str, Any], liquidity_role: str = "taker") -> str:
    role = "maker" if liquidity_role == "maker" else "taker"
    venue = str(market.get("venue") or "").lower()
    if fee_override(venue, role) is not None:
        return "account_override"
    if market.get(f"{role}_fee_rate") is not None:
        return str(market.get("fee_source") or "venue_public_tier")
    return "public_default"


def fee_rate_value(
    market: dict[str, Any],
    liquidity_role: str = "taker",
) -> float | None:
    role = "maker" if liquidity_role == "maker" else "taker"
    raw = market.get(f"{role}_fee_rate")
    if raw is None and role == "taker":
        raw = market.get("fee_rate")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    if role == "maker":
        if value < -0.001 or value > 0.02:
            return None
        return value
    if value < 0.0 or value > 0.02:
        return None
    return value


def _raw_fee_rate_value(
    market: dict[str, Any],
    liquidity_role: str,
) -> float | None:
    role = "maker" if liquidity_role == "maker" else "taker"
    raw = market.get(f"{role}_fee_rate")
    if raw is None and role == "taker":
        raw = market.get("fee_rate")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _select_fee_evidence(
    market: dict[str, Any],
    liquidity_role: str,
) -> dict[str, Any]:
    evidence = market.get("fee_evidence")
    if not isinstance(evidence, dict):
        return {}
    role = "maker" if liquidity_role == "maker" else "taker"
    role_payload = evidence.get(role)
    if isinstance(role_payload, dict):
        return role_payload
    generic = evidence.get("default") or evidence.get("generic")
    if isinstance(generic, dict):
        return generic
    return evidence


def fee_evidence_status(
    market: dict[str, Any],
    liquidity_role: str = "taker",
    *,
    now: datetime | None = None,
    max_age_seconds: float = 30.0 * 24.0 * 60.0 * 60.0,
) -> dict[str, Any]:
    """Return trust status for a fee rate without inventing verification."""
    role = "maker" if liquidity_role == "maker" else "taker"
    raw_rate = _raw_fee_rate_value(market, role)
    rate = fee_rate_value(market, role)
    venue = str(market.get("venue") or "").lower()
    status: dict[str, Any] = {
        "venue": venue,
        "liquidity_role": role,
        "rate": raw_rate if raw_rate is not None else rate,
        "verified": False,
        "trust_status": "UNKNOWN",
        "source_kind": None,
        "source_identifier": None,
        "observed_at": None,
        "reviewed_at": None,
        "blocker": None,
    }
    if rate is None:
        status["blocker"] = (
            f"{role}_fee_rate_out_of_range"
            if raw_rate is not None
            else f"{role}_fee_rate_missing"
        )
        return status

    evidence = _select_fee_evidence(market, role)
    source_kind = str(evidence.get("source_kind") or "").strip().lower()
    source_identifier = str(
        evidence.get("source_identifier")
        or evidence.get("source_url")
        or evidence.get("source")
        or ""
    ).strip()
    trust_status = str(evidence.get("trust_status") or "").strip().upper()
    observed = _parse_timestamp(
        evidence.get("observed_at")
        or evidence.get("reviewed_at")
        or market.get("fee_observed_at")
        or market.get("response_received_at")
    )
    reviewed = _parse_timestamp(evidence.get("reviewed_at") or market.get("fee_reviewed_at"))

    source = str(market.get("fee_source") or "").strip().lower()
    if not evidence:
        status["trust_status"] = "UNVERIFIED"
        status["source_identifier"] = source or None
        status["blocker"] = f"{role}_fee_evidence_missing"
        return status

    status.update(
        {
            "source_kind": source_kind or None,
            "source_identifier": source_identifier or None,
            "observed_at": observed.astimezone(UTC).isoformat() if observed else None,
            "reviewed_at": reviewed.astimezone(UTC).isoformat() if reviewed else None,
            "trust_status": trust_status or "UNKNOWN",
        }
    )

    if source_kind not in TRUSTED_FEE_SOURCE_KINDS:
        status["trust_status"] = trust_status or "UNVERIFIED"
        status["blocker"] = f"{role}_fee_source_untrusted"
        return status
    if not source_identifier:
        status["blocker"] = f"{role}_fee_source_identifier_missing"
        return status
    if trust_status not in TRUSTED_FEE_STATUSES:
        status["trust_status"] = trust_status or "UNVERIFIED"
        status["blocker"] = f"{role}_fee_trust_status_unverified"
        return status
    evidence_version = evidence.get("evidence_version") or evidence.get("schema_version")
    if not evidence_version:
        status["blocker"] = f"{role}_fee_evidence_version_missing"
        return status
    if observed is None and reviewed is None:
        status["blocker"] = f"{role}_fee_timestamp_missing"
        return status
    reference = now.astimezone(UTC) if now else datetime.now(UTC)
    timestamp = observed or reviewed
    if timestamp is not None:
        age = (reference - timestamp.astimezone(UTC)).total_seconds()
        if age < -60.0 or age > float(max_age_seconds):
            status["blocker"] = f"{role}_fee_evidence_stale"
            return status
    evidence_venue = str(evidence.get("venue") or "").strip().lower()
    if not evidence_venue or (venue and evidence_venue != venue):
        status["blocker"] = f"{role}_fee_venue_mismatch"
        return status
    evidence_role = str(evidence.get("liquidity_role") or "").strip().lower()
    if evidence_role != role:
        status["blocker"] = f"{role}_fee_role_mismatch"
        return status
    market_environment = str(market.get("environment") or "mainnet").strip().lower()
    evidence_environment = str(evidence.get("environment") or "").strip().lower()
    if not evidence_environment or evidence_environment != market_environment:
        status["blocker"] = f"{role}_fee_environment_mismatch"
        return status
    market_product = str(
        market.get("product_type")
        or market.get("market_type")
        or market.get("api_product_type")
        or "linear_perpetual"
    ).strip().lower()
    evidence_product = str(
        evidence.get("product_type")
        or evidence.get("market_type")
        or evidence.get("product")
        or ""
    ).strip().lower()
    if not evidence_product or evidence_product != market_product:
        status["blocker"] = f"{role}_fee_product_mismatch"
        return status
    applicability = evidence.get("applicability") or evidence.get("liquidity_role")
    if applicability not in (None, "", "all", role):
        if isinstance(applicability, (list, tuple, set)):
            if role not in {str(item).lower() for item in applicability}:
                status["blocker"] = f"{role}_fee_applicability_mismatch"
                return status
        else:
            status["blocker"] = f"{role}_fee_applicability_mismatch"
            return status

    status["verified"] = True
    status["blocker"] = None
    return status


def verified_vip1_fee_profile(venue: str) -> dict[str, Any] | None:
    profile = VERIFIED_VIP1_FEE_PROFILES.get(str(venue or "").lower())
    return dict(profile) if profile else None


def fee_override(venue: str, role: str) -> float | None:
    direct_name = f"FUNDING_{venue.upper()}_{role.upper()}_FEE"
    direct = os.getenv(direct_name)
    if direct not in {None, ""}:
        return parsed_fee(direct)

    raw = os.getenv("FUNDING_FEE_OVERRIDES_JSON", "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    venue_payload = payload.get(venue)
    if not isinstance(venue_payload, dict):
        return None
    return parsed_fee(venue_payload.get(role))


def parsed_fee(value: Any) -> float | None:
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    return max(-0.001, min(rate, 0.02))
