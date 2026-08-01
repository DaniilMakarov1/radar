from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime, timedelta
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

ACCOUNT_FEE_EVIDENCE_MAX_AGE_SECONDS = 24.0 * 60.0 * 60.0
PUBLIC_FEE_ENDPOINT_MAX_AGE_SECONDS = 7.0 * 24.0 * 60.0 * 60.0
REVIEWED_STATIC_FEE_MAX_AGE_SECONDS = 30.0 * 24.0 * 60.0 * 60.0

FEE_EVIDENCE_KIND_ACCOUNT_ENDPOINT = "ACCOUNT_ENDPOINT"
FEE_EVIDENCE_KIND_PUBLIC_FEE_ENDPOINT = "PUBLIC_FEE_ENDPOINT"
FEE_EVIDENCE_KIND_REVIEWED_STATIC_SCHEDULE = "REVIEWED_STATIC_SCHEDULE"
FEE_EVIDENCE_KIND_UNVERIFIED_MARKET_FIELD = "UNVERIFIED_MARKET_FIELD"
FEE_EVIDENCE_KIND_CONSERVATIVE_FALLBACK = "CONSERVATIVE_FALLBACK"

SUPPORTED_FEE_EVIDENCE_KINDS = {
    FEE_EVIDENCE_KIND_ACCOUNT_ENDPOINT,
    FEE_EVIDENCE_KIND_PUBLIC_FEE_ENDPOINT,
    FEE_EVIDENCE_KIND_REVIEWED_STATIC_SCHEDULE,
    FEE_EVIDENCE_KIND_UNVERIFIED_MARKET_FIELD,
    FEE_EVIDENCE_KIND_CONSERVATIVE_FALLBACK,
}

