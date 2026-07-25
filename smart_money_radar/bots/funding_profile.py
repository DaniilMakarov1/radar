from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FundingBotProfile:
    """Declarative configuration for a funding-based bot.

    A new bot is created by defining a profile — no code duplication.
    ``FundingBotBase`` reads this profile and wires up venues, strategies,
    scan config, entry timing, and Telegram automatically.
    """

    # Identity
    name: str = "Funding"
    model_version: str = "funding_paper_v1"

    # Venues — which adapters to instantiate.
    # ``primary_venue`` filters funding-carry pairs to routes involving
    # this venue.  ``None`` means all cross-venue pairs are eligible.
    venues: tuple[str, ...] = ()
    primary_venue: str | None = None

    # Strategies
    funding_carry_enabled: bool = True
    spread_arb_enabled: bool = False

    # Timing
    scan_interval_seconds: int = 300
    monitor_interval_seconds: int = 30
    status_report_interval_seconds: int = 3_600

    # Sizing
    venue_starting_balance: float = 2_000.0
    target_notional_per_leg: float = 500.0

    # Scanner overrides (mapped onto FundingScanConfig)
    minimum_net_profit: float = 1.0
    horizon_mode: str = "next_settlement"
    scan_config_overrides: dict[str, Any] = field(default_factory=dict)

    # Spread arb
    spread_arb_max_hold_hours: float = 4.0
    spread_arb_convergence_threshold: float = 0.3
    spread_arb_inversion_threshold: float = 0.5

    # Entry timing (funding carry)
    entry_window_seconds: int = 180
    entry_min_lead_seconds: int = 0
    entry_max_lead_seconds: int = 15
    arm_window_seconds: int = 900
    settlement_grace_seconds: int = 90
    collateral_reserve_fraction: float = 0.10

    # Telegram
    telegram_token_env: str = "TELEGRAM_BOT_TOKEN"
    telegram_chat_id_env: str = "TELEGRAM_CHAT_ID"
    telegram_enabled: bool = True

    # Runtime
    iterations: int | None = None

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------

    @property
    def total_starting_balance(self) -> float:
        return self.venue_starting_balance * max(len(self.venues), 1)

    @property
    def strategy_list(self) -> str:
        parts: list[str] = []
        if self.funding_carry_enabled:
            parts.append("funding_carry")
        if self.spread_arb_enabled:
            parts.append("spread_arb")
        return ", ".join(parts) or "none"
