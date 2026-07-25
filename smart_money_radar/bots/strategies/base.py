from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Opportunity:
    """A tradeable opportunity discovered by a strategy scan."""

    strategy: str
    canonical_asset: str
    primary_side: str  # long | short (side on the primary/target venue)
    hedge_venue: str
    hedge_side: str  # long | short
    notional: float
    spread_bps: float
    net_profit: float
    total_cost: float
    gross_profit: float
    primary_price: float
    hedge_price: float
    primary_funding_rate: float
    hedge_funding_rate: float
    primary_symbol: str = ""
    hedge_symbol: str = ""
    settlement_at: str | None = None
    confidence_score: float = 0.0
    blocking_reasons: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def primary_venue(self) -> str:
        return str(self.extra.get("primary_venue", ""))


class Strategy(Protocol):
    """Protocol for pluggable bot strategies."""

    name: str

    def scan(self, observed_at: str) -> list[Opportunity]:
        """Scan for opportunities at the given timestamp."""
        ...
