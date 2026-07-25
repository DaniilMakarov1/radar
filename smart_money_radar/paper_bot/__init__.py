"""Paper Bot — funding carry arbitrage paper trading bot.

Re-exports from ``funding.trader`` for backward compatibility.
The canonical import is ``from smart_money_radar.paper_bot import PaperBot``.
"""
from __future__ import annotations

from smart_money_radar.funding.trader import (
    PaperBot,
    PaperBotConfig,
    export_funding_paper_csv,
)

# Backward-compatible aliases
FundingPaperTrader = PaperBot
FundingPaperTraderConfig = PaperBotConfig

__all__ = [
    "PaperBot",
    "PaperBotConfig",
    "FundingPaperTrader",
    "FundingPaperTraderConfig",
    "export_funding_paper_csv",
]