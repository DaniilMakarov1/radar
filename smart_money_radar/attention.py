from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any

from smart_money_radar.config import load_env_file
from smart_money_radar.ingestion.market import DexScreenerClient, MarketDataError
from smart_money_radar.ingestion.social import GdeltSocialClient, SocialDataError, social_query
from smart_money_radar.storage import (
    SQLiteStore,
    normalize_chain_address,
    utc_now_iso,
)


NEYNAR_SEARCH_URL = "https://api.neynar.com/v2/farcaster/cast/search/"
GITHUB_API_BASE = "https://api.github.com"


def enrich_attention_snapshots(
    store: SQLiteStore,
    max_tokens: int = 3,
) -> dict[str, Any]:
    observations = store.latest_radar_observations(limit=500)
    targets = sorted(
        observations,
        key=lambda row: float(row.get("net_buy_usd") or 0),
        reverse=True,
    )[:max_tokens]
    gdelt = GdeltSocialClient(timeout_seconds=8, max_retries=0)
    observed_at = utc_now_iso()
    snapshots = []
    errors = []
    def collect(row: dict[str, Any]) -> dict[str, Any]:
        return build_attention_snapshot(
            store=store,
            observation=row,
            dexscreener=DexScreenerClient(),
            gdelt=gdelt,
            observed_at=observed_at,
        )

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(targets)))) as executor:
        futures = {executor.submit(collect, row): row for row in targets}
        for future in as_completed(futures):
            row = futures[future]
            try:
                snapshots.append(future.result())
            except Exception as exc:
                errors.append(
                    f"{row.get('market_token_symbol') or row.get('token_symbol') or row['token_address']}: {exc}"
                )
    snapshots.sort(key=lambda row: (row["chain_id"], row["token_address"]))
    count = store.upsert_token_attention_snapshots(snapshots)
    return {
        "target_count": len(targets),
        "snapshot_count": count,
        "errors": errors,
    }


