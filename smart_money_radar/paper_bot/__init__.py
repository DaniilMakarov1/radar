"""Paper Bot — funding carry arbitrage paper trading bot.

Canonical imports::

    from smart_money_radar.paper_bot import PaperBot, PaperBotConfig
    from smart_money_radar.paper_bot.helpers import tg, format_money, ...
    from smart_money_radar.paper_bot.position import close_decision, ...
    from smart_money_radar.paper_bot.telegram import armed_message, ...
"""
from __future__ import annotations

from smart_money_radar.paper_bot.helpers import (  # noqa: F401
    csv_value,
    format_datetime_utc,
    format_interval_hours,
    format_money,
    format_rate,
    format_seconds,
    format_signed_money,
    is_retention_skip_error,
    leg_by_side,
    optional_float,
    parse_iso,
    route_data_age_seconds,
    route_entry_key,
    route_settlement_leads,
    should_record_routine_scan,
    tg,
)
from smart_money_radar.paper_bot.position import (  # noqa: F401
    build_close_payload,
    build_position_from_route,
    build_settlement_accrual_payload,
    close_decision,
    close_reason_from_hold_reasons,
    compute_price_move_snapshot,
    compute_spread_snapshot,
    current_position_leg,
    entry_cross_spread,
    final_recheck_fallback_route,
    final_recheck_freeze_window_active,
    funding_leg_pnl,
    leg_vwap,
    position_hold_decision,
    required_live_net_profit,
    route_entry_decision,
    route_monitor_decision,
    settlement_rate_or_entry,
    settlement_rates_for_position,
    settlement_payload,
    price_stop_loss_triggered,
    spread_stop_loss_triggered,
)
from smart_money_radar.paper_bot.telegram import (  # noqa: F401
    armed_message,
    close_decision_details_message,
    close_message,
    close_reason_message,
    close_window_message,
    disarmed_message,
    funding_leg_compact_line,
    funding_rate_lines,
    funding_settlement_mismatch_line,
    hold_message,
    hold_reason_labels,
    lead_seconds,
    open_message,
    pending_message,
    position_summary,
    reprice_message,
    skipped_open_message,
    status_report_message,
    status_route_line,
)


def __getattr__(name: str):
    """Lazy import for PaperBot/PaperBotConfig to avoid circular imports."""
    if name in ("PaperBot", "PaperBotConfig", "FundingPaperTrader",
                "FundingPaperTraderConfig", "export_funding_paper_csv"):
        from smart_money_radar.funding.trader import (  # noqa: F811
            PaperBot,
            PaperBotConfig,
            export_funding_paper_csv,
        )
        _map = {
            "PaperBot": PaperBot,
            "PaperBotConfig": PaperBotConfig,
            "FundingPaperTrader": PaperBot,
            "FundingPaperTraderConfig": PaperBotConfig,
            "export_funding_paper_csv": export_funding_paper_csv,
        }
        return _map[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "PaperBot",
    "PaperBotConfig",
    "FundingPaperTrader",
    "FundingPaperTraderConfig",
    "export_funding_paper_csv",
]
