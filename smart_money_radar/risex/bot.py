"""RiseX paper trading bot — thin wrapper over FundingBotBase.

All position management, armed logic, messaging, and status reporting
lives in ``bots/funding_base.py``.  This module only defines the RiseX
profile and a backward-compatible ``RiseXBotConfig`` for the CLI.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from smart_money_radar.bots.funding_base import (
    FundingBotBase,
    PaperPosition,
    strategy_label,
)
from smart_money_radar.bots.funding_profile import FundingBotProfile
from smart_money_radar.bots.funding_entry import FundingEntryConfig
from smart_money_radar.funding.models import FundingScanConfig
from smart_money_radar.notifications import TelegramNotifier
from smart_money_radar.storage import SQLiteStore


RISEX_BOT_MODEL_VERSION = "risex_paper_v2"
RISEX_VENUE = "risex"
DEX_HEDGE_VENUES = ("hyperliquid", "dydx", "lighter", "variational")


# ---------------------------------------------------------------------------
# Backward-compatible config (CLI still constructs this)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RiseXBotConfig:
    venue_starting_balance: float = 2_000.0
    target_notional_per_leg: float = 500.0
    scan_interval_seconds: int = 180
    monitor_interval_seconds: int = 30
    status_report_interval_seconds: int = 900
    funding_carry_enabled: bool = True
    spread_arb_enabled: bool = True
    hedge_venues: tuple[str, ...] = DEX_HEDGE_VENUES
    iterations: int | None = None
    telegram_enabled: bool = True
    entry_window_seconds: int = 180
    entry_min_lead_seconds: int = 0
    entry_max_lead_seconds: int = 15
    arm_window_seconds: int = 900
    settlement_grace_seconds: int = 90
    collateral_reserve_fraction: float = 0.10
    min_live_net_profit: float = 0.0

    def validated(self) -> RiseXBotConfig:
        entry = FundingEntryConfig(
            entry_window_seconds=self.entry_window_seconds,
            entry_min_lead_seconds=self.entry_min_lead_seconds,
            entry_max_lead_seconds=self.entry_max_lead_seconds,
            arm_window_seconds=self.arm_window_seconds,
            settlement_grace_seconds=self.settlement_grace_seconds,
            collateral_reserve_fraction=self.collateral_reserve_fraction,
            min_live_net_profit=self.min_live_net_profit,
        ).validated()
        return RiseXBotConfig(
            venue_starting_balance=max(100.0, self.venue_starting_balance),
            target_notional_per_leg=max(50.0, self.target_notional_per_leg),
            scan_interval_seconds=max(10, self.scan_interval_seconds),
            monitor_interval_seconds=max(5, self.monitor_interval_seconds),
            status_report_interval_seconds=max(60, self.status_report_interval_seconds),
            funding_carry_enabled=self.funding_carry_enabled,
            spread_arb_enabled=self.spread_arb_enabled,
            hedge_venues=self.hedge_venues,
            iterations=self.iterations,
            telegram_enabled=self.telegram_enabled,
            entry_window_seconds=entry.entry_window_seconds,
            entry_min_lead_seconds=entry.entry_min_lead_seconds,
            entry_max_lead_seconds=entry.entry_max_lead_seconds,
            arm_window_seconds=entry.arm_window_seconds,
            settlement_grace_seconds=entry.settlement_grace_seconds,
            collateral_reserve_fraction=entry.collateral_reserve_fraction,
            min_live_net_profit=entry.min_live_net_profit,
        )

    def scan_config(self) -> FundingScanConfig:
        return FundingScanConfig(
            target_notional=self.target_notional_per_leg,
            horizon_mode="next_settlement",
            near_miss_full_depth_routes=0,
            history_refresh_hours=0,
            max_live_history_markets=0,
            minimum_net_profit=self.min_live_net_profit,
        ).validated()

    def to_profile(self) -> FundingBotProfile:
        return FundingBotProfile(
            name="RiseX",
            model_version=RISEX_BOT_MODEL_VERSION,
            venues=(RISEX_VENUE, *self.hedge_venues),
            primary_venue=RISEX_VENUE,
            funding_carry_enabled=self.funding_carry_enabled,
            spread_arb_enabled=self.spread_arb_enabled,
            scan_interval_seconds=self.scan_interval_seconds,
            monitor_interval_seconds=self.monitor_interval_seconds,
            status_report_interval_seconds=self.status_report_interval_seconds,
            venue_starting_balance=self.venue_starting_balance,
            target_notional_per_leg=self.target_notional_per_leg,
            minimum_net_profit=self.min_live_net_profit,
            horizon_mode="next_settlement",
            entry_window_seconds=self.entry_window_seconds,
            entry_min_lead_seconds=self.entry_min_lead_seconds,
            entry_max_lead_seconds=self.entry_max_lead_seconds,
            arm_window_seconds=self.arm_window_seconds,
            settlement_grace_seconds=self.settlement_grace_seconds,
            collateral_reserve_fraction=self.collateral_reserve_fraction,
            telegram_token_env="RISEX_TELEGRAM_BOT_TOKEN",
            telegram_chat_id_env="RISEX_TELEGRAM_CHAT_ID",
            telegram_enabled=self.telegram_enabled,
            iterations=self.iterations,
        )

    @property
    def all_venues(self) -> tuple[str, ...]:
        return (RISEX_VENUE, *self.hedge_venues)

    @property
    def total_starting_balance(self) -> float:
        return self.venue_starting_balance * len(self.all_venues)


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class RiseXBot(FundingBotBase):
    """RiseX paper trading bot.

    Accepts either a ``RiseXBotConfig`` (backward-compatible) or a
    ``FundingBotProfile`` directly.
    """

    def __init__(
        self,
        store: SQLiteStore,
        config: RiseXBotConfig | None = None,
        profile: FundingBotProfile | None = None,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        if profile is None:
            cfg = (config or RiseXBotConfig()).validated()
            profile = cfg.to_profile()
        super().__init__(store, profile, notifier=notifier)

    @staticmethod
    def default_profile(**overrides: Any) -> FundingBotProfile:
        """Create a RiseX profile with optional overrides."""
        defaults = RiseXBotConfig().validated().to_profile()
        if not overrides:
            return defaults
        from dataclasses import replace
        return replace(defaults, **overrides)


__all__ = [
    "PaperPosition",
    "RiseXBot",
    "RiseXBotConfig",
    "RISEX_BOT_MODEL_VERSION",
    "RISEX_VENUE",
    "DEX_HEDGE_VENUES",
    "strategy_label",
]
