from __future__ import annotations

import math
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Protocol

from smart_money_radar.funding.adapter_contracts import (
    USD_MAJOR_STABLE,
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
    """Fetch USDC/USDT prices from two independent public sources."""

    COINGECKO_IDS = {
        "USDC": "usd-coin",
        "USDT": "tether",
    }

    def __init__(
        self,
        *,
        timeout_seconds: float = 2.0,
        fetch_json: Callable[[str, float], Any] | None = None,
    ) -> None:
        self.timeout_seconds = max(0.25, float(timeout_seconds))
        self.fetch_json = fetch_json or self._fetch_json
        self._cache: dict[tuple[str, str], list[StablecoinPrice]] = {}

    def prices(self, asset: str, observed_at: str) -> list[StablecoinPrice]:
        symbol = str(asset or "").upper()
        if symbol not in {"USDC", "USDT"}:
            return []
        key = (symbol, observed_at)
        if key not in self._cache:
            self._cache[key] = self._fetch_prices(symbol, observed_at)
        return list(self._cache[key])

    def _fetch_prices(self, asset: str, observed_at: str) -> list[StablecoinPrice]:
        rows: list[StablecoinPrice] = []
        coingecko_id = self.COINGECKO_IDS.get(asset)
        if coingecko_id:
            query = urllib.parse.urlencode(
                {"ids": coingecko_id, "vs_currencies": "usd"}
            )
            payload, response_received_at = self._safe_fetch(
                f"https://api.coingecko.com/api/v3/simple/price?{query}"
            )
            try:
                price = float(payload[coingecko_id]["usd"])
            except (TypeError, ValueError, KeyError):
                price = 0.0
            if price > 0:
                rows.append(
                    StablecoinPrice(
                        asset,
                        price,
                        "coingecko",
                        "",
                        response_received_at,
                        "current_snapshot_response_time",
                    )
                )
        coinbase_payload, coinbase_response_received_at = self._safe_fetch(
            f"https://api.coinbase.com/v2/exchange-rates?currency={asset}"
        )
        try:
            coinbase_price = float(coinbase_payload["data"]["rates"]["USD"])
        except (TypeError, ValueError, KeyError):
            coinbase_price = 0.0
        if coinbase_price > 0:
            rows.append(
                StablecoinPrice(
                    asset,
                    coinbase_price,
                    "coinbase",
                    "",
                    coinbase_response_received_at,
                    "current_snapshot_response_time",
                )
            )
        return rows

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
        collateral_family(long_asset) == USD_MAJOR_STABLE
        and collateral_family(short_asset) == USD_MAJOR_STABLE
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
        return {
            "schema_version": 1,
            "compatible": status != "RESEARCH_ONLY" or not blockers or "stablecoin_family_not_compatible" not in blockers,
            "numeraire": "USD" if collateral_family(long_asset) == USD_MAJOR_STABLE and collateral_family(short_asset) == USD_MAJOR_STABLE else None,
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
    long_prices = _fresh_prices(
        provider.prices(long_asset, observed_at),
        observed_at,
        max_age_seconds=5.0,
    )
    short_prices = _fresh_prices(
        provider.prices(short_asset, observed_at),
        observed_at,
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
    blockers: list[str] = []
    if long_disagreement > 5.0 or short_disagreement > 5.0:
        blockers.append("stablecoin_cross_source_disagreement")
    long_price = sum(long_values) / len(long_values)
    short_price = sum(short_values) / len(short_values)
    basis = stablecoin_basis_bps(long_price, short_price)
    reserve = stablecoin_reserve_bps(
        basis,
        adverse_stablecoin_change_1m_bps or [],
    )
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
