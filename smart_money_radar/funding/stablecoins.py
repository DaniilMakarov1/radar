from __future__ import annotations

import math
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Protocol

from smart_money_radar.funding.adapter_contracts import (
    USD_COMPARABLE_STABLE_FAMILIES,
    USD_MAJOR_STABLE,
    USD_BRIDGED_STABLE_ALIASES,
    collateral_family,
)

TRUSTED_SAME_ASSET_COLLATERAL = {"USD", "USDT", "USDC"}


@dataclass(frozen=True)
class StablecoinPrice:
    asset: str
    usd_price: float
    source: str
    source_event_at: str
    response_received_at: str
    quality: str = "observed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "usd_price": self.usd_price,
            "source": self.source,
            "source_event_at": self.source_event_at,
            "response_received_at": self.response_received_at,
            "quality": self.quality,
        }


class StablecoinPriceProvider(Protocol):
    def prices(self, asset: str, observed_at: str) -> list[StablecoinPrice]: ...


class StaticStablecoinPriceProvider:
    def __init__(self, prices_by_asset: dict[str, list[StablecoinPrice]]) -> None:
        self.prices_by_asset = {
            str(asset).upper(): list(rows)
            for asset, rows in prices_by_asset.items()
        }

    def prices(self, asset: str, observed_at: str) -> list[StablecoinPrice]:
        return list(self.prices_by_asset.get(str(asset).upper(), []))


