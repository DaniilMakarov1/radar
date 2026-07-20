from __future__ import annotations

import json
import os
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