TRUSTED_FEE_SOURCE_KINDS = {
    "account_api",
    "account_fee_endpoint",
    "official_account_fee_endpoint",
    "official_public_fee_endpoint",
    "public_fee_endpoint",
    "reviewed_static_schedule",
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
    override_status = fee_override_status(venue, role)
    if override_status.get("present"):
        if override_status.get("valid"):
            return float(override_status["rate"])
        return math.nan
    field = f"{role}_fee_rate"
    raw = market.get(field)
    if raw is None and role == "taker":
        raw = market.get("fee_rate")
    defaults = (
        DEFAULT_MAKER_FEE_RATES if role == "maker" else DEFAULT_TAKER_FEE_RATES
    )
    if raw is not None:
        parsed = parsed_fee_for_role(raw, role)
        if parsed is None:
            return math.nan
        return parsed
    try:
        value = defaults.get(venue, 0.0006)
    except (TypeError, ValueError):
        value = defaults.get(venue, 0.0006)
    # Maker rebates are valid and should remain negative; taker fees cannot be.
    return max(-0.001, value) if role == "maker" else max(0.0, value)


def funding_fee_source(market: dict[str, Any], liquidity_role: str = "taker") -> str:
    role = "maker" if liquidity_role == "maker" else "taker"
    venue = str(market.get("venue") or "").lower()
    override_status = fee_override_status(venue, role)
    if override_status.get("present") and override_status.get("valid"):
        return "account_override"
    if override_status.get("present"):
        return "invalid_account_override"
    if market.get(f"{role}_fee_rate") is not None:
        return str(market.get("fee_source") or "venue_public_tier")
    return "public_default"


def fee_rate_value(
    market: dict[str, Any],
    liquidity_role: str = "taker",
) -> float | None:
    role = "maker" if liquidity_role == "maker" else "taker"
    venue = str(market.get("venue") or "").lower()
    override_status = fee_override_status(venue, role)
    if override_status.get("present"):
        return float(override_status["rate"]) if override_status.get("valid") else None
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


def _timestamp_iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _normalize_evidence_kind(evidence: dict[str, Any], source_kind: str) -> str:
    raw = str(
        evidence.get("fee_evidence_kind")
        or evidence.get("evidence_kind")
        or evidence.get("kind")
        or ""
    ).strip()
    if raw:
        normalized = raw.upper()
        if normalized in SUPPORTED_FEE_EVIDENCE_KINDS:
            return normalized
    source = str(source_kind or "").strip().lower()
    if source in {"account_api", "account_fee_endpoint", "official_account_fee_endpoint"}:
        return FEE_EVIDENCE_KIND_ACCOUNT_ENDPOINT
    if source == "public_fee_endpoint":
        return FEE_EVIDENCE_KIND_PUBLIC_FEE_ENDPOINT
    if source == "official_public_fee_endpoint":
        if any(
            evidence.get(key) not in (None, "")
            for key in (
                "fee_source_observed_at",
                "source_observed_at",
                "endpoint_observed_at",
            )
        ):
            return FEE_EVIDENCE_KIND_PUBLIC_FEE_ENDPOINT
        return FEE_EVIDENCE_KIND_REVIEWED_STATIC_SCHEDULE
    if source in {
        "configured_trusted_fee",
        "trusted_config",
        "versioned_trusted_config",
        "reviewed_static_schedule",
        "account_override",
    }:
        return FEE_EVIDENCE_KIND_REVIEWED_STATIC_SCHEDULE
    return FEE_EVIDENCE_KIND_UNVERIFIED_MARKET_FIELD


def _max_age_for_kind(
    kind: str,
    *,
    max_age_seconds: float | None,
    account_max_age_seconds: float | None,
    public_endpoint_max_age_seconds: float | None,
    reviewed_static_max_age_seconds: float | None,
) -> float:
    if kind == FEE_EVIDENCE_KIND_ACCOUNT_ENDPOINT:
        return float(
            account_max_age_seconds
            if account_max_age_seconds is not None
            else max_age_seconds
            if max_age_seconds is not None
            else ACCOUNT_FEE_EVIDENCE_MAX_AGE_SECONDS
        )
    if kind == FEE_EVIDENCE_KIND_PUBLIC_FEE_ENDPOINT:
        return float(
            public_endpoint_max_age_seconds
            if public_endpoint_max_age_seconds is not None
            else max_age_seconds
            if max_age_seconds is not None
            else PUBLIC_FEE_ENDPOINT_MAX_AGE_SECONDS
        )
    return float(
        reviewed_static_max_age_seconds
        if reviewed_static_max_age_seconds is not None
        else max_age_seconds
        if max_age_seconds is not None
        else REVIEWED_STATIC_FEE_MAX_AGE_SECONDS
    )


def _fee_evidence_timestamps(
    market: dict[str, Any],
    evidence: dict[str, Any],
    kind: str,
) -> dict[str, datetime | None]:
    market_observed_at = _parse_timestamp(
        evidence.get("market_observed_at")
        or market.get("market_observed_at")
        or market.get("observed_at")
        or market.get("market_response_received_at")
        or market.get("response_received_at")
    )
    fee_source_observed_at = _parse_timestamp(
        evidence.get("fee_source_observed_at")
        or evidence.get("source_observed_at")
        or evidence.get("endpoint_observed_at")
        or market.get("fee_source_observed_at")
    )
    account_fee_observed_at = _parse_timestamp(
        evidence.get("account_fee_observed_at")
        or evidence.get("account_observed_at")
        or market.get("account_fee_observed_at")
    )
    fee_schedule_reviewed_at = _parse_timestamp(
        evidence.get("fee_schedule_reviewed_at")
        or evidence.get("reviewed_at")
        or market.get("fee_schedule_reviewed_at")
        or market.get("fee_reviewed_at")
    )
    legacy_observed = _parse_timestamp(evidence.get("observed_at") or market.get("fee_observed_at"))
    if kind == FEE_EVIDENCE_KIND_ACCOUNT_ENDPOINT:
        if account_fee_observed_at is None:
            account_fee_observed_at = legacy_observed or fee_source_observed_at
        selected = account_fee_observed_at
    elif kind == FEE_EVIDENCE_KIND_PUBLIC_FEE_ENDPOINT:
        if fee_source_observed_at is None:
            fee_source_observed_at = legacy_observed
        selected = fee_source_observed_at
    elif kind == FEE_EVIDENCE_KIND_REVIEWED_STATIC_SCHEDULE:
        selected = fee_schedule_reviewed_at
    else:
        selected = legacy_observed
    return {
        "market_observed_at": market_observed_at,
        "fee_source_observed_at": fee_source_observed_at,
        "account_fee_observed_at": account_fee_observed_at,
        "fee_schedule_reviewed_at": fee_schedule_reviewed_at,
        "selected_evidence_at": selected,
    }


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
    max_age_seconds: float | None = None,
    account_max_age_seconds: float | None = None,
    public_endpoint_max_age_seconds: float | None = None,
    reviewed_static_max_age_seconds: float | None = None,
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
        "fee_evidence_kind": None,
        "market_observed_at": None,
        "fee_source_observed_at": None,
        "fee_schedule_reviewed_at": None,
        "account_fee_observed_at": None,
        "observed_at": None,
        "reviewed_at": None,
        "age_seconds": None,
        "max_age_seconds": None,
        "expires_at": None,
        "fallback_required": True,
        "uncertainty_reserve_required": True,
        "fee_scope": None,
        "account_applicability": None,
        "account_applicable": False,
        "conservative_worst_case": False,
        "blocker": None,
    }
    override_status = fee_override_status(venue, role)
    if override_status.get("present"):
        status["override_source"] = override_status.get("source")
        status["override_raw_value"] = override_status.get("raw_value")
        if not override_status.get("valid"):
            status["rate"] = override_status.get("raw_value")
            status["trust_status"] = "INVALID"
            status["fee_evidence_kind"] = FEE_EVIDENCE_KIND_UNVERIFIED_MARKET_FIELD
            status["blocker"] = str(
                override_status.get("blocker") or f"{role}_fee_override_invalid"
            )
            return status
        status["rate"] = float(override_status["rate"])
    if rate is None:
        status["fee_evidence_kind"] = (
            FEE_EVIDENCE_KIND_UNVERIFIED_MARKET_FIELD
            if raw_rate is not None
            else FEE_EVIDENCE_KIND_CONSERVATIVE_FALLBACK
        )
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
    evidence_kind = _normalize_evidence_kind(evidence, source_kind)
    timestamps = _fee_evidence_timestamps(market, evidence, evidence_kind)
    observed = timestamps["selected_evidence_at"]
    reviewed = timestamps["fee_schedule_reviewed_at"]

    source = str(market.get("fee_source") or "").strip().lower()
    if not evidence:
        status["trust_status"] = "UNVERIFIED"
        status["source_identifier"] = source or None
        status["fee_evidence_kind"] = (
            FEE_EVIDENCE_KIND_UNVERIFIED_MARKET_FIELD
            if rate is not None
            else FEE_EVIDENCE_KIND_CONSERVATIVE_FALLBACK
        )
        status["blocker"] = f"{role}_fee_evidence_missing"
        return status

    status.update(
        {
            "source_kind": source_kind or None,
            "source_identifier": source_identifier or None,
            "fee_evidence_kind": evidence_kind,
            "market_observed_at": _timestamp_iso(timestamps["market_observed_at"]),
            "fee_source_observed_at": _timestamp_iso(timestamps["fee_source_observed_at"]),
            "fee_schedule_reviewed_at": _timestamp_iso(timestamps["fee_schedule_reviewed_at"]),
            "account_fee_observed_at": _timestamp_iso(timestamps["account_fee_observed_at"]),
            "observed_at": _timestamp_iso(observed),
            "reviewed_at": _timestamp_iso(reviewed),
            "trust_status": trust_status or "UNKNOWN",
            "fee_scope": evidence.get("fee_scope"),
            "account_applicability": evidence.get("account_applicability"),
            "account_applicable": bool(evidence.get("account_applicable")),
            "conservative_worst_case": bool(evidence.get("conservative_worst_case")),
        }
    )

    if evidence_kind not in SUPPORTED_FEE_EVIDENCE_KINDS:
        status["blocker"] = f"{role}_fee_evidence_kind_unsupported"
        return status
    if evidence_kind in {
        FEE_EVIDENCE_KIND_UNVERIFIED_MARKET_FIELD,
        FEE_EVIDENCE_KIND_CONSERVATIVE_FALLBACK,
    }:
        status["trust_status"] = trust_status or "UNVERIFIED"
        status["blocker"] = f"{role}_fee_source_untrusted"
        return status
    if source_kind not in TRUSTED_FEE_SOURCE_KINDS:
        status["trust_status"] = trust_status or "UNVERIFIED"
        status["blocker"] = f"{role}_fee_source_untrusted"
        return status
    if not source_identifier:
        status["blocker"] = f"{role}_fee_source_identifier_missing"
        return status
    if evidence_kind == FEE_EVIDENCE_KIND_PUBLIC_FEE_ENDPOINT:
        fee_scope = str(evidence.get("fee_scope") or "").strip().lower()
        account_applicability = str(
            evidence.get("account_applicability") or ""
        ).strip().lower()
        account_applicable = bool(evidence.get("account_applicable")) or account_applicability in {
            "account_specific",
            "account_verified",
        }
        conservative_worst_case = bool(evidence.get("conservative_worst_case")) or fee_scope in {
            "public_worst_case_schedule",
            "reviewed_static_worst_case_schedule",
        } or account_applicability in {
            "public_worst_case_schedule",
            "conservative_worst_case_all_accounts",
        }
        status["account_applicable"] = account_applicable
        status["conservative_worst_case"] = conservative_worst_case
        if not account_applicable and not conservative_worst_case:
            status["verified"] = False
            status["trust_status"] = "UNVERIFIED"
            status["blocker"] = f"{role}_fee_account_applicability_unverified"
            return status
    if trust_status not in TRUSTED_FEE_STATUSES:
        status["trust_status"] = trust_status or "UNVERIFIED"
        status["blocker"] = f"{role}_fee_trust_status_unverified"
        return status
    evidence_version = evidence.get("evidence_version") or evidence.get("schema_version")
    if not evidence_version:
        status["blocker"] = f"{role}_fee_evidence_version_missing"
        return status
    if observed is None:
        status["blocker"] = f"{role}_fee_timestamp_missing"
        return status
    reference = now.astimezone(UTC) if now else datetime.now(UTC)
    max_age = _max_age_for_kind(
        evidence_kind,
        max_age_seconds=max_age_seconds,
        account_max_age_seconds=account_max_age_seconds,
        public_endpoint_max_age_seconds=public_endpoint_max_age_seconds,
        reviewed_static_max_age_seconds=reviewed_static_max_age_seconds,
    )
    age = (reference - observed.astimezone(UTC)).total_seconds()
    expires = observed.astimezone(UTC) + timedelta(seconds=max_age)
    status["age_seconds"] = age
    status["max_age_seconds"] = max_age
    status["expires_at"] = expires.isoformat()
    if age < -60.0 or age > max_age:
        status["stale_kind"] = evidence_kind
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
    status["fallback_required"] = False
    status["uncertainty_reserve_required"] = False
    status["blocker"] = None
    return status