class PublicStablecoinPriceProvider:
    """Fetch USD-stable prices from bounded public sources."""

    COINGECKO_IDS = {
        "USDC": "usd-coin",
        "USDT": "tether",
        "USDE": "ethena-usde",
        "USDT0": "usdt0",
    }
    COINBASE_CURRENCIES = {
        "USDC": "USDC",
        "USDT": "USDT",
    }
    DEFILLAMA_IDS = {
        "USDC": "coingecko:usd-coin",
        "USDT": "coingecko:tether",
        "USDE": "coingecko:ethena-usde",
        "USDT0": "coingecko:usdt0",
    }
    KRAKEN_USD_PAIRS = {
        "USDE": "USDEUSD",
    }
    GECKOTERMINAL_SEARCH = {
        "USDT0": "USDT0",
    }

    def __init__(
        self,
        *,
        timeout_seconds: float = 2.0,
        fetch_json: Callable[[str, float], Any] | None = None,
    ) -> None:
        self.timeout_seconds = max(0.25, float(timeout_seconds))
        self.fetch_json = fetch_json or self._fetch_json
        self._cache: dict[tuple[str, tuple[str, ...]], list[StablecoinPrice]] = {}

    def prices(self, asset: str, observed_at: str) -> list[StablecoinPrice]:
        symbol = str(asset or "").upper()
        lookup_symbol = self._lookup_symbol(symbol)
        sources = self._sources_for_symbol(lookup_symbol)
        if not sources:
            return []
        key = (symbol, sources)
        cached = self._cache.get(key)
        if cached is None or not self._cache_entry_fresh(cached):
            self._cache[key] = self._fetch_prices(symbol, lookup_symbol, observed_at)
        return list(self._cache[key])

    @staticmethod
    def _lookup_symbol(symbol: str) -> str:
        normalized = symbol.replace("-", "").replace("_", "")
        if symbol in USD_BRIDGED_STABLE_ALIASES or normalized in USD_BRIDGED_STABLE_ALIASES:
            return "USDC"
        return symbol

    @classmethod
    def _sources_for_symbol(cls, symbol: str) -> tuple[str, ...]:
        sources: list[str] = []
        if symbol in cls.COINBASE_CURRENCIES:
            sources.append("coinbase")
        if symbol in cls.COINGECKO_IDS:
            sources.append("coingecko")
        if symbol in cls.KRAKEN_USD_PAIRS:
            sources.append("kraken")
        if symbol not in cls.COINBASE_CURRENCIES and symbol in cls.DEFILLAMA_IDS:
            sources.append("defillama")
        if symbol in cls.GECKOTERMINAL_SEARCH:
            sources.append("geckoterminal")
        return tuple(sources)

    @staticmethod
    def _cache_entry_fresh(rows: list[StablecoinPrice]) -> bool:
        if len(rows) < 2:
            return False
        now = datetime.now(UTC)
        seen_sources: set[str] = set()
        for row in rows:
            event_at = _parse_time(row.source_event_at) or _parse_time(row.response_received_at)
            if event_at is None or row.usd_price <= 0:
                return False
            age = (now - event_at.astimezone(UTC)).total_seconds()
            if age < -1.0 or age > 5.0:
                return False
            seen_sources.add(str(row.source))
        return len(seen_sources) >= 2

    def _fetch_prices(
        self,
        asset: str,
        lookup_symbol: str,
        observed_at: str,
    ) -> list[StablecoinPrice]:
        rows: list[StablecoinPrice] = []
        quality = (
            "bridged_canonical_usdc_proxy"
            if asset != lookup_symbol and lookup_symbol == "USDC"
            else "current_snapshot_response_time"
        )
        coingecko_id = self.COINGECKO_IDS.get(lookup_symbol)
        if coingecko_id:
            price, response_received_at = self._coingecko_price(coingecko_id)
            if price is not None:
                rows.append(
                    StablecoinPrice(
                        asset,
                        price,
                        "coingecko",
                        "",
                        response_received_at,
                        quality,
                    )
                )
        coinbase_currency = self.COINBASE_CURRENCIES.get(lookup_symbol)
        if coinbase_currency:
            price, response_received_at = self._coinbase_price(coinbase_currency)
            if price is not None:
                rows.append(
                    StablecoinPrice(
                        asset,
                        price,
                        "coinbase",
                        "",
                        response_received_at,
                        quality,
                    )
                )
        defillama_id = self.DEFILLAMA_IDS.get(lookup_symbol)
        if defillama_id and len({row.source for row in rows}) < 2:
            price, source_event_at, response_received_at = self._defillama_price(defillama_id)
            if price is not None:
                rows.append(
                    StablecoinPrice(
                        asset,
                        price,
                        "defillama",
                        source_event_at,
                        response_received_at,
                        "defillama_current_price",
                    )
                )
        geckoterminal_search = self.GECKOTERMINAL_SEARCH.get(lookup_symbol)
        if geckoterminal_search and len({row.source for row in rows}) < 3:
            price, response_received_at = self._geckoterminal_price(geckoterminal_search)
            if price is not None:
                rows.append(
                    StablecoinPrice(
                        asset,
                        price,
                        "geckoterminal",
                        "",
                        response_received_at,
                        "public_dex_pool_usd_price",
                    )
                )
        kraken_pair = self.KRAKEN_USD_PAIRS.get(lookup_symbol)
        if kraken_pair:
            price, response_received_at = self._kraken_usd_price(kraken_pair)
            if price is not None:
                rows.append(
                    StablecoinPrice(
                        asset,
                        price,
                        "kraken",
                        "",
                        response_received_at,
                        "public_spot_usd_mid",
                    )
                )
        return rows

    def _coingecko_price(self, coingecko_id: str) -> tuple[float | None, str]:
        query = urllib.parse.urlencode({"ids": coingecko_id, "vs_currencies": "usd"})
        payload, response_received_at = self._safe_fetch(
            f"https://api.coingecko.com/api/v3/simple/price?{query}"
        )
        try:
            price = float(payload[coingecko_id]["usd"])
        except (TypeError, ValueError, KeyError):
            price = 0.0
        return (price if price > 0 else None), response_received_at

    def _coinbase_price(self, currency: str) -> tuple[float | None, str]:
        payload, response_received_at = self._safe_fetch(
            f"https://api.coinbase.com/v2/exchange-rates?currency={currency}"
        )
        try:
            price = float(payload["data"]["rates"]["USD"])
        except (TypeError, ValueError, KeyError):
            price = 0.0
        return (price if price > 0 else None), response_received_at

    def _defillama_price(self, asset_id: str) -> tuple[float | None, str, str]:
        payload, response_received_at = self._safe_fetch(
            f"https://coins.llama.fi/prices/current/{urllib.parse.quote(asset_id, safe=':')}"
        )
        row = (payload.get("coins") or {}).get(asset_id) if isinstance(payload, dict) else {}
        try:
            price = float(row["price"])
        except (TypeError, ValueError, KeyError):
            price = 0.0
        try:
            timestamp = float(row["timestamp"])
        except (TypeError, ValueError, KeyError):
            timestamp = 0.0
        # DefiLlama's coin timestamp can lag the current response by minutes for
        # stablecoins. Treat it like CoinGecko/Coinbase: current HTTP response
        # is the freshness point, and quality identifies the source family.
        return (price if price > 0 else None), "", response_received_at

    def _geckoterminal_price(self, query: str) -> tuple[float | None, str]:
        encoded = urllib.parse.urlencode({"query": query})
        payload, response_received_at = self._safe_fetch(
            f"https://api.geckoterminal.com/api/v2/search/pools?{encoded}"
        )
        rows = payload.get("data") if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            return None, response_received_at
        best_price = 0.0
        best_distance = math.inf
        best_reserve = -1.0
        for item in rows:
            attrs = item.get("attributes") if isinstance(item, dict) else {}
            name = str(attrs.get("name") or "").upper()
            if str(query).upper() not in name:
                continue
            try:
                price = float(attrs.get("base_token_price_usd") or 0.0)
                reserve = float(attrs.get("reserve_in_usd") or 0.0)
            except (TypeError, ValueError):
                continue
            distance = abs(price - 1.0)
            if price <= 0 or distance > 0.05:
                continue
            if distance < best_distance or (
                math.isclose(distance, best_distance) and reserve > best_reserve
            ):
                best_price = price
                best_distance = distance
                best_reserve = reserve
        return (best_price if best_price > 0 else None), response_received_at

    def _kraken_usd_price(self, pair: str) -> tuple[float | None, str]:
        payload, response_received_at = self._safe_fetch(
            f"https://api.kraken.com/0/public/Ticker?pair={urllib.parse.quote(pair)}"
        )
        result = payload.get("result") if isinstance(payload, dict) else {}
        row = result.get(pair) if isinstance(result, dict) else {}
        if not row and isinstance(result, dict) and result:
            row = next(iter(result.values()))
        try:
            bid = float((row.get("b") or [0])[0])
            ask = float((row.get("a") or [0])[0])
            last = float((row.get("c") or [0])[0])
        except (TypeError, ValueError, KeyError, IndexError):
            bid = ask = last = 0.0
        price = ((bid + ask) / 2.0) if bid > 0 and ask > 0 else last
        return (price if price > 0 else None), response_received_at

    def _safe_fetch(self, url: str) -> tuple[Any, str]:
        try:
            payload = self.fetch_json(url, self.timeout_seconds)
            return payload, datetime.now(UTC).isoformat()
        except Exception:
            return {}, datetime.now(UTC).isoformat()

    @staticmethod
    def _fetch_json(url: str, timeout_seconds: float) -> Any:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "smart-money-radar/0.1",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _fresh_prices(
    rows: list[StablecoinPrice],
    observed_at: str,
    *,
    max_age_seconds: float,
) -> list[StablecoinPrice]:
    observed = _parse_time(observed_at) or datetime.now(UTC)
    output: list[StablecoinPrice] = []
    seen_sources: set[str] = set()
    for row in rows:
        if row.usd_price <= 0:
            continue
        event_at = _parse_time(row.source_event_at) or _parse_time(
            row.response_received_at
        )
        if event_at is None:
            continue
        age = (observed.astimezone(UTC) - event_at.astimezone(UTC)).total_seconds()
        if age < -1.0 or age > max_age_seconds:
            continue
        source = str(row.source)
        if source in seen_sources:
            continue
        seen_sources.add(source)
        output.append(row)
    return output


