from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from smart_money_radar.prediction.clients import as_float


KALSHI_TAKER_FEE_RATE = 0.07
KALSHI_CONSERVATIVE_MAKER_FEE_RATE = 0.0175


def normalize_polymarket_catalog(
    raw_events: list[dict[str, Any]],
    observed_at: str,
    max_markets: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    markets: list[dict[str, Any]] = []
    selections = fair_market_selection(raw_events, max_markets)
    for raw_event, selected, total_count in selections:
        event = normalize_polymarket_event(raw_event, selected, observed_at)
        event["catalog_complete"] = len(selected) == total_count
        event["outcome_count"] = total_count
        event["exhaustive"] = bool(event["exhaustive"] and event["catalog_complete"])
        normalized_markets = [
            normalize_polymarket_market(event, raw_market, observed_at)
            for raw_market in selected
        ]
        events.append(event)
        markets.extend(normalized_markets)
    return events, markets


def normalize_polymarket_event(
    raw: dict[str, Any],
    raw_markets: list[dict[str, Any]],
    observed_at: str,
) -> dict[str, Any]:
    description = str(raw.get("description") or "").strip()
    neg_risk = as_bool(raw.get("negRisk")) or as_bool(raw.get("enableNegRisk"))
    augmented = as_bool(raw.get("negRiskAugmented"))
    slug = str(raw.get("slug") or raw.get("id") or "")
    return {
        "venue": "polymarket",
        "event_id": str(raw.get("id") or slug),
        "title": str(raw.get("title") or slug).strip(),
        "slug": slug,
        "category": polymarket_category(raw),
        "description": description,
        "starts_at": clean_timestamp(raw.get("startDate") or raw.get("creationDate")),
        "closes_at": clean_timestamp(raw.get("endDate")),
        "expected_resolution_at": clean_timestamp(raw.get("endDate")),
        "resolution_source": str(raw.get("resolutionSource") or "").strip()
        or extract_resolution_source(description),
        "resolution_rules": description,
        "cancellation_rules": extract_cancellation_rules(description),
        "mutually_exclusive": neg_risk,
        # Augmented events may contain hidden placeholders or a changing Other bucket.
        "exhaustive": bool(neg_risk and not augmented),
        "neg_risk": neg_risk,
        "augmented_neg_risk": augmented,
        "active": as_bool(raw.get("active")) and not as_bool(raw.get("closed")),
        "source_url": f"https://polymarket.com/event/{slug}" if slug else None,
        "observed_at": observed_at,
        "raw": raw,
        "outcome_count": len(raw_markets),
    }


def normalize_polymarket_market(
    event: dict[str, Any],
    raw: dict[str, Any],
    observed_at: str,
) -> dict[str, Any]:
    question = str(raw.get("question") or raw.get("groupItemTitle") or "").strip()
    tokens = parse_json_list(raw.get("clobTokenIds"))
    fee_schedule = raw.get("feeSchedule")
    fee_schedule = fee_schedule if isinstance(fee_schedule, dict) else {}
    fees_enabled = as_bool(raw.get("feesEnabled"))
    fee_rate = as_float(fee_schedule.get("rate")) if fees_enabled else 0.0
    fee_exponent = as_float(fee_schedule.get("exponent"), 1.0)
    fee_verified = bool(
        not fees_enabled or fee_schedule.get("rate") not in (None, "")
    )
    status = "closed" if as_bool(raw.get("closed")) else (
        "open" if as_bool(raw.get("acceptingOrders")) else "inactive"
    )
    description = str(raw.get("description") or event.get("description") or "").strip()
    return {
        "venue": "polymarket",
        "market_id": str(raw.get("id") or raw.get("conditionId") or question),
        "event_id": event["event_id"],
        "event_title": event["title"],
        "event_slug": event.get("slug"),
        "condition_id": str(raw.get("conditionId") or "") or None,
        "question": question,
        "outcome_label": polymarket_outcome_label(raw, question),
        "status": status,
        "result": str(raw.get("result") or "") or None,
        "yes_token_id": str(tokens[0]) if len(tokens) >= 1 else None,
        "no_token_id": str(tokens[1]) if len(tokens) >= 2 else None,
        "opens_at": clean_timestamp(raw.get("startDate")),
        "closes_at": clean_timestamp(raw.get("endDate") or event.get("closes_at")),
        "expected_resolution_at": clean_timestamp(
            raw.get("endDate") or event.get("expected_resolution_at")
        ),
        "resolution_source": str(raw.get("resolutionSource") or "").strip()
        or event.get("resolution_source"),
        "resolution_rules": description,
        "cancellation_rules": extract_cancellation_rules(description)
        or event.get("cancellation_rules"),
        "fee_rate": fee_rate,
        "fee_exponent": fee_exponent,
        "fee_taker_only": True,
        "maker_fee_rate": 0.0,
        "fees_enabled": fees_enabled,
        "fee_verified": fee_verified,
        "min_order_size": max(0.0, as_float(raw.get("orderMinSize"), 1.0)),
        "tick_size": max(0.0001, as_float(raw.get("orderPriceMinTickSize"), 0.01)),
        "volume": as_float(raw.get("volumeNum") or raw.get("volume")),
        "volume_24h": as_float(raw.get("volume24hr")),
        "liquidity": as_float(raw.get("liquidityNum") or raw.get("liquidity")),
        "accepting_orders": as_bool(raw.get("acceptingOrders")) and status != "closed",
        "is_other_outcome": as_bool(raw.get("negRiskOther")),
        "event_mutually_exclusive": bool(event.get("mutually_exclusive")),
        "event_exhaustive": bool(event.get("exhaustive")),
        "event_neg_risk": bool(event.get("neg_risk")),
        "event_augmented_neg_risk": bool(event.get("augmented_neg_risk")),
        "event_source_url": event.get("source_url"),
        "observed_at": observed_at,
        "raw": raw,
    }


def enrich_polymarket_market_info(
    markets: list[dict[str, Any]],
    market_info: dict[str, dict[str, Any]],
) -> int:
    updated = 0
    for market in markets:
        condition_id = str(market.get("condition_id") or "")
        info = market_info.get(condition_id)
        if not info:
            continue
        fee_details = info.get("fd") if isinstance(info.get("fd"), dict) else {}
        rate = fee_details.get("r")
        if rate not in (None, ""):
            market["fee_rate"] = as_float(rate)
            market["fee_exponent"] = as_float(fee_details.get("e"), 1.0)
            market["fee_taker_only"] = as_bool(fee_details.get("to"))
            market["fee_verified"] = True
        if info.get("mos") not in (None, ""):
            market["min_order_size"] = max(0.0, as_float(info.get("mos"), 1.0))
        if info.get("mts") not in (None, ""):
            market["tick_size"] = max(0.0001, as_float(info.get("mts"), 0.01))
        market["raw"] = {
            **(market.get("raw") or {}),
            "clob_market_info": info,
        }
        updated += 1
    return updated


def normalize_kalshi_catalog(
    raw_events: list[dict[str, Any]],
    observed_at: str,
    max_markets: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    markets: list[dict[str, Any]] = []
    selections = fair_market_selection(raw_events, max_markets)
    for raw_event, selected, total_count in selections:
        event = normalize_kalshi_event(raw_event, selected, observed_at)
        event["catalog_complete"] = len(selected) == total_count
        event["outcome_count"] = total_count
        event["exhaustive"] = bool(event["exhaustive"] and event["catalog_complete"])
        normalized_markets = [
            normalize_kalshi_market(event, raw_market, observed_at)
            for raw_market in selected
        ]
        events.append(event)
        markets.extend(normalized_markets)
    return events, markets


def fair_market_selection(
    raw_events: list[dict[str, Any]],
    max_markets: int,
) -> list[tuple[dict[str, Any], list[dict[str, Any]], int]]:
    """Allocate a global market budget round-robin so one large event cannot starve others."""
    candidates = [
        [row for row in raw.get("markets", []) if isinstance(row, dict)]
        for raw in raw_events
    ]
    selected: list[list[dict[str, Any]]] = [[] for _ in raw_events]
    budget = max(1, int(max_markets))
    cursor = [0 for _ in raw_events]
    while budget > 0:
        added = False
        for index, rows in enumerate(candidates):
            if budget <= 0:
                break
            if cursor[index] >= len(rows):
                continue
            selected[index].append(rows[cursor[index]])
            cursor[index] += 1
            budget -= 1
            added = True
        if not added:
            break
    return [
        (raw, selected[index], len(candidates[index]))
        for index, raw in enumerate(raw_events)
        if selected[index]
    ]


def normalize_kalshi_event(
    raw: dict[str, Any],
    raw_markets: list[dict[str, Any]],
    observed_at: str,
) -> dict[str, Any]:
    event_id = str(raw.get("event_ticker") or "")
    rules = "\n".join(
        str(market.get("rules_primary") or "")
        for market in raw_markets[:3]
        if market.get("rules_primary")
    )
    source_rows = raw.get("settlement_sources")
    source_rows = source_rows if isinstance(source_rows, list) else []
    source_names = [
        str(row.get("name"))
        for row in source_rows
        if isinstance(row, dict) and row.get("name")
    ]
    close_values = [
        clean_timestamp(market.get("close_time"))
        for market in raw_markets
        if market.get("close_time")
    ]
    resolution_values = [
        clean_timestamp(
            market.get("expected_expiration_time") or market.get("expiration_time")
        )
        for market in raw_markets
        if market.get("expected_expiration_time") or market.get("expiration_time")
    ]
    return_type = str(raw.get("collateral_return_type") or "").upper()
    mutually_exclusive = as_bool(raw.get("mutually_exclusive"))
    series = str(raw.get("series_ticker") or event_id).lower()
    return {
        "venue": "kalshi",
        "event_id": event_id,
        "title": str(raw.get("title") or event_id).strip(),
        "slug": series,
        "category": str(raw.get("category") or "").strip() or None,
        "description": str(raw.get("sub_title") or "").strip() or None,
        "starts_at": minimum_timestamp(
            market.get("open_time") for market in raw_markets
        ),
        "closes_at": maximum_timestamp(close_values),
        "expected_resolution_at": maximum_timestamp(resolution_values),
        "resolution_source": ", ".join(source_names),
        "resolution_rules": rules,
        "cancellation_rules": extract_cancellation_rules(rules),
        "mutually_exclusive": mutually_exclusive,
        "exhaustive": bool(mutually_exclusive and return_type == "MECNET"),
        "neg_risk": False,
        "augmented_neg_risk": False,
        "active": any(str(row.get("status") or "") == "active" for row in raw_markets),
        "source_url": f"https://kalshi.com/markets/{series}" if series else None,
        "observed_at": observed_at,
        "raw": raw,
        "outcome_count": len(raw_markets),
    }


def normalize_kalshi_market(
    event: dict[str, Any],
    raw: dict[str, Any],
    observed_at: str,
) -> dict[str, Any]:
    status_value = str(raw.get("status") or "").lower()
    status = "open" if status_value == "active" else (
        "closed" if status_value in {"closed", "settled", "finalized"} else "inactive"
    )
    rules = "\n".join(
        text
        for text in (
            str(raw.get("rules_primary") or "").strip(),
            str(raw.get("rules_secondary") or "").strip(),
            str(raw.get("early_close_condition") or "").strip(),
        )
        if text
    )
    return {
        "venue": "kalshi",
        "market_id": str(raw.get("ticker") or raw.get("title") or ""),
        "event_id": event["event_id"],
        "event_title": event["title"],
        "event_slug": event.get("slug"),
        "condition_id": None,
        "question": str(raw.get("title") or event["title"]).strip(),
        "outcome_label": str(raw.get("yes_sub_title") or "Yes").strip(),
        "status": status,
        "result": str(raw.get("result") or "") or None,
        "yes_token_id": None,
        "no_token_id": None,
        "opens_at": clean_timestamp(raw.get("open_time")),
        "closes_at": clean_timestamp(raw.get("close_time")),
        "expected_resolution_at": clean_timestamp(
            raw.get("expected_expiration_time") or raw.get("expiration_time")
        ),
        "resolution_source": event.get("resolution_source"),
        "resolution_rules": rules,
        "cancellation_rules": extract_cancellation_rules(rules)
        or event.get("cancellation_rules"),
        "fee_rate": KALSHI_TAKER_FEE_RATE,
        "fee_exponent": 1.0,
        "fee_taker_only": True,
        "maker_fee_rate": KALSHI_CONSERVATIVE_MAKER_FEE_RATE,
        "fees_enabled": True,
        "fee_verified": False,
        "min_order_size": 1.0,
        "tick_size": kalshi_tick_size(raw),
        "volume": as_float(raw.get("volume_fp")),
        "volume_24h": as_float(raw.get("volume_24h_fp")),
        "liquidity": as_float(raw.get("liquidity_dollars")),
        "accepting_orders": status == "open",
        "is_other_outcome": "other" in str(raw.get("yes_sub_title") or "").lower(),
        "event_mutually_exclusive": bool(event.get("mutually_exclusive")),
        "event_exhaustive": bool(event.get("exhaustive")),
        "event_neg_risk": False,
        "event_augmented_neg_risk": False,
        "event_source_url": event.get("source_url"),
        "observed_at": observed_at,
        "raw": {
            **raw,
            "fee_policy": "kalshi_2026_general_conservative",
        },
    }


def normalize_polymarket_orderbook(
    market: dict[str, Any],
    raw_books: dict[str, dict[str, Any]],
    observed_at: str,
) -> dict[str, Any] | None:
    yes_raw = raw_books.get(str(market.get("yes_token_id") or ""))
    no_raw = raw_books.get(str(market.get("no_token_id") or ""))
    if not yes_raw and not no_raw:
        return None
    yes_bids = dictionary_levels((yes_raw or {}).get("bids"), reverse=True)
    yes_asks = dictionary_levels((yes_raw or {}).get("asks"), reverse=False)
    no_bids = dictionary_levels((no_raw or {}).get("bids"), reverse=True)
    no_asks = dictionary_levels((no_raw or {}).get("asks"), reverse=False)
    if not no_bids:
        no_bids = complement_levels(yes_asks, reverse=True)
    if not no_asks:
        no_asks = complement_levels(yes_bids, reverse=False)
    return normalized_book(
        market,
        observed_at,
        yes_bids,
        yes_asks,
        no_bids,
        no_asks,
        raw={"yes": yes_raw or {}, "no": no_raw or {}},
    )


def normalize_kalshi_orderbook(
    market: dict[str, Any],
    raw: dict[str, Any] | None,
    observed_at: str,
) -> dict[str, Any] | None:
    if not raw:
        return None
    payload = raw.get("orderbook_fp")
    payload = payload if isinstance(payload, dict) else {}
    yes_bids = pair_levels(payload.get("yes_dollars"), reverse=True)
    no_bids = pair_levels(payload.get("no_dollars"), reverse=True)
    yes_asks = complement_levels(no_bids, reverse=False)
    no_asks = complement_levels(yes_bids, reverse=False)
    return normalized_book(
        market,
        observed_at,
        yes_bids,
        yes_asks,
        no_bids,
        no_asks,
        raw=raw,
    )


def normalized_book(
    market: dict[str, Any],
    observed_at: str,
    yes_bids: list[list[float]],
    yes_asks: list[list[float]],
    no_bids: list[list[float]],
    no_asks: list[list[float]],
    raw: dict[str, Any],
) -> dict[str, Any]:
    return {
        "venue": market["venue"],
        "market_id": market["market_id"],
        "observed_at": observed_at,
        "yes_bids": yes_bids,
        "yes_asks": yes_asks,
        "no_bids": no_bids,
        "no_asks": no_asks,
        "best_yes_bid": first_price(yes_bids),
        "best_yes_ask": first_price(yes_asks),
        "best_no_bid": first_price(no_bids),
        "best_no_ask": first_price(no_asks),
        "yes_bid_depth": sum(level[1] for level in yes_bids),
        "yes_ask_depth": sum(level[1] for level in yes_asks),
        "no_bid_depth": sum(level[1] for level in no_bids),
        "no_ask_depth": sum(level[1] for level in no_asks),
        "raw": raw,
    }


def dictionary_levels(value: Any, reverse: bool) -> list[list[float]]:
    rows = value if isinstance(value, list) else []
    levels = [
        [as_float(row.get("price")), as_float(row.get("size"))]
        for row in rows
        if isinstance(row, dict)
    ]
    return valid_levels(levels, reverse)


def pair_levels(value: Any, reverse: bool) -> list[list[float]]:
    rows = value if isinstance(value, list) else []
    levels = [
        [as_float(row[0]), as_float(row[1])]
        for row in rows
        if isinstance(row, list) and len(row) >= 2
    ]
    return valid_levels(levels, reverse)


def valid_levels(levels: list[list[float]], reverse: bool) -> list[list[float]]:
    aggregated: dict[float, float] = {}
    for price, size in levels:
        if price < 0 or price > 1 or size <= 0:
            continue
        rounded = round(price, 6)
        aggregated[rounded] = aggregated.get(rounded, 0.0) + size
    return [
        [price, aggregated[price]]
        for price in sorted(aggregated, reverse=reverse)
    ]


def complement_levels(levels: list[list[float]], reverse: bool) -> list[list[float]]:
    return valid_levels([[1.0 - price, size] for price, size in levels], reverse)


def first_price(levels: list[list[float]]) -> float | None:
    return levels[0][0] if levels else None


def parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def polymarket_outcome_label(raw: dict[str, Any], question: str) -> str:
    explicit = str(raw.get("groupItemTitle") or "").strip()
    if explicit:
        return explicit
    match = re.match(r"Will\s+(.+?)\s+(?:win|be|become|reach|finish|advance)\b", question, re.I)
    return match.group(1).strip() if match else "Yes"


def polymarket_category(raw: dict[str, Any]) -> str | None:
    tags = raw.get("tags")
    tags = tags if isinstance(tags, list) else []
    for tag in tags:
        if isinstance(tag, dict) and tag.get("label"):
            return str(tag["label"])
    return None


def extract_resolution_source(text: str) -> str | None:
    match = re.search(
        r"(?:primary\s+)?resolution\s+source\s+(?:will\s+be|is)\s+([^\n.]+)",
        text,
        re.I,
    )
    return match.group(1).strip() if match else None


def extract_cancellation_rules(text: str) -> str | None:
    if not text:
        return None
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    keywords = (
        "cancel",
        "postpon",
        "void",
        "refund",
        "other",
        "not completed",
        "no winner",
        "rescheduled",
    )
    matches = [sentence.strip() for sentence in sentences if any(k in sentence.lower() for k in keywords)]
    return " ".join(matches[:3]) or None


def kalshi_tick_size(raw: dict[str, Any]) -> float:
    ranges = raw.get("price_ranges")
    ranges = ranges if isinstance(ranges, list) else []
    steps = [
        as_float(row.get("step"))
        for row in ranges
        if isinstance(row, dict) and as_float(row.get("step")) > 0
    ]
    return min(steps) if steps else 0.01


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def clean_timestamp(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def minimum_timestamp(values: Any) -> str | None:
    normalized = [clean_timestamp(value) for value in values]
    return min((value for value in normalized if value), default=None)


def maximum_timestamp(values: Any) -> str | None:
    normalized = [clean_timestamp(value) for value in values]
    return max((value for value in normalized if value), default=None)
