from __future__ import annotations

import urllib.parse
from typing import Any

from smart_money_radar.funding.adapters.base import (
    FundingDataError,
    FundingHttpClient,
    as_float,
)


RISEX_API_URL = "https://api.rise.trade"

LEADERBOARD_TIMEFRAMES = {
    "24h": "LEADERBOARD_TIME_FRAME_24H",
    "7d": "LEADERBOARD_TIME_FRAME_7D",
    "30d": "LEADERBOARD_TIME_FRAME_30D",
    "all": "LEADERBOARD_TIME_FRAME_ALL",
}


class RiseXPointsClient:
    """Public API client for RiseX points, epochs, and leaderboards."""

    def __init__(
        self,
        http: FundingHttpClient | None = None,
        base_url: str = RISEX_API_URL,
    ) -> None:
        self.http = http or FundingHttpClient(min_delay_seconds=0.05)
        self.base_url = base_url.rstrip("/")

    def current_epoch(self) -> dict[str, Any]:
        payload = self.http.get_json(f"{self.base_url}/v1/points/epoch/current")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise FundingDataError("Invalid RiseX epoch response")
        return data

    def wallet_points(self, wallet: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"wallet": wallet})
        payload = self.http.get_json(f"{self.base_url}/v1/points/wallet?{query}")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise FundingDataError(f"Invalid RiseX wallet points for {wallet}")
        return data

    def points_history(self, wallet: str, limit: int = 50) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"wallet": wallet, "limit": limit})
        payload = self.http.get_json(f"{self.base_url}/v1/points/history?{query}")
        data = payload.get("data") if isinstance(payload, dict) else None
        records = data.get("records") if isinstance(data, dict) else None
        if not isinstance(records, list):
            return []
        return [r for r in records if isinstance(r, dict)]

    def volume_leaderboard(
        self,
        timeframe: str = "all",
        limit: int = 100,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        tf = LEADERBOARD_TIMEFRAMES.get(timeframe, LEADERBOARD_TIMEFRAMES["all"])
        query = urllib.parse.urlencode(
            {"timeframe": tf, "limit": min(limit, 500), "page": page}
        )
        payload = self.http.get_json(f"{self.base_url}/v1/leaderboard/volume?{query}")
        data = payload.get("data") if isinstance(payload, dict) else None
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, dict)]

    def pnl_leaderboard(
        self,
        timeframe: str = "all",
        limit: int = 100,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        tf = LEADERBOARD_TIMEFRAMES.get(timeframe, LEADERBOARD_TIMEFRAMES["all"])
        query = urllib.parse.urlencode(
            {"timeframe": tf, "limit": min(limit, 500), "page": page}
        )
        payload = self.http.get_json(f"{self.base_url}/v1/leaderboard/pnl?{query}")
        data = payload.get("data") if isinstance(payload, dict) else None
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, dict)]


def fetch_points_epoch(client: RiseXPointsClient | None = None) -> dict[str, Any]:
    """Fetch current points epoch with human-readable summary."""
    c = client or RiseXPointsClient()
    data = c.current_epoch()
    epoch = data.get("epoch", {})
    return {
        "epoch_id": epoch.get("epoch_id"),
        "description": epoch.get("description"),
        "starts_at": epoch.get("starts_at"),
        "ends_at": epoch.get("ends_at"),
        "distribution_at": epoch.get("distribution_at"),
        "seconds_until_distribution": as_float(
            data.get("seconds_until_distribution")
        ),
    }


def fetch_leaderboard_snapshot(
    client: RiseXPointsClient | None = None,
    timeframe: str = "7d",
    limit: int = 20,
) -> dict[str, Any]:
    """Fetch volume leaderboard and compute summary statistics."""
    c = client or RiseXPointsClient()
    entries = c.volume_leaderboard(timeframe=timeframe, limit=limit)

    notional_volumes = [as_float(e.get("notional_volume")) for e in entries]
    combined_volumes = [as_float(e.get("combined_volume")) for e in entries]
    total_notional = sum(notional_volumes)
    total_combined = sum(combined_volumes)

    rows = []
    for e in entries:
        notional = as_float(e.get("notional_volume"))
        referral = as_float(e.get("referral_volume"))
        combined = as_float(e.get("combined_volume"))
        rows.append(
            {
                "rank": int(as_float(e.get("rank"))),
                "address": str(e.get("address") or ""),
                "notional_volume": notional,
                "referral_volume": referral,
                "combined_volume": combined,
                "trades": int(as_float(e.get("trades"))),
                "notional_share_pct": (
                    notional / total_notional * 100 if total_notional > 0 else 0.0
                ),
            }
        )

    return {
        "timeframe": timeframe,
        "entry_count": len(rows),
        "total_notional_volume": total_notional,
        "total_combined_volume": total_combined,
        "top_notional_volume": notional_volumes[0] if notional_volumes else 0.0,
        "median_notional_volume": (
            sorted(notional_volumes)[len(notional_volumes) // 2]
            if notional_volumes
            else 0.0
        ),
        "entries": rows,
    }