def _freshness_reference_time(
    requested_observed: datetime,
    rows: list[StablecoinPrice],
) -> datetime:
    reference = requested_observed.astimezone(UTC)
    for row in rows:
        event_at = _parse_time(row.source_event_at) or _parse_time(row.response_received_at)
        if event_at is not None and event_at.astimezone(UTC) > reference:
            reference = event_at.astimezone(UTC)
    return reference


def stablecoin_basis_bps(long_usd_price: float, short_usd_price: float) -> float:
    mean_price = (float(long_usd_price) + float(short_usd_price)) / 2.0
    if mean_price <= 0:
        return math.inf
    return abs(float(long_usd_price) - float(short_usd_price)) / mean_price * 10_000.0


def percentile_95(values: list[float]) -> float:
    cleaned = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not cleaned:
        return 0.0
    if len(cleaned) == 1:
        return cleaned[0]
    rank = 0.95 * (len(cleaned) - 1)
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return cleaned[lower]
    weight = rank - lower
    return cleaned[lower] * (1.0 - weight) + cleaned[upper] * weight


def stablecoin_reserve_bps(
    current_basis_bps: float,
    adverse_stablecoin_change_1m_bps: list[float],
) -> float:
    adverse = [
        max(0.0, float(value))
        for value in adverse_stablecoin_change_1m_bps
        if math.isfinite(float(value))
    ]
    p95_adverse = percentile_95(adverse) if len(adverse) >= 10 else 10.0
    return max(10.0, float(current_basis_bps) + 3.0 * p95_adverse)


