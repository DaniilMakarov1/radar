from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from smart_money_radar.funding.adapters.base import as_float


@dataclass
class RiseXFarmingConfig:
    """Configuration for RiseX volume farming strategy."""

    target_notional_usd: float = 10_000.0
    cycles_per_day: int = 12
    hedge_venue: str = "binance"
    risex_fee_rate: float = 0.0001  # Tier 1 maker: 1 bp
    hedge_fee_rate: float = 0.0002  # Typical CEX taker: 2 bp
    point_boost_pct: float = 20.0  # Referral link boost
    leaderboard_prize_usd: float = 20_000.0
    leaderboard_total_notional_usd: float = 200_000_000.0
    funding_rate_hourly: float = 0.0000125  # ~0.01% per 8h default
    paper_mode: bool = True


def estimate_farming_economics(
    config: RiseXFarmingConfig | None = None,
) -> dict[str, Any]:
    """Estimate daily/weekly/monthly economics for volume farming.

    Returns a breakdown of costs, expected leaderboard rewards,
    funding carry income, and net P&L.
    """
    cfg = config or RiseXFarmingConfig()

    daily_volume = cfg.target_notional_usd * cfg.cycles_per_day
    weekly_volume = daily_volume * 7
    monthly_volume = daily_volume * 30

    # Fee costs per side (open + close = 2 sides)
    risex_fee_per_cycle = cfg.target_notional_usd * cfg.risex_fee_rate * 2
    hedge_fee_per_cycle = cfg.target_notional_usd * cfg.hedge_fee_rate * 2
    daily_fees = (risex_fee_per_cycle + hedge_fee_per_cycle) * cfg.cycles_per_day
    weekly_fees = daily_fees * 7
    monthly_fees = daily_fees * 30

    # Leaderboard reward (pro-rata share of prize pool by volume)
    volume_share = (
        weekly_volume / cfg.leaderboard_total_notional_usd
        if cfg.leaderboard_total_notional_usd > 0
        else 0.0
    )
    weekly_leaderboard_reward = cfg.leaderboard_prize_usd * volume_share

    # Funding carry income (holding delta-neutral position)
    # Average position held = target_notional (always in market)
    daily_funding_income = (
        cfg.target_notional_usd * cfg.funding_rate_hourly * 24
    )
    weekly_funding_income = daily_funding_income * 7
    monthly_funding_income = daily_funding_income * 30

    # Points estimate (200K pool/week, share by activity)
    # Activity factors: volume, fees, OI, hold time
    # Rough estimate: our share of total activity
    weekly_points_pool = 200_000
    estimated_points_share = volume_share  # Simplified: proportional to volume
    weekly_points = weekly_points_pool * estimated_points_share
    boosted_points = weekly_points * (1 + cfg.point_boost_pct / 100)

    # Net P&L
    weekly_net = weekly_leaderboard_reward + weekly_funding_income - weekly_fees
    monthly_net = monthly_fees * 0  # placeholder
    monthly_net = (
        (cfg.leaderboard_prize_usd * 4.33 * volume_share)
        + monthly_funding_income
        - monthly_fees
    )

    return {
        "config": {
            "target_notional_usd": cfg.target_notional_usd,
            "cycles_per_day": cfg.cycles_per_day,
            "hedge_venue": cfg.hedge_venue,
            "risex_fee_rate_bps": cfg.risex_fee_rate * 10_000,
            "hedge_fee_rate_bps": cfg.hedge_fee_rate * 10_000,
            "point_boost_pct": cfg.point_boost_pct,
            "paper_mode": cfg.paper_mode,
        },
        "volume": {
            "daily_usd": daily_volume,
            "weekly_usd": weekly_volume,
            "monthly_usd": monthly_volume,
        },
        "costs": {
            "daily_fees_usd": daily_fees,
            "weekly_fees_usd": weekly_fees,
            "monthly_fees_usd": monthly_fees,
            "risex_fee_per_cycle_usd": risex_fee_per_cycle,
            "hedge_fee_per_cycle_usd": hedge_fee_per_cycle,
        },
        "revenue": {
            "weekly_leaderboard_usd": weekly_leaderboard_reward,
            "weekly_funding_usd": weekly_funding_income,
            "monthly_funding_usd": monthly_funding_income,
            "weekly_points_est": boosted_points,
            "volume_share_pct": volume_share * 100,
        },
        "net_pnl": {
            "weekly_usd": weekly_net,
            "monthly_usd": monthly_net,
        },
        "breakeven": {
            "min_volume_share_pct": (
                weekly_fees / cfg.leaderboard_prize_usd * 100
                if cfg.leaderboard_prize_usd > 0
                else 0.0
            ),
            "min_weekly_volume_usd": (
                weekly_fees
                / cfg.leaderboard_prize_usd
                * cfg.leaderboard_total_notional_usd
                if cfg.leaderboard_prize_usd > 0
                else 0.0
            ),
        },
    }


def paper_farming_cycle(
    config: RiseXFarmingConfig | None = None,
    cycle_number: int = 1,
) -> dict[str, Any]:
    """Simulate one paper farming cycle (open + close on RiseX, hedge on CEX).

    Returns a paper trade record for audit/tracking.
    """
    cfg = config or RiseXFarmingConfig()

    risex_fee = cfg.target_notional_usd * cfg.risex_fee_rate * 2
    hedge_fee = cfg.target_notional_usd * cfg.hedge_fee_rate * 2
    total_fees = risex_fee + hedge_fee

    # Funding earned during hold period (assume ~2h between cycles)
    hold_hours = 24.0 / max(1, cfg.cycles_per_day)
    funding_earned = cfg.target_notional_usd * cfg.funding_rate_hourly * hold_hours

    net_pnl = funding_earned - total_fees

    return {
        "cycle": cycle_number,
        "mode": "paper",
        "risex_side": "open_long_close_long",
        "hedge_side": f"open_short_close_short_on_{cfg.hedge_venue}",
        "notional_usd": cfg.target_notional_usd,
        "risex_fee_usd": risex_fee,
        "hedge_fee_usd": hedge_fee,
        "total_fees_usd": total_fees,
        "hold_hours": hold_hours,
        "funding_earned_usd": funding_earned,
        "net_pnl_usd": net_pnl,
        "volume_generated_usd": cfg.target_notional_usd * 2,  # open + close
        "points_factors": {
            "volume": cfg.target_notional_usd * 2,
            "fees_paid": total_fees,
            "oi_held": cfg.target_notional_usd,
            "hold_time_hours": hold_hours,
        },
    }
