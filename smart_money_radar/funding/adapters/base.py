from __future__ import annotations

import json
import http.client
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Protocol


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


class FundingHttpClient:
    def __init__(
        self,
        timeout_seconds: int = 8,
        min_delay_seconds: float = 0.05,
        max_retries: int = 1,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.min_delay_seconds = min_delay_seconds
        self.max_retries = max_retries
        self._last_request_at = 0.0
        self._lock = threading.Lock()

    def get_json(self, url: str) -> Any:
        return self._request_json("GET", url)

    def post_json(self, url: str, payload: Any) -> Any:
        return self._request_json("POST", url, payload)

    def _request_json(self, method: str, url: str, payload: Any = None) -> Any:
        body = None
        headers = {
            "Accept": "application/json",
            "User-Agent": "SmartMoneyRadar-Funding/0.1",
        }
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        for attempt in range(self.max_retries + 1):
            self._reserve_request_slot()
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
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                    time.sleep(1.25 * (attempt + 1))
                    continue
                raise FundingDataError(
                    f"HTTP {exc.code} for {url}: {detail[:300]}"
                ) from exc
            except (
                urllib.error.URLError,
                TimeoutError,
                json.JSONDecodeError,
                http.client.HTTPException,
                ConnectionError,
            ) as exc:
                if attempt < self.max_retries:
                    time.sleep(1.25 * (attempt + 1))
                    continue
                raise FundingDataError(f"Request failed for {url}: {exc}") from exc
        raise FundingDataError(f"Request failed for {url}")

    def _reserve_request_slot(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.min_delay_seconds:
                time.sleep(self.min_delay_seconds - elapsed)
            self._last_request_at = time.monotonic()


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
