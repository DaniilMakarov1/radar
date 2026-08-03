from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FundingBotProfile:
    name: str
    description: str
    venue_set: tuple[str, ...] | None = None
    required_venues: tuple[str, ...] = ()
    strategy_set: tuple[str, ...] = (
        "synchronized_funding_capture",
    )
    metadata: dict[str, Any] | None = None


BUILTIN_FUNDING_BOT_PROFILES: dict[str, FundingBotProfile] = {
    "default": FundingBotProfile(
        name="default",
        description="RiseX plus Hyperliquid paper funding routes.",
        venue_set=("risex", "hyperliquid"),
        strategy_set=(
            "single_settlement_hedged_capture",
            "synchronized_funding_capture",
        ),
    ),
    "core_cex": FundingBotProfile(
        name="core_cex",
        description="Major CEX-only synchronized funding profile for cleaner venue risk.",
        venue_set=(
            "binance",
            "bybit",
            "okx",
            "bitget",
            "gate",
            "kucoin",
            "mexc",
            "kraken",
            "deribit",
        ),
    ),
    "risex_points": FundingBotProfile(
        name="risex_points",
        description=(
            "Points-farming profile shell. It keeps the shared funding "
            "logic while whitelisting RiseX plus Hyperliquid."
        ),
        venue_set=("risex", "hyperliquid"),
        strategy_set=(
            "single_settlement_hedged_capture",
            "synchronized_funding_capture",
        ),
        metadata={"purpose": "risex_points_farming_research"},
    ),
    "experimental_spread": FundingBotProfile(
        name="experimental_spread",
        description=(
            "Research-only legacy spread/funding opportunity profile. Not the "
            "default production paper strategy."
        ),
        strategy_set=(
            "funding_only",
            "spread_only",
            "combined",
            "opportunistic_any",
        ),
        metadata={"mode": "experimental_research_only"},
    ),
}


def funding_bot_profile(name: str | None) -> FundingBotProfile:
    key = (name or "default").strip().lower()
    if key not in BUILTIN_FUNDING_BOT_PROFILES:
        allowed = ", ".join(sorted(BUILTIN_FUNDING_BOT_PROFILES))
        raise ValueError(f"Unknown funding bot profile {name!r}. Allowed: {allowed}")
    return BUILTIN_FUNDING_BOT_PROFILES[key]


def funding_bot_profile_names(
    *,
    include_unavailable: bool = False,
) -> tuple[str, ...]:
    profiles = BUILTIN_FUNDING_BOT_PROFILES.values()
    if not include_unavailable:
        profiles = [
            profile
            for profile in profiles
            if not profile.required_venues
        ]
    return tuple(sorted(profile.name for profile in profiles))
