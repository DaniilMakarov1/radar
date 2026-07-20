from __future__ import annotations

import math
import re
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any


CANONICAL_ASSET_ALIASES = {
    "XBT": "BTC",
    "1000BONK": "BONK",
    "KBONK": "BONK",
    "1000FLOKI": "FLOKI",
    "KFLOKI": "FLOKI",
    "1000LUNC": "LUNC",
    "1000000MOG": "MOG",
    "1000PEPE": "PEPE",
    "KPEPE": "PEPE",
    "1000RATS": "RATS",
    "1000SATS": "SATS",
    "1000SHIB": "SHIB",
    "KSHIB": "SHIB",
    "1000XEC": "XEC",
}


# Some venues list a bundle as the base asset while others list one token.  The
# multiplier converts one exchange base unit into canonical token units.  Price
# must be divided by it and order-book size multiplied by it before routes are
# compared or hedged.
CANONICAL_ASSET_UNIT_MULTIPLIERS = {
    "1000BONK": 1_000.0,
    "KBONK": 1_000.0,
    "1000FLOKI": 1_000.0,
    "KFLOKI": 1_000.0,
    "1000LUNC": 1_000.0,
    "1000000MOG": 1_000_000.0,
    "1000PEPE": 1_000.0,
    "KPEPE": 1_000.0,
    "1000RATS": 1_000.0,
    "1000SATS": 1_000.0,
    "1000SHIB": 1_000.0,
    "KSHIB": 1_000.0,
    "1000XEC": 1_000.0,
}


def clean_asset_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol or len(symbol) > 24 or symbol.startswith("@"):
        return ""
    return symbol if re.fullmatch(r"[A-Z0-9]+", symbol) else ""


def canonical_asset_symbol(value: Any) -> str:
    cleaned = clean_asset_symbol(value)
    return CANONICAL_ASSET_ALIASES.get(cleaned, cleaned)


def canonical_asset_unit_multiplier(value: Any) -> float:
    return CANONICAL_ASSET_UNIT_MULTIPLIERS.get(clean_asset_symbol(value), 1.0)


def symbol_unit_alias(value: Any) -> str:
    cleaned = clean_asset_symbol(value)
    if not cleaned:
        cleaned = re.sub(r"[^A-Z0-9]", "", str(value or "").strip().upper())
    for alias in sorted(CANONICAL_ASSET_UNIT_MULTIPLIERS, key=len, reverse=True):
        if cleaned.startswith(alias):
            return alias
    return ""


def inferred_canonical_unit_multiplier(row: dict[str, Any]) -> float:
    base_scale = canonical_asset_unit_multiplier(row.get("base_asset"))
    symbol_scale = canonical_asset_unit_multiplier(symbol_unit_alias(row.get("symbol")))
    canonical_scale = canonical_asset_unit_multiplier(row.get("canonical_asset"))
    return max(base_scale, symbol_scale, canonical_scale)


def inferred_canonical_asset(row: dict[str, Any]) -> str:
    for value in (
        symbol_unit_alias(row.get("base_asset")),
        symbol_unit_alias(row.get("symbol")),
        row.get("base_asset"),
        row.get("canonical_asset"),
    ):
        asset = canonical_asset_symbol(value)
        if asset:
            return asset
    return ""


