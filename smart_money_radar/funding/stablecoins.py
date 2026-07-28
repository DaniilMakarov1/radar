from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from smart_money_radar.funding.adapter_contracts import (
    USD_MAJOR_STABLE,
    collateral_family,
)


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
        age = abs((observed.astimezone(UTC) - event_at.astimezone(UTC)).total_seconds())
        if age > max_age_seconds:
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
    if long_asset == short_asset:
        return {
            "compatible": True,
            "numeraire": "USD",
            "stablecoin_pair": f"{long_asset}/{short_asset}",
            "cross_stable": False,
            "status": "PASS",
            "blockers": [],
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
            "compatible": False,
            "numeraire": None,
            "stablecoin_pair": f"{long_asset}/{short_asset}",
            "cross_stable": True,
            "status": "RESEARCH_ONLY",
            "blockers": ["stablecoin_family_not_compatible"],
        }
    if provider is None:
        return {
            "compatible": True,
            "numeraire": "USD",
            "stablecoin_pair": f"{long_asset}/{short_asset}",
            "cross_stable": True,
            "status": "RESEARCH_ONLY",
            "blockers": ["stablecoin_price_provider_unavailable"],
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
            "compatible": True,
            "numeraire": "USD",
            "stablecoin_pair": f"{long_asset}/{short_asset}",
            "cross_stable": True,
            "status": "RESEARCH_ONLY",
            "blockers": ["insufficient_stablecoin_price_sources"],
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
    return {
        "compatible": True,
        "numeraire": "USD",
        "stablecoin_pair": f"{long_asset}/{short_asset}",
        "cross_stable": True,
        "status": "PASS" if not blockers else "RESEARCH_ONLY",
        "blockers": blockers,
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
        },
    }