def build_attention_snapshot(
    store: SQLiteStore,
    observation: dict[str, Any],
    dexscreener: DexScreenerClient | None = None,
    gdelt: GdeltSocialClient | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    load_env_file()
    timestamp = observed_at or utc_now_iso()
    chain_id = observation["chain_id"]
    token_address = normalize_chain_address(
        chain_id, observation["token_address"]
    )
    name = observation.get("market_token_name")
    symbol = observation.get("market_token_symbol") or observation.get("token_symbol")
    flags = []
    raw: dict[str, Any] = {}
    coverage = 0.0

    promotion = {"boosts": int(observation.get("boosts_active") or 0), "ads": 0, "profile": 0}
    try:
        promotion_result = (dexscreener or DexScreenerClient()).token_promotion_snapshot(
            chain_id,
            token_address,
        )
        promotion.update(promotion_result)
        promotion["boosts"] = int(observation.get("boosts_active") or 0)
        raw["dexscreener"] = promotion_result
        coverage += 0.25
    except MarketDataError as exc:
        flags.append("dexscreener_attention_unavailable")
        raw["dexscreener_error"] = str(exc)

    farcaster_24h = None
    farcaster_7d = None
    neynar_key = os.environ.get("NEYNAR_API_KEY")
    query = social_query(name, symbol)
    if neynar_key and query:
        try:
            farcaster_24h = neynar_cast_count(query, 24, neynar_key)
            farcaster_7d = neynar_cast_count(query, 24 * 7, neynar_key)
            coverage += 0.25
        except AttentionDataError as exc:
            flags.append("farcaster_unavailable")
            raw["farcaster_error"] = str(exc)
    else:
        flags.append("farcaster_api_not_configured")

    news_24h = None
    news_7d = None
    if query:
        try:
            news_client = gdelt or GdeltSocialClient()
            news_24h, news_7d = news_client.mention_counts(query)
            coverage += 0.2
        except SocialDataError as exc:
            flags.append("news_attention_unavailable")
            raw["news_error"] = str(exc)

    github_commits = None
    github_contributors = None
    repository = discover_github_repository(
        website_url=observation.get("website_url"),
        social_links=observation.get("social_links") or [],
    )
    if repository:
        try:
            github_commits, github_contributors, github_raw = github_activity(repository)
            raw["github"] = github_raw
            coverage += 0.15
        except AttentionDataError as exc:
            flags.append("github_attention_unavailable")
            raw["github_error"] = str(exc)
    else:
        flags.append("github_repository_not_found")

    trader_growth, volume_growth, onchain_raw = onchain_growth(
        store,
        chain_id,
        token_address,
    )
    raw["onchain_growth"] = onchain_raw
    coverage += 0.15

    public_level_components = []
    public_level_components.append(min(100.0, float(promotion.get("boosts") or 0) * 12.0))
    public_level_components.append(min(100.0, float(promotion.get("ads") or 0) * 45.0))
    if farcaster_7d is not None:
        public_level_components.append(log_score(farcaster_7d, 500))
    if news_7d is not None:
        public_level_components.append(log_score(news_7d, 100))
    if github_commits is not None:
        public_level_components.append(log_score(github_commits, 100) * 0.5)
    public_level = (
        sum(public_level_components) / len(public_level_components)
        if public_level_components
        else None
    )
    public_acceleration_components = [
        acceleration_score(farcaster_24h, farcaster_7d),
        acceleration_score(news_24h, news_7d),
    ]
    if repository and isinstance(raw.get("github"), dict):
        public_acceleration_components.append(
            period_acceleration_score(
                raw["github"].get("commits_30d"),
                30,
                raw["github"].get("commits_previous_60d"),
                60,
            )
        )
    public_acceleration_components = [
        value for value in public_acceleration_components if value is not None
    ]
    public_acceleration = (
        sum(public_acceleration_components) / len(public_acceleration_components)
        if public_acceleration_components
        else None
    )
    public_attention = weighted_available(
        ((public_acceleration, 0.65), (public_level, 0.35))
    )

    net_buy = max(0.0, float(observation.get("net_buy_usd") or 0))
    wallet_count = max(0, int(observation.get("effective_wallet_count") or observation.get("tracked_wallet_count") or 0))
    onchain_level_components = [
        log_score(net_buy, 250_000),
        min(100.0, wallet_count * 12.5),
    ]
    onchain_acceleration_components = []
    if trader_growth is not None:
        onchain_acceleration_components.append(clamp(50 + trader_growth * 35))
    if volume_growth is not None:
        onchain_acceleration_components.append(clamp(50 + volume_growth * 25))
    onchain_level = sum(onchain_level_components) / len(onchain_level_components)
    onchain_acceleration = (
        sum(onchain_acceleration_components) / len(onchain_acceleration_components)
        if onchain_acceleration_components
        else None
    )
    onchain_attention = weighted_available(
        ((onchain_acceleration, 0.65), (onchain_level, 0.35))
    ) or onchain_level
    attention_gap = (
        clamp(50 + (onchain_attention - public_attention) * 0.5)
        if public_attention is not None
        else None
    )
    if coverage < 0.5:
        flags.append("attention_coverage_low")
    if attention_gap is not None and attention_gap >= 70:
        flags.append("onchain_leads_public_attention")
    if public_attention is not None and public_attention >= 65:
        flags.append("public_attention_hot")
    raw["attention_components"] = {
        "public_level": public_level,
        "public_acceleration": public_acceleration,
        "onchain_level": onchain_level,
        "onchain_acceleration": onchain_acceleration,
        "gap_definition": "onchain acceleration minus public attention acceleration and level",
    }

    return {
        "chain_id": chain_id,
        "token_address": token_address,
        "observed_at": timestamp,
        "source": "attention_gap_v1",
        "dexscreener_boosts": int(promotion.get("boosts") or 0),
        "dexscreener_ads": int(promotion.get("ads") or 0),
        "dexscreener_profile": int(promotion.get("profile") or 0),
        "farcaster_mentions_24h": farcaster_24h,
        "farcaster_mentions_7d": farcaster_7d,
        "github_commits_30d": github_commits,
        "github_contributors_90d": github_contributors,
        "news_mentions_24h": news_24h,
        "news_mentions_7d": news_7d,
        "onchain_trader_growth_7d": trader_growth,
        "onchain_volume_growth_7d": volume_growth,
        "public_attention_score": public_attention,
        "onchain_attention_score": onchain_attention,
        "attention_gap_score": attention_gap,
        "coverage_score": min(1.0, coverage),
        "flags": sorted(set(flags)),
        "raw": raw,
    }


class AttentionDataError(RuntimeError):
    pass


def neynar_cast_count(query: str, hours: int, api_key: str) -> int:
    after = datetime.now(UTC) - timedelta(hours=hours)
    search = f"{query} after:{after.strftime('%Y-%m-%dT%H:%M:%S')}"
    url = NEYNAR_SEARCH_URL + "?" + urllib.parse.urlencode(
        {"q": search, "mode": "literal", "sort_type": "desc_chron", "limit": 100}
    )
    payload = request_json(url, headers={"x-api-key": api_key})
    result = payload.get("result") if isinstance(payload, dict) else None
    casts = result.get("casts") if isinstance(result, dict) else None
    return len(casts) if isinstance(casts, list) else 0


def discover_github_repository(
    website_url: Any,
    social_links: list[dict[str, Any]],
) -> str | None:
    candidates = []
    for row in social_links:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or row.get("handle") or "")
        if "github.com/" in url.lower():
            candidates.append(url)
    if website_url:
        try:
            html = request_text(str(website_url))
            parser = LinkParser()
            parser.feed(html)
            candidates.extend(parser.links)
        except AttentionDataError:
            pass
    for candidate in candidates:
        repository = normalize_github_repository(candidate)
        if repository:
            return repository
    return None