def verified_vip1_fee_profile(venue: str) -> dict[str, Any] | None:
    profile = VERIFIED_VIP1_FEE_PROFILES.get(str(venue or "").lower())
    return dict(profile) if profile else None


def fee_override(venue: str, role: str) -> float | None:
    status = fee_override_status(venue, role)
    return float(status["rate"]) if status.get("present") and status.get("valid") else None


def fee_override_status(venue: str, role: str) -> dict[str, Any]:
    role = "maker" if role == "maker" else "taker"
    venue = str(venue or "").lower()
    direct_name = f"FUNDING_{venue.upper()}_{role.upper()}_FEE"
    direct = os.getenv(direct_name)
    if direct not in {None, ""}:
        parsed = parsed_fee_for_role(direct, role)
        if parsed is None:
            return {
                "present": True,
                "valid": False,
                "source": direct_name,
                "raw_value": direct,
                "blocker": f"{role}_fee_override_invalid",
            }
        return {
            "present": True,
            "valid": True,
            "source": direct_name,
            "raw_value": direct,
            "rate": parsed,
        }

    raw = os.getenv("FUNDING_FEE_OVERRIDES_JSON", "").strip()
    if not raw:
        return {"present": False, "valid": False}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "present": True,
            "valid": False,
            "source": "FUNDING_FEE_OVERRIDES_JSON",
            "raw_value": raw,
            "blocker": "funding_fee_overrides_json_malformed",
        }
    if not isinstance(payload, dict):
        return {
            "present": True,
            "valid": False,
            "source": "FUNDING_FEE_OVERRIDES_JSON",
            "raw_value": raw,
            "blocker": "funding_fee_overrides_json_invalid",
        }
    venue_payload = payload.get(venue)
    if not isinstance(venue_payload, dict):
        return {"present": False, "valid": False}
    if role not in venue_payload:
        return {"present": False, "valid": False}
    parsed = parsed_fee_for_role(venue_payload.get(role), role)
    if parsed is None:
        return {
            "present": True,
            "valid": False,
            "source": "FUNDING_FEE_OVERRIDES_JSON",
            "raw_value": venue_payload.get(role),
            "blocker": f"{role}_fee_override_invalid",
        }
    return {
        "present": True,
        "valid": True,
        "source": "FUNDING_FEE_OVERRIDES_JSON",
        "raw_value": venue_payload.get(role),
        "rate": parsed,
    }


def parsed_fee(value: Any) -> float | None:
    return parsed_fee_for_role(value, "maker")


def parsed_fee_for_role(value: Any, role: str) -> float | None:
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(rate):
        return None
    if role == "maker":
        return rate if -0.001 <= rate <= 0.02 else None
    return rate if 0.0 <= rate <= 0.02 else None
