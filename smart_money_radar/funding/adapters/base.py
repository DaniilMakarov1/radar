from __future__ import annotations

from typing import Any, Protocol

from smart_money_radar.http import RateLimitedHttpClient


class FundingDataError(RuntimeError):
    pass


class FundingVenueClient(Protocol):
    venue: str

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]: ...

    def orderbook(
        self,
        symbol: str,
        observed_at: str,
        limit: int = 100,
    ) -> dict[str, Any]: ...

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]: ...


class FundingHttpClient(RateLimitedHttpClient):
    error_class = FundingDataError
    user_agent = "SmartMoneyRadar-Funding/0.1"


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