def normalize_catalog_canonical_units(
    instruments: list[dict[str, Any]],
    markets: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalize market prices to one canonical token without mutating input."""
    scales: dict[tuple[str, str], float] = {}
    normalized_instruments: list[dict[str, Any]] = []
    for source in instruments:
        row = dict(source)
        scale = inferred_canonical_unit_multiplier(row)
        key = (str(row.get("venue") or ""), str(row.get("symbol") or ""))
        scales[key] = scale
        row["canonical_asset"] = inferred_canonical_asset(row)
        row["canonical_unit_multiplier"] = scale
        normalized_instruments.append(row)

    normalized_markets: list[dict[str, Any]] = []
    for source in markets:
        row = dict(source)
        key = (str(row.get("venue") or ""), str(row.get("symbol") or ""))
        scale = max(scales.get(key, 1.0), inferred_canonical_unit_multiplier(row))
        row["canonical_asset"] = inferred_canonical_asset(row)
        row["canonical_unit_multiplier"] = scale
        if scale != 1.0:
            for name in ("mark_price", "index_price"):
                try:
                    value = float(row.get(name))
                except (TypeError, ValueError):
                    continue
                row[name] = value / scale if value > 0 else row.get(name)
        normalized_markets.append(row)
    return normalized_instruments, normalized_markets


def normalize_orderbook_canonical_units(
    book: dict[str, Any],
    multiplier: float,
) -> dict[str, Any]:
    """Convert price/size ladders from bundled units to canonical token units."""
    scale = max(1.0, float(multiplier or 1.0))
    if scale == 1.0:
        return dict(book)

    def levels(values: Any) -> list[list[float]]:
        return [
            [float(price) / scale, float(size) * scale]
            for price, size in values or []
            if float(price) > 0 and float(size) > 0
        ]

    bids = levels(book.get("bids"))
    asks = levels(book.get("asks"))
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    mid_price = (
        (best_bid + best_ask) / 2.0
        if best_bid is not None and best_ask is not None
        else None
    )
    return {
        **book,
        "bids": bids,
        "asks": asks,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_price": mid_price,
        "bid_depth_usd": sum(price * size for price, size in bids),
        "ask_depth_usd": sum(price * size for price, size in asks),
        "canonical_unit_multiplier": scale,
    }


def normalize_stored_orderbook_units(
    rows: list[dict[str, Any]],
    multiplier: float,
    current_mid_price: float | None,
) -> list[dict[str, Any]]:
    """Normalize legacy L2 rows while leaving newly normalized rows untouched."""
    scale = max(1.0, float(multiplier or 1.0))
    if scale == 1.0 or not current_mid_price or current_mid_price <= 0:
        return [dict(row) for row in rows]
    output: list[dict[str, Any]] = []
    for row in rows:
        try:
            ratio = float(row.get("mid_price") or 0.0) / current_mid_price
        except (TypeError, ValueError):
            ratio = 0.0
        # A legacy bundled price is approximately `scale` times the current
        # canonical price. Compare in log space so large token multipliers do
        # not need asset-specific thresholds.
        is_legacy = (
            ratio > 0
            and abs(math.log(ratio / scale)) < abs(math.log(ratio))
        )
        output.append(
            normalize_orderbook_canonical_units(row, scale)
            if is_legacy
            else dict(row)
        )
    return output


def iso_from_milliseconds(value: Any) -> str | None:
    try:
        timestamp = float(value) / 1000.0
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, UTC).replace(microsecond=0).isoformat()


def normalize_orderbook(
    venue: str,
    symbol: str,
    raw_bids: Any,
    raw_asks: Any,
    observed_at: str,
    raw: Any,
) -> dict[str, Any]:
    bids = normalize_levels(raw_bids, reverse=True)
    asks = normalize_levels(raw_asks, reverse=False)
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    mid_price = (
        (best_bid + best_ask) / 2.0
        if best_bid and best_ask and best_bid > 0 and best_ask > 0
        else None
    )
    return {
        "venue": venue,
        "symbol": symbol,
        "observed_at": observed_at,
        "bids": bids,
        "asks": asks,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_price": mid_price,
        "bid_depth_usd": sum(price * size for price, size in bids),
        "ask_depth_usd": sum(price * size for price, size in asks),
        "raw": raw,
    }


def normalize_levels(raw_levels: Any, *, reverse: bool) -> list[list[float]]:
    output: list[list[float]] = []
    if not isinstance(raw_levels, list):
        return output
    for row in raw_levels:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            price = float(row[0])
            size = float(row[1])
        except (TypeError, ValueError):
            continue
        if price > 0 and size > 0:
            output.append([price, size])
    output.sort(key=lambda row: row[0], reverse=reverse)
    return output


def funding_persistence(
    long_history: list[dict[str, Any]],
    short_history: list[dict[str, Any]],
) -> dict[str, Any]:
    long_buckets = funding_hourly_buckets(long_history)
    short_buckets = funding_hourly_buckets(short_history)
    common_hours = sorted(set(long_buckets).intersection(short_buckets))
    if not common_hours:
        return empty_persistence()
    spreads = [short_buckets[hour] - long_buckets[hour] for hour in common_hours]

    median_hourly = statistics.median(spreads)
    deviation = statistics.pstdev(spreads) if len(spreads) > 1 else 0.0
    positive_fraction = sum(value > 0 for value in spreads) / len(spreads)
    span_hours = int((common_hours[-1] - common_hours[0]).total_seconds() // 3600) + 1
    coverage_fraction = len(common_hours) / max(1, span_hours)
    signal_to_noise = abs(median_hourly) / (
        abs(median_hourly) + deviation + 1e-12
    )
    score = 100.0 * (0.75 * positive_fraction + 0.25 * signal_to_noise)
    return {
        "history_point_count": len(spreads),
        "historical_median_hourly_spread": median_hourly,
        "historical_mean_hourly_spread": statistics.fmean(spreads),
        "historical_stdev_hourly_spread": deviation,
        "positive_spread_fraction": positive_fraction,
        "history_coverage_fraction": coverage_fraction,
        "persistence_score": max(0.0, min(score, 100.0)),
        "recent_hourly_spreads": spreads[-72:],
        "history_start_at": common_hours[0].isoformat(),
        "history_end_at": common_hours[-1].isoformat(),
    }


def history_series(rows: list[dict[str, Any]]) -> list[tuple[datetime, float, float]]:
    output = []
    for row in rows:
        timestamp = parse_timestamp(row.get("funding_at"))
        if timestamp is None:
            continue
        try:
            hourly_rate = float(row.get("hourly_funding_rate"))
            interval_hours = max(1.0, float(row.get("funding_interval_hours") or 1.0))
        except (TypeError, ValueError):
            continue
        output.append((timestamp, hourly_rate, interval_hours))
    output.sort(key=lambda item: item[0])
    return output


def inferred_funding_intervals(
    timestamps: list[int | float],
    fallback_hours: float,
    *,
    units_per_second: float = 1.0,
) -> dict[int | float, float]:
    """Infer the interval attached to each settlement from adjacent timestamps.

    Exchanges expose the current interval alongside history, but that interval can
    change over time. Large gaps are treated as missing rows rather than as a new
    interval so one outage cannot dilute the recorded funding rate.
    """
    ordered = sorted(set(timestamps))
    fallback = max(0.25, float(fallback_hours or 8.0))
    if not ordered:
        return {}

    def adjacent_hours(left: int | float, right: int | float) -> float | None:
        hours = (float(right) - float(left)) / max(units_per_second, 1e-12) / 3600.0
        if not 0.25 <= hours <= 12.0:
            return None
        # API timestamps can carry a few seconds of settlement jitter.
        rounded = round(hours * 4.0) / 4.0
        return rounded if abs(hours - rounded) <= 0.05 else hours

    result: dict[int | float, float] = {}
    for index, timestamp in enumerate(ordered):
        previous = (
            adjacent_hours(ordered[index - 1], timestamp)
            if index > 0
            else None
        )
        following = (
            adjacent_hours(timestamp, ordered[index + 1])
            if index + 1 < len(ordered)
            else None
        )
        result[timestamp] = previous or following or fallback
    return result


def funding_hourly_buckets(rows: list[dict[str, Any]]) -> dict[datetime, float]:
    """Expand settled rates over the interval that ended at the settlement time."""
    output: dict[datetime, float] = {}
    for settled_at, hourly_rate, interval_hours in history_series(rows):
        settlement_hour = settled_at.replace(minute=0, second=0, microsecond=0)
        bucket_count = max(1, int(round(interval_hours)))
        for offset in range(1, bucket_count + 1):
            output[settlement_hour - timedelta(hours=offset)] = hourly_rate
    return output


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def empty_persistence() -> dict[str, Any]:
    return {
        "history_point_count": 0,
        "historical_median_hourly_spread": 0.0,
        "historical_mean_hourly_spread": 0.0,
        "historical_stdev_hourly_spread": 0.0,
        "positive_spread_fraction": 0.0,
        "history_coverage_fraction": 0.0,
        "persistence_score": 0.0,
        "recent_hourly_spreads": [],
        "history_start_at": None,
        "history_end_at": None,
    }