def github_activity(repository: str) -> tuple[int, int, dict[str, Any]]:
    load_env_file()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    since_90 = (datetime.now(UTC) - timedelta(days=90)).isoformat().replace("+00:00", "Z")
    url = f"{GITHUB_API_BASE}/repos/{repository}/commits?" + urllib.parse.urlencode(
        {"since": since_90, "per_page": 100}
    )
    payload = request_json(url, headers=headers)
    commits = payload if isinstance(payload, list) else []
    cutoff_30 = datetime.now(UTC) - timedelta(days=30)
    commits_30 = 0
    commits_previous_60 = 0
    contributors = set()
    for row in commits:
        if not isinstance(row, dict):
            continue
        author = row.get("author") if isinstance(row.get("author"), dict) else {}
        commit = row.get("commit") if isinstance(row.get("commit"), dict) else {}
        commit_author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
        date = commit_author.get("date")
        if date and parse_iso(date) >= cutoff_30:
            commits_30 += 1
        elif date:
            commits_previous_60 += 1
        identity = author.get("login") or commit_author.get("email") or commit_author.get("name")
        if identity:
            contributors.add(str(identity).lower())
    return commits_30, len(contributors), {
        "repository": repository,
        "commits_30d": commits_30,
        "commits_previous_60d": commits_previous_60,
        "commits_90d_capped": len(commits),
        "result_cap": 100,
    }


def onchain_growth(
    store: SQLiteStore,
    chain_id: str,
    token_address: str,
) -> tuple[float | None, float | None, dict[str, Any]]:
    universe = [
        row
        for row in store.research_universe_rows(chain_id=chain_id)
        if row["token_address"].lower() == token_address.lower()
    ]
    universe.sort(key=lambda row: row["snapshot_at"])
    if len(universe) >= 2:
        previous, current = universe[-2], universe[-1]
        trader_growth = growth_rate(current.get("trader_count"), previous.get("trader_count"))
        volume_growth = growth_rate(current.get("volume_usd"), previous.get("volume_usd"))
        return trader_growth, volume_growth, {"source": "weekly_universe"}
    history = store.radar_observation_history(chain_id, token_address, limit=2)
    if len(history) >= 2:
        current, previous = history[0], history[1]
        wallet_growth = growth_rate(
            current.get("tracked_wallet_count"),
            previous.get("tracked_wallet_count"),
        )
        flow_growth = growth_rate(current.get("net_buy_usd"), previous.get("net_buy_usd"))
        return wallet_growth, flow_growth, {"source": "radar_observations"}
    return None, None, {"source": "insufficient_history"}


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value and "github.com/" in value.lower():
                self.links.append(value)


def normalize_github_repository(value: str) -> str | None:
    text = value if "://" in value else "https://" + value.lstrip("/")
    parsed = urllib.parse.urlparse(text)
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[1].lower() in {"issues", "pulls", "orgs", "topics"}:
        return None
    return f"{parts[0]}/{parts[1].removesuffix('.git')}"


def request_json(url: str, headers: dict[str, str] | None = None) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "SmartMoneyRadar/0.3",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AttentionDataError(f"HTTP {exc.code}: {detail[:200]}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise AttentionDataError(str(exc)) from exc


def request_text(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "SmartMoneyRadar/0.3"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            content_type = response.headers.get("Content-Type", "")
            if "html" not in content_type.lower():
                return ""
            return response.read(1_000_000).decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        raise AttentionDataError(str(exc)) from exc


def growth_rate(current: Any, previous: Any) -> float | None:
    try:
        current_value = float(current)
        previous_value = float(previous)
    except (TypeError, ValueError):
        return None
    if previous_value <= 0:
        return None
    return max(-1.0, min(10.0, current_value / previous_value - 1))


def log_score(value: float, reference: float) -> float:
    return clamp(math.log1p(max(0.0, value)) / math.log1p(reference) * 100)


def acceleration_score(recent_24h: int | None, total_7d: int | None) -> float | None:
    if recent_24h is None or total_7d is None:
        return None
    prior_six_days = max(0, total_7d - recent_24h)
    return period_acceleration_score(recent_24h, 1, prior_six_days, 6)


def period_acceleration_score(
    current_count: Any,
    current_days: int,
    previous_count: Any,
    previous_days: int,
) -> float | None:
    if current_count is None or previous_count is None:
        return None
    current_rate = max(0.0, float(current_count)) / max(1, current_days)
    previous_rate = max(0.0, float(previous_count)) / max(1, previous_days)
    if previous_rate == 0:
        return 50.0 if current_rate == 0 else 100.0
    growth = max(-1.0, min(10.0, current_rate / previous_rate - 1))
    return clamp(50 + growth * 30)


def weighted_available(values: tuple[tuple[float | None, float], ...]) -> float | None:
    available = [(value, weight) for value, weight in values if value is not None]
    if not available:
        return None
    weight_sum = sum(weight for _, weight in available)
    return sum(float(value) * weight for value, weight in available) / weight_sum


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return min(high, max(low, value))


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
