from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from smart_money_radar.config import load_env_file
from smart_money_radar.storage import utc_now_iso


GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
X_COUNTS_URL = "https://api.x.com/2/tweets/counts/recent"


class SocialDataError(RuntimeError):
    pass


class GdeltSocialClient:
    def __init__(
        self,
        timeout_seconds: int = 15,
        min_delay_seconds: float = 6.2,
        max_retries: int = 2,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.min_delay_seconds = min_delay_seconds
        self.max_retries = max(0, int(max_retries))
        self._last_request_at = 0.0
        self._request_lock = threading.Lock()

    def mention_count(self, query: str, hours: int) -> int:
        count_24h, count_7d = self.mention_counts(query)
        return count_24h if hours <= 24 else count_7d

    def mention_counts(self, query: str) -> tuple[int, int]:
        end_at = datetime.now(UTC)
        start_at = end_at - timedelta(days=7)
        params = {
            "query": query,
            "mode": "artlist",
            "maxrecords": "250",
            "format": "json",
            "startdatetime": start_at.strftime("%Y%m%d%H%M%S"),
            "enddatetime": end_at.strftime("%Y%m%d%H%M%S"),
        }
        with self._request_lock:
            payload = self._get_json(
                GDELT_DOC_URL + "?" + urllib.parse.urlencode(params)
            )
        articles = payload.get("articles") if isinstance(payload, dict) else None
        rows = articles if isinstance(articles, list) else []
        cutoff_24h = end_at - timedelta(hours=24)
        count_24h = sum(
            1
            for article in rows
            if isinstance(article, dict)
            for seen_at in [gdelt_seen_at(article.get("seendate"))]
            if seen_at is not None and seen_at >= cutoff_24h
        )
        return count_24h, len(rows)

    def _get_json(self, url: str) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.min_delay_seconds:
                time.sleep(self.min_delay_seconds - elapsed)
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": "SmartMoneyRadar/0.3"},
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                last_error = SocialDataError(
                    f"GDELT HTTP {exc.code}: {detail[:200]}"
                )
                if exc.code != 429 or attempt == self.max_retries:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = SocialDataError(f"GDELT request failed: {exc}")
                if attempt == self.max_retries:
                    raise last_error from exc
            finally:
                self._last_request_at = time.monotonic()
            time.sleep(4 * (attempt + 1))
        raise SocialDataError(str(last_error or "GDELT request failed"))


class XSocialClient:
    def __init__(self, bearer_token: str | None = None, timeout_seconds: int = 25) -> None:
        load_env_file()
        self.bearer_token = bearer_token or os.environ.get("X_BEARER_TOKEN")
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.bearer_token)

    def mention_count(self, query: str, hours: int) -> int:
        if not self.bearer_token:
            raise SocialDataError("X_BEARER_TOKEN is not set")
        end_at = datetime.now(UTC)
        start_at = end_at - timedelta(hours=hours)
        params = {
            "query": f"{query} -is:retweet",
            "granularity": "hour",
            "start_time": start_at.isoformat().replace("+00:00", "Z"),
            "end_time": end_at.isoformat().replace("+00:00", "Z"),
        }
        request = urllib.request.Request(
            X_COUNTS_URL + "?" + urllib.parse.urlencode(params),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.bearer_token}",
                "User-Agent": "SmartMoneyRadar/0.2",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SocialDataError(f"X API HTTP {exc.code}: {detail[:200]}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise SocialDataError(f"X API request failed: {exc}") from exc
        rows = payload.get("data") if isinstance(payload, dict) else None
        return sum(int(row.get("tweet_count") or 0) for row in rows or [])


def social_query(token_name: Any, token_symbol: Any) -> str | None:
    name = str(token_name or "").strip()
    symbol = str(token_symbol or "").strip()
    if len(name) >= 4 and not name.isdigit():
        return f'"{name}"'
    if len(symbol) >= 5 and symbol.isalnum():
        return f'"{symbol}"'
    return None


def build_social_snapshot(
    chain_id: str,
    token_address: str,
    token_name: Any,
    token_symbol: Any,
    observed_at: str | None = None,
    gdelt: GdeltSocialClient | None = None,
    x_client: XSocialClient | None = None,
) -> dict[str, Any]:
    query = social_query(token_name, token_symbol)
    timestamp = observed_at or utc_now_iso()
    flags = []
    provider_counts: dict[str, dict[str, int]] = {}
    raw: dict[str, Any] = {}
    mentions_24h = None
    mentions_7d = None
    x_available = False
    coverage_score = 0.0

    if not query:
        flags.append("social_query_unavailable")
        return {
            "chain_id": chain_id,
            "token_address": token_address.lower(),
            "observed_at": timestamp,
            "source": "social_enrichment",
            "query_text": "",
            "mentions_24h": None,
            "mentions_7d": None,
            "social_silence_score": None,
            "coverage_score": coverage_score,
            "x_available": x_available,
            "provider_counts": provider_counts,
            "flags": flags,
            "raw": raw,
        }

    gdelt_client = gdelt or GdeltSocialClient()
    try:
        gdelt_24h, gdelt_7d = gdelt_client.mention_counts(query)
        provider_counts["gdelt_news"] = {
            "mentions_24h": gdelt_24h,
            "mentions_7d": gdelt_7d,
        }
        raw["gdelt_news"] = provider_counts["gdelt_news"]
        mentions_24h = gdelt_24h
        mentions_7d = gdelt_7d
        coverage_score += 0.35
        flags.append("gdelt_news_proxy_only")
    except SocialDataError as exc:
        flags.append("gdelt_unavailable")
        raw["gdelt_error"] = str(exc)

    x = x_client or XSocialClient()
    if not x.configured:
        flags.append("x_api_not_configured")
    else:
        try:
            x_24h = x.mention_count(query, hours=24)
            x_7d = x.mention_count(query, hours=24 * 7)
            provider_counts["x"] = {"mentions_24h": x_24h, "mentions_7d": x_7d}
            raw["x"] = provider_counts["x"]
            mentions_24h = x_24h
            mentions_7d = x_7d
            x_available = True
            coverage_score += 0.65
        except SocialDataError as exc:
            flags.append("x_api_unavailable")
            raw["x_error"] = str(exc)

    return {
        "chain_id": chain_id,
        "token_address": token_address.lower(),
        "observed_at": timestamp,
        "source": "social_enrichment",
        "query_text": query,
        "mentions_24h": mentions_24h,
        "mentions_7d": mentions_7d,
        "social_silence_score": social_silence_score(mentions_24h, mentions_7d),
        "coverage_score": round(min(1.0, coverage_score), 2),
        "x_available": x_available,
        "provider_counts": provider_counts,
        "flags": flags,
        "raw": raw,
    }


def social_silence_score(mentions_24h: int | None, mentions_7d: int | None) -> float | None:
    if mentions_24h is None and mentions_7d is None:
        return None
    day = max(0, int(mentions_24h or 0))
    week = max(0, int(mentions_7d or 0))
    pressure = min(100.0, math.log1p(day * 4 + week) / math.log(151) * 100)
    return round(100.0 - pressure, 2)


def gdelt_seen_at(value: Any) -> datetime | None:
    text = str(value or "").strip()
    for format_string in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(text, format_string).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None