def stablecoin_pair_compatible(long_asset: Any, short_asset: Any) -> bool:
    return (
        collateral_family(long_asset) in USD_COMPARABLE_STABLE_FAMILIES
        and collateral_family(short_asset) in USD_COMPARABLE_STABLE_FAMILIES
    )


def evaluate_stablecoin_route(
    *,
    long_collateral: str,
    short_collateral: str,
    provider: StablecoinPriceProvider | None,
    observed_at: str,
    reference_notional: float,
    funding_net_before_stablecoin_reserve: float,
    adverse_stablecoin_change_1m_bps: list[float] | None = None,
) -> dict[str, Any]:
    long_asset = str(long_collateral or "").upper()
    short_asset = str(short_collateral or "").upper()
    requested_observed = _parse_time(observed_at) or datetime.now(UTC)

    def base_result(status: str, blockers: list[str], *, cross_stable: bool) -> dict[str, Any]:
        comparable = stablecoin_pair_compatible(long_asset, short_asset)
        return {
            "schema_version": 1,
            "compatible": status != "RESEARCH_ONLY" or not blockers or "stablecoin_family_not_compatible" not in blockers,
            "numeraire": "USD" if comparable else None,
            "stablecoin_pair": f"{long_asset}/{short_asset}",
            "canonical_pair": f"{long_asset}/{short_asset}",
            "direction": "long_collateral/short_collateral",
            "cross_stable": cross_stable,
            "long_asset": long_asset,
            "short_asset": short_asset,
            "status": status,
            "blockers": blockers,
            "observed_at": requested_observed.astimezone(UTC).isoformat(),
            "expires_at": requested_observed.astimezone(UTC).isoformat(),
            "source_identity": {"provider": None, "sources": []},
            "evidence_version": "stablecoin-route-snapshot-v1",
        }

    if long_asset == short_asset:
        if (
            long_asset not in TRUSTED_SAME_ASSET_COLLATERAL
            or collateral_family(long_asset) != USD_MAJOR_STABLE
        ):
            return {
                **base_result(
                    "RESEARCH_ONLY",
                    ["same_asset_collateral_not_trusted"],
                    cross_stable=False,
                ),
                "compatible": False,
                "numeraire": None,
                "funding_net_before_stablecoin_reserve": (
                    funding_net_before_stablecoin_reserve
                ),
                "funding_net_after_stablecoin_reserve": None,
            }
        return {
            **base_result("PASS", [], cross_stable=False),
            "compatible": True,
            "current_stablecoin_basis_bps": 0.0,
            "stablecoin_reserve_bps": 0.0,
            "stablecoin_reserve_usd": 0.0,
            "funding_net_before_stablecoin_reserve": (
                funding_net_before_stablecoin_reserve
            ),
            "funding_net_after_stablecoin_reserve": (
                funding_net_before_stablecoin_reserve
            ),
        }
    if not stablecoin_pair_compatible(long_asset, short_asset):
        return {
            **base_result(
                "RESEARCH_ONLY",
                ["stablecoin_family_not_compatible"],
                cross_stable=True,
            ),
            "compatible": False,
            "numeraire": None,
        }
    if provider is None:
        return {
            **base_result(
                "RESEARCH_ONLY",
                ["stablecoin_price_provider_unavailable"],
                cross_stable=True,
            ),
            "compatible": True,
        }
    long_raw_prices = provider.prices(long_asset, observed_at)
    short_raw_prices = provider.prices(short_asset, observed_at)
    freshness_observed = _freshness_reference_time(
        requested_observed,
        [*long_raw_prices, *short_raw_prices],
    ).isoformat()
    long_prices = _fresh_prices(
        long_raw_prices,
        freshness_observed,
        max_age_seconds=5.0,
    )
    short_prices = _fresh_prices(
        short_raw_prices,
        freshness_observed,
        max_age_seconds=5.0,
    )
    if len(long_prices) < 2 or len(short_prices) < 2:
        return {
            **base_result(
                "RESEARCH_ONLY",
                ["insufficient_stablecoin_price_sources"],
                cross_stable=True,
            ),
            "compatible": True,
            "long_source_count": len(long_prices),
            "short_source_count": len(short_prices),
        }
    long_values = [row.usd_price for row in long_prices]
    short_values = [row.usd_price for row in short_prices]
    long_disagreement = stablecoin_basis_bps(min(long_values), max(long_values))
    short_disagreement = stablecoin_basis_bps(min(short_values), max(short_values))
    max_disagreement = max(long_disagreement, short_disagreement)
    major_pair = (
        collateral_family(long_asset) == USD_MAJOR_STABLE
        and collateral_family(short_asset) == USD_MAJOR_STABLE
    )
    source_disagreement_limit = 5.0 if major_pair else 25.0
    blockers: list[str] = []
    if max_disagreement > source_disagreement_limit:
        blockers.append("stablecoin_cross_source_disagreement")
    long_price = sum(long_values) / len(long_values)
    short_price = sum(short_values) / len(short_values)
    basis = stablecoin_basis_bps(long_price, short_price)
    reserve = stablecoin_reserve_bps(
        basis,
        adverse_stablecoin_change_1m_bps or [],
    )
    if not major_pair:
        reserve = max(reserve, max_disagreement * 2.0)
    if basis > 30.0:
        blockers.append("stablecoin_basis_above_30bps")
    if reserve > 50.0:
        blockers.append("stablecoin_reserve_above_50bps")
    reserve_usd = max(0.0, float(reference_notional)) * reserve / 10_000.0
    all_prices = [*long_prices, *short_prices]
    source_times = [
        _parse_time(row.source_event_at) or _parse_time(row.response_received_at)
        for row in all_prices
    ]
    source_times = [row.astimezone(UTC) for row in source_times if row is not None]
    snapshot_observed = min(source_times) if source_times else requested_observed.astimezone(UTC)
    expires_at = snapshot_observed + timedelta(seconds=5.0)
    sources = sorted({str(row.source) for row in all_prices if row.source})
    return {
        **base_result("PASS" if not blockers else "RESEARCH_ONLY", blockers, cross_stable=True),
        "compatible": True,
        "observed_at": snapshot_observed.isoformat(),
        "expires_at": expires_at.isoformat(),
        "source_identity": {
            "provider": type(provider).__name__,
            "sources": sources,
        },
        "long_collateral_usd_price": long_price,
        "short_collateral_usd_price": short_price,
        "long_source_count": len(long_prices),
        "short_source_count": len(short_prices),
        "long_cross_source_disagreement_bps": long_disagreement,
        "short_cross_source_disagreement_bps": short_disagreement,
        "source_disagreement_limit_bps": source_disagreement_limit,
        "current_stablecoin_basis_bps": basis,
        "stablecoin_reserve_bps": reserve,
        "stablecoin_reserve_usd": reserve_usd,
        "funding_net_before_stablecoin_reserve": (
            funding_net_before_stablecoin_reserve
        ),
        "funding_net_after_stablecoin_reserve": (
            funding_net_before_stablecoin_reserve - reserve_usd
        ),
        "prices": {
            "long": [row.as_dict() for row in long_prices],
            "short": [row.as_dict() for row in short_prices],
            long_asset: [row.as_dict() for row in long_prices],
            short_asset: [row.as_dict() for row in short_prices],
        },
    }
