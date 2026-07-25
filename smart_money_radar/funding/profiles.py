from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FundingBotProfile:
    name: str
    description: str
    venue_set: tuple[str, ...] | None = None
    required_venues: tuple[str, ...] = ()
    strategy_set: tuple[str, ...] = ("funding_carry",)
    metadata: dict[str, Any] | None = None


BUILTIN_FUNDING_BOT_PROFILES: dict[str, FundingBotProfile] = {
    "default": FundingBotProfile(
        name="default",
        description="All active funding venues with standard paper trading gates.",
    ),
    "core_cex": FundingBotProfile(
        name="core_cex",
        description="Major CEX-only research profile for cleaner venue risk.",
        venue_set=(
            "binance",
            "bybit",
            "okx",
            "bitget",
            "gate",
            "htx",
            "kucoin",
            "mexc",
            "kraken",
            "deribit",
        ),
    ),
    "risex_points": FundingBotProfile(
        name="risex_points",
        description=(
            "Points-farming profile shell. It keeps the shared funding/spread "
            "logic and is meant to whitelist RiseX plus hedging venues once a "
            "verified RiseX adapter is available."
        ),
        venue_set=("risex", "binance", "bybit", "okx"),
        required_venues=("risex",),
        strategy_set=("funding_carry", "spread_monitor", "points_farming"),
        metadata={"requires_verified_adapter": "risex"},
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
