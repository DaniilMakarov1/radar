from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Any


POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_URL = "https://clob.polymarket.com"
POLYMARKET_DATA_URL = "https://data-api.polymarket.com"
KALSHI_API_URL = "https://external-api.kalshi.com/trade-api/v2"
DEFAULT_KALSHI_EVENT_SEEDS = ("KXMENWORLDCUP-26",)


class PredictionDataError(RuntimeError):
    pass


class PredictionHttpClient:
    def __init__(
        self,
        timeout_seconds: int = 30,
        min_delay_seconds: float = 0.08,
        max_retries: int = 3,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.min_delay_seconds = min_delay_seconds
        self.max_retries = max_retries
        self._last_request_at = 0.0

    def get_json(self, url: str) -> Any:
        return self._request_json("GET", url)

    def post_json(self, url: str, payload: Any) -> Any:
        return self._request_json("POST", url, payload)

    def _request_json(self, method: str, url: str, payload: Any = None) -> Any:
        body = None
        headers = {
            "Accept": "application/json",
            "User-Agent": "SmartMoneyRadar-Prediction/0.1",
        }
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        for attempt in range(self.max_retries + 1):
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.min_delay_seconds:
                time.sleep(self.min_delay_seconds - elapsed)
            request = urllib.request.Request(
                url,
                data=body,
                headers=headers,
                method=method,
            )
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.timeout_seconds,
                ) as response:
                    result = json.loads(response.read().decode("utf-8"))
                self._last_request_at = time.monotonic()
                return result
            except urllib.error.HTTPError as exc:
                self._last_request_at = time.monotonic()
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                    time.sleep(1.25 * (attempt + 1))
                    continue
                raise PredictionDataError(
                    f"HTTP {exc.code} for {url}: {detail[:300]}"
                ) from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                self._last_request_at = time.monotonic()
                if attempt < self.max_retries:
                    time.sleep(1.25 * (attempt + 1))
                    continue
                raise PredictionDataError(f"Request failed for {url}: {exc}") from exc
        raise PredictionDataError(f"Request failed for {url}")


class PolymarketClient:
    def __init__(self, http: PredictionHttpClient | None = None) -> None:
        self.http = http or PredictionHttpClient()

    def events(self, limit: int = 12) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {
                "active": "true",
                "closed": "false",
                "limit": max(1, min(limit, 100)),
                "order": "volume24hr",
                "ascending": "false",
            }
        )
        payload = self.http.get_json(f"{POLYMARKET_GAMMA_URL}/events?{query}")
        return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []

    def orderbooks(self, token_ids: list[str]) -> dict[str, dict[str, Any]]:
        unique_ids = list(dict.fromkeys(str(token_id) for token_id in token_ids if token_id))
        books: dict[str, dict[str, Any]] = {}
        for start in range(0, len(unique_ids), 100):
            batch = unique_ids[start : start + 100]
            payload = self.http.post_json(
                f"{POLYMARKET_CLOB_URL}/books",
                [{"token_id": token_id} for token_id in batch],
            )
            if not isinstance(payload, list):
                continue
            for row in payload:
                if not isinstance(row, dict) or not row.get("asset_id"):
                    continue
                books[str(row["asset_id"])] = row
        return books

    def clob_market_info(
        self,
        condition_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        for condition_id in dict.fromkeys(
            str(value) for value in condition_ids if value
        ):
            payload = self.http.get_json(
                f"{POLYMARKET_CLOB_URL}/clob-markets/"
                f"{urllib.parse.quote(condition_id, safe='')}"
            )
            if isinstance(payload, dict):
                output[condition_id] = payload
        return output

    def leaderboard(self, limit: int = 20) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {
                "category": "OVERALL",
                "timePeriod": "ALL",
                "orderBy": "PNL",
                "limit": max(1, min(limit, 50)),
            }
        )
        payload = self.http.get_json(f"{POLYMARKET_DATA_URL}/v1/leaderboard?{query}")
        return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []

    def closed_positions(
        self,
        wallet_address: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page_size = min(50, max(1, limit))
        for offset in range(0, limit, page_size):
            query = urllib.parse.urlencode(
                {
                    "user": wallet_address,
                    "limit": min(page_size, limit - offset),
                    "offset": offset,
                    "sortBy": "TIMESTAMP",
                    "sortDirection": "DESC",
                }
            )
            payload = self.http.get_json(
                f"{POLYMARKET_DATA_URL}/closed-positions?{query}"
            )
            page = [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []
            rows.extend(page)
            if len(page) < min(page_size, limit - offset):
                break
        return rows[:limit]


class KalshiClient:
    def __init__(self, http: PredictionHttpClient | None = None) -> None:
        self.http = http or PredictionHttpClient()

    def top_events(
        self,
        limit: int = 12,
        market_pages: int = 2,
        seed_event_ids: tuple[str, ...] = DEFAULT_KALSHI_EVENT_SEEDS,
    ) -> list[dict[str, Any]]:
        markets: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(max(1, market_pages)):
            params: dict[str, Any] = {
                "status": "open",
                "limit": 1000,
                "mve_filter": "exclude",
            }
            if cursor:
                params["cursor"] = cursor
            payload = self.http.get_json(
                f"{KALSHI_API_URL}/markets?{urllib.parse.urlencode(params)}"
            )
            if not isinstance(payload, dict):
                break
            markets.extend(
                row for row in payload.get("markets", []) if isinstance(row, dict)
            )
            cursor = str(payload.get("cursor") or "")
            if not cursor:
                break

        volume_by_event: dict[str, float] = defaultdict(float)
        for market in markets:
            event_id = str(market.get("event_ticker") or "")
            if not event_id:
                continue
            volume_by_event[event_id] += as_float(market.get("volume_fp"))
        volume_event_ids = [
            event_id
            for event_id, _ in sorted(
                volume_by_event.items(),
                key=lambda item: item[1],
                reverse=True,
            )[: max(1, limit)]
        ]
        event_ids = list(
            dict.fromkeys(
                [event_id for event_id in seed_event_ids if event_id]
                + volume_event_ids
            )
        )[: max(1, limit)]

        events: list[dict[str, Any]] = []
        for event_id in event_ids:
            payload = self.http.get_json(
                f"{KALSHI_API_URL}/events/{urllib.parse.quote(event_id)}"
                "?with_nested_markets=true"
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("event"), dict):
                continue
            event = dict(payload["event"])
            nested_markets = event.get("markets")
            if not isinstance(nested_markets, list):
                nested_markets = payload.get("markets")
            event["markets"] = [
                row for row in (nested_markets or []) if isinstance(row, dict)
            ]
            events.append(event)
        return events

    def orderbooks(self, market_ids: list[str]) -> dict[str, dict[str, Any]]:
        unique_ids = list(dict.fromkeys(str(market_id) for market_id in market_ids if market_id))
        books: dict[str, dict[str, Any]] = {}
        for start in range(0, len(unique_ids), 100):
            batch = unique_ids[start : start + 100]
            query = urllib.parse.urlencode(
                [("tickers", ticker) for ticker in batch]
            )
            payload = self.http.get_json(
                f"{KALSHI_API_URL}/markets/orderbooks?{query}"
            )
            if not isinstance(payload, dict):
                continue
            for row in payload.get("orderbooks", []):
                if not isinstance(row, dict) or not row.get("ticker"):
                    continue
                books[str(row["ticker"])] = row
        return books


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
